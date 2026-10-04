"""Budget-bounded, repeatable zero-to-one GPU service for real queued work.

Each rental retains the existing single-use capacity approval, durable intent,
qualification, draining and billing contracts. A fresh approval is created only
after the previous instance is authoritatively removed and its work is settled.
No synthetic demand, minimum warm capacity, budget increases or TTL renewals.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, fields, replace
import json
import os
from pathlib import Path
import signal
import stat
import sys
import time
import uuid

from sqlalchemy import select

from .autoscale import ScalePolicy
from .capacity import proven_unsubmitted_capacity_job, transfer_unsubmitted_capacity
from .lium_provider import LiumManifest, LiumProvider
from .lium_runtime_aws import AwsLiumLoader
from .production_scaler import (FiniteConfig, FiniteController, MODEL, RECIPE, ScalerError,
    save, stdin_loader, unique, validate_settings, verify_identity_files, verify_sources)
from .repository import Repository, capacity_approvals, capacity_cycles, scaler_actions, scaler_leaders
from .scaler import LaunchSpec
from .settings import Settings
from .worker import _slot_lock


@dataclass(frozen=True)
class OnDemandConfig(FiniteConfig):
    service_mode: str = "on-demand"
    max_cycles: int = 8

    def __post_init__(self):
        super().__post_init__()
        if (self.service_mode != "on-demand" or type(self.max_cycles) is not int
                or not 1 <= self.max_cycles <= 8
                or getattr(self, "allowed_owners", None) != ["superdan", "supervan"]
                or self.scale_policy["max_instances"] != 1
                or self.scale_policy["max_physical_gpus"] != 1
                or self.scale_policy["idle_before_drain_s"] != 600
                or self.max_cycles*self.scale_policy["instance_reservation_microusd"]
                    > self.scale_policy["approved_remaining_microusd"]):
            raise ScalerError("ondemand_explicit_single_gpu_budget_and_idle_required")


def json_config(config):
    value = asdict(config)
    for key in ("work_dir", "data_dir", "source_dir", "ssh_key_file", "known_hosts_file"):
        value[key] = str(value[key])
    return value


def read_config(path):
    try:
        path = Path(path)
        if not path.is_absolute() or path.is_symlink():
            raise ValueError
        with path.open("rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or os.name != "nt" and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
                raise ValueError
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ValueError
        return OnDemandConfig(**json.loads(raw, object_pairs_hook=unique))
    except Exception:
        raise ScalerError("ondemand_config_unavailable_or_invalid") from None


def cycle_config(config, sequence):
    if type(sequence) is not int or not 1 <= sequence <= config.max_cycles:
        raise ScalerError("ondemand_cycle_limit")
    values = {f.name: getattr(config, f.name) for f in fields(FiniteConfig)}
    suffix = "-"+str(sequence).zfill(3)
    work = config.work_dir/"cycles"/str(sequence).zfill(3)
    values.update(cycle_id=config.cycle_id+suffix,
        capacity_approval_id=config.capacity_approval_id+suffix,
        work_dir=work, known_hosts_file=work/"known_hosts")
    return FiniteConfig(**values)


class ServiceCycle(FiniteController):
    """The existing finite controller scoped to one approved rental cycle."""

    def _managed(self):
        with self.repo.engine.connect() as conn:
            intent_id = conn.execute(select(capacity_cycles.c.intent_id).where(
                capacity_cycles.c.approval_id == self.config.capacity_approval_id)).scalar_one_or_none()
            rows = self.repo.list_instance_intents(pool=self.config.pool)
            actions = {r["intent_id"]: dict(r) for r in conn.execute(select(scaler_actions).where(
                scaler_actions.c.pool == self.config.pool)).mappings()}
        selected = [r for r in rows if r["id"] == intent_id]
        if (intent_id and len(selected) != 1
                or any(r["state"] != "destroyed" and r["id"] != intent_id for r in rows)
                or any(r["provider"] != "lium" or r["id"] not in actions
                    or actions[r["id"]]["launch_spec"] not in self.config.launches for r in selected)):
            raise ScalerError("ondemand_other_instance_requires_reconciliation")
        return selected, {r["id"]: actions[r["id"]] for r in selected}

    def initialize(self):
        c = self.config
        c.work_dir.mkdir(parents=True, exist_ok=True)
        receipt = c.work_dir/"cycle-state.json"
        if receipt.exists():
            if json.loads(receipt.read_text())["config_hash"] != c.fingerprint():
                raise ScalerError("ondemand_cycle_configuration_changed")
        else:
            self._managed()  # Reject an unrelated/unknown live lease, never hide it.
            if not c.created_at <= self.repo.clock() < c.stop_claiming_at:
                raise ScalerError("ondemand_service_approval_expired")
            save(receipt, {"config_hash": c.fingerprint(), "ports": {}, "created_at": self.repo.clock()})
        self.remaining_budget()
        from .execution_policy import read_policy
        policy = read_policy(self.settings.execution_policy_file)
        with self.repo.engine.connect() as conn:
            existing = conn.execute(select(capacity_approvals).where(
                capacity_approvals.c.id == c.capacity_approval_id)).mappings().first()
        if existing:
            # Expired/revoked grants retain their reconciliation path; calling
            # approve_capacity again would reject an expired deadline before
            # the old pod could be reconciled. Never re-enable the old grant.
            if existing["enabled"] != 1 or not self.approval_current(existing["payload"]):
                self.request_drain()
            return
        self.repo.approve_capacity(c.capacity_approval_id, tenant_id=c.tenant, pool=c.pool,
            model_id=MODEL, configuration_id=c.configuration_id, recipe_ids=[RECIPE],
            policy_hash=c.execution_policy_sha256, qualification_evidence_id=c.qualification_evidence_id,
            qualification_expires_at=policy["qualification"]["expires_at"],
            quote_expires_at=policy["reservation"]["expires_at"],
            expires_at=min(c.stop_claiming_at, policy["qualification"]["expires_at"], policy["reservation"]["expires_at"]),
            launch=LaunchSpec(**c.launches[0]), scale_policy=ScalePolicy(**c.scale_policy),
            budget_scope=c.scope, budget_account_ids=c.budget_account_ids, enabled=True)

    def request_rollover(self):
        # Separate from final service shutdown: admitted jobs retain their
        # IDs and reservations while the old GPU finishes existing work.
        (self.config.work_dir/"rollover.flag").touch()
        self.request_drain()

    def _close_unsubmitted(self):
        if ((self.config.work_dir/"rollover.flag").exists()
                and getattr(self, "preserve_rollover", lambda: False)()):
            self.repo.set_capacity_approval_enabled(self.config.capacity_approval_id, enabled=False)
            return
        super()._close_unsubmitted()


class OnDemandController:
    def __init__(self, repo, settings, config, *, provider, boot_factory=None):
        self.repo, self.settings, self.config, self.provider = repo, settings, config, provider
        self.boot_factory = boot_factory
        self.sequence = 1
        self.current = None
        self.leader_id = "on-demand-"+uuid.uuid4().hex
        self.transfer_from = None

    @property
    def receipt_path(self):
        return self.config.work_dir/"service-state.json"

    def stopping(self):
        return ((self.config.work_dir/"drain.flag").exists()
                or self.repo.clock() >= self.config.stop_claiming_at)

    def request_drain(self):
        (self.config.work_dir/"drain.flag").touch()
        if self.current:
            self.current.request_drain()

    def _persist(self):
        save(self.receipt_path, {"version": 1, "config_hash": self.config.fingerprint(),
            "sequence": self.sequence, "created_at": self.config.created_at,
            "transfer_from": self.transfer_from})

    def initialize(self):
        c = self.config
        c.work_dir.mkdir(parents=True, exist_ok=True)
        if self.receipt_path.exists():
            value = json.loads(self.receipt_path.read_text())
            if value.get("config_hash") != c.fingerprint():
                raise ScalerError("ondemand_service_configuration_changed")
            self.sequence = value["sequence"]
            self.transfer_from = value.get("transfer_from")
        else:
            if self.repo.list_instance_intents(pool=c.pool):
                raise ScalerError("ondemand_new_service_requires_unused_pool")
            self._persist()
        self._open_cycle()

    def _open_cycle(self):
        config = cycle_config(self.config, self.sequence)
        self.current = ServiceCycle(self.repo, self.settings, config,
            provider=self.provider, boot_factory=self.boot_factory)
        self.current.leader_id = self.leader_id
        self.current.preserve_rollover = lambda: not self.stopping()
        config.work_dir.mkdir(parents=True, exist_ok=True)
        self.current.config_path = config.work_dir/"operator-cycle.json"
        expected = json_config(config)
        if self.current.config_path.exists():
            if json.loads(self.current.config_path.read_text()) != expected:
                raise ScalerError("ondemand_saved_cycle_mismatch")
        else:
            save(self.current.config_path, expected)
        self.current.initialize()
        if self.transfer_from:
            expected_previous = cycle_config(self.config, self.sequence-1).capacity_approval_id
            if self.transfer_from != expected_previous:
                raise ScalerError("ondemand_transfer_identity_mismatch")
            transfer_unsubmitted_capacity(self.repo, self.transfer_from, config.capacity_approval_id,
                allowed_owners=self.config.allowed_owners, children_done_confirmed=True)
            self.transfer_from = None
            self._persist()

    def idle_probe(self, tag, instance_id):
        if not self.current:
            raise ScalerError("ondemand_cycle_not_initialized")
        return self.current.idle_probe(tag, instance_id)

    def _can_rotate(self, value):
        # Empty pool is the initial waiting state, not a completed rental.
        if (not value["instances"] or not value["all_destroyed"] or value["active_jobs_truncated"]
                or not all(b.children_done() for b in self.current.boots.values())):
            return False
        retired_path = self.current.config.work_dir/"children-retired.json"
        retired = {"config_hash": self.current.config.fingerprint(),
            "intent_ids": sorted(row["id"] for row in value["instances"])}
        if self.current.boots:
            # Persist the real process-handle observation before committing
            # rotation. A restart cannot infer natural exit from lost handles.
            save(retired_path, retired)
        else:
            started = False
            try:
                for row in value["instances"]:
                    path = self.current.config.work_dir/"boot"/row["id"]/"bootstrap-state.json"
                    if path.exists() and json.loads(path.read_text()).get("phase") in ("fleet_starting", "fleet_started"):
                        started = True
                if started and (not retired_path.exists() or json.loads(retired_path.read_text()) != retired):
                    return False
            except (OSError, ValueError, TypeError):
                return False
        # Only provably never-submitted backlog may outlive a GPU rental.
        # A completed job with an unresolved paid attempt is included by the
        # finite status audit and fails this proof too.
        with self.repo.engine.connect() as conn:
            for jid in value["active_job_ids"]:
                job = self.repo._job(conn, jid)
                if not proven_unsubmitted_capacity_job(conn, job):
                    return False
        return True

    def tick(self):
        # A failed qualification or explicit grant revocation ends the service.
        # Only our durable TTL-rollover marker authorizes preserving backlog
        # and opening a replacement cycle under the original service budget.
        if self.current.stopping() and not (self.current.config.work_dir/"rollover.flag").exists():
            self.request_drain()
        if self.stopping():
            self.current.request_drain()
        else:
            rows, _ = self.current._managed()
            if any(row["state"] != "destroyed" and
                   self.repo.clock() >= row["hard_deadline"]-self.config.drain_margin_s for row in rows):
                self.current.request_rollover()
        value = self.current.tick()
        if self.current.stopping() and not (self.current.config.work_dir/"rollover.flag").exists():
            self.request_drain()
        if "instances" not in value:
            # A competing leader/lease must never become a shutdown exception.
            value = self.current.status(decision=value.get("phase", "not_leader"))
        if self._can_rotate(value) and not self.stopping():
            self.repo.set_capacity_approval_enabled(self.current.config.capacity_approval_id, enabled=False)
            # Keep unsettled invoice reservations. A cycle limit never resets spend.
            enough = self.current.remaining_budget() >= self.config.scale_policy["instance_reservation_microusd"]
            if self.sequence >= self.config.max_cycles or not enough:
                self.request_drain()
                value = self.current.tick()
            else:
                self.transfer_from = self.current.config.capacity_approval_id
                self.sequence += 1
                self._persist()
                self._open_cycle()
                value = self.current.status()
        return self.status(value=value)

    def status(self, *, value=None, fresh_ledger_only=False):
        c = self.config
        if self.current is None:
            record = json.loads(self.receipt_path.read_text())
            if record.get("config_hash") != c.fingerprint():
                raise ScalerError("ondemand_service_configuration_changed")
            self.sequence = record["sequence"]
            self.current = ServiceCycle(self.repo, self.settings, cycle_config(c, self.sequence), provider=None)
        value = value or self.current.status(fresh_ledger_only=fresh_ledger_only)
        rows = self.repo.list_instance_intents(pool=c.pool)
        all_destroyed = all(r["state"] == "destroyed" for r in rows)
        drained = bool(self.stopping() and value["drained"] and all_destroyed)
        admission_ready = False
        if not self.stopping():
            try:
                preview = self.current.cold.preview(self.current.config.capacity_approval_id)
                with self.repo.engine.connect() as conn:
                    leader = conn.execute(select(scaler_leaders).where(
                        scaler_leaders.c.pool == c.pool)).mappings().first()
                admission_ready = bool(preview["approval_current"] and leader
                    and leader["expires_at"] > self.repo.clock())
            except Exception:
                pass
        result = {**value, "cycle_id": c.cycle_id, "config_hash": c.fingerprint(),
            "service_mode": "on-demand", "sequence": self.sequence, "max_cycles": c.max_cycles,
            "idle_shutdown_seconds": 600, "minimum_gpu_instances": 0,
            "admission_ready": admission_ready,
            "phase": "drained" if drained else "draining" if self.stopping() else
                "awaiting_jobs" if not value["instances"] else value["phase"],
            "drained": drained, "all_destroyed": all_destroyed,
            "billing_pending": sum(r["billing_status"] != "settled" for r in rows),
            "instances": [{k: r[k] for k in ("id", "state", "provider_instance_id", "hard_deadline", "billing_status")} for r in rows]}
        if not fresh_ledger_only:
            save(c.work_dir/"status.json", result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--enabled", action="store_true")
    parser.add_argument("--credential-stdin", action="store_true")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--request-drain", action="store_true")
    actions.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    repo = provider = loader = controller = None
    handlers = {}
    try:
        config = read_config(args.config)
        if not args.enabled and not args.request_drain and not args.status:
            print(json.dumps({"phase": "disabled", "config_valid": True,
                "config_hash": config.fingerprint(), "provider_calls_enabled": False}))
            return 0
        if args.request_drain:
            record = json.loads((config.work_dir/"service-state.json").read_text())
            if record.get("config_hash") != config.fingerprint():
                raise ScalerError("ondemand_service_configuration_changed")
            (config.work_dir/"drain.flag").touch()
            print(json.dumps({"phase": "drain_requested", "drained": False}))
            return 0
        settings = Settings.from_environment()
        validate_settings(config, settings, require_policy=not args.status)
        repo = Repository(settings.database_url)
        if args.status:
            print(json.dumps(OnDemandController(repo, settings, config, provider=None).status(fresh_ledger_only=True)))
            return 0
        if not config.enabled:
            raise ScalerError("ondemand_config_disabled")
        verify_sources(config)
        verify_identity_files(config)
        loader = stdin_loader(config, sys.stdin.buffer) if args.credential_stdin else AwsLiumLoader(config.secret_arn, config.secret_version_id)
        provider = LiumProvider(enabled=True, manifests=[LiumManifest(**v) for v in config.manifests], loader=loader,
            idle_probe=lambda tag, instance: controller.idle_probe(tag, instance), clock=repo.clock)
        config.work_dir.mkdir(parents=True, exist_ok=True)
        with _slot_lock(config.work_dir, "on-demand-production-scaler") as acquired:
            if not acquired:
                raise ScalerError("ondemand_controller_already_running")
            controller = OnDemandController(repo, settings, config, provider=provider)
            controller.initialize()
            for sig in (signal.SIGINT, signal.SIGTERM):
                handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, lambda *_: controller.request_drain())
            last = None
            while True:
                try:
                    value = controller.tick()
                    compact = {k: value.get(k) for k in ("phase", "sequence", "drained", "billing_pending")}
                    if compact != last:
                        print(json.dumps(compact), flush=True)
                        last = compact
                    if value.get("drained"):
                        return 0
                except Exception:
                    controller.request_drain()
                    print(json.dumps({"phase": "observation_unconfirmed", "drained": False}), flush=True)
                time.sleep(config.interval_s)
    except Exception:
        print(json.dumps({"phase": "ondemand_configuration_or_recovery_required", "drained": False,
            "provider_calls_enabled": False}))
        return 1
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        for resource in (provider, loader, repo):
            if resource:
                try:
                    resource.close()
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
