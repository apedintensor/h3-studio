"""Operator assembly tests with fake Session/server; no listener or GPU runtime."""
from contextlib import redirect_stdout
from dataclasses import replace
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from studio_platform.inference.protocol import NotReady
from studio_platform.inference.wangp_contract import EngineManifest, PreparedRequest, canonical_json
from studio_platform.inference import wangp_factory
from studio_platform.runtime_hosts import wangp_launcher
from studio_platform.runtime_hosts.wangp_receipts import ReceiptJournal


TEST_TOKEN = "synthetic-launcher-test-token-" + "x" * 32


class WanGPLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest_path = self.root / "manifest.json"
        document = json.loads((Path(__file__).parent / "deploy/wangp/manifest.json").read_text(encoding="utf-8"))
        self.manifest = EngineManifest.from_dict(document)
        self.manifest_path.write_text(json.dumps(document), encoding="utf-8")
        self.token = self.root / "private-token"
        self.token.write_text(TEST_TOKEN, encoding="ascii")
        self.token.chmod(0o600)
        self.state = self.root / "state"
        self.config = self.root / "wgp_config.json"
        self.config.write_text("{}", encoding="utf-8")
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.models = self.root / "models"
        self.models.mkdir()
        self.argv = ["--runtime-root", str(self.runtime), "--model-root", str(self.models),
            "--config", str(self.config), "--manifest", str(self.manifest_path),
            "--token-file", str(self.token), "--state-dir", str(self.state), "--slot-key", "slot-test"]

    def journal(self, create=False):
        return ReceiptJournal(self.state / "operations.sqlite3", slot_key="slot-test",
                              manifest_digest=self.manifest.digest, create=create)

    def runtime_document(self):
        path = self.root / "runtime.json"
        value = {"version": 1, "enabled": True, "slot_key": "slot-test", "configuration_id": "config-test",
                 "manifest_file": str(self.manifest_path), "token_file": str(self.token)}
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)
        slot = types.SimpleNamespace(runtime_config_file=str(path), endpoint="http://127.0.0.1:8199",
            spec=types.SimpleNamespace(backend="wangp-worker", model_id="MiniMax-H3-Base-BF16",
                configuration_id="config-test", engine_manifest_digest=self.manifest.digest))
        return path, value, slot

    def test_import_and_factory_construction_do_not_start_network_or_session(self):
        from studio_platform.runtime_hosts import wangp_session
        with patch("httpx.Client", side_effect=AssertionError("no client on import/construction")), \
                patch.object(wangp_session, "create_session", side_effect=AssertionError("no Session")):
            importlib.reload(wangp_factory)
            importlib.reload(wangp_launcher)
            _, _, slot = self.runtime_document()
            backend = wangp_factory.create_backend(slot, self.root)
            self.assertEqual(backend.manifest.digest, self.manifest.digest)
            self.assertIsNone(backend.transport._client)
            self.assertNotIn(TEST_TOKEN, repr(backend))
            backend.close()

    def test_factory_requires_configuration_manifest_and_private_endpoint(self):
        path, value, slot = self.runtime_document()
        for invalid in ({**value, "enabled": False}, {**value, "configuration_id": "other"},
                        {**value, "extra": "not-accepted"}):
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "configuration_mismatch"):
                wangp_factory.create_backend(slot, self.root)
        path.write_text(json.dumps(value), encoding="utf-8")
        slot.spec.engine_manifest_digest = "0" * 64
        with self.assertRaisesRegex(ValueError, "manifest_binding_mismatch"):
            wangp_factory.create_backend(slot, self.root)
        slot.spec.engine_manifest_digest = self.manifest.digest
        slot.endpoint = "http://example.org:8199"
        with self.assertRaisesRegex(ValueError, "loopback_tunnel_required"):
            wangp_factory.create_backend(slot, self.root)
        slot.endpoint = "http://127.0.0.1:8199"
        self.token.write_text("invalid", encoding="ascii")
        with self.assertRaisesRegex(ValueError, "invalid_token"):
            wangp_factory.create_backend(slot, self.root)

    def test_verify_only_does_not_create_journal_read_token_or_start_session(self):
        self.token.unlink()
        from studio_platform.runtime_hosts import wangp_session
        with patch.object(wangp_session, "verify_runtime", return_value={"manifest_digest": self.manifest.digest}), \
                patch.object(wangp_session, "create_session", side_effect=AssertionError("no Session")), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(wangp_launcher.main(self.argv + ["--verify-only"]), 0)
        self.assertFalse(self.state.exists())
        self.assertIn("runtime_files_verified", output.getvalue())

    def test_changed_verified_manifest_fails_before_session_or_listener(self):
        from studio_platform.runtime_hosts import wangp_session
        with patch.object(wangp_session, "verify_runtime", return_value={"manifest_digest": "0" * 64}), \
                patch.object(wangp_session, "create_session") as session, \
                patch.object(wangp_launcher, "create_app") as app, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(wangp_launcher.main(self.argv + ["--create-journal"]), 1)
            session.assert_not_called()
            app.assert_not_called()
        self.assertEqual(output.getvalue().strip(), '{"state": "wangp_runtime_start_failed"}')
        journal = self.journal()
        journal.acquire_host()  # Startup failure released ownership.
        journal.release_host()

    def test_host_lock_precedes_verification_and_session_and_pending_receipts_survive(self):
        original = PreparedRequest("job-original", "attempt-original", "1" * 64, self.manifest.digest,
            canonical_json({"synthetic": True}), canonical_json({"width": 256, "height": 256}), True)
        journal = self.journal(create=True)
        journal.claim(original, "old-incarnation")
        journal.transition(original.operation_id, expected={"prepared"}, state="dispatch_intent")
        events, hosts = [], []

        def check_owned():
            contender = self.journal()
            try:
                with self.assertRaisesRegex(NotReady, "owned"):
                    contender.acquire_host()
            finally:
                contender.release_host()

        def verify(*args):
            check_owned()
            events.append("verify")
            return {"manifest_digest": self.manifest.digest}

        def session(*args):
            check_owned()
            events.append("session")
            return types.SimpleNamespace(is_idle=lambda: True, close_when_idle=lambda: events.append("session-closed"))

        def app(host, inputs, token):
            hosts.append(host)
            self.assertEqual(token, TEST_TOKEN)
            self.assertFalse(host.readiness().idle)
            current = host.inspect(original.operation_id)
            self.assertEqual(current.state, "unknown")
            self.assertEqual(current.identity_digest, original.identity_digest)
            with self.assertRaisesRegex(NotReady, "obligation_pending"):
                journal.claim(replace(original, attempt_tag="different-attempt"), "new-incarnation")
            return object()

        fake_uvicorn = types.ModuleType("uvicorn")
        def serve(application, **kwargs):
            events.append("serve")
            self.assertEqual(kwargs["host"], "127.0.0.1")
            self.assertEqual(kwargs["workers"], 1)
            self.assertFalse(kwargs["access_log"])
            self.assertFalse(kwargs["proxy_headers"])
        fake_uvicorn.run = serve
        from studio_platform.runtime_hosts import wangp_session
        with patch.object(wangp_session, "verify_runtime", side_effect=verify), \
                patch.object(wangp_session, "create_session", side_effect=session), \
                patch.object(wangp_launcher, "create_app", side_effect=app), \
                patch.dict(sys.modules, {"uvicorn": fake_uvicorn}), patch.dict(os.environ), redirect_stdout(io.StringIO()):
            self.assertEqual(wangp_launcher.main(self.argv), 0)
        self.assertEqual(events, ["verify", "session", "serve", "session-closed"])
        self.assertEqual(journal.get(original.operation_id).state, "unknown")
        self.assertTrue(journal.has_obligations())
        journal.acquire_host()
        journal.release_host()

    def test_shutdown_waits_for_runtime_and_closes_before_releasing_host(self):
        now, events = [0], []
        host = types.SimpleNamespace(close=lambda: events.append("host-closed"))
        session = types.SimpleNamespace(is_idle=lambda: now[0] >= .5,
            close_when_idle=lambda: events.append("session-closed"))
        def sleep(seconds):
            now[0] += seconds
            events.append("wait")
        wangp_launcher.shutdown_owned_host(host, session, grace_seconds=1,
            clock=lambda: now[0], sleeper=sleep,
            terminate=lambda _: self.fail("must allow actual completion"))
        self.assertEqual(events, ["wait", "wait", "session-closed", "host-closed"])

    def test_shutdown_timeout_or_close_error_never_releases_journal_ownership(self):
        for idle in (False, True):
            with self.subTest(idle=idle):
                now, events = [0], []
                host = types.SimpleNamespace(close=lambda: events.append("host-closed"))
                def cannot_close():
                    raise RuntimeError("synthetic-close-failure")
                session = types.SimpleNamespace(is_idle=lambda: idle, close_when_idle=cannot_close)
                def sleep(seconds):
                    now[0] += seconds
                with self.assertRaisesRegex(RuntimeError, "termination_required"):
                    wangp_launcher.shutdown_owned_host(host, session, grace_seconds=1,
                        clock=lambda: now[0], sleeper=sleep, terminate=lambda code: events.append(("terminate", code)))
                self.assertEqual(events, [("terminate", 1)])

    def test_partial_session_initialization_is_not_treated_as_proven_stopped(self):
        now, events = [0], []
        pending = wangp_launcher.PendingSession()
        host = types.SimpleNamespace(close=lambda: events.append("host-closed"))
        def sleep(seconds):
            now[0] += seconds
        with self.assertRaisesRegex(RuntimeError, "termination_required"):
            wangp_launcher.shutdown_owned_host(host, pending, grace_seconds=1,
                clock=lambda: now[0], sleeper=sleep, terminate=lambda code: events.append(("terminate", code)))
        self.assertEqual(events, [("terminate", 1)])

    def test_interrupting_shutdown_requires_process_termination_before_release(self):
        events = []
        def interrupted(seconds):
            raise KeyboardInterrupt()
        with self.assertRaisesRegex(RuntimeError, "termination_required"):
            wangp_launcher.shutdown_owned_host(
                types.SimpleNamespace(close=lambda: events.append("host-closed")),
                wangp_launcher.PendingSession(), grace_seconds=1, clock=lambda: 0,
                sleeper=interrupted, terminate=lambda code: events.append(("terminate", code)))
        self.assertEqual(events, [("terminate", 1)])


if __name__ == "__main__":
    unittest.main()
