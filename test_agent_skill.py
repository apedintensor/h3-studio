import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

SCRIPT = Path(__file__).parent / "skills/sixnine-yingxu/scripts/sixnine.py"
spec = importlib.util.spec_from_file_location("sixnine_skill", SCRIPT)
skill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skill)


class AgentClientTests(unittest.TestCase):
    def args(self, **overrides):
        return argparse.Namespace(**{**dict(base_url="https://studio.example.test", registry_root=None,
            profile=None, command="request", method="GET", path="/v1/agent-guide", json_file=None,
            output=None, idempotency_key=None), **overrides})

    def run_client(self, handler, **overrides):
        with patch.dict("os.environ", {"SIXNINE_API_KEY": "synthetic-test-key"}), contextlib.redirect_stdout(io.StringIO()) as output:
            skill.run(self.args(**overrides), httpx.MockTransport(handler))
            return output.getvalue()

    def test_redirect_cannot_exfiltrate_authorization(self):
        seen = []
        def handler(request):
            seen.append(request.url.host)
            return httpx.Response(302, headers={"Location": "https://untrusted.example/file"})
        with self.assertRaisesRegex(ValueError, "HTTP 302"):
            self.run_client(handler)
        self.assertEqual(seen, ["studio.example.test"])

    def test_write_keeps_idempotency_and_does_not_retry(self):
        seen = []
        def handler(request):
            seen.append(request.headers["Idempotency-Key"])
            return httpx.Response(503, text="synthetic-test-key PRIVATE RESPONSE")
        with self.assertRaises(ValueError) as error:
            self.run_client(handler, method="POST", path="/v1/jobs", idempotency_key="stable-attempt")
        self.assertEqual(seen, ["stable-attempt"])
        self.assertNotIn("PRIVATE", str(error.exception))
        self.assertNotIn("synthetic-test-key", str(error.exception))

    def test_valid_download_matches_manifest_and_refuses_overwrite(self):
        data = b"representative artifact bytes"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "artifact.bin"
            options = dict(command="download", path="/v1/artifacts/result/content", output=path,
                sha256=hashlib.sha256(data).hexdigest(), max_bytes=100)
            result = json.loads(self.run_client(lambda _: httpx.Response(200, content=data), **options))
            self.assertTrue(result["verified"])
            self.assertEqual(path.read_bytes(), data)
            with self.assertRaises(FileExistsError):
                self.run_client(lambda _: httpx.Response(200, content=b"changed"), **options)
            self.assertEqual(path.read_bytes(), data)

    def test_rejects_credentials_and_external_paths(self):
        for path in ["https://untrusted.example/v1/projects", "//untrusted.example/v1/projects",
                     "/v1/api-keys", "/v1/auth/password", "/v1/%61pi-keys", "/v1/../auth"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                skill.api_path(path)
        with self.assertRaises(ValueError):
            skill.origin("http://public.example")


if __name__ == "__main__":
    unittest.main()
