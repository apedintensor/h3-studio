"""Fresh worker rows cannot override managed provider or execution windows."""
from sqlalchemy import select, update

from studio_platform.control import WorkerControl
from studio_platform.generation_availability import _mode_result
from studio_platform.operator_capacity import operator_nodes
from studio_platform.repository import instance_intents, registered_workers, jobs, budget_reservations
from test_operator_capacity import OperatorCase


class ManagedAdmissionTests(OperatorCase):
    def prepare(self, *, duration=120, profile=None):
        self.create()
        self.controller.tick()
        self.control = WorkerControl(self.repo)
        with self.repo.engine.connect() as conn:
            self.worker = dict(conn.execute(select(registered_workers)).mappings().one())
            self.intent = dict(conn.execute(select(instance_intents)).mappings().one())
        self.profile = profile or self.binding.runtime_profile_id
        plan = self.repo.create_plan(self.scope,
            {"recipe_id": self.binding.recipe_ids[0], "deployment_profile_id": self.profile,
             "request": {"model": self.binding.model_id}},
            {"pool": self.binding.pool, "backend": "wangp-worker", "enabled": True,
             "configuration_id": self.binding.configuration_id, "engine_manifest_digest": self.binding.engine_manifest_digest,
             "expected_runtime_s": duration}, expires_at=self.now+9000, estimated_cost_microusd=100)
        self.job = self.repo.create_job(self.scope, plan["id"], "only-original-job", budget_account_ids=["owner-budget"])
        self.duration = duration

    def counts(self):
        return self.control.pool_status(self.binding.pool, model_id=self.binding.model_id,
            configuration_id=self.binding.configuration_id, recipe_id=self.binding.recipe_ids[0],
            backend="wangp-worker", engine_manifest_digest=self.binding.engine_manifest_digest,
            expected_runtime_s=self.duration, deployment_profile_id=self.profile)

    def availability(self):
        from types import SimpleNamespace
        policy = {"deployment_profile_id": self.profile, "recipe_ids": list(self.binding.recipe_ids),
            "pool": self.binding.pool, "model_id": self.binding.model_id, "configuration_id": self.binding.configuration_id,
            "engine_manifest_digest": self.binding.engine_manifest_digest, "backend": "wangp-worker", "enabled": True,
            "qualification": {"status": "accepted", "verified_at": 0, "expires_at": 10000},
            "reservation": {"expected_runtime_s": self.duration, "expires_at": 10000}}
        return _mode_result(SimpleNamespace(generation_enabled=True, execution_backend="wangp-worker"),
            self.repo, self.registry, policy, True, "fl", self.binding.recipe_ids[0], self.now)

    def assert_blocked(self, reason):
        before = self.repo.get_job(self.scope, self.job["id"])
        budget = self.repo.get_budget("owner-budget")
        self.assertEqual(self.counts()["ready"], 0)
        self.assertIn(reason, self.counts()["reason_counts"])
        self.assertEqual(self.availability()["reason_code"], reason)
        self.assertFalse(self.availability()["available"])
        self.assertIsNone(self.control.claim(self.worker["id"], self.binding.pool))
        self.assertEqual(self.control.queued_diagnostic(before)["reason_code"], reason)
        self.assertEqual(self.repo.get_job(self.scope, self.job["id"]), before)
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)
        self.assertEqual(len(self.provider.creates), 1)

    def test_stale_provider_proof_is_not_ready_despite_fresh_worker(self):
        self.prepare()
        self.now += 31
        self.assert_blocked("managed_provider_lifetime_unverified")

    def test_stop_new_margin_matches_readiness_and_claim(self):
        self.prepare(duration=60)
        with self.repo.transaction() as conn:
            conn.execute(update(instance_intents).values(hard_deadline=self.now+299))
        self.assert_blocked("worker_window_closing")

    def test_long_job_does_not_fit_a_ready_machine(self):
        self.prepare(duration=3500)
        self.assert_blocked("job_exceeds_worker_window")

    def test_profile_identity_cannot_use_another_bound_mode(self):
        self.prepare(profile="different-profile")
        self.assert_blocked("matching_profile_unavailable")

    def test_managed_node_cannot_advertise_legacy_unspecified_profile(self):
        self.prepare()
        self.profile = None
        with self.repo.transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id == self.job["id"]).values(
                request={k: v for k, v in self.job["request"].items() if k != "deployment_profile_id"}))
        self.assert_blocked("matching_profile_unavailable")

    def test_provider_execution_unconfirmed_does_not_use_old_ready_slot(self):
        self.prepare()
        with self.repo.transaction() as conn:
            conn.execute(update(operator_nodes).values(runtime_state="provider_execution_unverified"))
        self.assert_blocked("managed_runtime_unavailable")

    def test_operator_summary_counts_claimable_slots_and_history_preserves_records(self):
        self.prepare()
        self.assertEqual(self.service.state(self.actor)["summary"]["slots_ready"], 1)
        node = self.service.state(self.actor)["nodes"][0]
        self.assertEqual(node["record_group"], "current")
        self.service.node_command(self.actor, node["id"], {"expected_version": node["version"]}, "stop-summary", "stop")
        stopped = self.service.state(self.actor)
        self.assertEqual(stopped["summary"]["slots_ready"], 0)
        self.assertEqual(stopped["nodes"][0]["record_group"], "current")
        self.assertEqual(self.repo.get_job(self.scope, self.job["id"])["status"], "queued")
        with self.repo.transaction() as conn:
            conn.execute(update(instance_intents).values(state="destroyed"))
        archived = self.service.state(self.actor)
        self.assertEqual(archived["nodes"][0]["record_group"], "history")
        self.assertEqual(archived["summary"]["slots_ready"], 0)
        self.assertEqual(self.repo.get_job(self.scope, self.job["id"])["status"], "queued")
        with self.repo.engine.connect() as conn:
            self.assertTrue(list(conn.execute(select(budget_reservations)).mappings()))

    def test_late_guard_rechecks_proof_and_preserves_collection(self):
        self.prepare()
        self.assertEqual(self.availability()["state"], "ready")
        self.assertEqual(self.control.queued_diagnostic(self.job)["reason_code"], "awaiting_worker_claim")
        claim = self.control.claim(self.worker["id"], self.binding.pool)
        self.assertIsNotNone(claim)
        self.assertTrue(self.control.submission_allowed(claim.job))
        self.now += 31
        self.assertFalse(self.control.submission_allowed(claim.job))
        with self.repo.transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id == self.job["id"]).values(status="collecting", lease_worker_id=None, lease_expires_at=None))
        collection = self.control.claim(self.worker["id"], self.binding.pool, purpose="collect")
        self.assertIsNotNone(collection)
        self.assertEqual(collection.job["id"], self.job["id"])
        self.assertEqual(len(self.provider.creates), 1)
