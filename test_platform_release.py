"""Host release contract tests using inert flat bundles, no service starts."""
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest.mock import patch
import sys

DIRECTORY = Path(__file__).resolve().parent / "deploy" / "platform"
COMMIT = "a"*40


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, DIRECTORY / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


validator = module("sixnine_release_test_validator", "check_config.py")
with patch.dict(sys.modules, {"check_config": validator}):
    release = module("sixnine_release_test", "release.py")


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.incoming = self.root / "incoming" / COMMIT
        self.incoming.mkdir(parents=True)
        (self.root / "releases").mkdir()
        hashes = {}
        for name in release.FILES:
            content = b"INERT TEST BUNDLE - never execute"
            (self.incoming / name).write_bytes(content)
            hashes[name] = hashlib.sha256(content).hexdigest()
        self.value = {"commit": COMMIT, "image": "sixnine-platform:"+COMMIT, "image_id": "sha256:"+"b"*64, "files": hashes}
        self.manifest()

    def manifest(self):
        (self.incoming / "release-manifest.json").write_text(json.dumps(self.value), encoding="utf-8")

    def test_exact_commit_and_file_manifest(self):
        self.assertEqual(release.manifest(self.incoming, COMMIT), self.value)
        for commit in ("../escape", "a"*39, "a"*40+"\n"):
            with self.assertRaises(release.ReleaseError):
                release.manifest(self.incoming, commit)
        self.value["files"]["another-file"] = "a"*64
        self.manifest()
        with self.assertRaises(release.ReleaseError):
            release.manifest(self.incoming, COMMIT)

    def test_tamper_rejected_before_publishing_release(self):
        (self.incoming / "compose.yaml").write_text("TAMPERED", encoding="utf-8")
        with self.assertRaises(release.ReleaseError):
            release.prepare_bundle(self.root, COMMIT)
        self.assertFalse((self.root / "releases" / COMMIT).exists())

    def test_publish_is_atomic_and_no_incoming_controller_copied(self):
        (self.incoming / "release.py").write_text("never copy", encoding="utf-8")
        result = release.prepare_bundle(self.root, COMMIT)
        self.assertEqual({p.name for p in result.iterdir()}, release.FILES | {"release-manifest.json"})
        self.assertEqual(release.manifest(result, COMMIT), self.value)
        self.assertEqual(list((self.root / "releases").iterdir()), [result])

    def test_interrupted_copy_does_not_poison_retry(self):
        original = release.os.fsync
        with patch.object(release.os, "fsync", side_effect=OSError("synthetic interruption")):
            with self.assertRaises(OSError):
                release.prepare_bundle(self.root, COMMIT)
        self.assertEqual(list((self.root / "releases").iterdir()), [])
        self.assertTrue(release.prepare_bundle(self.root, COMMIT).is_dir())

    def test_hardlink_input_rejected(self):
        path = self.incoming / "compose.yaml"
        other = self.root / "linked"
        os.link(path, other)
        with self.assertRaises(release.ReleaseError):
            release.manifest(self.incoming, COMMIT)

    def test_nonsecret_site_config_never_inherits_environment(self):
        site = self.root / "site.env"
        site.write_text("\n".join([
            "SIXNINE_IMAGE=old-tag",
            "SIXNINE_POSTGRES_IMAGE=postgres@sha256:"+"1"*64,
            "SIXNINE_CADDY_IMAGE=caddy@sha256:"+"2"*64,
            "SIXNINE_DB_ADMIN_SECRET_FILE=/run/sixnine-secrets/db_admin_password",
            "SIXNINE_APP_DSN_SECRET_FILE=/run/sixnine-secrets/app_database_url"]), encoding="utf-8")
        # Real POSIX ownership checked separately in Linux runtime; test does not
        # need root and never reads any real credential file.
        with patch.object(release, "regular"), patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "synthetic-do-not-inherit"}):
            env = release.deployment_environment(site, COMMIT)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertEqual(env["PATH"], "/usr/sbin:/usr/bin:/sbin:/bin")
        self.assertEqual(env["SIXNINE_IMAGE"], "sixnine-platform:"+COMMIT)
        with site.open("a", encoding="utf-8") as output:
            output.write("\nAWS_SECRET_ACCESS_KEY=synthetic-only")
        with patch.object(release, "regular"), self.assertRaises(release.ReleaseError):
            release.deployment_environment(site, COMMIT)

    def test_ci_uses_platform_and_installed_controller(self):
        text = (DIRECTORY.parents[1] / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn('build_platform_release.py "$GITHUB_SHA"', text)
        self.assertIn("sudo -n /opt/sixnine-release/release.py", text)
        self.assertNotIn("sudo -n bash '$target/", text)

    def test_compose_serialization_version_comes_only_from_trusted_docker(self):
        environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "SIXNINE_IMAGE": "fixture-app",
                       "SIXNINE_POSTGRES_IMAGE": "fixture-db", "SIXNINE_CADDY_IMAGE": "fixture-proxy"}
        rendered = {"untrusted_bundle_claim": "5.5.1", "services": {
            name: {"image": environment[key]} for name, key in (
                ("app", "SIXNINE_IMAGE"), ("db", "SIXNINE_POSTGRES_IMAGE"), ("caddy", "SIXNINE_CADDY_IMAGE"))}}
        with patch.object(release, "command", return_value=b"v2.38.2\n") as command, \
             patch.object(release, "compose", return_value=json.dumps(rendered)) as compose, \
             patch.object(release, "validate") as validate:
            release.approved_configuration(self.incoming, environment)
        command.assert_called_once_with(["compose", "version", "--short"], environment=environment)
        compose.assert_called_once_with(self.incoming, environment, "config", "--format", "json")
        validate.assert_called_once_with(rendered, deployment_directory=self.incoming, compose_version="v2.38.2")

    def archive(self, *, extra=False, unsafe=None):
        filename = self.root / "test-image.tar.gz"
        config = "b"*64+".json"
        record = {"Config": config, "RepoTags": [self.value["image"]], "Layers": []}
        with tarfile.open(filename, "w:gz") as output:
            for name, value in ((config, {}), ("manifest.json", [record, record] if extra else [record])):
                data = json.dumps(value).encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                output.addfile(info, io.BytesIO(data))
            if unsafe:
                info = tarfile.TarInfo(unsafe)
                output.addfile(info, io.BytesIO(b""))
        return filename

    def test_image_archive_rejects_extra_images_before_load(self):
        release.validate_image_archive(self.archive(), self.value)
        with self.assertRaises(release.ReleaseError):
            release.validate_image_archive(self.archive(extra=True), self.value)
        with self.assertRaises(release.ReleaseError):
            release.validate_image_archive(self.archive(unsafe="../other"), self.value)

    def test_incoming_manifest_is_not_its_own_approval(self):
        approval_dir = self.root / "approved-releases"
        approval_dir.mkdir()
        with self.assertRaisesRegex(release.ReleaseError, "independent_release_approval_missing"):
            release.approved_manifest(self.root, self.incoming, COMMIT)
        file = approval_dir / (COMMIT+".sha256")
        file.write_text(release.checksum(self.incoming / "release-manifest.json"), encoding="ascii")
        # CI is unprivileged; emulate the independently provisioned root dir.
        with patch.object(release, "regular"), patch.object(release.os, "name", "nt"):
            release.approved_manifest(self.root, self.incoming, COMMIT)
            self.value["image_id"] = "sha256:"+"c"*64
            self.manifest()
            with self.assertRaises(release.ReleaseError):
                release.approved_manifest(self.root, self.incoming, COMMIT)


class ApplyReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old, self.older = "c"*40, "d"*40
        self.calls = []
        self.expected = {"commit": COMMIT, "image": "sixnine-platform:"+COMMIT, "image_id": "sha256:"+"1"*64}
        self.env = {"SIXNINE_IMAGE": "sixnine-platform:"+COMMIT, "SIXNINE_POSTGRES_IMAGE": "postgres@sha256:"+"2"*64,
                    "SIXNINE_CADDY_IMAGE": "caddy@sha256:"+"3"*64}
        def compose(directory, env, *args, **kwargs):
            self.calls.append((str(directory), args))
            if args[:1] == ("config",):
                return json.dumps({"services": {"app": {"image": env["SIXNINE_IMAGE"]},
                    "db": {"image": env["SIXNINE_POSTGRES_IMAGE"]}, "caddy": {"image": env["SIXNINE_CADDY_IMAGE"]}}}).encode()
            return b""
        changes = {
            "approved_manifest": lambda *args: None,
            "prepare_bundle": lambda root, commit: root / "releases" / commit,
            "deployment_environment": lambda path, commit: {**self.env, "SIXNINE_IMAGE": "sixnine-platform:"+commit},
            "manifest": lambda *args: self.expected,
            "load_approved_image": lambda *args: self.calls.append(("load", args[2])) or self.expected,
            "regular": lambda *args, **kwargs: None,
            "validate": lambda *args, **kwargs: None,
            "command": lambda *args, **kwargs: b"v5.5.1\n",
            "compose": compose,
            "wait_ready": lambda *args, **kwargs: None,
            "verify_running_app": lambda *args: None,
            "wait_proxy_stable": lambda *args: None,
        }
        for name, replacement in changes.items():
            patched = patch.object(release, name, replacement)
            patched.start()
            self.addCleanup(patched.stop)

    def state(self, value=None):
        path = self.root / "release-state.json"
        if value is not None:
            value.setdefault("dependencies", release.pinned_dependencies(self.env))
            path.write_text(json.dumps(value), encoding="utf-8")
        return json.loads(path.read_text()) if path.exists() else None

    def test_success_and_same_commit_retry_keep_previous(self):
        self.state({"current": self.old, "previous": self.older, "status": "app_ready"})
        release.apply_locked(self.root, COMMIT)
        self.assertEqual(self.state()["previous"], self.old)
        previous = self.state()
        count = len(self.calls)
        release.apply_locked(self.root, COMMIT)
        self.assertEqual(self.state(), previous)
        self.assertEqual(len(self.calls), count+1)  # Read-only Compose config only.

    def test_proxy_failure_rolls_back_and_reloads_approved_old_image(self):
        self.state({"current": self.old, "previous": self.older, "status": "app_ready"})
        with patch.object(release, "wait_proxy_stable", side_effect=[release.ReleaseError("proxy_failed"), None]):
            with self.assertRaises(release.ReleaseError):
                release.apply_locked(self.root, COMMIT)
        self.assertEqual(self.state()["current"], self.old)
        self.assertEqual(self.state()["previous"], self.older)
        self.assertEqual(self.state()["status"], "rolled_back_app_only")
        self.assertEqual([call for call in self.calls if call[0] == "load"], [("load", COMMIT), ("load", self.old)])
        self.assertFalse(any("down" in args for _, args in self.calls))

    def test_first_failure_records_pending_and_requires_reconciliation(self):
        with patch.object(release, "wait_ready", side_effect=release.ReleaseError("first_failed")):
            with self.assertRaises(release.ReleaseError):
                release.apply_locked(self.root, COMMIT)
        self.assertIsNone(self.state()["current"])
        self.assertEqual(self.state()["pending"], COMMIT)
        self.assertEqual(self.state()["status"], "failed_needs_reconciliation")
        with self.assertRaisesRegex(release.ReleaseError, "another_release_requires_reconciliation"):
            release.apply_locked(self.root, self.old)

    def test_rollback_failure_never_claims_ready(self):
        self.state({"current": self.old, "previous": self.older, "status": "app_ready"})
        with patch.object(release, "wait_ready", side_effect=release.ReleaseError("both_failed")):
            with self.assertRaisesRegex(release.ReleaseError, "release_and_rollback_failed"):
                release.apply_locked(self.root, COMMIT)
        self.assertEqual(self.state()["status"], "rollback_failed_needs_reconciliation")
        self.assertEqual(self.state()["pending"], COMMIT)

    def test_host_crash_after_pending_can_resume_same_commit(self):
        self.state({"current": self.old, "previous": self.older, "pending": COMMIT, "status": "deploying"})
        release.apply_locked(self.root, COMMIT)
        self.assertEqual(self.state()["current"], COMMIT)
        self.assertEqual(self.state()["previous"], self.old)
        self.assertNotIn("pending", self.state())

    def test_dependency_change_cannot_be_smuggled_into_app_release_or_rollback(self):
        self.state({"current": self.old, "previous": self.older, "status": "app_ready"})
        before = self.state()
        with patch.object(release, "deployment_environment", return_value={**self.env,
                "SIXNINE_CADDY_IMAGE": "caddy@sha256:"+"f"*64}):
            with self.assertRaisesRegex(release.ReleaseError, "dependency_change_requires_separate_maintenance"):
                release.apply_locked(self.root, COMMIT)
        self.assertEqual(self.state(), before)
        self.assertFalse(any(call[0] == "load" or "up" in call[1] for call in self.calls))


if __name__ == "__main__":
    unittest.main()
