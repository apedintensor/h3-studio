"""Lium HTTP contracts use MockTransport and fake credential objects only."""
import base64
from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest

import httpx

from studio_platform.lium_provider import (
    BASE_URL, InferenceIdleProof, LiumError, LiumManifest, LiumProvider,
    MAX_RESPONSE_BYTES, PROFILE,
)
from studio_platform.scaler import LaunchSpec, ScaleCoordinator
from studio_platform.autoscale import Demand, ScalePolicy
from test_platform_repository import LedgerCase


TAG = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
POD = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OTHER = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
EXECUTOR = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
TEMPLATE = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
PUBLIC_KEY = "ssh-ed25519 " + base64.b64encode(
    (11).to_bytes(4, "big") + b"ssh-ed25519" + (32).to_bytes(4, "big") + b"x"*32
).decode() + " test-only"


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def manifest(**changes):
    return replace(LiumManifest("test-lium-configuration", "test-model", EXECUTOR, TEMPLATE,
        2, 1_000_000, 2, PUBLIC_KEY, 100_000,
        region="local-test", allow_preflight_only_price_cap=True), **changes)


def launch(**changes):
    return replace(LaunchSpec("lium", "test-lium-configuration", "test-model", region="local-test",
        offer_id=EXECUTOR, image_id=TEMPLATE), **changes)


def statement(tag=TAG, pod=POD, *, removed=True, total="0.1234567", **changes):
    return dict(pod_id=pod, pod_name="sixnine-"+tag, created_at=stamp(100), removed_at=stamp(900),
        removed=removed, total=total, billed_seconds=800, **changes)


class MockAPI:
    def __init__(self):
        self.calls = []
        self.pods = []
        self.executors = [{"id": EXECUTOR, "gpu_count": 2, "price_per_gpu": "0.75"}]
        self.templates = [{"id": TEMPLATE}]
        self.statements = {}
        self.hook = None
        self.on_rent = None
        self.on_delete = None

    def __call__(self, request):
        self.calls.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        assert request.url.scheme == "https" and request.url.host == "lium.io"
        assert request.headers["X-API-Key"] == "offline-test-key-never-real"
        if self.hook:
            override = self.hook(request)
            if override is not None:
                return override
        path = request.url.path.removeprefix("/api/")
        if request.method == "GET":
            if path == "pods":
                return httpx.Response(200, json=self.pods)
            if path == "executors":
                return httpx.Response(200, json=self.executors)
            if path == "templates":
                return httpx.Response(200, json=self.templates)
            if path.startswith("pods/") and path.endswith("/statement"):
                pod_id = path.split("/")[1]
                return httpx.Response(200, json=self.statements[pod_id]) if pod_id in self.statements else httpx.Response(404)
        if request.method == "POST" and path == f"executors/{EXECUTOR}/rent":
            if self.on_rent:
                result = self.on_rent(request)
                if result is not None:
                    return result
            return httpx.Response(200, json={"success": True, "pod_id": POD})
        if request.method == "DELETE" and path == f"pods/{POD}":
            if self.on_delete:
                result = self.on_delete(request)
                if result is not None:
                    return result
            return httpx.Response(200, json={"success": True})
        raise AssertionError("Unexpected offline request")

    def count(self, method):
        return sum(call[0] == method for call in self.calls)


class LiumProviderTests(unittest.TestCase):
    def setUp(self):
        self.now, self.api, self.loaded = 1000, MockAPI(), []
        self.providers = []

    def tearDown(self):
        for provider in self.providers:
            provider.close()

    def provider(self, **changes):
        def load(service, *, profile):
            self.loaded.append((service, profile))
            return SimpleNamespace(service="lium", profile=PROFILE, base_url=BASE_URL,
                primary_key_variable="LIUM_API_KEY", api_key="offline-test-key-never-real")
        options = dict(enabled=True, manifests=(manifest(),), loader=load,
            transport=httpx.MockTransport(self.api), clock=lambda: self.now)
        options.update(changes)
        result = LiumProvider(**options)
        self.providers.append(result)
        return result

    def pod(self, tag=TAG, pod=POD, status="RUNNING"):
        return {"id": pod, "name": "sixnine-"+tag, "status": status,
            "spend_to_date": "9999", "ssh_command": "not-used"}

    def test_default_disabled_never_loads_key_or_creates_http_client(self):
        provider = self.provider(enabled=False, loader=lambda *a, **k: self.fail("disabled loader"))
        for call in (lambda: provider.create(TAG, launch(), hard_deadline=10_000),
                     lambda: provider.reconcile(TAG, POD), lambda: provider.destroy(TAG, POD),
                     lambda: provider.billing(TAG, POD)):
            with self.assertRaisesRegex(LiumError, "lium_provider_disabled"):
                call()
        self.assertIsNone(provider._client)
        self.assertEqual(self.api.calls, [])

    def test_exact_manifest_payload_central_profile_and_relative_ttl_floor(self):
        provider = self.provider()
        fact = provider.create(TAG, launch(), hard_deadline=8300)
        self.assertEqual((fact.state, fact.instance_id), ("starting", POD))
        self.assertEqual(self.loaded, [("lium", PROFILE)])
        self.assertEqual(self.api.calls[-1], ("POST", f"/api/executors/{EXECUTOR}/rent", {
            "pod_name": "sixnine-"+TAG, "template_id": TEMPLATE, "gpu_count": 2,
            "user_public_key": PUBLIC_KEY, "termination_hours": 2}))
        self.assertFalse(fact.idle_confirmed)
        self.assertIsNone(fact.actual_cost_microusd)
        with self.assertRaisesRegex(LiumError, "already_submitted"):
            provider.create(TAG.upper(), launch(), hard_deadline=8300)
        self.assertEqual(self.api.count("POST"), 1)

    def test_config_mismatch_or_missing_url_never_falls_back_or_leaks_key(self):
        for field, value in (("service", "other"), ("profile", "other"), ("base_url", None),
                             ("base_url", "https://evil.invalid/api"), ("primary_key_variable", "OTHER"),
                             ("api_key", "")):
            config = SimpleNamespace(service="lium", profile=PROFILE, base_url=BASE_URL,
                primary_key_variable="LIUM_API_KEY", api_key="offline-test-key-never-real")
            setattr(config, field, value)
            provider = self.provider(loader=lambda *a, **k: config)
            with self.subTest(field=field, value=value), self.assertRaisesRegex(LiumError, "profile_unavailable") as caught:
                provider.reconcile(TAG)
            self.assertNotIn("offline-test-key", str(caught.exception))
        self.assertEqual(self.api.calls, [])

    def test_loader_exception_sanitized(self):
        def bad(*args, **kwargs):
            raise RuntimeError("secret-test-value-and-signature")
        with self.assertRaisesRegex(LiumError, "profile_unavailable") as caught:
            self.provider(loader=bad).reconcile(TAG)
        self.assertNotIn("secret-test", str(caught.exception))

    def test_same_configuration_can_bind_two_exact_executors(self):
        provider = self.provider(manifests=(manifest(), manifest(executor_id=OTHER)))
        self.assertEqual(provider._manifest(launch()).executor_id, EXECUTOR)
        self.assertEqual(provider._manifest(launch(offer_id=OTHER)).executor_id, OTHER)
        with self.assertRaises(LiumError):
            provider._manifest(launch(offer_id=POD))
        # A disappeared approved offer cannot silently rent another GPU.
        for unused in range(2):
            with self.assertRaisesRegex(LiumError, "unavailable"):
                provider.create(TAG, launch(offer_id=OTHER), hard_deadline=10000)
        self.assertEqual(self.api.count("POST"), 0)

    def test_opt_in_compatible_hardware_selects_available_alternative(self):
        gpu = "NVIDIA RTX PRO 6000 Blackwell Workstation Edition"
        self.api.executors = [{"id": OTHER, "gpu_count": 2, "price_per_gpu": "0.8",
            "specs": {"gpu": {"details": [{"name": gpu, "capacity": 97887}]}},
            "location": {"country_code": "SG"}}]
        provider = self.provider(manifests=(manifest(compatible_gpu_names=[gpu], minimum_vram_mib=95000,
                                                     allowed_countries=["SG"]),))
        self.api.hook = lambda req: httpx.Response(200, json={"success": True, "pod_id": POD}) if req.method == "POST" else None
        self.assertIsNone(provider.preflight_availability(launch()))
        result = provider.create(TAG, launch(), hard_deadline=10000)
        self.assertEqual(result.instance_id, POD)
        self.assertEqual(self.api.calls[-1][1], f"/api/executors/{OTHER}/rent")
        self.assertEqual(self.api.calls[-1][2]["gpu_count"], 2)

    def test_filter_only_manifest_needs_no_fixed_machine(self):
        gpu = "NVIDIA RTX PRO 6000 Blackwell Workstation Edition"
        provider = self.provider(manifests=(manifest(executor_id="", compatible_gpu_names=[gpu], minimum_vram_mib=95000),))
        self.api.executors = [{"id": OTHER, "gpu_count": 1, "price_per_gpu": "0.8",
            "specs": {"gpu": {"details": [{"name": gpu, "capacity": 97887}]}}}]
        self.assertEqual(provider.preflight_availability(launch(offer_id="")), "provider_inventory_unavailable")
        self.api.executors[0]["gpu_count"] = 2
        self.now += 61
        self.assertIsNone(provider.preflight_availability(launch(offer_id="")))
        self.api.hook = lambda req: httpx.Response(200, json={"success": True, "pod_id": POD}) if req.method == "POST" else None
        self.assertEqual(provider.create(TAG, launch(offer_id=""), hard_deadline=10000).instance_id, POD)
        with self.assertRaisesRegex(LiumError, "filter_required"):
            manifest(executor_id="")

    def test_hardware_selection_rejects_wrong_memory_price_country_and_name(self):
        gpu = "NVIDIA RTX PRO 6000 Blackwell Workstation Edition"
        for field, value in (("capacity", 49140), ("name", "NVIDIA RTX 6000 Ada Generation"),
                             ("price", "1.01"), ("country", "US"), ("capacity", True)):
            row = {"id": OTHER, "gpu_count": 2, "price_per_gpu": "0.8",
                "specs": {"gpu": {"details": [{"name": gpu, "capacity": 97887}]}},
                "location": {"country_code": "SG"}}
            if field == "price": row["price_per_gpu"] = value
            elif field == "country": row["location"]["country_code"] = value
            else: row["specs"]["gpu"]["details"][0][field] = value
            self.api.executors = [row]
            provider = self.provider(manifests=(manifest(compatible_gpu_names=[gpu], minimum_vram_mib=95000,
                                                         allowed_countries=["SG"]),))
            self.assertEqual(provider.preflight_availability(launch()), "provider_inventory_unavailable")
        self.assertEqual(self.api.count("POST"), 0)

    def test_negative_inventory_cache_expires_without_caching_create(self):
        provider = self.provider()
        offers = self.api.executors
        self.api.executors = []
        self.assertEqual(provider.preflight_availability(launch()), "provider_inventory_unavailable")
        count = len(self.api.calls)
        self.api.executors = offers
        self.assertEqual(provider.preflight_availability(launch()), "provider_inventory_unavailable")
        self.assertEqual(len(self.api.calls), count)
        self.now += 61
        self.assertIsNone(provider.preflight_availability(launch()))
        self.api.executors = []
        from studio_platform.scaler import CreationNotSubmitted
        with self.assertRaises(CreationNotSubmitted):
            provider.create(TAG, launch(), hard_deadline=10000)
        self.assertEqual(self.api.count("POST"), 0)

    def test_rent_timeout_never_claims_not_submitted_or_retries(self):
        from studio_platform.scaler import CreationNotSubmitted
        provider = self.provider()
        def timeout(req):
            raise httpx.ReadTimeout("synthetic secret must not leave adapter")
        self.api.on_rent = timeout
        with self.assertRaises(LiumError) as caught:
            provider.create(TAG, launch(), hard_deadline=10000)
        self.assertNotIsInstance(caught.exception, CreationNotSubmitted)
        self.assertEqual(str(caught.exception), "lium_request_unconfirmed")
        self.assertEqual(provider.reconcile(TAG).state, "unknown")
        with self.assertRaises(LiumError):
            provider.create(TAG, launch(), hard_deadline=10000)
        self.assertEqual(self.api.count("POST"), 1)

    def test_ssh_coordinates_require_exact_running_tag_and_public_host(self):
        provider = self.provider()
        self.api.pods = [self.pod()]
        detail = {"id": POD, "executor": {"executor_ip_address": "8.8.8.8"}, "ports_mapping": {"22": "2022"}}
        self.api.hook = lambda req: httpx.Response(200, json=detail) if req.url.path == "/api/pods/"+POD else None
        self.assertEqual(provider.ssh_connection(TAG, POD), {"instance_id": POD, "host": "8.8.8.8", "port": 2022, "username": "root"})
        for host in ("127.0.0.1", "10.0.0.1", "host.example", "1.2.3.4;touch /tmp/bad"):
            detail["executor"]["executor_ip_address"] = host
            with self.subTest(host=host), self.assertRaisesRegex(LiumError, "coordinates_unverified"):
                provider.ssh_connection(TAG, POD)
        detail["executor"]["executor_ip_address"] = "8.8.8.8"
        for port in (True, "x", 65536, 0):
            detail["ports_mapping"]["22"] = port
            with self.subTest(port=port), self.assertRaisesRegex(LiumError, "coordinates_unverified"):
                provider.ssh_connection(TAG, POD)
        self.api.pods = [self.pod(tag=OTHER)]
        with self.assertRaises(LiumError):
            provider.ssh_connection(TAG, POD)

    def test_actual_lifetime_shortens_relative_ttl_and_rejects_clock_ambiguity(self):
        provider = self.provider()
        self.api.pods = [self.pod()]
        detail = {"id": POD, "pod_name": "sixnine-"+TAG, "created_at": stamp(900), "removal_scheduled_at": stamp(11700)}
        self.api.hook = lambda req: httpx.Response(200, json=detail) if req.url.path == "/api/pods/"+POD else None
        self.assertEqual(provider.lifetime(TAG, POD, local_created_at=900)["safe_deadline"], 11100)
        detail.update(created_at=stamp(900).removesuffix("+00:00"), removal_scheduled_at=stamp(11700).removesuffix("+00:00"))
        self.assertEqual(provider.lifetime(TAG, POD, local_created_at=900)["timezone_evidence"], "naive_utc_crosschecked_against_local_intent")
        with self.assertRaisesRegex(LiumError, "lifetime_not_confirmed"):
            provider.lifetime(TAG, POD, local_created_at=900+8*3600)

    def test_manifest_matches_exact_model_endpoint_count_and_operator_price_ack(self):
        provider = self.provider()
        for changes in ({"model_id": "other"}, {"region": "other"}, {"offer_id": OTHER}, {"image_id": OTHER}):
            with self.subTest(changes=changes), self.assertRaisesRegex(LiumError, "unapproved"):
                provider.create(TAG, launch(**changes), hard_deadline=10_000)
        with self.assertRaisesRegex(LiumError, "unapproved"):
            self.provider(manifests=(manifest(allow_preflight_only_price_cap=False),)).create(
                TAG, launch(), hard_deadline=10_000)
        self.assertEqual(self.api.calls, [])

    def test_bad_public_key_private_path_and_ttl_are_rejected(self):
        for key in ("C:/private/id_ed25519", "-----BEGIN PRIVATE KEY-----", PUBLIC_KEY+"\n",
                    "command=bad "+PUBLIC_KEY, "ssh-ed25519 !!!"):
            with self.subTest(key=key), self.assertRaises(LiumError):
                manifest(user_public_key=key)
        for ttl in (0, 721, 1.2, True):
            with self.assertRaises(LiumError):
                manifest(termination_hours=ttl)

    def test_pure_reservation_validation_binds_gpu_slots_and_full_ttl_cap(self):
        provider = self.provider()
        self.assertEqual(provider.validate_launch(launch(), physical_gpus=2, slots=1,
            reserved_cost_microusd=4_000_000, hard_deadline=10_000)["ttl_cap_reservation_microusd"], 4_000_000)
        for changes in ({"physical_gpus": 1}, {"slots": 2}, {"reserved_cost_microusd": 3_999_999}):
            arguments = dict(physical_gpus=2, slots=1, reserved_cost_microusd=4_000_000, hard_deadline=10_000)
            arguments.update(changes)
            with self.assertRaises(LiumError):
                provider.validate_launch(launch(), **arguments)
        self.assertEqual(self.loaded, [])
        self.assertEqual(self.api.calls, [])

    def test_price_per_gpu_cap_count_and_exact_template_fail_before_rent(self):
        for offers, templates in (([{"id": EXECUTOR, "gpu_count": 2, "price_per_gpu": "1.000001"}], [{"id": TEMPLATE}]),
                                  ([{"id": EXECUTOR, "gpu_count": 1, "price_per_gpu": "0.1"}], [{"id": TEMPLATE}]),
                                  ([{"id": OTHER, "gpu_count": 4, "price_per_gpu": "0.01"}], [{"id": TEMPLATE}]),
                                  ([{"id": EXECUTOR, "gpu_count": 2, "price_per_gpu": "0.1"}], [{"id": OTHER}])):
            self.api.executors, self.api.templates = offers, templates
            with self.assertRaises(LiumError):
                self.provider().create(TAG, launch(), hard_deadline=10_000)
        self.assertEqual(self.api.count("POST"), 0)

    def test_deadline_and_approval_rechecked_after_slow_preflight(self):
        provider = self.provider()
        with self.assertRaisesRegex(LiumError, "ttl_window"):
            provider.create(TAG, launch(), hard_deadline=self.now+3600)
        self.assertEqual(self.api.calls, [])
        def hook(request):
            if request.url.path.endswith("/templates"):
                self.now = 6000
        self.api.hook = hook
        with self.assertRaisesRegex(LiumError, "approval_expired"):
            provider.create(TAG, launch(), hard_deadline=8300)
        self.assertEqual(self.api.count("POST"), 0)

    def test_lost_create_response_is_one_post_reconciliation_only(self):
        provider = self.provider()
        def rent(request):
            self.api.pods = [self.pod()]
            raise httpx.ReadTimeout("pretend-secret-response", request=request)
        self.api.on_rent = rent
        with self.assertRaisesRegex(LiumError, "request_unconfirmed") as caught:
            provider.create(TAG, launch(), hard_deadline=10_000)
        self.assertNotIn("pretend-secret", str(caught.exception))
        with self.assertRaisesRegex(LiumError, "already_submitted"):
            provider.create(TAG, launch(), hard_deadline=10_000)
        fact = provider.reconcile(TAG)
        self.assertEqual((fact.state, fact.instance_id), ("running", POD))
        self.assertEqual(self.api.count("POST"), 1)

    def test_exact_tag_not_prefix_and_duplicates_refuse_selection(self):
        provider = self.provider()
        self.api.pods = [self.pod(tag=OTHER), {"id": POD, "name": "sixnine-"+TAG+"-extra", "status": "RUNNING"}]
        self.assertEqual(provider.reconcile(TAG).state, "unknown")
        self.api.pods = [self.pod(), self.pod(pod=OTHER)]
        with self.assertRaisesRegex(LiumError, "duplicate_tag"):
            provider.reconcile(TAG)
        with self.assertRaisesRegex(LiumError, "duplicate_tag"):
            provider.create(TAG, launch(), hard_deadline=10_000)
        self.assertEqual(self.api.count("POST"), 0)

    def test_existing_single_tag_returns_vm_only_and_never_posts(self):
        self.api.pods = [self.pod(status="STARTING")]
        fact = self.provider().create(TAG, launch(), hard_deadline=10_000)
        self.assertEqual(fact.state, "starting")
        self.assertFalse(fact.idle_confirmed)
        self.assertEqual(self.api.count("POST"), 0)

    def test_known_id_conflict_does_not_delete_or_change_identity(self):
        self.api.pods = [self.pod(pod=OTHER)]
        provider = self.provider()
        for call in (lambda: provider.reconcile(TAG, POD), lambda: provider.destroy(TAG, POD)):
            with self.assertRaisesRegex(LiumError, "identity_conflict"):
                call()
        self.assertEqual(self.api.count("DELETE"), 0)

    def test_empty_pods_and_statement_404_are_unknown_not_removed_or_free(self):
        provider = self.provider()
        for fact in (provider.reconcile(TAG), provider.reconcile(TAG, POD), provider.destroy(TAG, POD)):
            self.assertEqual(fact.state, "unknown")
            self.assertFalse(fact.absence_confirmed)
            self.assertIsNone(fact.actual_cost_microusd)
        self.assertIsNone(provider.billing(TAG, POD))
        self.assertEqual(self.api.count("DELETE"), 0)

    def test_failed_stopped_vm_is_not_destroyed_and_spend_estimate_not_settled(self):
        provider = self.provider()
        for status in ("FAILED", "STOPPED", "REBOOT_FAILED", "REMOVED"):
            self.api.pods = [self.pod(status=status)]
            fact = provider.reconcile(TAG, POD)
            self.assertEqual(fact.state, "starting")
            self.assertIsNone(fact.actual_cost_microusd)
        self.api.statements[POD] = statement(removed=False)
        self.assertIsNone(provider.billing(TAG, POD))

    def test_fresh_matching_inference_idle_proof_separate_from_vm_running(self):
        self.api.pods = [self.pod()]
        self.assertFalse(self.provider().reconcile(TAG, POD).idle_confirmed)
        for proof in (InferenceIdleProof(POD, 999, 980, True), InferenceIdleProof(POD, 960, 950, True),
                      InferenceIdleProof(OTHER, 999, 980, True), InferenceIdleProof(POD, 1001, 980, True),
                      InferenceIdleProof(POD, 999, 1000, True), InferenceIdleProof(POD, 999, 980, False)):
            provider = self.provider(idle_probe=lambda tag, pod: proof)
            fact = provider.reconcile(TAG, POD)
            self.assertEqual(fact.idle_confirmed, proof == InferenceIdleProof(POD, 999, 980, True))
        self.assertFalse(self.provider(idle_probe=lambda *args: {"idle": True}).reconcile(TAG, POD).idle_confirmed)

    def test_removed_statement_exact_identity_final_ledger_cost_not_uptime(self):
        self.api.statements[POD] = statement()
        provider = self.provider()
        fact = provider.reconcile(TAG, POD)
        self.assertEqual((fact.state, fact.instance_id, fact.actual_cost_microusd), ("destroyed", POD, 123457))
        self.assertEqual(provider.billing(TAG, POD), 123457)
        self.assertEqual(provider.destroy(TAG, POD), fact)
        self.assertEqual(self.api.count("DELETE"), 0)

    def test_removed_statement_missing_or_invalid_amount_keeps_cost_pending(self):
        for total in (None, "NaN", "-1", True):
            self.api.statements[POD] = statement(total=total)
            fact = self.provider().reconcile(TAG, POD)
            self.assertEqual(fact.state, "destroyed")
            self.assertIsNone(fact.actual_cost_microusd)

    def test_live_statement_naive_utc_and_fractional_seconds_are_supported(self):
        data = statement()
        data.update(created_at=stamp(100).removesuffix("+00:00"),
            removed_at=stamp(900.25).removesuffix("+00:00"), billed_seconds=800.25)
        self.api.statements[POD] = data
        fact = self.provider().reconcile(TAG, POD)
        self.assertEqual((fact.state, fact.actual_cost_microusd), ("destroyed", 123457))

    def test_bad_fractional_billing_keeps_removed_fact_but_money_pending(self):
        for seconds in ("NaN", "-0.1", True, "9000000", None):
            data = statement(); data["billed_seconds"] = seconds
            self.api.statements[POD] = data
            with self.subTest(seconds=seconds):
                fact = self.provider().reconcile(TAG, POD)
                self.assertEqual(fact.state, "destroyed")
                self.assertIsNone(fact.actual_cost_microusd)

    def test_mixed_timezone_statement_cannot_prove_removed(self):
        data = statement(); data["created_at"] = stamp(100).removesuffix("+00:00")
        self.api.statements[POD] = data
        with self.assertRaises(LiumError):
            self.provider().reconcile(TAG, POD)

    def test_statement_tag_id_removed_time_mismatch_cannot_release_or_settle(self):
        for change in ({"pod_id": OTHER}, {"pod_name": "sixnine-"+OTHER}, {"removed_at": stamp(2000)},
                       {"created_at": stamp(950)}, {"removed_at": "no-time"}):
            data = statement()
            data.update(change)
            self.api.statements[POD] = data
            with self.subTest(change=change), self.assertRaises(LiumError):
                self.provider().reconcile(TAG, POD)

    def test_delete_accepted_alone_still_unknown_final_statement_confirms(self):
        self.api.pods = [self.pod()]
        provider = self.provider()
        fact = provider.destroy(TAG, POD)
        self.assertEqual(fact.state, "unknown")
        self.assertIsNone(fact.actual_cost_microusd)
        self.assertEqual(self.api.count("DELETE"), 1)
        with self.assertRaisesRegex(LiumError, "destruction_already_submitted"):
            provider.destroy(TAG, POD)
        self.api.pods = []
        self.api.statements[POD] = statement()
        self.assertEqual(provider.reconcile(TAG, POD).state, "destroyed")
        self.assertEqual(self.api.count("DELETE"), 1)

    def test_delete_removed_statement_can_confirm_same_request(self):
        self.api.pods = [self.pod()]
        def deleted(request):
            self.api.pods = []
            self.api.statements[POD] = statement(total="0")
        self.api.on_delete = deleted
        fact = self.provider().destroy(TAG, POD)
        self.assertEqual((fact.state, fact.actual_cost_microusd), ("destroyed", 0))

    def test_permission_404_after_delete_timeout_never_proves_destroyed(self):
        self.api.pods = [self.pod()]
        def deleted(request):
            self.api.pods = []
            raise httpx.ReadTimeout("offline uncertain delete", request=request)
        self.api.on_delete = deleted
        provider = self.provider()
        with self.assertRaisesRegex(LiumError, "request_unconfirmed"):
            provider.destroy(TAG, POD)
        self.assertEqual(provider.reconcile(TAG, POD).state, "unknown")
        self.assertEqual(self.api.count("DELETE"), 1)

    def test_redirect_error_malformed_or_large_response_sanitized_without_retry(self):
        for response in (httpx.Response(302, headers={"Location": "https://evil.invalid/?token=secret"}),
                         httpx.Response(403, text="secret-body"), httpx.Response(200, text="secret-not-json"),
                         httpx.Response(200, content=b"x"*(MAX_RESPONSE_BYTES+1))):
            self.api.hook = lambda request: response
            with self.assertRaises(LiumError) as caught:
                self.provider().reconcile(TAG)
            self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(len(self.api.calls), 4)

    def test_unsafe_id_cannot_become_url_or_header(self):
        provider = self.provider()
        for tag in ("../../pods", "?token=secret", "tag\n", "non-uuid"):
            with self.assertRaises(LiumError):
                provider.reconcile(tag, POD)
        self.assertEqual(self.api.calls, [])


class LiumCoordinatorTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.api = MockAPI()
        self.provider = LiumProvider(enabled=True, manifests=(manifest(),),
            loader=lambda *a, **k: SimpleNamespace(service="lium", profile=PROFILE, base_url=BASE_URL,
                primary_key_variable="LIUM_API_KEY", api_key="offline-test-key-never-real"),
            transport=httpx.MockTransport(self.api), clock=lambda: self.now)
        self.scaler = ScaleCoordinator(self.repo, provider=self.provider, enabled=True)
        self.repo.configure_pool("lium-test", max_instances=1, max_physical_gpus=2)
        self.policy = ScalePolicy(dry_run=False, max_instances=1, max_physical_gpus=2, cold_start_s=30,
            new_instance_physical_gpus=2,
            min_improvement_s=1, cooldown_s=0, queue_target_s=60, approved_remaining_microusd=10_000_000,
            instance_reservation_microusd=4_000_000, hard_deadline=10_000)
        self.demands = [Demand("job-"+str(i), "superdan", 900, 120) for i in range(12)]

    def tearDown(self):
        self.provider.close()
        super().tearDown()

    def tick(self, leader="leader"):
        return self.scaler.tick(leader, self.scope, "lium-test", self.demands, [], policy=self.policy,
            launch=launch(), budget_account_ids=["owner-budget"])

    def test_lost_rent_response_persists_unknown_and_retries_never_post_again(self):
        def rent(request):
            row = self.repo.list_instance_intents()[0]
            self.assertEqual(row["state"], "creating")
            self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 4_000_000)
            raise httpx.ReadTimeout("offline lost response", request=request)
        self.api.on_rent = rent
        self.tick()
        self.now += 16
        self.tick()
        self.assertEqual(self.repo.list_instance_intents()[0]["state"], "creation_unknown")
        for _ in range(3):
            self.now += 16
            self.tick()
        self.assertEqual(self.api.count("POST"), 1)
        self.assertEqual(len(self.repo.list_instance_intents()), 1)
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 4_000_000)

    def test_capacity_or_budget_underreported_blocks_before_intent_and_network(self):
        for changes in ({"new_instance_physical_gpus": 1}, {"new_instance_slots": 2},
                        {"instance_reservation_microusd": 100_000}):
            self.policy = replace(self.policy, **changes)
            self.tick()
            self.now += 16
            result = self.tick()
            self.assertEqual(result["reason"], "provider_manifest_or_reservation_mismatch")
            self.assertEqual(self.repo.list_instance_intents(), [])
            self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 0)
            self.now += 16
            self.policy = replace(self.policy, new_instance_physical_gpus=2, new_instance_slots=1,
                instance_reservation_microusd=4_000_000)
        self.assertEqual(self.api.calls, [])

    def test_new_provider_process_reconciles_exact_tag_after_lost_response(self):
        def rent(request):
            payload = json.loads(request.content)
            self.api.pods = [{"id": POD, "name": payload["pod_name"], "status": "RUNNING"}]
            raise httpx.ReadTimeout("offline lost response", request=request)
        self.api.on_rent = rent
        self.tick()
        self.now += 16
        self.tick()
        self.provider.close()
        # A fresh process has no in-memory submitted-tag set. Persisted intent
        # still prevents create; coordinator only invokes exact-tag reconcile.
        self.provider = LiumProvider(enabled=True, manifests=(manifest(),),
            loader=lambda *a, **k: SimpleNamespace(service="lium", profile=PROFILE, base_url=BASE_URL,
                primary_key_variable="LIUM_API_KEY", api_key="offline-test-key-never-real"),
            transport=httpx.MockTransport(self.api), clock=lambda: self.now)
        self.scaler = ScaleCoordinator(self.repo, provider=self.provider, enabled=True)
        self.now += 61
        self.tick(leader="next-leader")
        row = self.repo.list_instance_intents()[0]
        self.assertEqual((row["state"], row["provider_instance_id"]), ("starting", POD))
        self.assertEqual(self.api.count("POST"), 1)


if __name__ == "__main__":
    unittest.main()
