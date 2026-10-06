"""Independent local safety checks for admission recovery and OS-bound clients.

All state is synthetic and temporary. Httpx MockTransport makes no network calls.
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import insert

from studio_platform.backup import _quarantine_restored_database
from studio_platform.quick_chat import objects
from studio_platform.repository import Conflict, attempts
import test_platform_quick_chat_admission as admission_test


SCRIPT = Path(__file__).parent / "skills/sixnine-yingxu/scripts/sixnine.py"
_spec = importlib.util.spec_from_file_location("sixnine_security_review_client", SCRIPT)
client_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(client_script)


class InertPlanHistoryReview(unittest.TestCase):
    """The canonical attempt ledger must outweigh missing summary counters."""

    setUp = admission_test.QuickAdmissionTests.setUp

    def test_hidden_attempt_history_prevents_inert_plan_refresh(self):
        admission = self.app.state.generation_admission
        job = admission.create_planned(self.principal, self.plan["plan_id"], "review-original")
        old = self.app.state.owned_plan(self.principal, self.plan["plan_id"])
        from studio_platform.repository import Scope
        scope = Scope("sixnine", "superdan", "story-one", "quick-chat-execution")
        fresh = self.repo.create_plan(scope, old["request"], old["execution_plan"],
            expires_at=self.repo.clock()+600, estimated_cost_microusd=old["estimated_cost_microusd"])
        # Deliberately simulate inconsistent recovered/legacy summary fields:
        # the job says no attempt, but the authoritative ledger records an
        # upstream submission whose stop has not been confirmed.
        with self.repo.engine.begin() as conn:
            conn.execute(insert(attempts).values(id="review-unknown-attempt", job_id=job["id"],
                number=1, status="submission_unknown", fence=1, worker_id="review-worker",
                created_at=1, updated_at=1, submission_started_at=1,
                upstream_task_id="review-upstream-task", upstream_stopped=0))
        with self.assertRaisesRegex(Conflict, "upstream_stop_unconfirmed"):
            admission.refresh_planned(self.principal, job["id"], fresh["id"])
        self.assertEqual(self.repo.get_job(scope, job["id"])["plan_id"], job["plan_id"])

    def test_restore_holds_jobless_executions_and_unknown_assistant_calls(self):
        rows = [
            ("review-exec-no-job", "execution", {"status": "pending_admission", "job_id": None}),
            ("review-exec-unknown", "execution", {"status": "unknown", "job_id": None}),
            ("review-running-turn", "turn", {"status": "running", "assistant_run": {"fence": 9}}),
            ("review-preflight", "preflight", {"status": "ready", "expires_at": 9999999999}),
        ]
        with self.repo.engine.begin() as conn:
            for ident, kind, payload in rows:
                conn.execute(insert(objects).values(id=ident, tenant="sixnine", owner="superdan",
                    session_id="review-session", kind=kind, payload=payload,
                    version=1, created_at=1, updated_at=1))
        _quarantine_restored_database(self.app.state.settings.data_dir / "platform.sqlite3")
        with contextlib.closing(sqlite3.connect(self.app.state.settings.data_dir / "platform.sqlite3")) as db:
            result = {ident: json.loads(raw) for ident, raw in db.execute(
                "SELECT id,payload FROM platform_quick_chat_objects WHERE id LIKE 'review-%'")}
        for ident in ("review-exec-no-job", "review-exec-unknown"):
            self.assertEqual(result[ident]["status"], "recovery_hold")
            self.assertIsNone(result[ident]["job_id"])
        self.assertEqual(result["review-running-turn"]["status"], "unknown")
        self.assertEqual(result["review-running-turn"]["assistant_run"]["fence"], 10)
        self.assertEqual(result["review-preflight"]["status"], "blocked")
        self.assertEqual(result["review-preflight"]["expires_at"], 0)


class OSConnectionClientReview(unittest.TestCase):
    def options(self, **kwargs):
        return argparse.Namespace(**{**dict(base_url="https://studio.example.test", connection="review-connection",
            profile=None, registry_root=None, command="request", method="GET", path="/v1/quick-chat/sessions",
            output=None, idempotency_key=None, json_file=None), **kwargs})

    @contextlib.contextmanager
    def fake_secure_store(self):
        state = {"protocol": "review-protocol", "status": "connected", "api_key": "synthetic-review-pat",
                 "expected": {"origin": "https://studio.example.test"}}
        module = SimpleNamespace(PROTOCOL="review-protocol", SecureStore=lambda: SimpleNamespace(load=lambda *a: state))
        spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda _: None))
        with patch.dict(os.environ, {}, clear=True), patch("importlib.util.spec_from_file_location", return_value=spec), \
                patch("importlib.util.module_from_spec", return_value=module):
            yield state

    def test_session_upload_uses_os_credential_without_registry_or_project_id(self):
        seen = []
        def handler(request):
            seen.append(str(request.url))
            self.assertEqual(request.headers["authorization"], "Bearer synthetic-review-pat")
            body = request.read()
            self.assertIn(b'client_asset_id', body)
            self.assertNotIn(b'client_project_id', body)
            return httpx.Response(201, json={"asset_id": "review-receipt", "status": "ready"})
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "review.png"
            source.write_bytes(b"synthetic media, never decoded")
            output = io.StringIO()
            with self.fake_secure_store(), contextlib.redirect_stdout(output):
                client_script.run(self.options(command="upload", session="review-session", project=None,
                    asset_id="review-stable-client-id", file=source), httpx.MockTransport(handler))
        self.assertEqual(seen, ["https://studio.example.test/v1/quick-chat/sessions/review-session/assets"])
        self.assertNotIn("synthetic-review-pat", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["asset_id"], "review-receipt")

    def test_session_resume_only_uses_original_receipt_and_session_endpoint(self):
        seen = []
        def handler(request):
            seen.append((request.method, request.url.path))
            if request.method == "GET":
                return httpx.Response(200, json={"assets": [{"asset_id": "review-receipt",
                    "client_asset_id": "review-stable-client-id", "status": "pending"}]})
            return httpx.Response(200, json={"asset_id": "review-receipt", "status": "ready"})
        with self.fake_secure_store(), contextlib.redirect_stdout(io.StringIO()):
            client_script.run(self.options(command="resume-upload", session="review-session", project=None,
                asset_id="review-stable-client-id"), httpx.MockTransport(handler))
        self.assertEqual(seen, [
            ("GET", "/v1/quick-chat/sessions/review-session/assets"),
            ("POST", "/v1/quick-chat/sessions/review-session/assets/review-receipt/resume")])

    def test_connection_credential_conflict_stops_before_request(self):
        def forbid(_):
            self.fail("Credential ambiguity must stop before any HTTP call")
        with patch.dict(os.environ, {"SIXNINE_API_KEY": "synthetic-other-account"}), \
                self.assertRaisesRegex(ValueError, "one explicit credential source"):
            client_script.run(self.options(), httpx.MockTransport(forbid))


if __name__ == "__main__":
    unittest.main()
