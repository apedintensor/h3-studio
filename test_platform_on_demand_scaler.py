"""Repeated real-job cold-start lifecycle using local ledger/fake cloud only."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import unittest

from sqlalchemy import select, update

from studio_platform.autoscale import ScalePolicy
from studio_platform.capabilities import compile_request
from studio_platform.control import WorkerControl
from studio_platform.execution_policy import ExecutionPolicies
from studio_platform.on_demand_scaler import OnDemandConfig, OnDemandController
from studio_platform.production_scaler import MODEL, RECIPE, ScalerError
from studio_platform.queue import TaskQueue
from studio_platform.repository import Scope, request_hash, instance_intents, attempts, capacity_waiters
from studio_platform.settings import Settings
from test_platform_api import generation_request
from test_platform_execution_policy import policy
from test_platform_production_scaler import configuration, FakeProvider, FakeBoot
from test_platform_repository import LedgerCase


class HeartbeatBoot(FakeBoot):
    def tick(self, *args, **kwargs):
        worker = self.control.get(self.worker)
        if worker["state"] != "retired" and worker["expires_at"] > self.repo.clock():
            self.control.heartbeat(self.worker, worker["fence"])
        return super().tick(*args, **kwargs)


class OnDemandTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        base = configuration(self.root, self.now)
        scale = {**base.scale_policy, "max_instances": 1, "max_physical_gpus": 1, "idle_before_drain_s": 600}
        self.config = OnDemandConfig(**{**asdict(base), "work_dir": self.root/"service",
            "allowed_owners": ["superdan", "supervan"], "scale_policy": scale,
            "launches": base.launches[:1], "manifests": base.manifests[:1], "max_cycles": 2})
        self.value = policy(self.now)
        self.value.update(pool=self.config.pool, configuration_id=self.config.configuration_id,
            recipe_ids=[RECIPE], budget_accounts=["job-budget"])
        self.value["qualification"].update(evidence_id=self.config.qualification_evidence_id, expires_at=self.now+7000)
        self.value["reservation"].update(expected_runtime_s=300, expires_at=self.now+7000)
        self.value["envelope"].update(max_duration_seconds=6, max_reference_files=0, max_guides=0, allow_first_last=False)
        self.value["envelope"]["controls"].update(video_decode=["tiled"], encoder_device=["cpu"], ref_image_size=["max"])
        self.path = self.root/"policy.json"
        self.path.write_text(json.dumps(self.value))
        self.path.chmod(0o600)
        self.config = replace(self.config, execution_policy_sha256=request_hash(self.value))
        self.settings = Settings(self.config.data_dir, database_url=self.url, auth_mode="password",
            public_origin="https://www.sixnine.art", generation_enabled=True, execution_backend="comfy-worker",
            execution_policy_file=self.path)
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        self.repo.configure_pool(self.config.pool, max_instances=1, max_physical_gpus=1)
        self.repo.configure_budget("finite-budget", tenant_id="sixnine", limit_microusd=6_000_000)
        self.repo.configure_budget("job-budget", tenant_id="sixnine", limit_microusd=30_000_000)
        self.provider = FakeProvider(lambda: self.now)
        self.controller = OnDemandController(self.repo, self.settings, self.config,
            provider=self.provider, boot_factory=HeartbeatBoot)
        self.controller.initialize()
        self.policies = ExecutionPolicies(self.settings, self.repo)

    def tick(self, seconds=16):
        self.now += seconds
        return self.controller.tick()

    def submit(self, owner="superdan", story="story-one"):
        scope = Scope("sixnine", owner, story)
        request = generation_request()
        request["controls"].update(duration=5, steps=50, resolution="768P", video_decode="tiled", encoder_device="cpu")
        compiled, fingerprint = compile_request(request, lambda _: None)
        admission = self.policies.evaluate(compiled, scope, fingerprint)
        self.assertTrue(admission.execution["enabled"], admission.execution)
        plan = self.repo.create_plan(scope, compiled, admission.execution, expires_at=admission.expires_at,
            estimated_cost_microusd=admission.cost)
        job = self.repo.create_job(scope, plan["id"], owner+story, initial_status=admission.execution["admission_state"],
            budget_account_ids=admission.execution["budget_account_ids"])
        return scope, job

    def finish(self, scope, job):
        boot = next(iter(self.controller.current.boots.values()))
        claim = boot.control.claim(boot.worker, self.config.pool)
        self.assertEqual(claim.job["id"], job["id"])
        queue = TaskQueue(self.repo)
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, "synthetic-task")
        queue.begin_collection(claim.lease)
        queue.complete(claim.lease, [{"kind": "video", "object_key": "synthetic/result.mp4", "size_bytes": 1,
            "sha256": "a"*64, "validated": True}], actual_cost_microusd=0)
        boot.control.observe(boot.worker, job["id"])
        self.assertEqual(self.repo.get_job(scope, job["id"])["status"], "succeeded")

    def start_job(self, owner="superdan", story="story-one"):
        scope, job = self.submit(owner, story)
        self.assertEqual(job["status"], "waiting_capacity")
        for _ in range(3):
            self.tick()
        self.assertEqual(self.repo.get_job(scope, job["id"])["status"], "queued")
        return scope, job

    def test_no_user_job_never_creates_or_resets_budget(self):
        budget = self.repo.get_budget("finite-budget")
        for _ in range(4):
            self.assertEqual(self.tick()["phase"], "awaiting_jobs")
        self.assertEqual(self.provider.creates, [])
        self.assertEqual(self.repo.get_budget("finite-budget"), budget)

    def test_idle_600_seconds_removes_then_other_owner_can_start_next_cycle(self):
        scope, job = self.start_job()
        self.assertEqual(len(self.provider.creates), 1)
        self.finish(scope, job)
        self.tick()  # First whole-business idle observation starts the timer.
        for _ in range(37):
            self.tick()  # 592 seconds since idle began.
        self.assertEqual(self.provider.destroys, [])
        for _ in range(3):
            self.tick()
        self.assertEqual(len(self.provider.destroys), 1)
        self.assertEqual(self.controller.sequence, 2)
        before = self.repo.get_budget("finite-budget")
        self.assertEqual(before["reserved_microusd"], 2_000_000)  # invoice pending
        scope2, job2 = self.start_job("supervan", "another-story")
        self.assertEqual(len(self.provider.creates), 2)
        self.assertEqual(self.repo.get_budget("finite-budget")["reserved_microusd"], 4_000_000)
        self.assertEqual(self.repo.get_budget("finite-budget")["limit_microusd"], before["limit_microusd"])
        self.assertEqual(self.repo.get_job(scope2, job2["id"])["status"], "queued")

    def test_unknown_creation_does_not_rotate_or_rerent(self):
        self.provider.uncertain = self.provider.unknown = True
        self.submit()
        for _ in range(8):
            self.tick()
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(self.controller.sequence, 1)
        self.assertEqual(self.repo.list_instance_intents()[0]["state"], "creation_unknown")
        self.assertEqual(self.provider.destroys, [])

    def test_next_job_resets_idle_timer_and_reuses_same_gpu(self):
        first = self.start_job()
        self.finish(*first)
        self.tick()
        for _ in range(37):
            self.tick()
        second = self.submit("supervan", "second-story")
        self.assertEqual(second[1]["status"], "queued")
        self.tick()  # New queued work resets the whole-business idle timer.
        self.assertEqual(self.provider.destroys, [])
        self.finish(*second)
        self.tick()
        for _ in range(37):
            self.tick()
        self.assertEqual(self.provider.destroys, [])
        self.assertEqual(len(self.provider.creates), 1)
        for _ in range(3):
            self.tick()
        self.assertEqual(len(self.provider.destroys), 1)

    def test_other_leader_does_not_turn_compact_status_into_drain_or_creation(self):
        lease = self.controller.current.scaler.acquire(self.config.pool, "another-controller")
        self.assertIsNotNone(lease)
        value = self.tick()
        self.assertFalse(self.controller.stopping())
        self.assertFalse(value["drained"])
        self.assertEqual(self.provider.creates, [])

    def shorten_current_ttl(self, seconds=160):
        row = self.repo.list_instance_intents(pool=self.config.pool)[-1]
        with self.repo.transaction() as conn:
            conn.execute(update(instance_intents).where(instance_intents.c.id == row["id"])
                .values(hard_deadline=self.now+seconds))
        return row

    def test_ttl_rollover_preserves_queued_job_id_budget_and_running_result(self):
        first = self.start_job()
        second_scope, second = self.submit("supervan", "queued-at-ttl")
        boot = next(iter(self.controller.current.boots.values()))
        claim = boot.control.claim(boot.worker, self.config.pool)
        queue = TaskQueue(self.repo)
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, "original-paid-task")
        old = self.shorten_current_ttl()
        lease = claim.lease
        for _ in range(12):
            self.tick()
            lease = queue.heartbeat(lease)
        self.assertEqual(self.controller.sequence, 1)
        self.assertEqual(self.provider.destroys, [])
        self.assertEqual(self.repo.get_job(second_scope, second["id"])["status"], "queued")
        self.assertEqual(self.repo.get_job(first[0], first[1]["id"])["status"], "running")
        queue.begin_collection(lease)
        queue.complete(lease, [{"kind": "video", "object_key": "synthetic/original.mp4",
            "size_bytes": 1, "sha256": "b"*64, "validated": True}], actual_cost_microusd=0)
        boot.control.observe(boot.worker, first[1]["id"])
        reserved_before = self.repo.get_budget("job-budget")["reserved_microusd"]
        self.tick()
        self.assertEqual(self.controller.sequence, 2)
        self.assertEqual(len(self.provider.creates), 1)
        moved = self.repo.get_job(second_scope, second["id"])
        self.assertEqual(moved["status"], "waiting_capacity")
        self.assertEqual(moved["attempt_no"], 0)
        self.assertEqual(self.repo.get_budget("job-budget")["reserved_microusd"], reserved_before)
        self.assertEqual(moved["execution_plan"]["capacity_approval_id"], self.controller.current.config.capacity_approval_id)
        for _ in range(3):
            self.tick()
        self.assertEqual(len(self.provider.creates), 2)
        self.assertEqual(self.repo.get_job(second_scope, second["id"])["status"], "queued")
        with self.repo.engine.connect() as conn:
            paid = list(conn.execute(select(attempts).where(attempts.c.job_id == first[1]["id"])).mappings())
        self.assertEqual(len(paid), 1)
        self.assertEqual(paid[0]["upstream_task_id"], "original-paid-task")

    def test_ttl_rollover_preserves_deferred_unsubmitted_attempt_history(self):
        scope, job = self.start_job()
        boot = next(iter(self.controller.current.boots.values()))
        claim = boot.control.claim(boot.worker, self.config.pool)
        TaskQueue(self.repo).defer_unsubmitted(claim.lease, retry_after_s=0)
        boot.control.observe(boot.worker, job["id"])
        self.shorten_current_ttl()
        for _ in range(12):
            self.tick()
        self.assertEqual(self.controller.sequence, 2)
        moved = self.repo.get_job(scope, job["id"])
        self.assertEqual(moved["current_attempt_id"], claim.lease.attempt_id)
        self.assertEqual(moved["attempt_no"], 1)
        self.assertEqual(moved["status"], "queued")
        for _ in range(3):
            self.tick()
        self.assertEqual(len(self.provider.creates), 2)
        with self.repo.engine.connect() as conn:
            history = list(conn.execute(select(attempts).where(attempts.c.job_id == job["id"])).mappings())
        self.assertEqual(len(history), 1)
        self.assertIsNone(history[0]["submission_started_at"])
        self.assertEqual(history[0]["status"], "deferred")

    def test_ttl_rollover_budget_exhaustion_cancels_pending_without_second_rental(self):
        self.repo.configure_budget("finite-budget", tenant_id="sixnine", limit_microusd=2_000_000)
        first = self.start_job()
        second_scope, second = self.submit("supervan", "budget-limited")
        self.finish(*first)
        self.shorten_current_ttl()
        for _ in range(13):
            value = self.tick()
        self.assertTrue(self.controller.stopping())
        self.assertTrue(value["drained"])
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(self.repo.get_job(second_scope, second["id"])["status"], "cancelled")
        self.assertEqual(self.repo.get_budget("job-budget")["reserved_microusd"], 0)

    def test_revoked_grant_is_service_stop_not_new_automatic_grant(self):
        job = self.start_job()
        self.repo.set_capacity_approval_enabled(self.controller.current.config.capacity_approval_id, enabled=False)
        value = self.tick()
        self.assertTrue(self.controller.stopping())
        self.assertEqual(self.controller.sequence, 1)
        self.assertEqual(len(self.provider.creates), 1)


if __name__ == "__main__":
    unittest.main()
