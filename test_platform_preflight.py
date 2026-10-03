"""Synthetic host metadata only; never read a real secret or call a daemon."""
import copy
import importlib.util
from pathlib import Path, PurePosixPath
import stat
import unittest
from unittest.mock import patch


source = Path(__file__).parent/"deploy"/"platform"/"preflight_host.py"
spec = importlib.util.spec_from_file_location("sixnine_host_preflight_test", source)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def info(uid=0, gid=0, mode=0o755, *, regular=False):
    return {"mode": (stat.S_IFREG if regular else stat.S_IFDIR)|mode,
            "uid": uid, "gid": gid, "links": 1, "bytes": 64}


def valid_host():
    dirs = {name: info() for name in ("root", "incoming", "releases", "approved-releases")}
    dirs["incoming"] = info(gid=1200, mode=0o2770)
    for name in ("platform-data", "upload-spool"):
        dirs[name] = info(10001, 10001, 0o700)
    for name in ("secret-root", "docker-config", "postgres"):
        dirs[name] = info(mode=0o700)
    dirs["postgres"] = info(70, 70, 0o700)
    return {"platform": "posix", "euid": 0, "parents": [info(), info()], "directories": dirs,
            "docker_config_empty": True,
            "secrets": {"db_admin_password": info(mode=0o400, regular=True),
                        "app_database_url": info(gid=10001, mode=0o440, regular=True)},
            "secret_filesystems": dict.fromkeys(("secret-root", "db_admin_password", "app_database_url"), "tmpfs"),
            "docker": {"os": "linux", "cpus": 2, "memory_bytes": 8*1024**3}}


class PreflightTests(unittest.TestCase):
    def test_reviewed_two_core_host_is_accepted_without_process_or_secret_reads(self):
        with patch.object(preflight.subprocess, "run", side_effect=AssertionError("No process in pure validation")), \
             patch("builtins.open", side_effect=AssertionError("No content read")):
            result = preflight.validate_snapshot(valid_host())
        self.assertEqual(result["state"], "host_metadata_ready")
        self.assertEqual(result["cpu_count"], 2)
        self.assertFalse(result["secret_contents_checked"])
        self.assertFalse(result["services_started"])

    def test_actual_app_identity_and_secret_group_are_required(self):
        cases = [lambda s: s["directories"]["platform-data"].update(uid=0),
                 lambda s: s["directories"]["upload-spool"].update(mode=stat.S_IFDIR|0o777),
                 lambda s: s["secrets"]["app_database_url"].update(gid=0),
                 lambda s: s["secrets"]["db_admin_password"].update(mode=stat.S_IFREG|0o644),
                 lambda s: s["secrets"]["app_database_url"].update(links=2),
                 lambda s: s["secrets"]["db_admin_password"].update(bytes=0),
                 lambda s: s["directories"]["secret-root"].update(mode=stat.S_IFDIR|0o770)]
        for change in cases:
            value = valid_host()
            change(value)
            with self.subTest(change=change), self.assertRaises(preflight.PreflightError):
                preflight.validate_snapshot(value)

    def test_links_rootless_small_host_and_persistent_secrets_fail_closed(self):
        cases = [lambda s: s.update(euid=1000),
                 lambda s: s["parents"][0].update(mode=stat.S_IFLNK|0o755),
                 lambda s: s["directories"]["releases"].update(mode=stat.S_IFDIR|0o777),
                 lambda s: s["directories"]["postgres"].update(uid=10001, gid=10001),
                 lambda s: s["directories"]["postgres"].update(uid=0, gid=0),
                 lambda s: s["docker"].update(cpus=1),
                 lambda s: s["docker"].update(memory_bytes=4*1024**3),
                 lambda s: s["docker"].update(os="windows"),
                 lambda s: s["secret_filesystems"].update(app_database_url="ext4"),
                 lambda s: s.update(docker_config_empty=False)]
        for change in cases:
            value = valid_host()
            change(value)
            with self.subTest(change=change), self.assertRaises(preflight.PreflightError):
                preflight.validate_snapshot(value)

    def test_reviewed_postgres_data_identity_survives_first_boot(self):
        for uid in (70, 999):
            value = valid_host()
            value["directories"]["postgres"].update(uid=uid, gid=uid)
            self.assertEqual(preflight.validate_snapshot(value)["state"], "host_metadata_ready")

    def test_mountinfo_uses_deepest_mount_including_bind_mounted_secret(self):
        mounts = ("1 0 8:1 / / rw - ext4 /dev/root rw\n"
                  "2 1 0:20 / /run rw - tmpfs tmpfs rw\n"
                  "3 2 8:1 /private/secret /run/sixnine-secrets/app_database_url ro - ext4 /dev/root ro\n")
        self.assertEqual(preflight.filesystem_for(PurePosixPath("/run/sixnine-secrets"), mounts), "tmpfs")
        self.assertEqual(preflight.filesystem_for(PurePosixPath("/run/sixnine-secrets/app_database_url"), mounts), "ext4")
        self.assertEqual(preflight.filesystem_for(PurePosixPath("/runaway/secret"), mounts), "ext4")
        with self.assertRaises(preflight.PreflightError):
            preflight.filesystem_for(PurePosixPath("/run/secret"), "malformed")


if __name__ == "__main__":
    unittest.main()
