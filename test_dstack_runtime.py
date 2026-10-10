"""Original ledger/native contracts with fake SSH; no provider/GPU operations."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import func, select

from studio_platform.auth import Principal
from studio_platform.dstack_capacity import DstackCapacity, DstackError
from studio_platform.dstack_operator import DstackOperator, LedgerDstackStore
from studio_platform.dstack_runtime import DstackNativeRuntime, DstackRuntimeConfig
from studio_platform.inference.wangp_contract import HostReadiness
from studio_platform.repository import registered_workers, scaler_receipts
from studio_platform.runtime_catalog import engine_manifest
from studio_platform.runtime_hosts.wangp_http import private_token_file
from test_dstack_operator import Client, PROFILE, PUB
from test_platform_repository import LedgerCase
from tools.build_operator_sources import _runtime

GPU = "GPU-12345678-1234-1234-1234-123456789abc"


class NativeRuntimeTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        self.repo.configure_pool("dstack-test", max_instances=3, max_physical_gpus=3)
        settings = SimpleNamespace(tenant_id=self.scope.tenant_id, operator_capacity_owners=("superdan",))
        self.store = LedgerDstackStore(self.repo, self.scope.tenant_id)
        self.client = Client(self.store)
        self.capacity = DstackCapacity(self.client, self.store, clock=self.repo.clock)
        profiles = [{"id": "5090-" + mode, "owner_id": "superdan", "project_id": "project-1",
            "budget_account_ids": ["owner-budget"], "pool": "dstack-test", "backend": "vastai",
            "runtime_profile_id": PROFILE, "mode": mode, "configuration_id": "test-native-" + mode,
            "image": "example/native@sha256:" + "a" * 64, "gpu_names": ["RTX5090"],
            "memory_gib": 96, "disk_gib": 128, "gpu_memory_gib": 30, "cpu_count": 12,
            "max_price_microusd": 750000, "max_ttl_seconds": 1800, "extra_reservation_microusd": 0}
            for mode in ("fl", "ref")]
        self.operator = DstackOperator(self.repo, settings, self.capacity, {"version": 1, "ssh_public_key": PUB,
            "policy": {"enabled": True, "expires_at": 5000., "max_instances": 3, "max_physical_gpus": 3,
                "max_hourly_cost_microusd": 3000000, "max_ttl_seconds": 1800,
                "idle_shutdown_seconds": 600, "observation_fresh_seconds": 30}, "profiles": profiles})
        self.configs, self.remotes = {}, {}
        self.owner = Principal("superdan", "browser-1")
        for mode in ("fl", "ref"):
            source = self.root / ("sources-" + mode); source.mkdir()
            manifest = engine_manifest(PROFILE, mode)
            package = b"Offline fixture only, not a deployable bundle"
            files = {"wangp-package.tar.gz": package, "wangp-bootstrap.py": b"# Offline SSH fixture",
                "wangp-manifest.json": json.dumps(manifest.document).encode(),
                "wangp-runtime.json": json.dumps(_runtime(PROFILE, 0, 1, hashlib.sha256(package).hexdigest())).encode()}
            for name, data in files.items():
                (source / name).write_bytes(data)
            self.configs[mode] = DstackRuntimeConfig(self.root / "runtime", source,
                self.root / "ssh-key", self.root / "known-hosts", 18900 if mode == "fl" else 18901,
                "dstack-test", "vast", PROFILE, mode, manifest.document["model_id"], "test-native-" + mode,
                manifest.digest, {name: hashlib.sha256(data).hexdigest() for name, data in files.items()})

    def allocation(self, mode="fl"):
        preview = self.operator.preview(self.owner, {"profile_id": "5090-" + mode, "ttl_seconds": 900})
        node = self.operator.start(self.owner, {"preview_id": preview["preview_id"]}, "start-" + mode)
        binding = self.store.load(node["node_id"])
        run = self.client.runs[binding["run_name"]]
        run.update(status="running", latest_job_submission={"job_provisioning_data": {
            "backend": "vastai", "instance_id": "gpu-" + mode,
            "instance_type": {"resources": {"gpus": [{"name": "RTX5090"}]}}}})
        self.store.record(binding["intent_id"], {"run_id": run["id"], "provider_instance_id": "gpu-" + mode,
            "state": "runtime_unconfirmed", "observed_at": self.repo.clock()})
        return self.store.load(binding["intent_id"]), run

    def bridge(self):
        outer = self
        def host_factory(config, coordinates):
            remote = outer.remotes.setdefault(config.mode, {"starts": 0, "uploads": 0,
                "incarnation": "c" * 32, "gpu": GPU, "idle": True, "identity": None})
            class Host:
                def upload(self, files):
                    remote["uploads"] += 1
                    assert set(files) == set(config.source_sha256)
                def start(self, identity):
                    # Real transactional boot intent and protected token are
                    # present BEFORE anything resembling a remote launch.
                    assert outer.store.load(identity["intent_id"])["bootstrap_started"] is True
                    private_token_file(config.work_dir / identity["intent_id"] / "wangp-token")
                    remote["starts"] += 1
                    remote["identity"] = copy.deepcopy(identity)
                    if remote.get("lose_start"):
                        raise RuntimeError("SECRET upstream SSH response")
                def report(self):
                    return {"identity": remote["identity"], "state": "ready", "runtime_verified": True,
                        "engine_manifest_digest": config.engine_manifest_digest,
                        "source_revision": engine_manifest(PROFILE, config.mode).document["source_revision"],
                        "gpus": [{"uuid": remote["gpu"]}], "runtime": {"gpu_total_bytes": 32 * 1024**3}}
                def open_tunnel(self, port):
                    assert port == config.local_port
                    remote["tunnel"] = True
                def close(self):
                    remote["tunnel"] = False
            return Host()
        def transport_factory(endpoint, token):
            mode = "fl" if endpoint.endswith(":18900") else "ref"
            config, remote = outer.configs[mode], outer.remotes[mode]
            assert len(token) >= 32
            return SimpleNamespace(readiness=lambda: HostReadiness(config.engine_manifest_digest,
                remote["identity"]["intent_id"], remote["incarnation"], remote["idle"]), close=lambda: None)
        return DstackNativeRuntime(self.store, lambda binding: self.configs[binding["mode"]],
            lambda binding, _run: {"host": "offline.invalid", "port": 22, "username": "root",
                "instance_id": binding["provider_instance_id"]},
            ssh_factory=host_factory, transport_factory=transport_factory)

    def test_owned_ledger_journals_once_and_original_native_factory_creates_hatchet_slot(self):
        binding, run = self.allocation()
        bridge = self.bridge()
        native = bridge.readiness(binding, run)
        slot = bridge.slot(binding["intent_id"])
        self.assertEqual(native.slot_key, binding["intent_id"])
        self.assertEqual(slot.spec.dispatch_backend, "hatchet-v1")
        self.assertEqual(slot.spec.physical_gpu_ids, (GPU,))
        self.assertEqual(slot.spec.recipe_ids, ("h3-base-fl2va-v1",))
        self.assertEqual(slot.spec.engine_manifest_digest, binding["manifest_digest"])
        self.assertEqual(slot.spec.instance_id, "gpu-fl")
        self.assertTrue(slot.confirmed_idle)
        bridge.readiness(self.store.load(binding["intent_id"]), run)
        self.assertEqual((self.remotes["fl"]["starts"], self.remotes["fl"]["uploads"]), (1, 1))
        with self.repo.engine.connect() as conn:
            self.assertEqual(conn.execute(select(func.count()).select_from(scaler_receipts).where(
                scaler_receipts.c.intent_id == binding["intent_id"], scaler_receipts.c.operation == "bootstrap")).scalar_one(), 1)
            self.assertEqual(conn.execute(select(func.count()).select_from(registered_workers)).scalar_one(), 0)

    def test_lost_start_reply_reconnects_original_marker_without_install_start_or_token_recreation(self):
        binding, run = self.allocation()
        self.remotes["fl"] = {"starts": 0, "uploads": 0, "incarnation": "c" * 32,
            "gpu": GPU, "idle": True, "identity": None, "lose_start": True}
        bridge = self.bridge()
        with self.assertRaisesRegex(DstackError, "^dstack_runtime_bootstrap_response_unknown$"):
            bridge.readiness(binding, run)
        token = private_token_file(self.configs["fl"].work_dir / binding["intent_id"] / "wangp-token")
        bridge.close()
        recovered = self.bridge()
        self.assertTrue(recovered.readiness(self.store.load(binding["intent_id"]), run).idle)
        self.assertEqual(token, private_token_file(self.configs["fl"].work_dir / binding["intent_id"] / "wangp-token"))
        self.assertEqual((self.remotes["fl"]["starts"], self.remotes["fl"]["uploads"]), (1, 1))

    def test_missing_remote_marker_or_cpu_token_stays_unknown_without_relaunch(self):
        binding, run = self.allocation()
        bridge = self.bridge(); bridge.readiness(binding, run)
        self.remotes["fl"]["identity"] = None
        with self.assertRaisesRegex(DstackError, "remote_identity_unconfirmed"):
            bridge.readiness(self.store.load(binding["intent_id"]), run)
        token = self.configs["fl"].work_dir / binding["intent_id"] / "wangp-token"
        token.unlink()
        with self.assertRaisesRegex(DstackError, "recovery_material_missing"):
            self.bridge().readiness(self.store.load(binding["intent_id"]), run)
        self.assertEqual(self.remotes["fl"]["starts"], 1)
        self.assertFalse(token.exists())

    def test_changed_physical_gpu_or_incarnation_never_produces_a_new_slot(self):
        binding, run = self.allocation()
        bridge = self.bridge(); bridge.readiness(binding, run)
        self.remotes["fl"]["gpu"] = "GPU-aaaaaaaa-1234-1234-1234-123456789abc"
        with self.assertRaisesRegex(DstackError, "physical_gpu_changed"):
            bridge.readiness(self.store.load(binding["intent_id"]), run)
        self.remotes["fl"]["gpu"] = GPU
        self.remotes["fl"]["incarnation"] = "d" * 32
        with self.assertRaisesRegex(DstackError, "incarnation_changed"):
            bridge.readiness(self.store.load(binding["intent_id"]), run)
        with self.assertRaisesRegex(DstackError, "slot_not_connected"):
            bridge.slot(binding["intent_id"])
        self.assertEqual(self.remotes["fl"]["starts"], 1)

    def test_altered_sources_and_multiple_allocated_gpus_are_rejected_before_bootstrap(self):
        binding, run = self.allocation()
        (self.configs["fl"].source_dir / "wangp-bootstrap.py").write_bytes(b"changed")
        with self.assertRaisesRegex(DstackError, "source_changed"):
            self.bridge().readiness(binding, run)
        self.assertFalse(self.store.load(binding["intent_id"]).get("bootstrap_started"))
        ref, ref_run = self.allocation("ref")
        ref_run["latest_job_submission"]["job_provisioning_data"]["instance_type"]["resources"]["gpus"] *= 2
        with self.assertRaisesRegex(DstackError, "allocation_mismatch"):
            self.bridge().readiness(ref, ref_run)
        self.assertFalse(self.store.load(ref["intent_id"]).get("bootstrap_started"))

    def test_ref_mode_and_busy_native_runtime_are_explicit_and_close_only_closes_local_tunnel(self):
        binding, run = self.allocation("ref")
        bridge = self.bridge(); bridge.readiness(binding, run)
        self.remotes["ref"]["idle"] = False
        native = bridge.readiness(self.store.load(binding["intent_id"]), run)
        self.assertFalse(native.idle)
        self.assertEqual(bridge.slot(binding["intent_id"]).spec.recipe_ids, ("h3-base-ref2va-v1",))
        self.assertFalse(bridge.slot(binding["intent_id"]).confirmed_idle)
        bridge.close(binding["intent_id"])
        self.assertFalse(self.remotes["ref"]["tunnel"])
        self.assertEqual(self.client.stopped, [])
        self.assertEqual(self.store.load(binding["intent_id"])["provider_instance_id"], "gpu-ref")


if __name__ == "__main__":
    import unittest
    unittest.main()
