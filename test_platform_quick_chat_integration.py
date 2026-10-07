"""New public HTTP API through the actual asset/compiler/queue services.

Isolated temporary SQL + CPU decode; mock is only an explicit test backend.
"""
import tempfile
import hashlib
import shutil
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from studio_platform.api import create_app
from studio_platform.auth import API_SCOPES, Principal
from studio_platform.capabilities import VERSION
from studio_platform.repository import jobs
from studio_platform.settings import Settings
from test_platform_api import png
from test_platform_repository import LedgerCase
from studio_platform.worker import MockBackend, WorkerRunner


class QuickChatIntegrationTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(Path(self.tmp.name), auth_mode="local-test",
                                 database_url=self.url, generation_enabled=True, execution_backend="mock")
        self.app = create_app(self.settings, repository=self.repo)
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

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "CPU media tools required")
    def test_agent_turn_card_confirm_same_job_then_owner_downloads_mock_outputs(self):
        guide = self.client.get("/for-agents/guide.json").json()["quick_chat"]
        examples = guide["examples"]
        self.assertEqual(self.client.get(guide["schema_url"]).json()["agent_contract"], guide)
        key = self.client.post("/v1/api-keys", json={"name": "Offline Agent",
            "scopes": ["projects:create", "projects:read", "projects:write", "assets:read", "assets:write", "jobs:read", "jobs:write"],
            "all_projects": True, "project_ids": [], "expires_in_days": 1})
        key.raise_for_status()
        auth = {"Authorization": "Bearer "+key.json()["api_key"]}
        self.client.post("/api/auth/logout").raise_for_status()
        session = self.write(examples["session"]["path"], examples["session"]["body"], "agent-session", headers=auth)["session"]
        base = "/v1/quick-chat/sessions/"+session["id"]
        turn = self.write(base+"/turns", {**examples["turn_to_card"]["body"], "expected_version": session["version"], "assistant_mode": "none",
            "create_card": True, "model_id": session["model_id"],
            "text": "Synthetic CPU demo with audio."}, "agent-turn", headers=auth)
        card = self.client.get(base+"/cards/"+turn["card_id"], headers=auth).json()
        revision = self.client.get(base+"/revisions/"+card["current_revision_id"], headers=auth).json()
        path = base+"/revisions/"+revision["id"]
        preflight = self.write(path+"/preflights", {"capabilities_version": VERSION,
            "revision_hash": revision["input_hash"]}, "agent-preflight", headers=auth)
        self.assertEqual(preflight["status"], "ready", preflight)
        body = {"revision_hash": revision["input_hash"], "preflight_id": preflight["id"], "confirmed": False}
        rejected = self.client.post(path+"/submissions", json=body,
            headers={**auth, "Idempotency-Key": "unconfirmed"})
        self.assertEqual(rejected.status_code, 422, rejected.text)
        with self.repo.engine.connect() as conn:
            self.assertEqual(conn.execute(select(jobs.c.id)).all(), [])
        submitted = self.write(path+"/submissions", {**body, "confirmed": True}, "confirmed", headers=auth)
        ident = submitted["items"][0]["job_id"]
        self.assertIsNotNone(ident)
        backend = MockBackend(Path(self.tmp.name)/"mock-runtime", enabled=True)
        runner = WorkerRunner(self.repo, self.app.state.storage, Path(self.tmp.name)/"worker",
                              backend=backend, retry_after_s=0)
        for _ in range(5):
            runner.run_once("cpu-test-only", "mock")
            observed = self.client.get(base+"/submissions/"+submitted["id"], headers=auth).json()
            if observed["items"][0]["status"] == "succeeded":
                break
        job = observed["items"][0]["job"]
        self.assertEqual(job["id"], ident)
        self.assertEqual(job["status"], "succeeded", observed)
        self.assertTrue(job["simulation"])
        self.assertTrue({"video", "audio"}.issubset({a["kind"] for a in job["artifacts"]}))
        for artifact in job["artifacts"]:
            response = self.client.get(artifact["download_url"], headers=auth)
            self.assertEqual(response.status_code, 200, response.text[:100] if response.status_code != 200 else "")
            self.assertEqual(len(response.content), artifact["size_bytes"])
            self.assertEqual(hashlib.sha256(response.content).hexdigest(), artifact["sha256"])
        replay = self.write(path+"/submissions", {**body, "confirmed": True}, "confirmed", headers=auth)
        self.assertEqual(replay["items"][0]["job_id"], ident)
        self.assertEqual(self.repo.get_job_for_owner("sixnine", "superdan", ident)["attempt_no"], 1)
        self.client.post("/api/auth/login", json={"username": "supervan"}).raise_for_status()
        self.assertEqual(self.client.get(base+"/submissions/"+submitted["id"]).status_code, 404)
        self.assertEqual(self.client.get(job["artifacts"][0]["download_url"]).status_code, 404)

    def test_concurrent_confirmations_across_actors_keep_one_submission_and_job_per_item(self):
        service = self.app.state.quick_chat
        browser = Principal("superdan", "browser")
        agent = Principal("superdan", "agent-two", True, (), tuple(API_SCOPES), True)
        session = service.create_session(browser, {}, "race-session")["session"]
        sid = session["id"]
        revision = service.save_card(browser, sid, {"recipe_id": "h3-base-fl2va-v1",
            "prompt": "Synthetic concurrent confirmation", "inputs": {}, "copies": 2,
            "controls": {"duration": 5, "resolution": "480P"}}, "race-card")["revision"]
        preflight = service.preflight(browser, sid, revision["id"], {
            "capabilities_version": VERSION, "revision_hash": revision["input_hash"]}, "race-preflight")
        body = {"confirmed": True, "preflight_id": preflight["id"], "revision_hash": revision["input_hash"]}
        barrier = threading.Barrier(2)
        def confirm(principal, key):
            barrier.wait(10)
            return service.submit(principal, sid, revision["id"], body, key)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(confirm, browser, "race-browser")
            b = pool.submit(confirm, agent, "race-agent")
            results = (a.result(15), b.result(15))
        self.assertEqual(results[0]["id"], results[1]["id"])
        observed = service.get_submission(agent, sid, results[0]["id"])
        ids = [i["job_id"] for i in observed["items"]]
        self.assertEqual(len(set(ids)), 2)
        self.assertNotIn(None, ids)
        with self.repo.engine.connect() as conn:
            self.assertEqual(set(conn.execute(select(jobs.c.id)).scalars()), set(ids))


if __name__ == "__main__":
    unittest.main()
