"""Owned CPU processes only; no model, provider or application environment."""
from dataclasses import replace
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from studio_platform.control import WorkerSpec
from studio_platform.fleet import FleetConfig, SlotConfig
from studio_platform.fleet_process import owned_process, prepare_launch
from unittest.mock import patch


def configuration(root):
    slot = SlotConfig(WorkerSpec("cpu-test", "mock", "mock", "local-only", ("cpu",), (),
        "SIMULATION", "simulation-v1", backend="mock"), enabled=True)
    return FleetConfig(Path(root)/"fleet", (slot,), enabled=True, max_children=1)


class FleetProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = configuration(self.root)

    def child(self, token):
        script = """import pathlib,sys,time
from test_platform_fleet_process import configuration
from studio_platform.fleet_process import owned_process
root=pathlib.Path(sys.argv[1]);config=configuration(root)
with owned_process(config,'cpu-test',sys.argv[2]):
 (root/'ready').write_text('owned')
 deadline=time.monotonic()+15
 while not (root/'stop').exists() and time.monotonic()<deadline: time.sleep(.01)
"""
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        proc = subprocess.Popen([sys.executable, "-c", script, str(self.root), token],
            cwd=Path(__file__).parent, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, **kwargs)
        def cleanup():
            if proc.poll() is None:
                proc.terminate()
            proc.communicate(timeout=5)
        self.addCleanup(cleanup)
        deadline = time.monotonic()+5
        while not (self.root/"ready").exists() and proc.poll() is None and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertTrue((self.root/"ready").exists(), "Synthetic child failed to acquire ownership")
        return proc

    def test_delayed_old_launch_cannot_register_or_run_after_replacement(self):
        old, _ = prepare_launch(self.config, "cpu-test")
        new, _ = prepare_launch(self.config, "cpu-test", recovering=True)
        actions = []
        with self.assertRaisesRegex(ValueError, "launch_superseded"):
            with owned_process(self.config, "cpu-test", old):
                actions.append("old-registration-or-inference")
        with owned_process(self.config, "cpu-test", new):
            actions.append("replacement-original-worker")
        self.assertEqual(actions, ["replacement-original-worker"])

    def test_real_live_cpu_owner_is_observed_not_signalled_or_relaunched(self):
        token, _ = prepare_launch(self.config, "cpu-test")
        proc = self.child(token)
        replacement, observed = prepare_launch(self.config, "cpu-test", recovering=True)
        self.assertIsNone(replacement)
        self.assertIsNone(observed.pid)
        self.assertIsNone(observed.poll())
        self.assertIsNone(observed.stopped_proof())
        observed.send_signal(9)
        self.assertIsNone(proc.poll())
        with self.assertRaisesRegex(ValueError, "already_owned"):
            with owned_process(self.config, "cpu-test", token):
                self.fail("Two owners entered the execution scope")
        (self.root/"stop").touch()
        proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(observed.poll(), -1)  # Non-parent cannot claim OS exit zero.
        self.assertTrue(observed.stopped_proof()["cpu_owner_stopped"])
        with self.assertRaisesRegex(ValueError, "launch_superseded"):
            with owned_process(self.config, "cpu-test", token):
                self.fail("Exited token replayed")

    def test_real_cpu_crash_releases_kernel_lock_but_old_launch_stays_fenced(self):
        token, _ = prepare_launch(self.config, "cpu-test")
        proc = self.child(token)
        _, observed = prepare_launch(self.config, "cpu-test", recovering=True)
        proc.terminate()  # Exact Popen created by this test, never a saved PID.
        proc.communicate(timeout=5)
        self.assertEqual(observed.poll(), -1)
        new, inherited = prepare_launch(self.config, "cpu-test", recovering=True)
        self.assertIsNone(inherited)
        self.assertNotEqual(token, new)
        with self.assertRaisesRegex(ValueError, "superseded"):
            observed.poll()
        with self.assertRaisesRegex(ValueError, "launch_superseded"):
            with owned_process(self.config, "cpu-test", token):
                self.fail("Crashed launch replayed")
        with owned_process(self.config, "cpu-test", new):
            pass

    def test_missing_corrupt_legacy_or_changed_identity_never_mints_recovery(self):
        with self.assertRaises(FileNotFoundError):
            prepare_launch(self.config, "cpu-test", recovering=True)
        path = self.config.work_dir/"cpu-test"/"process-owner.json"
        self.assertFalse(path.exists())
        prepare_launch(self.config, "cpu-test")
        original = path.read_bytes()
        changed = replace(self.config, shutdown_grace_s=300)
        with self.assertRaisesRegex(ValueError, "identity_conflict"):
            prepare_launch(changed, "cpu-test", recovering=True)
        with patch("studio_platform.fleet_process._boot_identity", return_value="different-kernel-boot"), \
                self.assertRaisesRegex(ValueError, "identity_conflict"):
            prepare_launch(self.config, "cpu-test", recovering=True)
        self.assertEqual(path.read_bytes(), original)
        for raw in (b"{", b"{}", b"[]", b"x"*65537):
            path.write_bytes(raw)
            with self.assertRaises(ValueError):
                prepare_launch(self.config, "cpu-test", recovering=True)
            self.assertEqual(path.read_bytes(), raw)

    def test_normal_start_cannot_replace_launch_and_exception_replay_is_fenced(self):
        token, _ = prepare_launch(self.config, "cpu-test")
        with self.assertRaisesRegex(ValueError, "recovery_required"):
            prepare_launch(self.config, "cpu-test")
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            with owned_process(self.config, "cpu-test", token):
                raise RuntimeError("synthetic")
        with self.assertRaisesRegex(ValueError, "launch_superseded"):
            with owned_process(self.config, "cpu-test", token):
                pass
        new, _ = prepare_launch(self.config, "cpu-test", recovering=True)
        self.assertNotEqual(token, new)

    def test_hard_link_receipt_is_not_owned_evidence(self):
        prepare_launch(self.config, "cpu-test")
        path = self.config.work_dir/"cpu-test"/"process-owner.json"
        os.link(path, self.root/"alias")
        with self.assertRaisesRegex(ValueError, "file_untrusted"):
            prepare_launch(self.config, "cpu-test", recovering=True)

    @unittest.skipIf(os.name == "nt", "POSIX owner-only permissions")
    def test_public_receipt_and_replaced_lock_symlink_fail_closed(self):
        prepare_launch(self.config, "cpu-test")
        path = self.config.work_dir/"cpu-test"/"process-owner.json"
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "file_untrusted"):
            prepare_launch(self.config, "cpu-test", recovering=True)
        lock = path.with_name("process-owner.lock")
        lock.unlink()
        lock.symlink_to(path)
        with self.assertRaisesRegex(ValueError, "path_untrusted"):
            prepare_launch(self.config, "cpu-test", recovering=True)
