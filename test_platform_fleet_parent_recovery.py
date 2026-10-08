"""Same-host parent reconstruction; fake GPU/SSH and real isolated ledger only."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid

from studio_platform.control import WorkerControl
from studio_platform.fleet import run_slot
from studio_platform.fleet_process import owned_process, ObservedProcess, PROTOCOL
from studio_platform.production_scaler_boot import ProductionBoot, run_child
from studio_platform.repository import jobs
from studio_platform.settings import Settings
from studio_platform.worker import Outcome, WorkerRunner
from studio_platform.queued_task_runner import QueuedTaskRunner
import test_platform_production_scaler as fixture
import test_platform_queued_task_runner as queued
import test_platform_repository as ledger


class ParentRecoveryTests(ledger.LedgerCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        self.config = fixture.configuration(self.root, self.now)
        self.config.work_dir.mkdir()
        self.repo.configure_pool(self.config.pool, max_instances=2, max_physical_gpus=2)
        intent = self.repo.reserve_instance_intent(self.scope, self.config.pool, "boot", physical_gpus=1,
            slots=1, reserved_cost_microusd=2_000_000, hard_deadline=self.now+7200,
            budget_account_ids=["owner-budget"], dry_run=False, provider="lium")
        self.repo.update_instance(intent["id"], "creating")
        self.repo.update_instance(intent["id"], "starting", provider_instance_id=str(uuid.uuid4()))
        self.intent = self.repo.list_instance_intents()[0]
        self.host, self.backend = fixture.FakeHost(), fixture.FakeBackend()
        self.backend.outcome = Outcome("succeeded", "synthetic-qualified-task")
        self.provider = fixture.FakeProvider(lambda: self.now)
        self.provider.ssh_connection = lambda *args: {"host": "203.0.113.1", "port": 22}
        self.worker = "lium-"+intent["id"].replace("-", "")
        self.control = WorkerControl(self.repo)
        self.process = SimpleNamespace(pid=4242, poll=lambda: None, send_signal=lambda _: None)
        self.boot = self.new_boot()
        with patch.object(self.boot, "_popen_impl", return_value=self.process):
            self.assertEqual(self.boot.tick(intent["id"])["state"], "fleet_running")
        self.fleet = self.boot.fleet.config
        self.token = self.boot.fleet.process_tokens[self.worker]
        self.receipt = self.boot.config.work_dir/intent["id"]/"bootstrap-state.json"
        self.counts = (self.host.starts, self.host.uploads, self.backend.submissions)

    def new_boot(self, config=None):
        return ProductionBoot(self.repo, self.provider, config or self.config, self.intent, 19300,
            config_path=self.root/"config.json", ssh_factory=lambda *a: self.host,
            backend_factory=lambda **kw: self.backend, verify_smoke=lambda paths, request: {"request": request})

    def assert_no_upstream_repeat(self):
        self.assertEqual((self.host.starts, self.host.uploads, self.backend.submissions), self.counts)
        self.assertEqual((self.provider.creates, self.provider.destroys), ([], []))

    def test_running_original_child_is_observed_without_launch_or_pid_signals(self):
        before = self.control.get(self.worker)
        with owned_process(self.fleet, self.worker, self.token):
            recovered = self.new_boot()
            with patch.object(recovered, "_popen_impl", side_effect=AssertionError("No second child")):
                state = recovered.tick(self.intent["id"])
                self.assertEqual(state["state"], "fleet_running")
                self.assertTrue(state["recovered_original_fleet"])
                self.assertIsInstance(recovered.fleet.children[self.worker], ObservedProcess)
                self.assertIsNone(recovered.fleet.children[self.worker].pid)
                self.assertFalse(recovered.children_done())
            self.assertEqual(self.control.get(self.worker), before)
        self.assertTrue(recovered.children_done())
        self.assert_no_upstream_repeat()

    def test_exited_owner_launches_one_fenced_cpu_recovery_not_another_boot(self):
        with owned_process(self.fleet, self.worker, self.token):
            pass
        recovered = self.new_boot()
        with patch.object(recovered, "_popen_impl", return_value=self.process) as popen:
            self.assertEqual(recovered.tick(self.intent["id"])["state"], "fleet_running")
            recovered.tick(self.intent["id"])
        self.assertEqual(popen.call_count, 1)
        argv = popen.call_args.args[0]
        self.assertIn("--recover-slot", argv)
        self.assertNotEqual(argv[argv.index("--owner-token")+1], self.token)
        with self.assertRaisesRegex(ValueError, "superseded"):
            with owned_process(self.fleet, self.worker, self.token):
                self.fail("Old launch replayed")
        self.assert_no_upstream_repeat()

    def test_legacy_or_missing_receipt_evidence_stays_held(self):
        state = json.loads(self.receipt.read_text())
        self.receipt.write_text(json.dumps({k: v for k, v in state.items() if k != "fleet_process_protocol"}))
        recovered = self.new_boot()
        with patch.object(recovered, "_popen_impl", side_effect=AssertionError):
            self.assertEqual(recovered.tick(self.intent["id"])["state"], "fleet_recovery_required")
            self.assertFalse(recovered.children_done())
        self.receipt.write_text(json.dumps(state))
        (self.fleet.work_dir/self.worker/"process-owner.json").unlink()
        recovered = self.new_boot()
        with patch.object(recovered, "_popen_impl", side_effect=AssertionError), self.assertRaises(FileNotFoundError):
            recovered.tick(self.intent["id"])
        self.assertFalse(recovered.children_done())
        with self.assertRaisesRegex(ValueError, "recovery_incomplete"):
            recovered.fleet.tick()
        self.assert_no_upstream_repeat()

    def test_operator_identity_and_physical_binding_changes_never_resume(self):
        state = json.loads(self.receipt.read_text())
        variants = [dict(state, fleet_controller_hash="f"*64), dict(state, fleet_config_hash="f"*64)]
        for value in variants:
            self.receipt.write_text(json.dumps(value))
            recovered = self.new_boot()
            with patch.object(recovered, "_popen_impl", side_effect=AssertionError), self.assertRaisesRegex(Exception, "identity_conflict"):
                recovered.tick(self.intent["id"])
        self.receipt.write_text(json.dumps(state))
        self.host.patch["gpus"] = [{**state["hardware"]["gpu"], "uuid": "other-gpu"}]
        recovered = self.new_boot()
        with patch.object(recovered, "_popen_impl", side_effect=AssertionError), self.assertRaisesRegex(Exception, "identity"):
            recovered.tick(self.intent["id"])
        self.assert_no_upstream_repeat()

    def test_new_protocol_child_cannot_bypass_lifetime_token(self):
        with patch("studio_platform.production_scaler_boot.Repository", side_effect=AssertionError("No database entry")):
            with self.assertRaisesRegex(ValueError, "token_required"):
                run_child(self.config, self.intent["id"], self.fleet.fingerprint(), Settings(self.config.data_dir))
            with self.assertRaisesRegex(Exception, "binding_changed"):
                run_child(replace(self.config, allowed_owners=["superdan", "supervan"]), self.intent["id"],
                    self.fleet.fingerprint(), Settings(self.config.data_dir), owner_token=self.token)

    def test_drain_restart_observes_original_process_then_requires_positive_exit(self):
        self.control.mark_ready(self.worker, upstream_idle_confirmed=True)
        with owned_process(self.fleet, self.worker, self.token):
            recovered = self.new_boot()
            with patch.object(recovered, "_popen_impl", side_effect=AssertionError):
                result = recovered.tick(self.intent["id"], stopping=True)
            self.assertEqual((result["state"], result["children_done"]), ("draining", False))
            self.assertTrue((self.fleet.work_dir/self.worker/"drain.flag").exists())
            self.assertEqual(self.control.get(self.worker)["drain_requested"], 1)
        self.assertTrue(recovered.children_done())
        result = recovered.tick(self.intent["id"], stopping=True)
        self.assertTrue(result["children_done"])
        self.assertEqual(self.control.get(self.worker)["state"], "retired")
        self.assert_no_upstream_repeat()

    def job(self):
        spec = self.fleet.slot(self.worker).spec
        request = {"recipe_id": spec.recipe_ids[0], "request": {"model": spec.model_id,
            "prompt": "synthetic-only", "duration": 5, "generate_audio": False}}
        plan = self.repo.create_plan(self.scope, request, {"pool": spec.pool, "backend": spec.backend,
            "enabled": True, "configuration_id": spec.configuration_id, "expected_runtime_s": 10},
            expires_at=self.now+3600, estimated_cost_microusd=100_000)
        job = self.repo.create_job(self.scope, plan["id"], uuid.uuid4().hex, budget_account_ids=("owner-budget",))
        self.now += .1  # Establish queue order independently of random UUIDs.
        return job

    def test_unknown_attempt_resumes_collection_under_revoked_new_admission_once(self):
        self.control.mark_ready(self.worker, upstream_idle_confirmed=True)
        first, waiting = self.job(), self.job()
        fake = queued.Backend()
        fake.uncertain = True
        runner = QueuedTaskRunner(self.repo, None, self.fleet.work_dir/self.worker,
            backend=fake, control=self.control, retry_after_s=0, submission_guard=lambda _: True,
            stop_new=lambda: False, job_allowed=lambda _: True, collection_lock_dir=self.config.work_dir/"collection-lock",
            qualification_evidence_file=self.root/"synthetic-job-evidence.json",
            evidence_identity={**json.loads(self.receipt.read_text())["identity"], "qualification_profile": queued.QUEUED_TASK_PROFILE})
        with owned_process(self.fleet, self.worker, self.token):
            first_run = runner.run_once(self.worker, self.config.pool)
            self.assertEqual((first_run["state"], first_run["job_id"]), ("submission_unknown", first["id"]))
        bound = self.repo.get_job(self.scope, first["id"])
        reserved = self.repo.get_budget("owner-budget")["reserved_microusd"]
        recovered = self.new_boot()
        with patch.object(recovered, "_popen_impl", return_value=self.process):
            recovered.tick(self.intent["id"])
        fake.uncertain, fake.state = False, "succeeded"
        self.now += 31
        def injected_slot(fleet, worker_id, settings, **kw):
            self.assertTrue(fleet.slot(worker_id).recovery_only)
            return run_slot(fleet, worker_id, settings, **kw, backend_factory=lambda *_: fake,
                store_factory=lambda _: None, once=True)
        with patch("studio_platform.production_scaler_boot.Repository", return_value=self.repo), \
                patch.object(self.repo, "close"), \
                patch("studio_platform.production_scaler_boot.run_slot", side_effect=injected_slot), \
                patch("studio_platform.production_scaler.verify_policy", side_effect=ValueError("revoked")), \
                patch.object(WorkerRunner, "_collect", queued.verified_collect):
            result = run_child(self.config, self.intent["id"], self.fleet.fingerprint(),
                Settings(self.config.data_dir, generation_enabled=True, execution_backend="comfy-worker"),
                owner_token=recovered.fleet.process_tokens[self.worker], recover_slot=True)
        current = self.repo.get_job(self.scope, first["id"])
        self.assertEqual((result["state"], current["status"], fake.submits), ("succeeded", "succeeded", 1))
        self.assertEqual((current["id"], current["request_hash"], current["current_attempt_id"], current["attempt_no"]),
            (bound["id"], bound["request_hash"], bound["current_attempt_id"], 1))
        self.assertEqual(self.repo.get_job(self.scope, waiting["id"])["attempt_no"], 0)
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], reserved)
        self.assert_no_upstream_repeat()

    def test_expired_or_drained_owner_without_obligation_exits_without_backend_or_claim(self):
        with owned_process(self.fleet, self.worker, self.token):
            pass
        recovered = self.new_boot()
        with patch.object(recovered, "_popen_impl", return_value=self.process):
            recovered.tick(self.intent["id"])
        self.control.drain(self.worker)
        with patch("studio_platform.production_scaler_boot.Repository", return_value=self.repo), \
                patch.object(self.repo, "close"), \
                patch("studio_platform.production_scaler_boot.run_slot", side_effect=AssertionError("No new runner")):
            self.assertEqual(run_child(self.config, self.intent["id"], self.fleet.fingerprint(),
                Settings(self.config.data_dir), owner_token=recovered.fleet.process_tokens[self.worker], recover_slot=True), 0)
        self.assertEqual(self.control.get(self.worker)["drain_requested"], 1)
        self.assert_no_upstream_repeat()

    def test_long_outage_retirement_needs_owned_exit_and_fresh_idle_not_expired_heartbeat(self):
        self.control.mark_ready(self.worker, upstream_idle_confirmed=True)
        original_expiry = self.control.get(self.worker)["expires_at"]
        with owned_process(self.fleet, self.worker, self.token):
            recovered = self.new_boot()
            self.now = original_expiry+1
            with patch.object(recovered, "_popen_impl", side_effect=AssertionError):
                recovered.tick(self.intent["id"], stopping=True)
            self.assertNotEqual(self.control.get(self.worker)["state"], "retired")
        self.backend.queue = {"queue_running": ["synthetic-still-busy"], "queue_pending": []}
        recovered.tick(self.intent["id"], stopping=True)
        self.assertNotEqual(self.control.get(self.worker)["state"], "retired")
        self.backend.queue = {"queue_running": [], "queue_pending": []}
        recovered.tick(self.intent["id"], stopping=True)
        worker = self.control.get(self.worker)
        self.assertEqual((worker["state"], worker["expires_at"]), ("retired", original_expiry))
        self.assert_no_upstream_repeat()
