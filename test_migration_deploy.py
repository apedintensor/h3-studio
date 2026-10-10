"""Real Compose config parsing and structural failures; no containers started."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.check_migration_deploy import ROOT, check, local_inputs, main, validate


def fixture_environment():
    environment = dict(os.environ)
    names = ("SIXNINE_POSTGRES_IMAGE", "SIXNINE_CADDY_IMAGE", "SIXNINE_IMAGE", "SIXNINE_DISPATCH_IMAGE")
    environment.update({name: "test/never-pulled@sha256:"+"a"*64 for name in names})
    for name in ("SIXNINE_DB_ADMIN_SECRET_FILE", "SIXNINE_APP_DSN_SECRET_FILE",
        "SIXNINE_EXECUTION_PROFILES_SECRET_FILE", "SIXNINE_GRAFANA_CONFIG_FILE", "SIXNINE_DSTACK_OPERATOR_CONFIG_FILE",
        "SIXNINE_DSTACK_RUNTIME_CONFIG_FILE", "SIXNINE_DSTACK_TOKEN_FILE", "SIXNINE_DSTACK_DSN_FILE",
        "SIXNINE_DSTACK_SSH_PUBLIC_KEY_FILE", "SIXNINE_GPU_SSH_KEY_FILE", "SIXNINE_HATCHET_BROKER_CONFIG_FILE",
        "SIXNINE_HATCHET_TOKEN_FILE", "SIXNINE_DSTACK_STATE_DIR", "SIXNINE_DSTACK_SERVER_CONFIG_FILE",
        "SIXNINE_DSTACK_RUNTIME_DIR", "SIXNINE_DSTACK_SOURCE_DIR", "SIXNINE_HATCHET_SECRET_DIR"):
        environment[name] = "/never-opened-fixtures/"+name.lower()
    environment.update(SIXNINE_HATCHET_POSTGRES_VOLUME="existing-hatchet-postgres",
        SIXNINE_HATCHET_CONFIG_VOLUME="existing-hatchet-config", SIXNINE_OPERATOR_CAPACITY_OWNERS="superdan,supervan",
        SIXNINE_MIGRATION_GENERATION_ENABLED="0", SIXNINE_MIGRATION_EXECUTION_BACKEND="disabled")
    return environment


class MigrationDeployTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("docker"):
            raise unittest.SkipTest("Docker Compose CLI required for offline configuration parsing")
        import subprocess
        def capture(command, **kwargs):
            result = subprocess.run(command, **kwargs)
            if result.returncode == 0:
                cls.document = json.loads(result.stdout)
            return result
        cls.receipt = check(ROOT / "deploy/platform/compose.yaml", ROOT / "deploy/dstack/platform-overlay.yaml",
            environment=fixture_environment(), runner=capture)

    def assert_rejected(self, mutate, code):
        value = copy.deepcopy(self.document); mutate(value)
        with self.assertRaisesRegex(ValueError, "^"+code+"$"):
            validate(value)

    def test_real_compose_merge_shared_namespace_pins_and_memory(self):
        self.assertEqual(self.receipt["service_memory_cap_mib"], 3296)
        self.assertGreaterEqual(self.receipt["host_reserve_mib"], 512)
        self.assertEqual(self.receipt["gpu_operations"], 0)
        services = self.document["services"]
        self.assertEqual(services["dstack"]["entrypoint"], ["python", "-c"])
        self.assertEqual(services["hatchet-db"]["networks"], {"database": None})
        self.assertEqual(services["controller"]["command"], ["python", "-m", "studio_platform.dstack_controller"])
        self.assertIn("app_database_url", {s["source"] for s in services["app"]["secrets"]})
        self.assertIn("google_titles", {s["source"] for s in services["app"]["secrets"]})

    def test_loopback_split_or_public_management_ports_rejected(self):
        self.assert_rejected(lambda d: d["services"]["controller"].update(network_mode="bridge"), "migration_loopback_namespace_split")
        self.assert_rejected(lambda d: d["services"]["dstack"].update(ports=["3000:3000"]), "migration_private_port_published")

    def test_presence_based_raw_otel_and_secret_environment_rejected(self):
        self.assert_rejected(lambda d: d["services"]["dstack"]["environment"].update(DSTACK_OTEL_TRACES_ENABLED="0"),
                             "migration_raw_dstack_telemetry_enabled")
        self.assert_rejected(lambda d: d["services"]["dstack"]["environment"].update(DSTACK_SERVER_ADMIN_TOKEN="SECRET"),
                             "migration_secret_value_in_compose")
        self.assert_rejected(lambda d: d["services"]["dstack"]["environment"].update(DSTACK_SERVER_LOG_LEVEL="INFO"),
                             "migration_dstack_auth_invalid")

    def test_existing_broker_volumes_and_business_ledger_cannot_be_replaced(self):
        self.assert_rejected(lambda d: d["volumes"]["hatchet-postgres"].update(external=False), "migration_blank_broker_volume")
        self.assert_rejected(lambda d: d["services"]["controller"]["environment"].update(SIXNINE_DATABASE_URL_FILE="/tmp/new.db"),
                             "migration_business_authority_split")
        self.assert_rejected(lambda d: d["services"]["hatchet"]["environment"].update(SERVER_ALLOW_SIGNUP="true"),
                             "migration_hatchet_auth_invalid")

    def test_unpinned_images_gpu_access_and_memory_overcommit_rejected(self):
        self.assert_rejected(lambda d: d["services"]["controller"].update(image="test:latest"), "migration_image_unpinned")
        self.assert_rejected(lambda d: d["services"]["controller"].update(gpus="all"), "migration_unsafe_service")
        self.assert_rejected(lambda d: d["services"]["controller"].update(mem_limit="2g"), "migration_host_memory_overcommitted")

    def test_runtime_keys_config_and_cloud_mounts_are_not_optional(self):
        self.assert_rejected(lambda d: d["services"]["controller"].update(volumes=[]), "migration_assets_split")
        self.assert_rejected(lambda d: d["services"]["controller"]["environment"].update(SIXNINE_TELEMETRY_CONFIG_FILE=""),
                             "migration_cloud_telemetry_missing")

    def test_checker_only_config_then_explicit_read_probe_no_secret_output(self):
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            if "config" in command:
                return SimpleNamespace(returncode=0, stdout=json.dumps(self.document), stderr="SECRET should not print")
            self.assertEqual(command[-5:-1], ["-T", "app", "python", "-c"])
            return SimpleNamespace(returncode=0, stdout="migration_private_probe_passed\n", stderr="SECRET")
        output = check(ROOT / "deploy/platform/compose.yaml", ROOT / "deploy/dstack/platform-overlay.yaml", runner=runner)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("SECRET", json.dumps(output))
        self.assertIn("--env-file", calls[0])
        check(ROOT / "deploy/platform/compose.yaml", ROOT / "deploy/dstack/platform-overlay.yaml", runner=runner, probe=True)
        self.assertFalse(any(any(action in cmd for action in ("up", "pull", "stop", "rm", "create")) for cmd in calls))

    def test_cli_masks_raw_compose_errors_and_missing_mounts(self):
        output = io.StringIO()
        with patch("tools.check_migration_deploy.check", side_effect=RuntimeError("SECRET DSN")), contextlib.redirect_stdout(output):
            self.assertEqual(main([]), 1)
        self.assertEqual(json.loads(output.getvalue()), {"state": "failed", "code": "migration_deploy_check_failed"})
        with self.assertRaises(OSError):
            local_inputs(self.document)


if __name__ == "__main__":
    unittest.main()
