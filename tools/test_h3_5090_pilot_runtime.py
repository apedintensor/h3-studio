"""Offline tests for no-replay, identity, and INT8 pilot boundaries."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("pilot", Path(__file__).with_name("h3_5090_pilot_runtime.py"))
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)


class FakeCUDA:
    def __getattr__(self, name):
        return lambda *args: 0


class FakeSession:
    def __init__(self, receipt=None, fail=False):
        self.calls = []
        self.closes = 0
        self.receipt = receipt
        self.fail = fail

    def get_default_settings(self, model):
        return {"stale_default": True}

    def submit_task(self, settings, callbacks=None):
        if self.receipt:
            assert json.loads(self.receipt.read_text())["state"] == "dispatch_intent"
        self.calls.append(copy.deepcopy(settings))
        if self.fail:
            raise ConnectionError("ambiguous_after_submit")
        return types.SimpleNamespace(done=True, result=lambda timeout=0: None)

    def close(self):
        self.closes += 1


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.outputs = self.root / "outputs"
        self.outputs.mkdir()
        self.torch = types.SimpleNamespace(cuda=FakeCUDA())
        self.task = {"id": "fl20", "mode": "fl", "steps": 20, "seed": 42,
                     "prompt": "synthetic scene", "resolution": "832x480", "frames": 124}

    def tearDown(self):
        self.temp.cleanup()

    def collect(self, result, output, task):
        path = output / (task["id"] + ".mp4")
        path.write_bytes(b"fake artifact")
        return [{"path": str(path), "sha256": pilot.sha256(path), "size_bytes": path.stat().st_size}], {}

    def execute(self, session, tasks):
        pilot.run_tasks(session, tasks, self.root, self.outputs, self.torch,
                        time.time() + 3600, collect=self.collect)

    def test_dispatch_is_durable_before_external_call_and_ambiguity_cannot_replay(self):
        receipt = self.root / "receipts" / "fl20.json"
        session = FakeSession(receipt, fail=True)
        with self.assertRaises(ConnectionError):
            self.execute(session, [self.task])
        self.assertEqual(json.loads(receipt.read_text())["state"], "reconcile_required")
        with self.assertRaisesRegex(ValueError, "reconciliation"):
            self.execute(session, [self.task])
        self.assertEqual(len(session.calls), 1)

    def test_complete_resume_rehashes_and_never_submits_again(self):
        session = FakeSession()
        self.execute(session, [self.task])
        self.execute(session, [self.task])
        self.assertEqual(len(session.calls), 1)
        (self.outputs / "fl20.mp4").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "output_changed"):
            self.execute(session, [self.task])
        self.assertEqual(len(session.calls), 1)

    def test_changed_prompt_is_not_a_resume(self):
        session = FakeSession()
        self.execute(session, [self.task])
        changed = {**self.task, "prompt": "different"}
        with self.assertRaisesRegex(ValueError, "identity_changed"):
            self.execute(session, [changed])
        self.assertEqual(len(session.calls), 1)

    def test_fl_to_ref_is_sequential_and_releases_previous_pipeline(self):
        image = self.root / "ref.png"
        image.write_bytes(b"synthetic fixture")
        ref = {**self.task, "id": "ref50", "mode": "ref", "steps": 50,
               "inputs": {"image": {"path": str(image), "sha256": pilot.sha256(image)}}}
        session = FakeSession()
        self.execute(session, [self.task, ref])
        self.assertEqual(session.closes, 1)
        self.assertEqual([s["num_inference_steps"] for s in session.calls], [20, 50])
        self.assertEqual([s["model_type"] for s in session.calls],
                         ["minimax_h3_fl2va_pruned", "minimax_h3_ref2va_pruned"])
        self.assertTrue(all(s["config"] == "int8,int8_convrot,lower_ram" for s in session.calls))
        self.assertEqual(session.calls[1]["video_prompt_type"], "I")

    def test_unknown_input_role_and_audio_without_visual_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported_input_roles"):
            pilot.settings_for({**self.task, "inputs": {"unimplemented_control": {}}})
        with self.assertRaisesRegex(ValueError, "visual_required"):
            pilot.settings_for({**self.task, "mode": "ref", "inputs": {"audio": {}}})

    def test_nonbaseline_steps_not_silently_rewritten(self):
        with self.assertRaisesRegex(ValueError, "20_or_50"):
            pilot.settings_for({**self.task, "steps": 8})

    def test_expired_authorization_prevents_dispatch(self):
        session = FakeSession()
        result = pilot.run_tasks(session, [self.task], self.root, self.outputs, self.torch, time.time()-1)
        self.assertEqual(result["state"], "deferred_unstarted")
        self.assertFalse(session.calls)
        self.assertFalse((self.root / "receipts" / "fl20.json").exists())

    def test_deadline_gate_rechecks_after_defaults_without_submission_receipt(self):
        session = FakeSession()
        clock = [100.0]
        def slow_defaults(model):
            clock[0] = 171.0
            return {}
        session.get_default_settings = slow_defaults
        with patch.object(pilot.time, "time", side_effect=lambda: clock[0]):
            result = pilot.run_tasks(session, [self.task], self.root, self.outputs, self.torch, 1100)
        self.assertEqual(result["state"], "deferred_unstarted")
        self.assertEqual(result["required_seconds"], 930)
        self.assertFalse(session.calls)
        self.assertFalse((self.root / "receipts" / "fl20.json").exists())
        self.assertTrue((self.root / "deferrals" / "fl20.json").exists())
        self.execute(session, [self.task])
        self.assertEqual(len(session.calls), 1)

    def test_completed_collection_finishes_before_next_task_is_deferred(self):
        session = FakeSession()
        clock = [100.0]
        def slow_collect(result, output, task):
            clock[0] = 2000.0
            return self.collect(result, output, task)
        with patch.object(pilot.time, "time", side_effect=lambda: clock[0]):
            outcome = pilot.run_tasks(session, [self.task, {**self.task, "id": "later"}],
                self.root, self.outputs, self.torch, 1100, collect=slow_collect)
        self.assertEqual(json.loads((self.root / "receipts" / "fl20.json").read_text())["state"], "complete")
        self.assertEqual(outcome["task_id"], "later")
        self.assertEqual(outcome["state"], "deferred_unstarted")
        self.assertEqual(len(session.calls), 1)

    def test_output_probe_checks_fps_audio_and_finite_durations(self):
        probe = {"streams": [
            {"codec_type": "video", "width": 832, "height": 480, "nb_frames": "124",
             "avg_frame_rate": "24/1", "r_frame_rate": "24/1", "duration": "5.166667"},
            {"codec_type": "audio", "sample_rate": "32000", "channels": 2, "duration": "5.184"}],
            "format": {"duration": "5.184"}}
        pilot.validate_output_probe(probe, self.task)
        for index, key, value in ((0, "avg_frame_rate", "25/1"), (0, "r_frame_rate", "0/0"),
                                  (0, "duration", "nan"), (1, "duration", "1.0"),
                                  (1, "channels", 1), (1, "sample_rate", "48000")):
            invalid = copy.deepcopy(probe)
            invalid["streams"][index][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                pilot.validate_output_probe(invalid, self.task)
        for duration in (float("inf"), float("nan"), 0, 5.3):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                pilot.check_output_duration(duration, self.task, "independent_audio_samples")
        pilot.check_output_duration(165333 / 32000, self.task, "independent_audio_samples")

    def test_asset_checks_only_pending_modes_but_always_hashes_shared_components(self):
        assets = []
        for name in ("fl.bin", "ref.bin", "shared.bin"):
            path = self.root / name
            path.write_bytes(name.encode())
            assets.append((name, path.stat().st_size, pilot.sha256(path)))
        (self.root / "ref.bin").unlink()
        with patch.object(pilot, "ASSETS", assets):
            self.assertEqual(pilot.verify_assets(self.root, modes={"fl"}), ["fl.bin", "shared.bin"])
            with self.assertRaisesRegex(ValueError, "missing_or_wrong_size_asset:ref.bin"):
                pilot.verify_assets(self.root, modes={"fl", "ref"})
            (self.root / "shared.bin").write_bytes(b"changed!!!")
            with self.assertRaisesRegex(ValueError, "asset_hash_mismatch:shared.bin"):
                pilot.verify_assets(self.root, modes={"fl"})

    def test_environment_prioritizes_reviewed_vendored_mmgp_and_rejects_collision(self):
        def import_probe(name):
            self.assertEqual(pilot.sys.path[0], str(self.root.resolve()))
            if name == "mmgp":
                return types.SimpleNamespace(__file__=str(self.root / "mmgp" / "__init__.py"))
            raise RuntimeError("remaining_dependency_probes_reached")
        with patch.object(pilot, "assert_no_ui"), patch.object(pilot, "source_identity"), \
             patch.object(pilot.sys, "path", list(pilot.sys.path)), \
             patch.object(pilot.importlib.metadata, "distributions", return_value=[]), \
             patch.object(pilot.importlib, "import_module", side_effect=import_probe):
            with self.assertRaisesRegex(RuntimeError, "remaining_dependency_probes_reached"):
                pilot.environment(self.root)
        with patch.object(pilot, "assert_no_ui"), patch.object(pilot, "source_identity"), \
             patch.object(pilot.sys, "path", list(pilot.sys.path)), \
             patch.object(pilot.importlib, "import_module", return_value=types.SimpleNamespace(
                 __file__=str(self.root.parent / "unreviewed" / "mmgp.py"))):
            with self.assertRaisesRegex(ValueError, "vendored_mmgp_import_collision"):
                pilot.environment(self.root)

    def test_upstream_config_defaults_are_reset_but_authored_changes_refuse(self):
        path = self.root / "wgp_config.json"
        requested = pilot.runtime_config(self.root)
        expanded = {**requested, "last_model_type": "minimax_h3_fl2va_pruned",
                    "last_resolution_choice": "832x480", "audio_profile": 3.5}
        pilot.write_json(path, expanded)
        result = pilot.prepare_runtime_config(path, requested)
        self.assertEqual(json.loads(path.read_text()), requested)
        self.assertEqual(result["upstream_added_keys_reset"],
                         ["audio_profile", "last_model_type", "last_resolution_choice"])
        for change in ({**expanded, "transformer_quantization": "bf16"},
                       {key: value for key, value in expanded.items() if key != "int8_kernels"},
                       {**expanded, "save_queue_if_crash": False}):
            pilot.write_json(path, change)
            with self.assertRaisesRegex(ValueError, "pilot_runtime_config_changed"):
                pilot.prepare_runtime_config(path, requested)
            self.assertEqual(json.loads(path.read_text()), change)

    def test_active_clean_file_cache_is_reclaimable_and_availability_is_bounded(self):
        fields = {"anon": 3567616, "file": 67471482880, "shmem": 0,
                  "file_dirty": 4096, "file_writeback": 0, "file_mapped": 2170880,
                  "inactive_file": 39544774656, "active_file": 27926704128, "unevictable": 0}
        available, reclaimable = pilot.cgroup_ram_headroom(113770000000, 112742891520, 67767894016, fields)
        self.assertEqual(reclaimable, min(fields["file"], fields["active_file"] + fields["inactive_file"]) - 4096)
        self.assertGreaterEqual(available, 96 * 1024**3)
        self.assertLessEqual(available, 112742891520)
        self.assertEqual(pilot.cgroup_ram_headroom(80, 100, 0, fields)[0], 80)
        self.assertEqual(pilot.cgroup_ram_headroom(200, 100, 0, fields)[0], 100)

    def test_headroom_does_not_credit_anon_shmem_dirty_writeback_or_pinned_pages(self):
        fields = {"anon": 1000, "file": 80, "shmem": 20, "active_file": 40,
                  "inactive_file": 40, "file_dirty": 10, "file_writeback": 5, "unevictable": 7}
        self.assertEqual(pilot.cgroup_ram_headroom(200, 100, 90, fields), (48, 38))
        self.assertEqual(pilot.cgroup_ram_headroom(200, 100, 90, {"anon": 90}), (10, 0))
        self.assertEqual(pilot.cgroup_ram_headroom(200, 100, 90,
            {**fields, "file_dirty": 100}), (10, 0))

    def test_runtime_audit_records_effective_profile_and_refuses_control_mutation(self):
        requested = pilot.runtime_config(self.root)
        self.assertEqual(requested["video_preload_mode"], "default")
        self.assertEqual(requested["enhancer_enabled"], 3)
        self.assertEqual(pilot.settings_for(self.task)["prompt_enhancer"], "")
        module = types.SimpleNamespace(server_config={**requested, "extensions_defaults_version": "1.25"},
            default_profile_video=4, loaded_profile=4, preload_mode=lambda output: "default")
        result = pilot.runtime_audit(module, requested)
        self.assertEqual(result["loaded_profile"], 4)
        self.assertEqual(result["effective_config"], requested)
        module.server_config["kernel_precision"] = "fast"
        with self.assertRaisesRegex(ValueError, "effective_pilot_config_changed:kernel_precision"):
            pilot.runtime_audit(module, requested)


if __name__ == "__main__":
    unittest.main()
