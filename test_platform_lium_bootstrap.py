"""CPU-only boot state machine tests; never SSH, rent, install, or generate."""
from dataclasses import replace
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

import test_platform_repository as ledger
from studio_platform.lium_bootstrap import BootConfig, BootController, BootError, COMFY_REVISION, MODEL_REVISION, main
from studio_platform.worker import Outcome


POD = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
GPU = "GPU-01234567-0123-0123-0123-012345678901"


class FakeHost:
    def __init__(self):
        self.starts = self.uploads = self.tunnels = 0
        self.identity = None
        self.state = "ready"
        self.start_error = False
        self.patch = {}
        self.manifest = json.loads((Path(__file__).parent/"model_manifest.json").read_text())

    def upload(self, files):
        self.uploads += 1
        assert set(files) == {"bootstrap_cloud.py", "model_manifest.json"}

    def start(self, identity):
        self.starts += 1
        self.identity = identity
        if self.start_error:
            raise OSError("lost response")

    def report(self):
        return {"identity": self.identity, "state": self.state, "phase": "download",
            "model_revision": MODEL_REVISION, "comfyui_revision": COMFY_REVISION,
            "actual_comfy_revision": COMFY_REVISION,
            "runtime": {"gpu_total_bytes": 96*1024**3},
            "gpus": [{"uuid": GPU, "memory_mib": 98304, "name": "test GPU"}],
            "files": {item["path"]: {"state": "verified_size", "revision": MODEL_REVISION,
                "size_bytes": item["size_bytes"]} for item in self.manifest["files"]}, **self.patch}

    def open_tunnel(self, port):
        self.tunnels += 1

    def close(self):
        pass


class FakeBackend:
    def __init__(self):
        self.submissions = self.reconciles = self.fetches = 0
        self.fail_submit = False
        self.queue = {"queue_running": [], "queue_pending": []}
        self.outcome = Outcome("running", "task-test")

    def _json(self, *args):
        return self.queue

    def submit(self, graph, tag):
        self.submissions += 1
        assert graph
        if self.fail_submit:
            raise OSError("unknown")
        return "task-test"

    def poll(self, *args):
        return self.outcome

    def reconcile(self, tag):
        self.reconciles += 1
        return self.outcome

    def fetch(self, job, tag, task, directory, heartbeat):
        self.fetches += 1
        return {"video": directory/"fake.mp4", "audio": directory/"fake.flac"}

    def close(self):
        pass


class FakeFleet:
    def __init__(self, config, repo, path):
        self.config, self.path = config, path
        self.starts = self.drains = self.stops = 0

    def start(self):
        self.starts += 1

    def tick(self):
        return {"running": 1}

    def drain(self):
        self.drains += 1

    def shutdown(self):
        self.stops += 1


class BootTests(ledger.LedgerCase):
    def setUp(self):
        super().setUp()
        root = Path(self.temp.name)
        self.config = BootConfig(root/"boot", Path(__file__).parent.resolve(), root/"key", root/"known_hosts",
            18881, "h3-live-test", enabled=True)
        self.repo.configure_pool("boot-test", max_instances=2, max_physical_gpus=2)
        self.intent = self.repo.reserve_instance_intent(self.scope, "boot-test", "bootstrap-test",
            physical_gpus=1, slots=1, reserved_cost_microusd=100_000, hard_deadline=self.now+10000,
            budget_account_ids=("owner-budget",), dry_run=False, provider="lium")
        self.repo.update_instance(self.intent["id"], "creating")
        self.repo.update_instance(self.intent["id"], "starting", provider_instance_id=POD)
        self.host, self.backend = FakeHost(), FakeBackend()
        self.provider_calls = []
        def coordinates(tag, instance):
            self.provider_calls.append((tag, instance))
            return {"host": "8.8.8.8", "port": 2022, "instance_id": POD}
        self.provider = SimpleNamespace(ssh_connection=coordinates)

    def controller(self, **changes):
        return BootController(self.repo, self.provider, replace(self.config, **changes),
            ssh_factory=lambda *a: self.host, backend_factory=lambda **k: self.backend,
            fleet_factory=FakeFleet, verify_smoke=lambda paths, request: {"offline_test": True})

    def tick(self, controller):
        return controller.tick(self.intent["id"])

    def test_disabled_has_no_files_provider_or_ledger_access(self):
        controller = self.controller(enabled=False)
        controller.repo = None
        self.assertEqual(controller.tick("not-an-id")["state"], "disabled")
        self.assertFalse(self.config.work_dir.exists())
        self.assertEqual(self.provider_calls, [])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main([]), 0)
        self.assertIn('"state": "disabled"', output.getvalue())

    def test_qualification_opt_in_and_pinned_boot_identity(self):
        controller = self.controller()
        result = self.tick(controller)
        self.assertEqual(result, {"state": "ready_for_qualification", "generation_verified": False})
        self.assertEqual((self.host.starts, self.host.uploads, self.backend.submissions), (1, 1, 0))
        self.tick(controller)
        self.assertEqual(self.host.starts, 1)
        self.assertEqual(self.provider_calls, [(self.intent["id"], POD)])
        self.assertIn("bootstrap_cloud.py", self.host.identity["sources"])

    def test_lost_boot_start_never_relaunches_after_restart(self):
        self.host.start_error = True
        self.assertEqual(self.tick(self.controller())["state"], "bootstrap_start_unknown")
        self.host.start_error = False
        self.assertEqual(self.tick(self.controller())["state"], "ready_for_qualification")
        self.assertEqual((self.host.starts, self.host.uploads), (1, 1))

    def test_unknown_remote_marker_does_not_restart(self):
        self.host.start_error = True
        self.tick(self.controller())
        self.host.identity = None
        self.assertEqual(self.tick(self.controller())["state"], "bootstrap_start_unknown")
        self.assertEqual(self.host.starts, 1)

    def test_bad_gpu_or_revision_never_smokes(self):
        for patch in ({"actual_comfy_revision": "0"*40}, {"gpus": []},
                      {"runtime": {"gpu_total_bytes": 32*1024**3}}, {"files": {}}):
            self.host.patch = patch
            with self.subTest(patch=patch), self.assertRaises(BootError):
                self.tick(self.controller(smoke_enabled=True))
        self.assertEqual(self.backend.submissions, 0)

    def test_config_port_change_fails_before_another_ssh_connection(self):
        self.tick(self.controller())
        with self.assertRaisesRegex(BootError, "receipt_identity_conflict"):
            self.tick(self.controller(local_port=18882))
        self.assertEqual(len(self.provider_calls), 1)

    def test_smoke_unknown_submission_reconciles_without_resubmit(self):
        self.backend.fail_submit = True
        self.assertEqual(self.tick(self.controller(smoke_enabled=True))["state"], "smoke_submission_unknown")
        self.backend.fail_submit = False
        self.assertEqual(self.tick(self.controller(smoke_enabled=True))["state"], "smoke_running")
        self.assertEqual((self.backend.submissions, self.backend.reconciles), (1, 1))
        self.backend.outcome = Outcome("succeeded", "task-test")
        self.assertEqual(self.tick(self.controller(smoke_enabled=True))["state"], "qualified")
        self.assertEqual(self.backend.submissions, 1)

    def test_smoke_busy_and_failed_not_qualified_or_retried(self):
        controller = self.controller(smoke_enabled=True, fleet_enabled=True)
        self.backend.queue["queue_running"] = ["foreign"]
        self.assertEqual(self.tick(controller)["state"], "qualification_upstream_busy")
        self.assertEqual(self.backend.submissions, 0)
        self.backend.queue["queue_running"] = []
        self.backend.outcome = Outcome("failed", "task-test")
        self.assertEqual(self.tick(controller)["state"], "qualification_failed")
        self.assertEqual(self.tick(controller)["state"], "qualification_failed")
        self.assertIsNone(controller.fleet)
        self.assertEqual(self.backend.submissions, 1)

    def test_media_verification_failure_never_registers_fleet(self):
        controller = self.controller(smoke_enabled=True, fleet_enabled=True)
        self.backend.outcome = Outcome("succeeded", "task-test")
        def reject(*args):
            raise BootError("qualification_bad_media")
        controller.verify_smoke = reject
        with self.assertRaisesRegex(BootError, "qualification_bad_media"):
            self.tick(controller)
        self.assertIsNone(controller.fleet)

    def test_fleet_after_qualification_once_and_restart_requires_recovery(self):
        controller = self.controller(smoke_enabled=True, fleet_enabled=True)
        self.assertEqual(self.tick(controller)["state"], "smoke_running")
        self.assertIsNone(controller.fleet)
        self.backend.outcome = Outcome("succeeded", "task-test")
        self.assertEqual(self.tick(controller)["state"], "fleet_running")
        self.assertEqual(self.tick(controller)["state"], "fleet_running")
        self.assertEqual(controller.fleet.starts, 1)
        self.assertEqual(controller.fleet.config.slots[0].spec.physical_gpu_ids, (GPU,))
        self.assertEqual(controller.fleet.config.slots[0].spec.recipe_ids, ("h3-base-fl2va-v1",))
        self.assertEqual(self.tick(self.controller(smoke_enabled=True, fleet_enabled=True))["state"], "fleet_recovery_required")
        self.assertEqual(self.backend.submissions, 1)

    def test_deadline_prevents_boot_and_draining_prevents_new_work(self):
        self.now += 9000
        self.assertEqual(self.tick(self.controller())["state"], "bootstrap_deadline_insufficient")
        self.assertEqual(self.host.starts, 0)
        self.repo.update_instance(self.intent["id"], "draining")
        self.assertEqual(self.tick(self.controller())["state"], "instance_not_admitting")
        self.assertEqual(self.provider_calls, [])

    def test_fleet_exited_child_is_not_reported_running(self):
        controller = self.controller(smoke_enabled=True, fleet_enabled=True)
        self.backend.outcome = Outcome("succeeded", "task-test")
        self.tick(controller)
        controller.fleet.tick = lambda: {"children": [{"state": "exited", "exit_code": 1}]}
        self.assertEqual(self.tick(controller)["state"], "fleet_attention_required")

    def test_idle_requires_exact_instance_and_continuous_empty_queue(self):
        controller = self.controller()
        self.tick(controller)
        with self.assertRaisesRegex(BootError, "not_bound"):
            controller.idle_probe(self.intent["id"], "other")
        first = controller.idle_probe(self.intent["id"], POD)
        self.now += 10
        second = controller.idle_probe(self.intent["id"], POD)
        self.assertEqual(first.idle_since, second.idle_since)
        self.backend.queue["queue_pending"] = ["job"]
        self.assertFalse(controller.idle_probe(self.intent["id"], POD).idle)
        self.backend.queue["queue_pending"] = []
        self.now += 10
        self.assertGreater(controller.idle_probe(self.intent["id"], POD).idle_since, first.idle_since)


if __name__ == "__main__":
    unittest.main()
