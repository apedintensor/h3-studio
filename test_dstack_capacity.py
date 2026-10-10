"""Pinned REST and journal semantics with fake capacity; never leases a GPU."""
from dataclasses import replace
import copy
import json
import importlib.util
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

import httpx

from studio_platform.dstack_capacity import (CapacityRequest, DstackCapacity,
    DstackClient, DstackError, idle_action, native_bootstrap_config, main)
from studio_platform.inference.wangp_contract import HostReadiness

INTENT = "a1111111-1111-4111-8111-111111111111"
RUN = "b2222222-2222-4222-8222-222222222222"
PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKey"


def request(**changes):
    return replace(CapacityRequest(INTENT, "vastai", "h3-pruned-int8-v1", "fl", "h3-int8",
        "config-1", "a" * 64, "example/runtime@sha256:" + "b" * 64, ("RTX5090",),
        30, 160, 850000, 10., 3610., 3600), **changes)


class Store:
    def __init__(self):
        self.value = {"intent_id": INTENT}
        self.lock = threading.Lock()
        self.obligations = False
        self.events = []

    def load(self, intent_id):
        self.assert_identity(intent_id)
        with self.lock:
            return copy.deepcopy(self.value)

    def assert_identity(self, intent_id):
        assert intent_id == INTENT

    def begin_apply(self, intent_id, binding):
        self.assert_identity(intent_id)
        with self.lock:
            if self.value.get("apply_started"):
                return False
            self.value.update(copy.deepcopy(binding), apply_started=True)
            self.events.append("apply_committed")
            return True

    def record(self, intent_id, observation):
        self.assert_identity(intent_id)
        with self.lock:
            self.value.update(copy.deepcopy(observation))

    def begin_stop(self, intent_id, run_id):
        self.assert_identity(intent_id)
        with self.lock:
            assert self.value["run_id"] == run_id
            if self.obligations or self.value.get("stop_started"):
                return False
            self.value["stop_started"] = True
            self.events.append("stop_committed")
            return True


class FakeClient:
    project = "sixnine"

    def __init__(self, store):
        self.store = store
        self.run = None
        self.apply_count = self.stop_count = 0
        self.apply_lost = self.stop_lost = False
        self.get_failed = False

    def get(self, **kw):
        if self.get_failed:
            raise DstackError("dstack_api_unavailable")
        return copy.deepcopy(self.run)

    def apply(self, spec):
        assert self.store.value["apply_started"]
        self.apply_count += 1
        self.run = {"id": RUN, "project_name": self.project, "run_spec": copy.deepcopy(spec),
            "status": "submitted", "cost": 0.1}
        if self.apply_lost:
            raise DstackError("dstack_api_unavailable")
        return copy.deepcopy(self.run)

    def stop(self, name):
        assert self.store.value["stop_started"]
        assert name == self.run["run_spec"]["run_name"]
        self.stop_count += 1
        if self.stop_lost:
            raise DstackError("dstack_api_unavailable")

    def running(self):
        self.run.update(status="running", latest_job_submission={"job_provisioning_data": {
            "backend": "vastai", "instance_id": "1234567", "instance_type": {"resources": {"gpus": [{"name": "RTX5090"}]}}}})


class DstackCapacityTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.client = FakeClient(self.store)
        self.native = Mock(return_value=HostReadiness("a" * 64, INTENT, "c" * 32, True))
        self.capacity = DstackCapacity(self.client, self.store, clock=lambda: 20., readiness=self.native)

    def start(self):
        return self.capacity.start(request(), PUB)

    def test_journal_precedes_apply_and_repeat_never_reapplies(self):
        self.start()
        self.capacity.start(request(), PUB)
        self.assertEqual(self.client.apply_count, 1)
        self.assertEqual(self.store.events, ["apply_committed"])

    def test_lost_apply_response_reconciles_same_run(self):
        self.client.apply_lost = True
        observed = self.start()
        self.assertEqual(observed["run_id"], RUN)
        self.capacity.start(request(), PUB)
        self.assertEqual(self.client.apply_count, 1)

    def test_absent_exact_get_is_unknown_not_create_proof(self):
        self.start()
        self.client.run = None
        for _ in range(3):
            self.assertEqual(self.capacity.start(request(), PUB)["state"], "creation_unknown")
        self.assertEqual(self.client.apply_count, 1)

    def test_binding_changes_cannot_replay_original_intent(self):
        self.start()
        with self.assertRaisesRegex(DstackError, "dstack_intent_binding_changed"):
            self.capacity.start(request(mode="ref"), PUB)
        self.assertEqual(self.client.apply_count, 1)

    def test_foreign_named_run_is_not_adopted_or_stopped(self):
        self.client.run = {"id": RUN, "project_name": "other"}
        with self.assertRaisesRegex(DstackError, "dstack_existing_run_requires_reconciliation"):
            self.start()
        self.assertEqual(self.client.apply_count, 0)

    def test_running_requires_native_identity_and_first_load_is_unknown(self):
        self.start()
        self.client.running()
        self.capacity.readiness = None
        value = self.capacity.observe(INTENT)
        self.assertFalse(value["ready"])
        self.capacity.readiness = self.native
        value = self.capacity.observe(INTENT)
        self.assertTrue(value["ready"])
        self.assertEqual(value["model_load_state"], "not_observed")
        self.assertEqual(value["estimated_cost_microusd"], 100000)
        self.assertEqual(value["billing_state"], "unsettled")

    def test_manifest_and_incarnation_changes_hold_slot(self):
        self.start()
        self.client.running()
        self.capacity.observe(INTENT)
        self.native.return_value = HostReadiness("a" * 64, INTENT, "d" * 32, True)
        value = self.capacity.observe(INTENT)
        self.assertEqual(value["reason_code"], "dstack_native_incarnation_changed")
        self.assertFalse(value["ready"])
        self.native.return_value = HostReadiness("b" * 64, INTENT, "c" * 32, True)
        self.assertEqual(self.capacity.observe(INTENT)["reason_code"], "dstack_native_binding_mismatch")

    def test_busy_model_retains_identity_and_disables_admission(self):
        self.start()
        self.client.running()
        self.native.return_value = HostReadiness("a" * 64, INTENT, "c" * 32, False)
        self.assertEqual(self.capacity.observe(INTENT)["state"], "busy")

    def test_failed_observation_revokes_cached_ready(self):
        self.start()
        self.client.running()
        self.capacity.observe(INTENT)
        self.client.get_failed = True
        self.assertFalse(self.capacity.observe(INTENT)["ready"])

    def test_changed_resource_configuration_is_rejected(self):
        self.start()
        self.client.run["run_spec"]["configuration"]["resources"]["gpu"]["count"] = 2
        self.assertEqual(self.capacity.observe(INTENT)["reason_code"], "dstack_run_binding_mismatch")

    def test_stop_preserves_business_obligations(self):
        self.start()
        self.store.obligations = True
        self.assertEqual(self.capacity.stop(INTENT)["state"], "draining")
        self.assertEqual(self.client.stop_count, 0)
        self.assertNotIn("stop_started", self.store.value)

    def test_lost_stop_ack_is_not_replayed_or_billed_as_zero(self):
        self.start()
        self.client.stop_lost = True
        self.assertEqual(self.capacity.stop(INTENT)["state"], "removal_unknown")
        self.capacity.stop(INTENT)
        self.assertEqual(self.client.stop_count, 1)
        self.client.run["status"] = "terminated"
        value = self.capacity.observe(INTENT)
        self.assertEqual(value["state"], "stopped")
        self.assertEqual(value["billing_state"], "unsettled")

    def test_single_gpu_and_digest_are_required(self):
        with self.assertRaisesRegex(DstackError, "dstack_single_gpu_required"):
            request(gpu_count=2)
        with self.assertRaisesRegex(DstackError, "dstack_image_digest_required"):
            request(image="example/runtime:latest")

    def test_supported_backend_never_falls_back(self):
        config = request(backend="runpod").run_spec(PUB)["configuration"]
        self.assertEqual(config["backends"], ["runpod"])
        with self.assertRaisesRegex(DstackError, "dstack_backend_unsupported"):
            request(backend="lium")

    def test_concurrent_start_commits_only_one_paid_apply(self):
        barrier = threading.Barrier(2)
        original_get = self.client.get
        first_reads = []
        def get(**kw):
            if "run_name" in kw and not self.store.value.get("apply_started"):
                first_reads.append(1)
                barrier.wait(timeout=5)
                return None
            return original_get(**kw)
        self.client.get = get
        failures = []
        def start():
            try:
                self.start()
            except Exception as exc:
                failures.append(type(exc).__name__)
        threads = [threading.Thread(target=start) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertFalse(failures)
        self.assertEqual(self.client.apply_count, 1)

    def test_multi_gpu_allocation_cannot_be_marked_ready(self):
        self.start()
        self.client.running()
        self.client.run["latest_job_submission"]["job_provisioning_data"]["instance_type"]["resources"]["gpus"] *= 2
        value = self.capacity.observe(INTENT)
        self.assertFalse(value["ready"])
        self.assertEqual(value["reason_code"], "dstack_allocated_gpu_count_unconfirmed")

    def test_expired_window_rejects_before_network(self):
        with self.assertRaisesRegex(DstackError, "dstack_window_expired"):
            self.capacity.start(request(hard_deadline=19), PUB)
        self.assertEqual(self.client.apply_count, 0)

    def test_pinned_model_parsed_response_is_compatible(self):
        # Optional SDK check, also run explicitly with dstack==0.22.3 locally.
        try:
            from dstack._internal.core.models.configurations import TaskConfiguration
        except ImportError:
            self.skipTest("optional pinned dstack SDK absent")
        self.start()
        config = self.client.run["run_spec"]["configuration"]
        self.client.run["run_spec"]["configuration"] = TaskConfiguration.model_validate(config).model_dump(mode="json")
        self.assertEqual(self.capacity.observe(INTENT)["state"], "starting")

    def test_idle_policy_distinguishes_holds_and_unknown_obligations(self):
        policy = dict(now=1000, hard_deadline=2000, hold_until=0, last_business_activity=10,
            idle_seconds=600, active_jobs=0, unsafe_attempts=0, collection_holds=0, observations_fresh=True)
        self.assertEqual(idle_action(**policy), "stop")
        self.assertEqual(idle_action(**{**policy, "hold_until": 1500}), "retain")
        self.assertEqual(idle_action(**{**policy, "collection_holds": 1}), "retain")
        self.assertEqual(idle_action(**{**policy, "now": 2100, "unsafe_attempts": 1}), "drain")
        self.assertEqual(idle_action(**{**policy, "observations_fresh": False}), "reconcile")

    @unittest.skipUnless(os.name == "posix", "GPU bootstrap validates Linux absolute paths")
    def test_native_bootstrap_keeps_original_one_slot_interface(self):
        path = Path(__file__).parent / "deploy" / "wangp" / "bootstrap.py"
        spec = importlib.util.spec_from_file_location("sixnine_inert_bootstrap", path)
        bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bootstrap)
        value = native_bootstrap_config(request(), source_bundle_sha256="d" * 64)
        # Validate grammar/interface without reading this CPU user's /root.
        # Actual GPU filesystem ownership remains the original bootstrap guard.
        with patch.object(bootstrap.Path, "is_symlink", return_value=False):
            self.assertEqual(bootstrap.validate_config(value), value)
        self.assertEqual(value["expected_host_gpus"], 1)
        self.assertEqual(value["profile_slot_index"], 0)
        self.assertFalse(value["dependency_artifact_url"])
        self.assertNotIn("token", json.dumps(value))

    def test_bootstrap_refuses_path_traversal(self):
        with self.assertRaisesRegex(DstackError, "dstack_runtime_path_invalid"):
            native_bootstrap_config(request(), source_bundle_sha256="d" * 64, model_root="/root/../private")


class DstackRESTTests(unittest.TestCase):
    def test_exact_paths_and_create_only_apply(self):
        seen = []
        def handler(req):
            seen.append((req.url.path, json.loads(req.content), req.headers["Authorization"]))
            return httpx.Response(200, json={})
        client = DstackClient("http://127.0.0.1:3000", "secret-token-123456", "sixnine",
            session=httpx.Client(transport=httpx.MockTransport(handler)))
        client.plan(request().run_spec(PUB))
        client.apply(request().run_spec(PUB))
        client.get(run_id=RUN)
        client.stop(request().run_name)
        self.assertEqual([row[0] for row in seen], ["/api/project/sixnine/runs/get_plan",
            "/api/project/sixnine/runs/apply", "/api/project/sixnine/runs/get", "/api/project/sixnine/runs/stop"])
        self.assertFalse(seen[1][1]["force"])
        self.assertIsNone(seen[1][1]["plan"]["current_resource"])
        self.assertFalse(seen[3][1]["abort"])

    def test_server_errors_never_expose_response_secrets(self):
        session = httpx.Client(transport=httpx.MockTransport(
            lambda _: httpx.Response(500, text="secret prompt and token")))
        client = DstackClient("http://localhost:3000", "secret-token-123456", "sixnine", session=session)
        with self.assertRaisesRegex(DstackError, "^dstack_api_rejected$"):
            client.get(run_id=RUN)

    def test_remote_plain_http_and_url_credentials_are_refused(self):
        for endpoint in ("http://remote.example", "https://user:password@example.com", "https://example.com/?key=a"):
            with self.assertRaisesRegex(DstackError, "dstack_endpoint_invalid"):
                DstackClient(endpoint, "secret-token-123456", "sixnine")


if __name__ == "__main__":
    unittest.main()
