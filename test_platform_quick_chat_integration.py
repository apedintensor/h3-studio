"""New public HTTP API through the actual asset/compiler/queue services.

Isolated temporary SQL + CPU decode; mock is only an explicit test backend.
"""
import tempfile
from pathlib import Path
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from studio_platform.api import create_app
from studio_platform.auth import API_SCOPES
from studio_platform.capabilities import VERSION
from studio_platform.repository import jobs
from studio_platform.settings import Settings
from test_platform_api import png


class QuickChatIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(Path(self.tmp.name), auth_mode="local-test",
                                 generation_enabled=True, execution_backend="mock")
        self.app = create_app(self.settings)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.post("/api/auth/login", json={"username": "superdan"}).raise_for_status()

    def write(self, path, body, key, *, method="POST", headers=None):
        r = self.client.request(method, path, json=body,
            headers={"Idempotency-Key": key, **(headers or {})})
        self.assertLess(r.status_code, 300, r.text)
        return r.json()

    def test_web_and_two_agent_keys_share_one_card_and_two_jobs(self):
        created = self.write("/v1/quick-chat/sessions", {"title": "HTTP contract fixture"}, "new-session")
        session = created["session"]
        base = "/v1/quick-chat/sessions/"+session["id"]
        upload = self.client.post(base+"/assets", data={"client_asset_id": "original-first-frame"},
                                  files={"file": ("reference.png", png(), "image/png")})
        self.assertEqual(upload.status_code, 201, upload.text)
        asset = upload.json()
        self.assertEqual(asset["status"], "ready")
        self.write(base+"/materials", {"expected_version": session["version"], "bindings": [{
            "binding_id": "first-image", "version": 0, "asset_id": asset["asset_id"],
            "kind": "image", "slot": "first_frame", "enabled": True}]}, "bind-image", method="PUT")
        saved = self.write(base+"/cards", {"title": "Image motion", "prompt": "A person slowly turns toward the camera.",
            "recipe_id": "h3-base-fl2va-v1", "controls": {"duration": 5, "resolution": "480P", "seed": "18446744073709551615"},
            "inputs": {"first_frame": {"asset_id": asset["asset_id"]}}, "copies": 2}, "new-card")
        revision = saved["revision"]
        seeds = [x["seed"] for x in revision["items"]]
        self.assertEqual(seeds, ["18446744073709551615", "0"])
        path = base+"/revisions/"+revision["id"]
        preflight = self.write(path+"/preflights", {"capabilities_version": VERSION,
            "revision_hash": revision["input_hash"]}, "preflight")
        self.assertEqual(preflight["status"], "ready", preflight)
        body = {"revision_hash": revision["input_hash"], "preflight_id": preflight["id"], "confirmed": True}
        submitted = self.write(path+"/submissions", body, "web-submit")
        ids = [x["job_id"] for x in submitted["items"]]
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(all(x["status"] == "queued" for x in submitted["items"]), submitted)
        for index in range(2):
            key = self.client.post("/v1/api-keys", json={"name": "Agent test", "scopes": sorted(API_SCOPES),
                "all_projects": True, "project_ids": [], "expires_in_days": 1})
            key.raise_for_status()
            headers = {"Authorization": "Bearer "+key.json()["api_key"]}
            replay = self.write(path+"/submissions", body, "agent-submit-"+str(index), headers=headers)
            self.assertEqual(replay["id"], submitted["id"])
            self.assertEqual([x["job_id"] for x in replay["items"]], ids)
        with self.app.state.repository.engine.connect() as conn:
            self.assertEqual(len(conn.execute(select(jobs.c.id)).all()), 2)
        self.client.post("/api/auth/logout").raise_for_status()
        self.client.post("/api/auth/login", json={"username": "supervan"}).raise_for_status()
        self.assertEqual(self.client.get(base).status_code, 404)
        self.assertEqual(self.client.get("/v1/quick-chat/sessions").json()["sessions"], [])

    def test_disabled_assistant_saves_real_turn_and_does_not_create_video_job(self):
        session = self.write("/v1/quick-chat/sessions", {}, "new-session")["session"]
        base = "/v1/quick-chat/sessions/"+session["id"]
        turn = self.write(base+"/turns", {"expected_version": session["version"],
            "text": "Help me discuss a camera movement.", "model_id": "gemini-3.8-flash"}, "discuss")
        self.assertEqual(turn["status"], "failed")
        self.assertEqual(turn["error_code"], "assistant_disabled")
        self.assertIsNone(turn["reply"])
        restored = self.client.get(base+"/turns/"+turn["id"]).json()
        self.assertEqual(restored["id"], turn["id"])
        with self.app.state.repository.engine.connect() as conn:
            self.assertEqual(conn.execute(select(jobs.c.id)).all(), [])


if __name__ == "__main__":
    unittest.main()
