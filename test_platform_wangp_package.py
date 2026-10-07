"""Offline package boundaries; no resolver, installer, upstream import or GPU."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from studio_platform.runtime_hosts import wangp_environment as env

ROOT = Path(__file__).resolve().parent


def load_file(name):
    spec = importlib.util.spec_from_file_location("test_wangp_" + name, ROOT / "deploy/wangp" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = load_file("bootstrap")
package = load_file("package_tool")


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "upstream"
        self.source.mkdir()
        (self.source / "wgp.py").write_bytes(b"# pinned source\n")
        self.lock = {
            "format": env.FORMAT, "python": env.PYTHON, "source_revision": env.REVISION,
            "requirements_sha256": env.REQUIREMENTS_SHA,
            "base_image": "example/runtime@sha256:" + "a" * 64,
            "requirements_lock_sha256": "b" * 64,
            "source_files": {"wgp.py": {"size_bytes": 16, "sha256": env.sha_file(self.source / "wgp.py")}},
            "wheels": [{"file": "example-1.0-py3-none-any.whl", "name": "example", "version": "1.0",
                        "size_bytes": 1, "sha256": "c" * 64}],
            "installed_packages": {"example": "1.0"}, "system_packages": {"libc6:amd64": "2.36-9"},
        }

    def test_source_hash_and_extra_executable_file_are_enforced(self):
        env.verify_source(self.source, self.lock)
        (self.source / "shadow.py").write_text("pass", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "untracked_code"):
            env.verify_source(self.source, self.lock)
        (self.source / "wgp.py").write_bytes(b"# changed source")
        with self.assertRaisesRegex(ValueError, "source_modified"):
            env.verify_source(self.source, self.lock)

    def test_wheel_paths_cannot_escape_dependency_directory(self):
        for name in ("../evil.whl", "sub/evil.whl", "C:/evil.whl", "evil\\a.whl"):
            value = copy.deepcopy(self.lock)
            value["wheels"][0]["file"] = name
            with self.subTest(name=name), self.assertRaises(ValueError):
                env.validate_lock(value)

    def test_full_package_and_system_versions_must_match(self):
        with patch.object(env.platform, "system", return_value="Linux"), \
                patch.object(env.platform, "machine", return_value="x86_64"), \
                patch.object(env.platform, "python_version", return_value=env.PYTHON), \
                patch.object(env, "installed_packages", return_value=self.lock["installed_packages"]), \
                patch.object(env, "system_packages", return_value=self.lock["system_packages"]):
            self.assertFalse(env.verify_environment(self.lock)["inference_verified"])
            with patch.object(env, "system_packages", return_value={**self.lock["system_packages"], "openssh-server": "1.0"}):
                self.assertEqual(env.verify_environment(self.lock)["additional_system_packages"], ["openssh-server"])
            with patch.object(env, "installed_packages", return_value={"example": "2.0"}):
                with self.assertRaisesRegex(ValueError, "dependency_mismatch"):
                    env.verify_environment(self.lock)
            with patch.object(env, "system_packages", return_value={"libc6:amd64": "other"}):
                with self.assertRaisesRegex(ValueError, "system_mismatch"):
                    env.verify_environment(self.lock)

    def test_nested_vendor_metadata_does_not_change_wheel_identity(self):
        wheel = self.root / "example.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("setuptools-84.0.0.dist-info/METADATA", "Name: setuptools\nVersion: 84.0.0\n")
            archive.writestr("setuptools/_vendor/example-1.0.dist-info/METADATA", "Name: example\nVersion: 1.0\n")
        self.assertEqual(package.wheel_metadata(wheel)["Name"], "setuptools")

    def test_environment_lock_is_bound_before_source_or_package_verification(self):
        (self.source / ".sixnine-environment.json").write_bytes(env.canonical(self.lock))
        with patch.object(env, "verify_source") as source, patch.object(env, "verify_environment"):
            with self.assertRaisesRegex(ValueError, "binding_mismatch"):
                env.verify_bound_environment(self.source, {"runtime_digest_kind": "sixnine-environment-lock-sha256",
                                                           "runtime_digest": "0" * 64})
            source.assert_not_called()

    def test_actual_python_patch_is_frozen_and_deb_escape_is_rejected(self):
        lock = copy.deepcopy(self.lock)
        lock["python"] = "3.11.13"
        env.validate_lock(lock)
        with patch.object(env.platform, "system", return_value="Linux"), \
                patch.object(env.platform, "machine", return_value="x86_64"), \
                patch.object(env.platform, "python_version", return_value="3.11.14"):
            with self.assertRaisesRegex(ValueError, "platform_mismatch"):
                env.verify_environment(lock)
        lock["debs"] = [{"file": "../libc.deb", "name": "libc6", "version": "2.36", "architecture": "amd64",
                         "size_bytes": 1, "sha256": "a" * 64}]
        with self.assertRaises(ValueError):
            env.validate_lock(lock)

    def archive(self, member, *, kind=tarfile.REGTYPE, size=1):
        path = self.root / ("input-" + str(len(list(self.root.glob("input-*")))) + ".tar.gz")
        with tarfile.open(path, "w:gz") as target:
            item = tarfile.TarInfo(member)
            item.type = kind
            item.size = size if kind == tarfile.REGTYPE else 0
            item.linkname = "outside" if kind != tarfile.REGTYPE else ""
            target.addfile(item, io.BytesIO(b"x" * size) if kind == tarfile.REGTYPE else None)
        return path

    def test_archive_traversal_links_and_limits_rejected(self):
        for index, (name, kind, size, limit) in enumerate([
            ("../escape", tarfile.REGTYPE, 1, 10), ("/escape", tarfile.REGTYPE, 1, 10),
            ("link", tarfile.SYMTYPE, 0, 10), ("hardlink", tarfile.LNKTYPE, 0, 10),
            ("large", tarfile.REGTYPE, 11, 10),
        ]):
            with self.subTest(name=name), self.assertRaises(ValueError):
                bootstrap.extract(self.archive(name, kind=kind, size=size), self.root / f"out{index}", maximum=limit)

    def config(self):
        value = json.loads((ROOT / "deploy/wangp/runtime-config.template.json").read_text(encoding="utf-8"))
        for key in ("install_root", "source_bundle_path", "manifest_path", "model_root", "config_path", "status_path",
                    "dependency_artifact_path"):
            value[key] = str(self.root / key)
        value["config_path"] = str(self.root / "wgp_config.json")
        value.update(source_bundle_sha256="a" * 64, dependency_artifact_sha256="b" * 64)
        return value

    def test_unknown_existing_bootstrap_is_never_relaunched_or_status_overwritten(self):
        value = self.config()
        base = Path(value["install_root"])
        base.mkdir()
        marker = base / "wangp-bootstrap-started.json"
        marker.write_text('{"slot_key":"original"}', encoding="utf-8")
        status = Path(value["status_path"])
        status.write_text('{"state":"unknown"}', encoding="utf-8")
        with patch.object(bootstrap.subprocess, "Popen") as popen, patch.object(bootstrap, "download") as download:
            result = bootstrap.install(value, "new-slot", str(self.root / "token"))
        self.assertEqual(result["state"], "reconcile_required")
        self.assertEqual(json.loads(marker.read_text())["slot_key"], "original")
        self.assertEqual(json.loads(status.read_text())["state"], "unknown")
        popen.assert_not_called()
        download.assert_not_called()

    def test_unsigned_config_and_dependency_selection_fail_closed(self):
        invalid = self.config()
        invalid["config_path"] = str(self.root / "wgp-config.json")
        with self.assertRaisesRegex(ValueError, "filename_invalid"):
            bootstrap.validate_config(invalid)
        value = self.config()
        value.update(dependency_artifact_path="", dependency_artifact_url="https://example.invalid/file?token=secret")
        with self.assertRaisesRegex(ValueError, "unsigned_https"):
            bootstrap.validate_config(value)
        value = self.config()
        value["prepared_root"] = str(self.root)
        with self.assertRaisesRegex(ValueError, "one_dependency_source"):
            bootstrap.validate_config(value)

    def test_source_bundle_is_explicit_and_contains_no_configuration_or_credentials(self):
        output = self.root / "source.tar.gz"
        receipt = package.small_bundle(output)
        self.assertEqual(receipt["sha256"], env.sha_file(output))
        self.assertLess(receipt["size_bytes"], bootstrap.MAX_SOURCE)
        with tarfile.open(output) as source:
            self.assertEqual(set(source.getnames()), set(package.PRIVATE_FILES))
            self.assertTrue(all(item.isfile() for item in source))
        self.assertTrue(all(name.endswith(".py") for name in package.PRIVATE_FILES))

    def test_bound_manifest_is_new_identity_and_never_claims_inference(self):
        lock_file = self.root / "environment.json"
        lock_file.write_bytes(env.canonical(self.lock))
        output = self.root / "manifest.json"
        image = "example/runtime@sha256:" + "d" * 64
        result = package.bind_manifest(ROOT / "deploy/wangp/manifest.json", lock_file, output, image)
        value = json.loads(output.read_text())
        self.assertEqual(value["runtime_digest"], env.digest(self.lock))
        self.assertEqual(value["runtime_recipe"]["full_dependency_lock"], env.digest(self.lock))
        self.assertFalse(value["inference_qualified"])
        self.assertFalse(result["inference_verified"])
        with self.assertRaises(FileExistsError):
            package.bind_manifest(ROOT / "deploy/wangp/manifest.json", lock_file, output, image)


if __name__ == "__main__":
    unittest.main()
