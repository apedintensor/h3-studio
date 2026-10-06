"""Client-held PAT and recoverable local state; all API calls stay in process."""
from importlib.util import module_from_spec, spec_from_file_location
import io
import json
import os
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch

from studio_platform.agent_connect import AgentConnect, ConnectError, PROFILE_ID, PROFILE_VERSION
from studio_platform.auth import Auth, digest
from studio_platform.repository import Repository

HELPER_PATH = Path(__file__).resolve().parent / "skills" / "sixnine-yingxu" / "scripts" / "connect.py"
spec = spec_from_file_location("sixnine_connect_test", HELPER_PATH)
helper = module_from_spec(spec)
spec.loader.exec_module(helper)


class MemoryStore:
    def __init__(self):
        self.values, self.writes, self.fail = {}, 0, False
    def load(self, origin, connection_id):
        result = self.values.get((origin, connection_id))
        return json.loads(json.dumps(result)) if result else None
    def save(self, origin, connection_id, record, *, replace=False):
        if self.fail:
            raise helper.HelperError("os_credential_storage_failed")
        if not replace and (origin, connection_id) in self.values:
            raise helper.HelperError("connection_already_saved")
        self.values[(origin, connection_id)] = json.loads(json.dumps(record))
        self.writes += 1


class InProcessHTTP:
    def __init__(self, service, origin, store):
        self.service, self.origin, self.store = service, origin, store
        self.calls, self.lose_response, self.lose_before_claim, self.tamper = [], False, False, False
    def request(self, path, *, method="GET", body=None, token=None, idempotency_key=None):
        self.calls.append({"path": path, "method": method, "token_in_header": bool(token), "idempotency_key": idempotency_key})
        if path == "/v1/agent-connect/exchange":
            if not self.store.writes:
                raise AssertionError("token must be saved before exchange")
            self.assert_no_raw_pat(body)
            if self.lose_before_claim:
                self.lose_before_claim = False
                raise helper.HelperError("network_result_unknown")
            try:
                result = self.service.exchange(**body, origin=self.origin, source="in-process")
            except ConnectError as error:
                raise helper.HelperError(error.code) from None
            if self.lose_response:
                self.lose_response = False
                raise helper.HelperError("network_result_unknown")
            if self.tamper:
                result["connection"]["authorization"]["owner"] = "supervan"
            return result
        principal = self.service.auth.bearer(token)
        if not principal:
            raise helper.HelperError("http_request_failed")
        return {"username": principal.owner, "machine": principal.machine}
    @staticmethod
    def assert_no_raw_pat(body):
        if "api_key" in body:
            raise AssertionError("raw PAT reached exchange")


class AgentConnectHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Repository("sqlite:///" + (Path(self.temp.name) / "test.sqlite").as_posix())
        self.addCleanup(self.repo.close)
        self.auth = Auth(self.repo.engine, mode="local-test")
        session = self.auth.login("superdan", "", source="helper")
        self.service = AgentConnect(self.auth)
        self.origin = "http://127.0.0.1:8850"
        self.grant = self.service.issue(self.auth.session(session), authorization_profile_id=PROFILE_ID,
            authorization_profile_version=PROFILE_VERSION, idempotency_key="helper-create", origin=self.origin)
        self.connection_id = self.grant["connection"]["id"]
        self.store = MemoryStore()
        self.http = InProcessHTTP(self.service, self.origin, self.store)
        self.client = helper.AgentClient(self.origin, store=self.store, http=self.http)

    def connect(self):
        return self.client.connect(code=self.grant["code"], connection_id=self.connection_id,
            owner="superdan", tenant="sixnine", fingerprint=self.grant["connection"]["authorization_fingerprint"])

    def test_local_token_saved_first_and_output_contains_no_credentials(self):
        result = self.connect()
        self.assertEqual(result["status"], "connected")
        record = self.store.load(self.origin, self.connection_id)
        public_output = json.dumps(result)
        for value in (record["api_key"], record["verifier"], record["code"]):
            self.assertTrue(value not in public_output, "credential leaked in public result")
        self.assertEqual(self.client.request(self.connection_id, "/api/auth/me")["username"], "superdan")

    def test_storage_failure_prevents_any_registration(self):
        self.store.fail = True
        with self.assertRaisesRegex(helper.HelperError, "os_credential_storage_failed"):
            self.connect()
        self.assertEqual(len(self.http.calls), 0)

    def test_lost_response_recovers_same_key_and_saved_pat(self):
        self.http.lose_response = True
        with self.assertRaisesRegex(helper.HelperError, "network_result_unknown"):
            self.connect()
        before = self.store.load(self.origin, self.connection_id)
        recovered = self.client.resume(self.connection_id)
        after = self.store.load(self.origin, self.connection_id)
        self.assertTrue(before["api_key"] == after["api_key"], "recovery rotated key")
        self.assertTrue(recovered["recovered"])
        self.assertEqual(len(self.http.calls), 2)

    def test_request_lost_before_server_recovers_then_exact_original_claim(self):
        self.http.lose_before_claim = True
        with self.assertRaises(helper.HelperError):
            self.connect()
        before = self.store.load(self.origin, self.connection_id)
        result = self.client.resume(self.connection_id)
        self.assertEqual(result["status"], "connected")
        self.assertTrue(before["api_key"] == self.store.load(self.origin, self.connection_id)["api_key"], "retry rotated key")
        self.assertEqual(len(self.http.calls), 3)

    def test_connected_local_resume_does_not_claim_online_verified(self):
        self.connect()
        calls = len(self.http.calls)
        result = self.client.resume(self.connection_id)
        self.assertEqual(result["status"], "connected_locally")
        self.assertFalse(result["online_verified"])
        self.assertEqual(len(self.http.calls), calls)

    def test_wrong_account_return_fails_closed(self):
        self.http.tamper = True
        with self.assertRaisesRegex(helper.HelperError, "exchange_authorization_mismatch"):
            self.connect()
        self.assertEqual(self.store.load(self.origin, self.connection_id)["status"], "unknown")
        with self.assertRaisesRegex(helper.HelperError, "saved_connection_not_active"):
            self.client.request(self.connection_id, "/v1/projects")

    def test_original_saved_authorization_cannot_be_changed(self):
        self.connect()
        with self.assertRaisesRegex(helper.HelperError, "saved_authorization_mismatch"):
            self.client.connect(code=self.grant["code"], connection_id=self.connection_id,
                owner="supervan", tenant="sixnine", fingerprint=self.grant["connection"]["authorization_fingerprint"])

    def test_request_uses_process_key_and_preserves_business_idempotency(self):
        self.connect()
        result = self.client.request(self.connection_id, "/v1/quick-chat/sessions", method="POST",
            body={"title": "A story"}, idempotency_key="my-create-one")
        self.assertEqual(result["username"], "superdan")
        self.assertTrue(self.http.calls[-1]["token_in_header"])
        self.assertEqual(self.http.calls[-1]["idempotency_key"], "my-create-one")

    def test_insecure_authenticated_origin_and_redirect_rejected(self):
        for url in ("http://remote.example", "https://user:pass@example.com", "https://example.com/?token=x",
                    "https://example.com/#code", "https://example.com/path"):
            with self.assertRaises(helper.HelperError):
                helper.checked_origin(url)
        with self.assertRaisesRegex(helper.HelperError, "redirect_rejected"):
            helper._NoRedirect().redirect_request(None)

    def test_linux_without_session_safe_storage_stops(self):
        with patch.object(helper.os, "name", "posix"), patch.object(helper.sys, "platform", "linux"), \
                patch.object(helper.shutil, "which", return_value=None):
            with self.assertRaisesRegex(helper.HelperError, "os_credential_storage_unavailable"):
                helper.SecureStore()

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI only")
    def test_windows_dpapi_temp_file_is_encrypted_and_roundtrips(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name}):
            store = helper.SecureStore()
            fake = {"api_key": "fake-unit-test-not-a-real-key", "verifier": "fake-test-data"}
            store.save(self.origin, self.connection_id, fake)
            path = store.directory / (store.reference(self.origin, self.connection_id) + ".dpapi")
            self.assertTrue(b"fake-unit-test-not-a-real-key" not in path.read_bytes(), "plaintext credential file")
            self.assertTrue(store.load(self.origin, self.connection_id) == fake, "DPAPI mismatch")
            with self.assertRaisesRegex(helper.HelperError, "connection_already_saved"):
                store.save(self.origin, self.connection_id, fake)


if __name__ == "__main__":
    unittest.main()
