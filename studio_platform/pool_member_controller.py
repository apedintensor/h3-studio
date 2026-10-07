"""Explicit two-member lifecycle within the existing scaler and rental ledger.

This module supplies no provider, operating authority or alternate task queue.
The caller owns the same fenced pool leader and injects its current policy guard.
"""
from __future__ import annotations

from dataclasses import asdict
import json

from sqlalchemy import insert, select, update

from .capacity import pool_member_ids, reserve_capacity_member
from .repository import (Conflict, capacity_approvals, capacity_pool_members,
    capacity_waiters, instance_intents, jobs, registered_workers, scaler_actions, canonical, BudgetExceeded)
from .scaler import LaunchSpec


def port_for_member(config, repo, intent_id):
    """Stable ports belong to approved member IDs, never offer aliases or order."""
    from .production_scaler import ScalerError, save
    from .service_policy import service_member_ids
    members = service_member_ids(config)
    if not members:
        raise ScalerError("capacity_pool_members_required")
    with repo.engine.connect() as connection:
        approval = connection.execute(select(capacity_approvals).where(
            capacity_approvals.c.id == config.capacity_approval_id)).mappings().one()
        bindings = list(connection.execute(select(capacity_pool_members).where(
            capacity_pool_members.c.approval_id == approval["id"])).mappings())
    if (pool_member_ids(approval["payload"]) != members
            or any(r["approval_hash"] != approval["approval_hash"] or r["member_id"] not in members for r in bindings)):
        raise ScalerError("capacity_pool_member_identity_mismatch")
    expected = {r["intent_id"]: config.port_start+members.index(r["member_id"]) for r in bindings}
    if intent_id not in expected:
        raise ScalerError("capacity_pool_unknown_member")
    path = config.work_dir/"cycle-state.json"
    receipt = json.loads(path.read_text())
    ports = receipt.get("ports")
    if (receipt.get("config_hash") != config.fingerprint() or not isinstance(ports, dict)
            or any(key not in expected or type(value) is not int or value != expected[key]
                   for key, value in ports.items())):
        raise ScalerError("capacity_pool_port_identity_mismatch")
    if intent_id not in ports:
        ports[intent_id] = expected[intent_id]
        save(path, receipt)
    return ports[intent_id]


class MemberLaunchCoordinator:
    """Atomically attach an approved original member to its only create intent.

    A return after a persisted action is never permission to repeat its POST.
    The existing ScaleCoordinator reconciles that exact intent after uncertainty.
    No replacement or rebound membership is provided by this primitive.
    """

    def __init__(self, scaler, *, approval_guard, job_guard):
        self.scaler, self.repo = scaler, scaler.repo
        self.approval_guard, self.job_guard = approval_guard, job_guard

    def managed(self, connection, approval):
        p = approval["payload"]
        members = pool_member_ids(p)
        if not members:
            raise Conflict("capacity_pool_members_required")
        bindings = list(connection.execute(select(capacity_pool_members).where(
            capacity_pool_members.c.approval_id == approval["id"])).mappings())
        if any(r["approval_hash"] != approval["approval_hash"] or r["member_id"] not in members for r in bindings):
            raise Conflict("capacity_pool_member_identity_mismatch")
        ids = {r["intent_id"] for r in bindings}
        rows = list(connection.execute(select(instance_intents).where(
            instance_intents.c.pool == p["pool"])).mappings())
        if (any(r["state"] != "destroyed" and r["id"] not in ids for r in rows)
                or not ids <= {r["id"] for r in rows}):
            raise Conflict("capacity_pool_unknown_member")
        selected = [self.repo._instance_billing(connection, dict(r)) for r in rows if r["id"] in ids]
        observed = [(r["provider"], r["provider_instance_id"]) for r in selected if r["provider_instance_id"]]
        if len(observed) != len(set(observed)):
            raise Conflict("capacity_pool_duplicate_provider_instance")
        actions = {r["intent_id"]: dict(r) for r in connection.execute(select(scaler_actions).where(
            scaler_actions.c.intent_id.in_(ids))).mappings()}
        if any(r["provider"] != p["launch"]["provider"] or r["id"] not in actions
                or actions[r["id"]]["pool"] != p["pool"]
                or actions[r["id"]]["launch_spec"] != p["launch"] for r in selected):
            raise Conflict("capacity_pool_member_action_mismatch")
        return selected, actions, {r["member_id"]: r["intent_id"] for r in bindings}

    def _demand(self, connection, approval):
        p, now = approval["payload"], self.repo.clock()
        if (not approval["enabled"] or min(approval["expires_at"], p["qualification_expires_at"],
                p["quote_expires_at"], p["scale_policy"]["hard_deadline"]) <= now
                or self.approval_guard(p) is not True):
            return False
        rows = connection.execute(select(jobs, capacity_waiters.c.deadline.label("wait_deadline"))
            .join(capacity_waiters, capacity_waiters.c.job_id == jobs.c.id).where(
                capacity_waiters.c.approval_id == approval["id"],
                capacity_waiters.c.approval_hash == approval["approval_hash"],
                capacity_waiters.c.intent_id.is_(None), jobs.c.tenant_id == approval["tenant_id"],
                jobs.c.pool == p["pool"],
                # Unknown/cancel/collection still block idle destruction, but
                # do not themselves finance an unused original member.
                jobs.c.status.in_(("waiting_capacity", "queued", "claimed", "submitting", "running")))
            .order_by(jobs.c.id).limit(4097)).mappings()
        values = list(rows)
        if len(values) > 4096:
            raise Conflict("capacity_pool_demand_window_exceeded")
        for job in values:
            execution = job["execution_plan"]
            if (execution.get("capacity_binding") != "pool-members-v1"
                    or execution.get("capacity_approval_id") != approval["id"]
                    or execution.get("capacity_approval_hash") != approval["approval_hash"]
                    or self.job_guard(job) is not True):
                continue
            if job["status"] in ("waiting_capacity", "queued") and job["wait_deadline"] <= now:
                continue
            return True
        return False

    def before_create(self, lease, approval_id, member_id, intent_id):
        """A bounded read before the sole POST; never renew or reacquire a lease."""
        try:
            with self.repo.transaction() as connection:
                self.scaler._leader(connection, lease)
                approval = connection.execute(select(capacity_approvals).where(
                    capacity_approvals.c.id == approval_id)).mappings().one()
                _, _, mapping = self.managed(connection, approval)
                return (approval["pool"] == lease.pool and mapping.get(member_id) == intent_id
                    and self._demand(connection, approval))
        except Exception:
            return False

    def create_once(self, lease, approval_id, member_id):
        """Reserve/bind/action/barrier commit precedes exactly one provider call.

        Only *original* approved members can be created; an unknown A still
        consumes its reservation while an unbound, separately approved B can
        proceed. Foreign/unbound live intents fail closed before any reservation.
        """
        if self.scaler.enabled is not True or self.scaler.provider.enabled is not True:
            return {"state": "disabled"}
        with self.repo.transaction() as connection:
            self.scaler._leader(connection, lease)
            self.repo._lock_capacity(connection)
            approval = self.repo._locked(connection, select(capacity_approvals).where(
                capacity_approvals.c.id == approval_id))
            if approval is None or approval["pool"] != lease.pool:
                raise Conflict("capacity_pool_approval_mismatch")
            p = approval["payload"]
            if member_id not in pool_member_ids(p):
                raise Conflict("capacity_pool_member_not_approved")
            _, _, mapping = self.managed(connection, approval)
            if member_id in mapping:
                return {"state": "member_already_bound", "intent_id": mapping[member_id]}
            if not self._demand(connection, approval):
                return {"state": "no_confirmed_pool_demand"}
            launch = LaunchSpec(**p["launch"])
            if launch.provider != getattr(self.scaler.provider, "provider_id", None):
                raise Conflict("scaler_provider_identity_mismatch")
            validate = getattr(self.scaler.provider, "validate_launch", None)
            if validate is not None:
                validate(launch, physical_gpus=1, slots=1,
                    reserved_cost_microusd=p["scale_policy"]["instance_reservation_microusd"],
                    hard_deadline=min(p["scale_policy"]["hard_deadline"], approval["expires_at"]))
            intent = reserve_capacity_member(self.repo, approval_id, member_id, connection=connection)
            if not intent["created"]:
                raise Conflict("capacity_pool_member_action_missing")
            connection.execute(insert(scaler_actions).values(intent_id=intent["id"], pool=p["pool"],
                launch_spec=canonical(asdict(launch)), create_started_at=self.repo.clock()))
            if self.scaler.preparation_timeout_s is not None:
                self.scaler._preparation_receipt(connection, intent, "awaiting_provider")
            self.repo.update_instance(intent["id"], "creating", connection=connection)
        fact, observed_at = self.scaler._call(intent, "create", launch=launch,
            before_create=lambda: self.before_create(lease, approval_id, member_id, intent["id"]))
        self.scaler._apply(lease, intent["id"], fact, observed_at)
        return {"state": "creation_observed", "intent_id": intent["id"], "provider_state": fact.state}


# Imported after the pure transaction primitives to keep the controller entry
# point's lazy import free of an on-demand/controller cycle.
from .production_scaler import FiniteController, ScalerError, MODEL, save


class PoolServiceCycle(FiniteController):
    """Two original members; no in-place replacement or generation rebinding.

    Each node retains its own ProductionBoot and original worker/attempts. A
    failed peer is quarantined locally. Global policy expiry/revocation still
    drains the pool. A subsequent pair requires complete prior retirement.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .service_policy import service_member_ids
        self.member_ids = service_member_ids(self.config)
        if len(self.member_ids) != 2:
            raise ScalerError("capacity_pool_members_required")
        self.members = MemberLaunchCoordinator(self.scaler,
            approval_guard=self.approval_current, job_guard=self.job_allowed)
        self.scaler.unused_preparation_guard = self._unused_provider_preparation
        self.scaler.unsubmitted_retirement_guard = self._member_retirement_allowed

    def approval_current(self, payload):
        c = self.config
        return bool(not self.stopping() and payload.get("pool_controller") == "continuing-two-members-v1"
            and pool_member_ids(payload) == self.member_ids
            and payload["tenant_id"] == c.tenant and payload["pool"] == c.pool
            and payload["configuration_id"] == c.configuration_id and payload["recipe_ids"] == list(c.recipe_ids)
            and payload["policy_hash"] == c.execution_policy_sha256
            and payload["budget_scope"] == asdict(c.scope)
            and sorted(payload["budget_account_ids"]) == sorted(c.budget_account_ids)
            and payload["scale_policy"] == c.scale_policy and payload["launch"] == c.launches[0]
            and self.policies.capacity_approval_current(payload))

    def _approval(self, connection):
        return connection.execute(select(capacity_approvals).where(
            capacity_approvals.c.id == self.config.capacity_approval_id)).mappings().first()

    def _managed(self):
        with self.repo.engine.connect() as connection:
            grant = self._approval(connection)
            if grant is None:
                rows = self.repo.list_instance_intents(pool=self.config.pool)
                if any(row["state"] != "destroyed" for row in rows):
                    raise ScalerError("capacity_pool_unknown_member")
                return [], {}
            rows, actions, _ = self.members.managed(connection, grant)
            return rows, actions

    def initialize(self):
        from .autoscale import ScalePolicy
        from .execution_policy import read_policy
        c = self.config
        c.work_dir.mkdir(parents=True, exist_ok=True)
        path = c.work_dir/"cycle-state.json"
        if path.exists():
            if json.loads(path.read_text()).get("config_hash") != c.fingerprint():
                raise ScalerError("ondemand_cycle_configuration_changed")
        else:
            self._managed()
            if not c.created_at <= self.repo.clock() < c.stop_claiming_at:
                raise ScalerError("ondemand_service_approval_expired")
            save(path, {"config_hash": c.fingerprint(), "ports": {}, "created_at": self.repo.clock()})
        self.remaining_budget()
        with self.repo.engine.connect() as connection:
            existing = self._approval(connection)
        if existing:
            if existing["enabled"] != 1 or not self.approval_current(existing["payload"]):
                self.request_drain()
            return
        policy = read_policy(self.settings.execution_policy_file)
        engine = ({"backend": c.execution_backend, "engine_manifest_digest": c.engine_manifest_digest}
            if c.execution_backend == "wangp-worker" else {})
        if c.output_delivery:
            engine["output_delivery"] = c.output_delivery
        self.repo.approve_capacity(c.capacity_approval_id, tenant_id=c.tenant, pool=c.pool,
            model_id=MODEL, configuration_id=c.configuration_id, recipe_ids=list(c.recipe_ids),
            policy_hash=c.execution_policy_sha256, qualification_evidence_id=c.qualification_evidence_id,
            qualification_expires_at=policy["qualification"]["expires_at"], quote_expires_at=policy["reservation"]["expires_at"],
            expires_at=min(c.stop_claiming_at, policy["qualification"]["expires_at"], policy["reservation"]["expires_at"]),
            launch=LaunchSpec(**c.launches[0]), scale_policy=ScalePolicy(**c.scale_policy),
            budget_scope=c.scope, budget_account_ids=c.budget_account_ids, enabled=True,
            pool_members={"version": 1, "member_ids": list(self.member_ids)},
            pool_controller="continuing-two-members-v1", **engine)

    def port_for(self, intent_id):
        return port_for_member(self.config, self.repo, intent_id)

    def member_hold(self, intent):
        path = self.config.work_dir/"member-holds"/(intent["id"]+".json")
        if not path.exists():
            return None
        value = json.loads(path.read_text())
        if (value.get("config_hash") != self.config.fingerprint() or value.get("intent_id") != intent["id"]
                or value.get("instance_id") != intent["provider_instance_id"]
                or value.get("sources") != self.config.source_sha256):
            raise ScalerError("capacity_pool_member_hold_mismatch")
        return value

    def _boot_failure(self, intent, state):
        # This is a quarantine, not proof of stopped inference or empty devices.
        # Existing worker recovery and provider idle proofs still gate deletion.
        directory = self.config.work_dir/"member-holds"
        directory.mkdir(exist_ok=True)
        if not self.member_hold(intent):
            save(directory/(intent["id"]+".json"), {"config_hash": self.config.fingerprint(),
                "intent_id": intent["id"], "instance_id": intent["provider_instance_id"],
                "sources": self.config.source_sha256, "reason": state["state"], "observed_at": self.repo.clock()})
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            row = self.repo._locked(connection, select(instance_intents).where(instance_intents.c.id == intent["id"]))
            if row["state"] in ("starting", "ready", "busy"):
                self.repo.update_instance(row["id"], "draining", connection=connection)
            connection.execute(update(registered_workers).where(registered_workers.c.provider == row["provider"],
                registered_workers.c.instance_id == row["provider_instance_id"],
                registered_workers.c.state != "retired").values(drain_requested=1, state="draining", updated_at=self.repo.clock()))
        if intent["id"] in self.boots:
            self.boots[intent["id"]].request_drain()

    def _unused_provider_preparation(self, connection, intent):
        grant = self._approval(connection)
        if grant is None:
            return False
        _, _, mapping = self.members.managed(connection, grant)
        if intent["id"] not in mapping.values() or intent["id"] in self.boots:
            return False
        # The scaler already requires the exact committed awaiting-provider
        # barrier, fresh provider fact and absence of any registered worker.
        for path in (self.config.work_dir/"boot"/intent["id"],
                self.config.work_dir/("lifetime-"+intent["id"]+".json")):
            if path.exists() or path.is_symlink():
                return False
        return True

    def _member_retirement_allowed(self, connection, intent):
        grant = self._approval(connection)
        if grant is None:
            return False
        _, _, mapping = self.members.managed(connection, grant)
        return intent["id"] in mapping.values() and self.member_hold(intent) is not None

    def _bootstrap_start_allowed(self, intent_id, lease):
        if self.stopping():
            return False
        try:
            with self.repo.transaction() as connection:
                self.scaler._leader(connection, lease)
                grant = self._approval(connection)
                rows, _, mapping = self.members.managed(connection, grant)
                row = next(r for r in rows if r["id"] == intent_id)
                return bool(intent_id in mapping.values() and row["state"] in ("starting", "ready", "busy")
                    and row["hard_deadline"]-self.repo.clock() >= self.config.drain_margin_s
                    and self.member_hold(row) is None and self.members._demand(connection, grant))
        except Exception:
            return False

    def _capacity_decision(self, instances, actions, stopping):
        from .autoscale import ScalePolicy
        from .scaler import LeaseLost
        c = self.config
        # The same scaler reconciles every original action and performs proven
        # idle/TTL retirement. Creation is exclusively the member transaction.
        policy = ScalePolicy(**{**c.scale_policy, "max_instances": 0, "max_physical_gpus": 0})
        decision = self.scaler.tick(self.leader_id, c.scope, c.pool, (), (), policy=policy,
            launch=None, budget_account_ids=c.budget_account_ids)
        if stopping:
            return decision
        lease = self.scaler.acquire(c.pool, self.leader_id)
        if lease is None:
            return {"state": "not_leader"}
        for member in self.member_ids:
            try:
                result = self.members.create_once(lease, c.capacity_approval_id, member)
                if result["state"] == "creation_observed":
                    decision = result
            except BudgetExceeded:
                return {"state": "blocked", "reason": "ledger_capacity_or_budget_limit"}
            except LeaseLost:
                return {"state": "leader_changed_reconcile_required"}
        return decision

    def request_rollover(self):
        # Only whole-pair retirement uses this; one member's TTL is local.
        (self.config.work_dir/"rollover.flag").touch()
        self.request_drain()

    def preparation_hold(self):
        return None  # Member holds must never revoke a healthy peer's approval.

    def provider_retirement(self):
        return None  # Legacy pool-wide provider-retry streak does not apply.

    def rotation_allowed(self):
        rows, _ = self._managed()
        if not rows or any(self.member_hold(row) is not None for row in rows):
            return False
        with self.repo.engine.connect() as connection:
            for row in rows:
                phase = self.scaler.preparation(connection, row)
                if phase and phase["phase"] == "retiring_unused":
                    return False  # No automatic failed-member replacement in this slice.
        return all(row["state"] == "destroyed" and row["billing_status"] == "settled" for row in rows)

    def _close_unsubmitted(self):
        # Final service shutdown retains the normal queued-task hold semantics.
        # An idle pair has no accepted obligations; rotation does not cancel.
        return super()._close_unsubmitted()
