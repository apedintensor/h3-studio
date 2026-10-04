import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import sys
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

    def run_client(self, handler, *, resolver=None, clock=None, sleep=None, **overrides):
        with patch.dict("os.environ", {"SIXNINE_API_KEY": "synthetic-test-key"}), contextlib.redirect_stdout(io.StringIO()) as output:
            options = {"resolver": resolver}
            if clock is not None:
                options["clock"] = clock
            if sleep is not None:
                options["sleep"] = sleep
            skill.run(self.args(**overrides), httpx.MockTransport(handler), **options)
            return output.getvalue()

    @staticmethod
    def public_dns(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

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

    def test_download_signed_storage_hop_pins_ip_sni_and_drops_all_api_credentials(self):
        seen, data = [], b"verified-storage-bytes"
        signed = "https://objects.example.test/bucket/take.mp4?X-Amz-Signature=private-grant"
        def handler(request):
            seen.append(request)
            if len(seen) == 1:
                return httpx.Response(307, headers={"Location": signed, "Set-Cookie": "website-session=private; Path=/"})
            self.assertEqual(str(request.url).split("?")[0], "https://93.184.216.34/bucket/take.mp4")
            self.assertEqual(request.headers["Host"], "objects.example.test")
            self.assertEqual(request.extensions["sni_hostname"], "objects.example.test")
            for name in ("Authorization", "Cookie", "Referer", "Idempotency-Key"):
                self.assertNotIn(name, request.headers)
            return httpx.Response(200, content=data)
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "take.mp4"
            result = self.run_client(handler, resolver=self.public_dns, command="download",
                path="/v1/artifacts/take/content", output=target, max_bytes=100,
                sha256=hashlib.sha256(data).hexdigest())
            self.assertTrue(json.loads(result)["verified"])
            self.assertEqual(target.read_bytes(), data)
            self.assertNotIn("private-grant", result)
            self.assertNotIn("objects.example.test", result)
        self.assertEqual(len(seen), 2)
        self.assertIn("Authorization", seen[0].headers)

    def test_download_rejects_unsafe_or_unsigned_storage_target_before_second_request(self):
        for location in ("http://objects.example.test/file?sig=private-grant",
                         "https://user:password@objects.example.test/file?sig=private-grant",
                         "https://objects.example.test:8443/file?sig=private-grant",
                         "https://objects.example.test/file", "/relative?sig=private-grant",
                         "https://objects.example.test/file?sig=private-grant#fragment",
                         "https://objects.example.test\\other/file?sig=private-grant"):
            seen = []
            def handler(request):
                seen.append(request)
                return httpx.Response(307, headers={"Location": location})
            with tempfile.TemporaryDirectory() as folder, self.subTest(location=location):
                with self.assertRaises(ValueError) as error:
                    self.run_client(handler, resolver=self.public_dns, command="download",
                        path="/v1/artifacts/take/content", output=Path(folder)/"take.mp4", max_bytes=100, sha256=None)
                self.assertNotIn("private-grant", str(error.exception))
                self.assertEqual(len(seen), 1)

    def test_download_rejects_private_mixed_and_multicast_dns(self):
        for addresses in (["127.0.0.1"], ["10.2.3.4"], ["169.254.169.254"], ["::1"],
                          ["::ffff:127.0.0.1"], ["224.0.0.1"], ["93.184.216.34", "192.168.1.1"]):
            def resolver(*args, **kwargs):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses]
            with self.subTest(addresses=addresses), self.assertRaises(ValueError):
                skill.signed_download_target("https://storage.example.test/file?sig=hidden", resolver)

    def test_download_does_not_follow_second_redirect_or_leak_storage_exception(self):
        for fail in (False, True):
            seen = []
            def handler(request):
                seen.append(request)
                if len(seen) == 1:
                    return httpx.Response(307, headers={"Location": "https://storage.example.test/file?sig=private-grant"})
                if fail:
                    raise httpx.ConnectError("failed URL contains private-grant", request=request)
                return httpx.Response(307, headers={"Location": "https://other.example.test/file?sig=second-grant"})
            with tempfile.TemporaryDirectory() as folder, self.subTest(network_error=fail):
                with self.assertRaises(ValueError) as error:
                    self.run_client(handler, resolver=self.public_dns, command="download",
                        path="/v1/artifacts/take/content", output=Path(folder)/"take.mp4", max_bytes=100, sha256=None)
                self.assertNotIn("private-grant", str(error.exception))
                self.assertNotIn("second-grant", str(error.exception))
                self.assertEqual(len(seen), 2)

    def test_non_content_download_cannot_authorize_external_redirect(self):
        seen = []
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(ValueError):
            self.run_client(lambda request: seen.append(request), command="download", path="/v1/projects",
                output=Path(folder)/"file", sha256=None, max_bytes=100)
        self.assertEqual(seen, [])

    def test_mismatched_checksum_and_excess_bytes_fail_without_claiming_success(self):
        for maximum, digest in ((2, None), (100, "0"*64)):
            with tempfile.TemporaryDirectory() as folder, self.assertRaises(ValueError):
                self.run_client(lambda _: httpx.Response(200, content=b"bytes"), command="download",
                    path="/v1/artifacts/result/content", output=Path(folder)/"take", sha256=digest, max_bytes=maximum)

    def test_existing_output_prevents_network_write(self):
        seen = []
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"receipt.json"
            path.write_text("preserve", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                self.run_client(lambda request: seen.append(request), method="POST", path="/v1/jobs", output=path)
            self.assertEqual(path.read_text(), "preserve")
            self.assertEqual(seen, [])

    def test_cli_accepts_patch(self):
        with patch.object(sys, "argv", ["sixnine", "--base-url", "https://studio.example.test", "request", "PATCH", "/v1/projects/p/entities/s"]), patch.object(skill, "run") as run:
            self.assertEqual(skill.main(), 0)
            self.assertEqual(run.call_args.args[0].method, "PATCH")

    def test_resume_upload_uses_only_original_receipt_once(self):
        seen = []
        def handler(request):
            seen.append((request.method, request.url.path))
            if request.method == "GET":
                self.assertEqual(request.url.params["client_project_id"], "project-one")
                return httpx.Response(200, json={"assets": [{"asset_id": "original", "client_asset_id": "logical-input", "status": "validating"}]})
            return httpx.Response(200, json={"asset_id": "original", "status": "ready"})
        value = json.loads(self.run_client(handler, command="resume-upload", project="project-one", asset_id="logical-input"))
        self.assertEqual(value["asset_id"], "original")
        self.assertEqual(seen, [("GET", "/v1/assets"), ("POST", "/v1/assets/original/resume")])

    def test_upload_validation_error_explains_constraints_without_echoing_response_or_retrying(self):
        seen = []
        def handler(request):
            seen.append((request.method, request.url.path))
            return httpx.Response(422, json={"detail": "PRIVATE filename and synthetic-test-key"})
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/"small.mp4"
            source.write_bytes(b"representative invalid media")
            with self.assertRaises(ValueError) as error:
                self.run_client(handler, command="upload", project="project-one", asset_id="original-input", file=source)
            message = str(error.exception)
            self.assertIn("upload_constraints", message)
            self.assertIn("original receipt", message)
            self.assertIn("do not upload a new ID blindly", message)
            self.assertNotIn("PRIVATE", message)
            self.assertNotIn("synthetic-test-key", message)
            self.assertEqual(seen, [("POST", "/v1/assets")])
            self.assertEqual(source.read_bytes(), b"representative invalid media")

    def test_resume_ready_is_read_only_and_missing_receipt_does_not_reupload(self):
        for ready in (True, False):
            seen = []
            def handler(request):
                seen.append(request.method)
                return httpx.Response(200, json={"assets": [{"asset_id": "original", "client_asset_id": "logical-input", "status": "ready"}] if ready else []})
            if ready:
                self.run_client(handler, command="resume-upload", project="project-one", asset_id="logical-input")
            else:
                with self.assertRaises(ValueError):
                    self.run_client(handler, command="resume-upload", project="project-one", asset_id="logical-input")
            self.assertEqual(seen, ["GET"])

    def test_poll_respects_retry_after_and_is_get_only(self):
        moment, seen, sleeps = [0.0], [], []
        def sleep(seconds):
            sleeps.append(seconds)
            moment[0] += seconds
        def handler(request):
            seen.append((request.method, request.url.path))
            if len(seen) == 1:
                return httpx.Response(429, headers={"Retry-After": "7"})
            return httpx.Response(200, json={"id": "original-job", "status": "succeeded"})
        value = json.loads(self.run_client(handler, command="poll", job="original-job", max_wait=20,
            interval=2, clock=lambda:moment[0], sleep=sleep))
        self.assertEqual(value["poll_status"], "stopped")
        self.assertEqual(value["job"]["status"], "succeeded")
        self.assertEqual(sleeps, [7])
        self.assertEqual(seen, [("GET", "/v1/jobs/original-job")]*2)

    def test_poll_returns_waiting_when_server_delay_exceeds_limit(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(503, headers={"Retry-After": "90"})
        value = json.loads(self.run_client(handler, command="poll", job="original-job", max_wait=20,
            interval=2, clock=lambda:0, sleep=lambda _: self.fail("must not sleep beyond bound")))
        self.assertEqual(value, {"job": None, "poll_status": "waiting", "retry_after_seconds": 90})
        self.assertEqual(len(seen), 1)

    def test_poll_unknown_submission_stops_without_reposting_or_auto_adopting(self):
        for status in ("submission_unknown", "recovery_hold", "failed", "cancelled"):
            seen = []
            def handler(request):
                seen.append(request.method)
                return httpx.Response(200, json={"id": "original-job", "status": status})
            value = json.loads(self.run_client(handler, command="poll", job="original-job", max_wait=20,
                interval=2, sleep=lambda _: self.fail("reconciliation must stop polling")))
            self.assertEqual(value["job"]["status"], status)
            self.assertEqual(seen, ["GET"])


if __name__ == "__main__":
    unittest.main()
