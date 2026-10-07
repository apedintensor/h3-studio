"""Anonymous discovery, authenticated contract and distributable skill invariants."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from studio_platform import agent_discovery
import test_platform_api as fixtures


class AgentDiscoveryTests(unittest.TestCase):
    setUp = fixtures.ApiTests.setUp
    login = fixtures.ApiTests.login

    def test_public_docs_readable_without_frontend_or_credentials(self):
        for path in sorted(agent_discovery.PUBLIC_PATHS):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, path)
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                head = self.client.head(path)
                self.assertEqual(head.status_code, 200)
                self.assertEqual(head.content, b"")
        html = self.client.get("/for-agents").text
        self.assertNotIn("<script", html)
        for link in ("/for-agents/guide.json", "/for-agents/SKILL.md", "/for-agents/skill.zip", "/llms.txt"):
            self.assertIn('href="'+link+'"', html)
        self.assertIn('rel="service-doc"', self.client.get("/for-agents").headers["Link"])

    def test_only_exact_safe_methods_are_public_and_api_stays_private(self):
        for path in sorted(agent_discovery.PUBLIC_PATHS):
            self.assertEqual(self.client.post(path, json={}).status_code, 401)
        for path in ("/for-agents/private", "/for-agents/key", "/v1/projects", "/v1/capabilities",
                     "/v1/agent-guide", "/v1/guided-schema", "/v1/agent-skill.zip", "/openapi.json",
                     "/v1/projects/nonexistent/activity"):
            self.assertEqual(self.client.get(path).status_code, 401, path)
        self.assertEqual(self.client.get("/for-agents", headers={"Host": "attacker.example"}).status_code, 400)
        self.assertEqual(self.client.get("/for-agents", headers={"Authorization": "Bearer invalid"}).status_code, 401)

    def test_public_content_does_not_change_after_private_edit(self):
        paths = ("/for-agents", "/llms.txt", "/for-agents/guide.json", "/for-agents/SKILL.md")
        anonymous = {path: self.client.get(path).content for path in paths}
        self.login()
        self.client.post("/v1/projects", json={"title": "PRIVATE PROJECT NEVER ADVERTISE",
            "logline": "PRIVATE CONTENT NEVER ADVERTISE"}).raise_for_status()
        for path in paths:
            self.assertEqual(self.client.get(path).content, anonymous[path])
            self.assertNotIn(b"PRIVATE", anonymous[path])
        self.client.cookies.clear()
        for path in paths:
            self.assertEqual(self.client.get(path).content, anonymous[path])

    def test_zip_contains_only_reviewed_files_and_matches_public_skill(self):
        response = self.client.get("/for-agents/skill.zip")
        self.assertEqual(response.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(set(archive.namelist()), {"sixnine-yingxu/"+name for name in agent_discovery.SKILL_FILES})
            self.assertEqual(archive.read("sixnine-yingxu/SKILL.md"), self.client.get("/for-agents/SKILL.md").content)
            for name in agent_discovery.SKILL_FILES:
                self.assertEqual(archive.read("sixnine-yingxu/"+name), (agent_discovery.SKILL_ROOT/name).read_bytes())
        manifest = self.client.get("/for-agents/connect-manifest.json").json()
        helper = self.client.get(manifest["helper"]).content
        self.assertEqual(manifest["sha256"], hashlib.sha256(helper).hexdigest())
        guide = self.client.get("/for-agents/guide.json").json()
        self.assertFalse(manifest["raw_api_key_response"])
        self.assertTrue(guide["connection"]["profile"]["owner_only"])

    def test_missing_or_oversize_skill_fails_closed_without_local_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(agent_discovery, "SKILL_ROOT", root):
                for path in ("/for-agents/skill.zip", "/for-agents/SKILL.md"):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 503)
                    self.assertNotIn(folder, response.text)
                (root / "SKILL.md").write_bytes(b"x" * (agent_discovery.MAX_SKILL_BYTES+1))
                self.assertEqual(self.client.get("/for-agents/SKILL.md").status_code, 503)

    def test_linked_file_rejected_without_read(self):
        # Do not require Windows symlink privileges to test the rejection.
        with patch.object(Path, "is_symlink", return_value=True), patch.object(Path, "open") as opened:
            response = self.client.get("/for-agents/SKILL.md")
        self.assertEqual(response.status_code, 503)
        opened.assert_not_called()

    def test_manifest_examples_produce_navigable_story_with_scoped_agent(self):
        guide = self.client.get("/for-agents/guide.json").json()
        self.assertFalse(guide["discovery_is_authorization"])
        self.assertEqual(guide["web_links"]["entity"], "/?project={project_id}&entity={entity_id}")
        self.login()
        key_response = self.client.post("/v1/api-keys", json={"name": "Documented onboarding fixture",
            "scopes": ["projects:read", "projects:create", "projects:write"], "all_projects": True,
            "project_ids": [], "expires_in_days": 1})
        key_response.raise_for_status()
        headers = {"Authorization": "Bearer "+key_response.json()["api_key"]}
        self.client.cookies.clear()
        create = guide["examples"]["create_story"]
        created = self.client.request(create["method"], create["path"], json=create["body"],
            headers={**headers, **create["headers"]})
        self.assertEqual(created.status_code, 201, created.text)
        row = created.json()
        edit = guide["examples"]["create_chapter_scene_shot"]
        body = copy.deepcopy(edit["body"])
        body["expected_version"] = row["version"]
        result = self.client.request(edit["method"], edit["path"].format(project_id=row["id"]),
            json=body, headers={**headers, **edit["headers"]})
        self.assertEqual(result.status_code, 200, result.text)
        saved = self.client.get("/v1/projects/"+row["id"], headers=headers).json()
        entities = {item["id"]: item for item in saved["project"]["entities"]}
        self.assertEqual(entities["shot-one"]["parentId"], "scene-one")
        self.assertEqual(entities["scene-one"]["parentId"], "chapter-one")
        self.assertEqual(saved["version"], row["version"]+1)
        # The exact retry resolves the first receipt and cannot duplicate entities.
        retry = self.client.request(edit["method"], edit["path"].format(project_id=row["id"]),
            json=body, headers={**headers, **edit["headers"]})
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()["version"], saved["version"])
        self.login("supervan")
        self.assertEqual(self.client.get("/v1/projects/"+row["id"]).status_code, 404)

    def test_quick_examples_save_one_web_draft_preflight_and_never_submit_implicitly(self):
        guide = self.client.get("/for-agents/guide.json").json()
        self.assertEqual(guide["version"], 2)
        self.assertEqual(guide["web_links"]["quick"], "/freestyle?project={project_id}&entity={shot_id}")
        self.login()
        issued = self.client.post("/v1/api-keys", json={"name": "Quick agent fixture",
            "scopes": ["projects:read", "projects:create", "projects:write", "assets:read", "assets:write", "jobs:read", "jobs:write"],
            "all_projects": True, "project_ids": [], "expires_in_days": 1})
        issued.raise_for_status()
        headers = {"Authorization": "Bearer "+issued.json()["api_key"]}
        self.client.cookies.clear()
        example = guide["examples"]["create_quick_project"]
        created = self.client.request(example["method"], example["path"], json=example["body"], headers={**headers, **example["headers"]})
        self.assertEqual(created.status_code, 201, created.text)
        row = created.json()
        shot_id = row["project"]["journey"]["reviewShotId"]
        self.assertEqual(row["project"]["journey"]["workspace"], "freestyle")
        self.assertEqual(len([e for e in row["project"]["entities"] if e["type"] == "shot"]), 1)
        retry = self.client.request(example["method"], example["path"], json=example["body"], headers={**headers, **example["headers"]})
        self.assertEqual(retry.json()["id"], row["id"])
        example = guide["examples"]["configure_quick_text"]
        body = copy.deepcopy(example["body"])
        body["expected_version"] = row["version"]
        body["actions"][0]["shot_id"] = shot_id
        response = self.client.request(example["method"], example["path"].format(project_id=row["id"]), json=body,
            headers={**headers, **example["headers"]})
        self.assertEqual(response.status_code, 200, response.text)
        current = response.json()
        same = self.client.request(example["method"], example["path"].format(project_id=row["id"]), json=body,
            headers={**headers, **example["headers"]})
        self.assertEqual(same.json()["version"], current["version"])
        shot = next(e for e in current["project"]["entities"] if e["id"] == shot_id)
        self.assertEqual(shot["data"]["prompt"], body["actions"][0]["prompt"])
        self.assertEqual(shot["data"]["h3"]["controls"]["seed"], "42")
        example = guide["examples"]["read_quick_draft"]
        draft = self.client.get(example["path"].format(project_id=row["id"], shot_id=shot_id), headers=headers)
        self.assertEqual(draft.status_code, 200, draft.text)
        draft = draft.json()
        self.assertEqual(draft["project_version"], current["version"])
        self.assertEqual(draft["draft"]["prompt"], shot["data"]["prompt"])
        self.assertEqual(draft["web_url"], guide["web_links"]["quick"].format(project_id=row["id"], shot_id=shot_id))
        example = guide["examples"]["plan_quick_draft"]
        plan = self.client.post(example["path"].format(project_id=row["id"], shot_id=shot_id),
            json={"expected_version": draft["project_version"]}, headers=headers)
        self.assertEqual(plan.status_code, 201, plan.text)
        self.assertEqual(plan.json()["status"], "blocked")
        listed = self.client.get("/v1/jobs", params={"client_project_id": row["id"]}, headers=headers).json()
        self.assertEqual(listed["jobs"], [])
        self.login("supervan")
        self.assertEqual(self.client.get(guide["authenticated_resources"]["generation_draft"].format(project_id=row["id"], shot_id=shot_id)).status_code, 404)

    def test_authenticated_guide_reuses_public_quick_contract_without_public_private_data(self):
        public = self.client.get("/for-agents/guide.json").json()
        self.login()
        private = self.client.get("/v1/agent-guide").json()
        self.assertEqual(private["quick_creation"], public["quick_creation"])
        self.assertIn("shot.configure_generation", self.client.get("/v1/guided-schema").json()["operation_schemas"])
        self.assertIn("workspace=freestyle", self.client.get("/llms.txt").text)
        self.assertIn("快速草稿", self.client.get("/for-agents").text)


if __name__ == "__main__":
    unittest.main()
