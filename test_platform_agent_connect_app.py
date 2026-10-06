"""Actual app middleware integration, entirely in-process and disposable."""
from pathlib import Path
import secrets
import tempfile
import unittest

from fastapi.testclient import TestClient

from studio_platform.api import create_app
from studio_platform.auth import digest
from studio_platform.settings import Settings


class AgentConnectAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.origin = "http://127.0.0.1:8850"
        self.app = create_app(Settings(Path(self.temp.name), auth_mode="local-test"))
        self.client = TestClient(self.app, base_url=self.origin)
        self.addCleanup(self.client.close)
        self.addCleanup(self.app.state.repository.close)
        response = self.client.post("/api/auth/login", json={"username": "superdan"}, headers={"Origin": self.origin})
        self.assertEqual(response.status_code, 200)

    def grant(self):
        profile = self.client.get("/v1/account/agent-connections").json()["authorization_profile"]
        response = self.client.post("/v1/account/agent-connections", json={"name": "Codex",
            "authorization_profile_id": profile["id"], "authorization_profile_version": profile["version"]},
            headers={"Origin": self.origin, "Idempotency-Key": secrets.token_hex(12)})
        self.assertEqual(response.status_code, 201)
        return response.json()

    def test_public_discovery_helper_then_anonymous_exchange_uses_real_auth(self):
        anon = TestClient(self.app, base_url=self.origin)
        self.addCleanup(anon.close)
        manifest = anon.get("/for-agents/connect-manifest.json")
        self.assertEqual(manifest.status_code, 200)
        helper = anon.get("/for-agents/connect.py")
        self.assertEqual(helper.status_code, 200)
        self.assertTrue(len(helper.content) > 1000)
        grant = self.grant()
        token, verifier = "sxp_" + secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        body = {"code": grant["code"], "client_challenge": digest(verifier), "token_hash": digest(token),
            "key_prefix": token[:12], "expected_authorization_fingerprint": grant["connection"]["authorization_fingerprint"]}
        exchanged = anon.post("/v1/agent-connect/exchange", json=body)
        self.assertEqual(exchanged.status_code, 200)
        machine = TestClient(self.app, base_url=self.origin, headers={"Authorization": "Bearer " + token})
        self.addCleanup(machine.close)
        response = machine.get("/api/auth/me")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["username"], "superdan")
        self.assertTrue(response.json()["machine"])
        self.assertIn("assistant:run", response.json()["scopes"])
        self.assertEqual(machine.get("/v1/account/agent-connections").status_code, 403)
        self.assertEqual(anon.get("/v1/projects").status_code, 401)

    def test_cross_site_guard_rejects_code_and_private_apis_stay_private(self):
        grant = self.grant()
        token, verifier = "sxp_" + secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        anon = TestClient(self.app, base_url=self.origin)
        self.addCleanup(anon.close)
        response = anon.post("/v1/agent-connect/exchange", json={"code": grant["code"],
            "client_challenge": digest(verifier), "token_hash": digest(token), "key_prefix": token[:12],
            "expected_authorization_fingerprint": grant["connection"]["authorization_fingerprint"]},
            headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(self.app.state.auth.bearer(token))
        self.assertEqual(anon.post("/v1/account/agent-connections", json={}, headers={"Origin": self.origin}).status_code, 401)
        self.assertEqual(anon.get("/openapi.json").status_code, 401)
        self.assertFalse(self.client.get("/healthz").json()["generation_enabled"])


if __name__ == "__main__":
    unittest.main()
