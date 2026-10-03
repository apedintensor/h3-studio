"""Synthetic policies and locally registered fake slots, never a GPU/provider call."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from studio_platform.api import create_app
from studio_platform.capabilities import MODEL, compile_request
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.execution_policy import ExecutionPolicies, read_policy
from studio_platform.repository import Repository, Scope, Conflict
from studio_platform.settings import Settings
from test_platform_api import project, generation_request


def policy(now):
    return {"id": "self-hosted-default", "revision": "synthetic-v1", "enabled": True,
        "model_id": MODEL, "backend": "comfy-worker", "pool": "synthetic-pool", "configuration_id": "synthetic-config",
        "recipe_ids": ["h3-base-fl2va-v1", "h3-base-ref2va-v1"],
        "qualification": {"status": "accepted", "evidence_id": "synthetic-test-only", "verified_at": now-1, "expires_at": now+3600},
        "envelope": {"max_pixels": 768*1344, "max_duration_seconds": 16, "max_steps": 50, "max_reference_files": 12,
            "max_guides": 8, "allow_first_last": True, "allow_audio": True,
            "controls": {"sampler_name": ["res_multistep"], "scheduler": ["auto"], "video_decode": ["normal", "tiled"],
                "audio_decode": ["normal"], "encoder_device": ["default", "cpu"], "ref_image_size": ["max", "match"]}},
        "reservation": {"cost_microusd": 500000, "expected_runtime_s": 300, "expires_at": now+3600, "source_id": "synthetic-quote"},
        "budget_accounts": ["test-tenant:{tenant_id}", "test-owner:{tenant_id}:{owner_id}"]}


class ExecutionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = Repository("sqlite:///" + (self.root / "test.sqlite3").as_posix())
        self.repo.create_schema()
        self.addCleanup(self.repo.close)
        self.path = self.root / "synthetic-policy.json"
        self.settings = Settings(self.root, auth_mode="local-test", execution_backend="comfy-worker",
            generation_enabled=True, execution_policy_file=self.path)
        self.scope = Scope("sixnine", "superdan", "story-one")
        self.compiled, self.fingerprint = compile_request(generation_request(), lambda _: None)
        self.value = policy(self.repo.clock())
        self.write()
        self.policies = ExecutionPolicies(self.settings, self.repo)
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        self.control = WorkerControl(self.repo)
        self.control.register(WorkerSpec("synthetic-worker", "synthetic-pool", "synthetic-provider", "synthetic-instance",
            ("synthetic-gpu",), ("h3-base-fl2va-v1", "h3-base-ref2va-v1"), MODEL, "synthetic-config"))
        self.control.mark_ready("synthetic-worker", upstream_idle_confirmed=True)
        self.repo.configure_budget("test-tenant:sixnine", tenant_id="sixnine", limit_microusd=1000000)
        self.repo.configure_budget("test-owner:sixnine:superdan", tenant_id="sixnine", owner_id="superdan", limit_microusd=1000000)

    def write(self):
        self.path.write_text(json.dumps(self.value), encoding="utf-8")
        self.path.chmod(0o600)

    def evaluate(self):
        return self.policies.evaluate(self.compiled, self.scope, self.fingerprint)

    def test_enabled_exact_policy_preserves_controls_and_separates_cost(self):
        result = self.evaluate()
        self.assertTrue(result.execution["enabled"])
        self.assertEqual(result.cost, 500000)
        self.assertEqual(result.estimate["kind"], "budget_reservation")
        self.assertFalse(result.estimate["actual_charge_known"])
        self.assertEqual(result.execution["configuration_id"], "synthetic-config")
        self.assertEqual(self.compiled["request"]["seed"], "18446744073709551615")
        self.assertEqual(self.compiled["request"]["model"], MODEL)

    def test_missing_invalid_or_disabled_policy_never_falls_back(self):
        self.path.unlink()
        self.assertFalse(self.evaluate().execution["enabled"])
        self.path.write_text('{"not-a-policy":"invalid"}', encoding="utf-8")
        self.assertFalse(self.evaluate().execution["enabled"])
        self.value["enabled"] = False
        self.write()
        self.assertFalse(self.evaluate().execution["enabled"])
        with self.assertRaises(ValueError):
            read_policy(Path("relative.json"))

    def test_qualification_expiry_budget_scope_and_envelope_block(self):
        original = copy.deepcopy(self.value)
        modifications = [
            ("qualification", "status", "unverified"),
            ("qualification", "expires_at", self.repo.clock()-0.1),
            ("reservation", "expires_at", self.repo.clock()-1),
            ("envelope", "max_duration_seconds", 2),
            ("envelope", "max_pixels", 65536),
            ("envelope", "max_steps", 1),
            ("envelope", "allow_audio", False),
        ]
        for section, key, val in modifications:
            self.value = copy.deepcopy(original)
            self.value[section][key] = val
            self.write()
            with self.subTest(section=section, key=key):
                self.assertFalse(self.evaluate().execution["enabled"])
        self.value = original
        self.value["budget_accounts"] = ["test-owner:sixnine:supervan"]
        self.repo.configure_budget("test-owner:sixnine:supervan", tenant_id="sixnine", owner_id="supervan", limit_microusd=1000000)
        self.write()
        self.assertFalse(self.evaluate().execution["enabled"])

    def test_healthy_busy_slot_accepts_queue_but_unknown_does_not(self):
        admission = self.evaluate()
        plan = self.repo.create_plan(self.scope, self.compiled, admission.execution,
            expires_at=admission.expires_at, estimated_cost_microusd=admission.cost)
        self.repo.create_job(self.scope, plan["id"], "one", budget_account_ids=admission.execution["budget_account_ids"])
        claim = self.control.claim("synthetic-worker", "synthetic-pool")
        self.assertIsNotNone(claim)
        self.assertTrue(self.evaluate().execution["enabled"])
        from sqlalchemy import update
        from studio_platform.repository import registered_workers
        with self.repo.transaction() as conn:
            conn.execute(update(registered_workers).values(expires_at=self.repo.clock()-1))
        self.assertFalse(self.evaluate().execution["enabled"])

    def test_changed_policy_needs_new_consent_but_identical_plan_is_current(self):
        result = self.evaluate()
        plan = self.repo.create_plan(self.scope, self.compiled, result.execution,
            expires_at=result.expires_at, estimated_cost_microusd=result.cost)
        self.assertEqual(len(self.policies.ensure_current(plan, self.scope)), 2)
        self.value["reservation"]["cost_microusd"] = 600000
        self.write()
        with self.assertRaises(Conflict):
            self.policies.ensure_current(plan, self.scope)

    def test_submission_guard_does_not_reserve_twice_and_honors_revocation(self):
        admission = self.evaluate()
        plan = self.repo.create_plan(self.scope, self.compiled, admission.execution,
            expires_at=admission.expires_at, estimated_cost_microusd=admission.cost)
        job = self.repo.create_job(self.scope, plan["id"], "one", budget_account_ids=admission.execution["budget_account_ids"])
        self.repo.configure_budget("test-tenant:sixnine", tenant_id="sixnine", limit_microusd=500000)
        self.assertTrue(self.policies.submission_allowed(job))
        self.assertEqual(self.repo.get_budget("test-tenant:sixnine")["reserved_microusd"], 500000)
        self.value["enabled"] = False
        self.write()
        self.assertFalse(self.policies.submission_allowed(job))

    def test_real_api_reserves_budgets_and_idempotent_retry_survives_revocation(self):
        app = create_app(self.settings, repository=self.repo)
        with TestClient(app) as client:
            client.post("/api/auth/login", json={"username": "superdan"})
            self.assertEqual(client.post("/v1/projects", json={"project": project()}).status_code, 201)
            plan = client.post("/v1/generation-plans", json=generation_request()).json()
            self.assertEqual(plan["status"], "ready")
            response = client.post("/v1/jobs", json={"plan_id": plan["plan_id"]}, headers={"Idempotency-Key": "one"})
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json()["status"], "queued")
            self.assertFalse(response.json()["simulation"])
            for account_id in ("test-tenant:sixnine", "test-owner:sixnine:superdan"):
                self.assertEqual(self.repo.get_budget(account_id)["reserved_microusd"], 500000)
            self.value["enabled"] = False
            self.write()
            retry = client.post("/v1/jobs", json={"plan_id": plan["plan_id"]}, headers={"Idempotency-Key": "one"})
            self.assertEqual(retry.status_code, 202)
            self.assertEqual(retry.json()["id"], response.json()["id"])
            self.assertEqual(client.post("/v1/jobs", json={"plan_id": plan["plan_id"]},
                headers={"Idempotency-Key": "new-attempt"}).status_code, 409)

    def test_batch_admission_reserves_same_budgets_only_once(self):
        app = create_app(self.settings, repository=self.repo)
        with TestClient(app) as client:
            client.post("/api/auth/login", json={"username": "superdan"})
            client.post("/v1/projects", json={"project": project()})
            ids = [client.post("/v1/generation-plans", json=generation_request()).json()["plan_id"] for _ in range(3)]
            body = {"client_project_id": "story-one", "plan_ids": ids}
            response = client.post("/v1/batches", json=body, headers={"Idempotency-Key": "batch"})
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json()["counts"], {"queued": 2, "rejected": 1})
            retry = client.post("/v1/batches", json=body, headers={"Idempotency-Key": "batch"})
            self.assertEqual(retry.status_code, 202, retry.text)
            self.assertEqual(retry.json()["id"], response.json()["id"])
            self.assertEqual(self.repo.get_budget("test-tenant:sixnine")["reserved_microusd"], 1000000)

    def test_config_schema_cannot_broaden_or_select_unknown_fields(self):
        for field in ("backend", "model_id", "configuration_id"):
            previous = self.value[field]
            self.value[field] = "" if field == "configuration_id" else "unknown"
            self.write()
            with self.assertRaises(ValueError):
                read_policy(self.path)
            self.value[field] = previous
        self.value["budget_accounts"] = ["{owner_id.__class__}"]
        self.write()
        with self.assertRaises(ValueError):
            read_policy(self.path)


if __name__ == "__main__":
    unittest.main()
