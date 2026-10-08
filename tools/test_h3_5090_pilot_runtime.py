"""Offline tests for no-replay, identity, and INT8 pilot boundaries."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import types
import unittest

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
                        time.time() + 60, collect=self.collect)

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
        with self.assertRaisesRegex(ValueError, "deadline_elapsed"):
            pilot.run_tasks(session, [self.task], self.root, self.outputs, self.torch, time.time()-1)
        self.assertFalse(session.calls)


if __name__ == "__main__":
    unittest.main()
