"""Finite scaler's boot/qualification and drain-safe worker child.

Same CPU container owns SSH tunnels and its fleet. This module has no provider
credential reader, rent path, budget initialization or automatic child restart.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys

from sqlalchemy import select, update

from .control import WorkerControl
from .fleet import FleetSupervisor, read_config as read_fleet, run_slot
from .lium_bootstrap import BootConfig, BootController, BootError, SSHHost
from .repository import Repository, instance_intents, registered_workers
from .worker import ComfyBackend, SubmissionRejected, _slot_lock


class ProductionBoot(BootController):
    def __init__(self, repo, provider, finite, intent, local_port, *, config_path=None,
                 ssh_factory=SSHHost, backend_factory=ComfyBackend, verify_smoke=None, popen=None):
        self.finite, self.operator_path = finite, Path(config_path) if config_path else None
        self.intent_id = intent["id"]
        self._popen_impl = popen or subprocess.Popen
        self._stopping = False
        config = BootConfig(finite.work_dir/"boot", finite.source_dir, finite.ssh_key_file,
            finite.known_hosts_file, local_port, finite.configuration_id, enabled=True,
            smoke_enabled=True, fleet_enabled=True, trust_first_host_key=finite.trust_first_host_key,
            minimum_remaining_s=finite.drain_margin_s)
        super().__init__(repo, provider, config, ssh_factory=ssh_factory, backend_factory=backend_factory,
            verify_smoke=verify_smoke, fleet_factory=self._fleet)

    def _fleet(self, config, repo, path):
        return FleetSupervisor(config, repo, path, popen=self._popen)

    def _popen(self, argv, **kwargs):
        fleet_file = self.config.work_dir/self.intent_id/"fleet.json"
        expected = [sys.executable, "-m", "studio_platform.fleet", "--config", str(fleet_file),
            "--slot", "lium-"+self.intent_id.replace("-", ""), "--config-hash", self.fleet.config.fingerprint()]
        if argv != expected or self.operator_path is None:
            raise BootError("finite_unexpected_child_command")
        # stdin must never inherit the one-shot provider credential pipe.
        kwargs["stdin"] = subprocess.DEVNULL
        return self._popen_impl([sys.executable, "-m", "studio_platform.production_scaler", "--config", str(self.operator_path),
            "--enabled", "--slot", self.intent_id, "--config-hash", self.fleet.config.fingerprint()], **kwargs)

    def request_drain(self):
        self._stopping = True
        if self.fleet:
            self.fleet.drain()

    def _lifetime(self, intent):
        path = self.finite.work_dir/("lifetime-"+intent["id"]+".json")
        if path.exists():
            value = json.loads(path.read_text())
        else:
            value = self.provider.lifetime(intent["id"], intent["provider_instance_id"],
                local_created_at=intent["created_at"], maximum_hours=4)
            from .production_scaler import save
            save(path, value)
        if (value.get("instance_id") != intent["provider_instance_id"]
                or type(value.get("safe_deadline")) not in (int, float)):
            raise BootError("finite_lifetime_identity_unconfirmed")
        with self.repo.transaction() as conn:
            row = self.repo._locked(conn, select(instance_intents).where(instance_intents.c.id == intent["id"]))
            conn.execute(update(instance_intents).where(instance_intents.c.id == intent["id"]).values(
                hard_deadline=min(row["hard_deadline"], value["safe_deadline"])))
        return min(intent["hard_deadline"], value["safe_deadline"])

    def tick(self, intent_id, *, stopping=False):
        from .production_scaler import verify_sources
        verify_sources(self.finite)
        with self.repo.engine.connect() as conn:
            intent = dict(conn.execute(select(instance_intents).where(instance_intents.c.id == intent_id)).mappings().one())
        if intent_id != self.intent_id:
            raise BootError("finite_boot_identity_mismatch")
        deadline = self._lifetime(intent)
        self._stopping = (self._stopping or stopping or intent["state"] in ("draining", "destroying", "destroyed")
            or self.repo.clock() >= deadline-self.finite.drain_margin_s)
        if self._stopping:
            self.request_drain()
            # Preserve pending boot qualification collection too. No new
            # qualification POST is permitted once draining begins.
            receipt = self.config.work_dir/intent_id/"bootstrap-state.json"
            if receipt.exists():
                state = json.loads(receipt.read_text())
                if state.get("smoke_submission_started") and state.get("phase") not in ("qualified", "qualification_failed", "fleet_starting", "fleet_started"):
                    if self.backend is None:
                        return {"state": "qualification_recovery_required"}
                    result = self._smoke(receipt.parent, receipt, state)
                    if result["state"] != "qualified":
                        return result
            if self.fleet:
                self.fleet.tick()
                self._retire_idle_children()
            return {"state": "draining", "children_done": self.children_done()}
        result = super().tick(intent_id)
        # The base boot module remains compatible with its historical 4-step
        # smoke; only this subclass supplies the stronger immutable request.
        if result.get("generation_verified"):
            result["qualification_scope"] = "single_host_fl2va_5s_768p_50steps_audio_not_all_controls"
        return result

    def _smoke(self, directory, receipt, state):
        from comfy_workflow import build_workflow
        request = {"mode": "fl", "prompt": "A red ceramic teapot on a wooden table, slow cinematic camera move, gentle ambient sound.",
            "duration": 5, "resolution": "768P", "aspect_ratio": "16:9", "steps": 50, "seed": "12345", "generate_audio": True,
            "video_decode": "tiled", "encoder_device": "cpu", "_job_id": state["tag"]}
        from .repository import request_hash
        digest = request_hash(request)
        if state.get("smoke_request_hash") not in (None, digest):
            raise BootError("finite_qualification_request_changed")
        if state["phase"] in ("qualified", "fleet_starting", "fleet_started"):
            if (state.get("smoke_request_hash") != digest
                    or state.get("evidence", {}).get("scope") != "single_host_fl2va_5s_768p_50steps_audio_not_all_controls"):
                raise BootError("finite_full_qualification_evidence_required")
            return {"state": "qualified", "generation_verified": True, "evidence": state["evidence"]}
        if state["phase"] == "qualification_failed":
            return {"state": "qualification_failed"}
        if not state.get("smoke_submission_started"):
            if self._stopping:
                return {"state": "qualification_not_started_draining"}
            # Qualification is itself real GPU work and does not pass through
            # WorkerControl's job deadline check. Twenty minutes is a bounded
            # conservative allowance for this exact 50-step envelope, not an
            # inference SLA; keep the configured collection margin in addition.
            with self.repo.engine.connect() as conn:
                deadline = conn.execute(select(instance_intents.c.hard_deadline).where(
                    instance_intents.c.id == self.intent_id)).scalar_one()
            if self.repo.clock()+1200+self.finite.collection_margin_s >= min(deadline, self.finite.hard_deadline):
                self.request_drain()
                return {"state": "qualification_deadline_insufficient"}
            queue = self.backend._json("GET", "/queue")
            if queue.get("queue_running") != [] or queue.get("queue_pending") != []:
                return {"state": "qualification_upstream_busy"}
            graph = build_workflow(request, {}, {})
            state.update(smoke_request_hash=digest, smoke_submission_started=self.repo.clock(), phase="smoke_submitting")
            self._save(receipt, state)
            try:
                state["smoke_task_id"] = self.backend.submit(graph, state["tag"])
                state["phase"] = "smoke_running"
                self._save(receipt, state)
            except SubmissionRejected:
                state["phase"] = "qualification_failed"
                self._save(receipt, state)
                return {"state": "qualification_failed"}
            except Exception:
                return {"state": "smoke_submission_unknown"}
        task = state.get("smoke_task_id")
        outcome = self.backend.poll(state["tag"], task) if task else self.backend.reconcile(state["tag"])
        if outcome.task_id and not task:
            state["smoke_task_id"] = task = outcome.task_id
            self._save(receipt, state)
        if outcome.state in ("failed", "cancelled"):
            state["phase"] = "qualification_failed"
            self._save(receipt, state)
            return {"state": "qualification_failed"}
        if outcome.state != "succeeded" or not task:
            return {"state": "smoke_running" if outcome.state == "running" else "smoke_submission_unknown"}
        lock = self.finite.work_dir/"collection-lock"
        lock.mkdir(exist_ok=True)
        with _slot_lock(lock, "production-cpu-collection-v1") as acquired:
            if not acquired:
                return {"state": "qualification_collection_waiting"}
            paths = self.backend.fetch({"request": {"request": request}}, state["tag"], task, directory, lambda: None)
            evidence = self.verify_smoke(paths, request)
        evidence.update(scope="single_host_fl2va_5s_768p_50steps_audio_not_all_controls",
            elapsed_wall_seconds=self.repo.clock()-state["smoke_submission_started"], completed_at=self.repo.clock())
        state.update(phase="qualified", evidence=evidence)
        self._save(receipt, state)
        return {"state": "qualified", "generation_verified": True, "evidence": evidence}

    def idle_probe(self, tag, instance_id):
        receipt = self.config.work_dir/tag/"bootstrap-state.json"
        if receipt.exists():
            state = json.loads(receipt.read_text())
            if state.get("smoke_submission_started") and state.get("phase") not in ("qualified", "qualification_failed", "fleet_starting", "fleet_started"):
                raise BootError("finite_qualification_result_still_unresolved")
        return super().idle_probe(tag, instance_id)

    def _retire_idle_children(self):
        if not self.children_done() or self.backend is None:
            return
        proof = self.idle_probe(self.bound_intent, self.bound_instance)
        if not proof.idle:
            return
        for worker_id in self.fleet.children:
            control = WorkerControl(self.repo)
            worker = control.get(worker_id)
            if worker["state"] == "retired":
                continue
            if worker["current_job_id"] is None and worker["expires_at"] > self.repo.clock() and worker["drain_requested"]:
                control.retire(worker_id, upstream_idle_confirmed=True)

    def children_done(self):
        return self.fleet is None or all(proc.poll() is not None for proc in self.fleet.children.values())

    def close_if_safe(self, *, destroyed=False):
        if not destroyed or not self.children_done():
            return False
        if self.backend:
            self.backend.close()
            self.backend = None
        if self.host:
            self.host.close()
            self.host = None
        return True

    def close(self):
        # Intentionally cannot use BootController.close's finite shutdown
        # timeout as proof that SSH may be closed.
        raise BootError("finite_close_requires_confirmed_destroyed_and_children_done")


def run_child(config, intent_id, expected_hash, settings):
    from .drain_safe_runner import DrainSafeRunner
    from .production_scaler import ScalerError
    from .repository import jobs
    from sqlalchemy import and_
    repo = Repository(settings.database_url)
    try:
        fleet_path = config.work_dir/"boot"/intent_id/"fleet.json"
        fleet = read_fleet(fleet_path)
        receipt = json.loads((fleet_path.parent/"bootstrap-state.json").read_text())
        identity = receipt.get("identity", {})
        if (not expected_hash or fleet.fingerprint() != expected_hash or identity.get("intent_id") != intent_id
                or identity.get("sources") != config.source_sha256 or identity.get("configuration_id") != config.configuration_id
                or receipt.get("phase") not in ("fleet_starting", "fleet_started")):
            raise ScalerError("finite_child_configuration_mismatch")
        worker_id = "lium-"+intent_id.replace("-", "")
        spec = fleet.slot(worker_id).spec
        if (len(fleet.slots) != 1 or spec.pool != config.pool or spec.configuration_id != config.configuration_id
                or spec.instance_id != identity.get("instance_id")):
            raise ScalerError("finite_child_identity_mismatch")
        with repo.engine.connect() as conn:
            row = conn.execute(select(instance_intents).where(instance_intents.c.id == intent_id)).mappings().one()
        deadline = min(config.hard_deadline, row["hard_deadline"])
        def stop_new():
            return (config.work_dir/"drain.flag").exists() or repo.clock() >= deadline-config.drain_margin_s
        def job_allowed(job):
            # Called under the worker/queue transaction; no separate DB read.
            duration = job.get("expected_runtime_s")
            return (job["tenant_id"] == config.tenant and job["owner_id"] == config.owner
                and job["project_id"] == config.project_id and job["pool"] == config.pool
                and job["execution_plan"].get("configuration_id") == config.configuration_id
                and type(duration) in (float, int) and duration > 0
                and repo.clock()+duration+config.collection_margin_s < deadline)
        predicate = and_(jobs.c.tenant_id == config.tenant, jobs.c.owner_id == config.owner,
            jobs.c.project_id == config.project_id, jobs.c.pool == config.pool,
            jobs.c.execution_plan["configuration_id"].as_string() == config.configuration_id)
        return run_slot(fleet, worker_id, settings, repository=repo,
            runner_factory=lambda *a, **kw: DrainSafeRunner(*a, stop_new=stop_new, job_allowed=job_allowed,
                job_filter=predicate, collection_lock_dir=config.work_dir/"collection-lock", **kw)) or 0
    finally:
        repo.close()
