"""Model-first discovery and exact cached-offer validation; no provider calls."""
from dataclasses import replace
import copy
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from studio_platform.auth import Principal
from studio_platform.capacity_candidates import candidates_projection, resolve_selected_offer
from studio_platform.capacity_inventory import allocation_resources
from studio_platform.capacity_market import publish_observation
from studio_platform.operator_capacity import DeploymentBinding, OperatorCapacity, OperatorError, OperatorRegistry
from studio_platform.operator_routes import register_routes
from studio_platform.runtime_catalog import PROFILE_IDS, get_profile, public_catalog
from studio_platform.scaler import LaunchSpec
from test_platform_repository import LedgerCase


PRUNED = "MiniMax-H3-Pruned-Rank8-INT8"
BASE = "MiniMax-H3-Base-INT8"
PRO = "RTX PRO 6000 Blackwell Workstation Edition"


def offer(identity="executor-1", provider="lium", gpu="RTX 5090", **changes):
    return {"provider": provider, "offer_id": identity, "gpu_type": gpu, "gpu_count": 1,
            "available_count": 1, "ram_gib": 125, "disk_gib": 320, "download_mbps": 300,
            "country": "SG", "cpu_cores": 32, "hourly_cost_microusd": 750000,
            "price_per_gpu_hour_microusd": 750000, "kind": "executor" if provider == "lium" else "vm",
            "unverified_fields": [], **changes}


class CandidateTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.registry = OperatorRegistry(catalog=public_catalog, qualified_providers=("lium", "targon"))

    def publish(self, provider, rows=(), status="ok", observed=None):
        publish_observation(self.repo, {"provider": provider, "status": status,
            "observed_at": self.now if observed is None else observed, "offers": list(rows)})

    def project(self, model=PRUNED, ttl=7200):
        with self.repo.engine.connect() as connection:
            return candidates_projection(connection, self.registry, model, "ref", ttl, self.now)

    def resolve(self, choice):
        with self.repo.engine.connect() as connection:
            return resolve_selected_offer(connection, self.registry, choice, self.now)

    def qualify(self, profile_id=PROFILE_IDS[0], gpu="RTX 5090", provider="lium", **changes):
        profile = get_profile(profile_id)
        binding = DeploymentBinding(binding_id="candidate-" + provider, runtime_profile_id=profile_id,
            gpu_type=gpu, gpu_count=1, execution_slots=1, pool="test", configuration_id="candidate",
            model_id=profile["model_id"], recipe_ids=("h3-base-ref2va-v1",), engine_manifest_digest="a"*64,
            launch=LaunchSpec(provider, "candidate", profile["model_id"]), scope=self.scope,
            budget_account_ids=("owner-budget",), hourly_cost_microusd=2000000,
            reservation_per_node_microusd=4000000, expires_at=self.now+10000, max_ttl_seconds=7200,
            min_ttl_seconds=120, enabled=True, filters={"min_ram_gib":96, "min_disk_gib":128})
        binding = replace(binding, **changes)
        existing = list(self.registry.bindings.values())
        self.registry = OperatorRegistry(existing+[binding], catalog=public_catalog,
                                         qualified_providers=("lium", "targon"))
        return binding

    def test_groups_only_exact_model_across_profiles_and_explicit_gpu_names(self):
        self.publish("lium", [offer(), offer("pro", gpu=PRO), offer("h100", gpu="H100")])
        self.publish("targon", [offer("rtx6000b-small", "targon", "RTX PRO 6000 Blackwell")])
        result = self.project()
        self.assertEqual({row["offer_id"] for row in result["candidates"]}, {"executor-1", "pro", "rtx6000b-small"})
        self.assertEqual({row["selection"]["runtime_profile_id"] for row in result["candidates"]},
                         {PROFILE_IDS[0], PROFILE_IDS[3]})
        for row in result["candidates"]:
            self.assertEqual(get_profile(row["selection"]["runtime_profile_id"])["model_id"], PRUNED)
            self.assertEqual(row["selection"]["offer_id"], row["offer_id"])
            self.assertEqual(row["selection"]["node_count"], 1)
        self.assertEqual(self.project(BASE)["candidates"], [])

    def test_base_never_infers_gpu_edition_or_changes_precision(self):
        self.publish("lium", [offer("base", gpu=PRO, gpu_count=2, ram_gib=279, disk_gib=400)])
        self.publish("targon", [offer("generic", "targon", "RTX PRO 6000 Blackwell", ram_gib=512, disk_gib=400)])
        rows = self.project(BASE)["candidates"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["selection"]["runtime_profile_id"], PROFILE_IDS[1])
        self.assertEqual(rows[0]["selection"]["gpu_count"], 2)

    def test_ram_disk_exclude_unknown_specs_remain_and_preferences_do_not_hide_stock(self):
        self.publish("lium", [offer("ram-low", ram_gib=64), offer("disk-low", disk_gib=100),
            offer("unknown", ram_gib=None, disk_gib=None, cpu_cores=None, hourly_cost_microusd=None),
            offer("costly-slow", hourly_cost_microusd=900000, price_per_gpu_hour_microusd=900000,
                  download_mbps=100)])
        self.publish("targon")
        rows = {row["offer_id"]:row for row in self.project()["candidates"]}
        self.assertEqual(set(rows), {"unknown", "costly-slow"})
        self.assertFalse(rows["unknown"]["specs_confirmed"])
        self.assertTrue({"inventory_unknown_ram", "inventory_unknown_disk", "inventory_unknown_cpu",
                         "inventory_unknown_price"} <= set(rows["unknown"]["blockers"]))
        self.assertTrue({"inventory_price_above_guidance", "inventory_bandwidth_below_guidance"}
                        <= set(rows["costly-slow"]["preference_hints"]))
        self.assertNotIn("inventory_price_above_limit", rows["costly-slow"]["blockers"])

    def test_lium_uses_allocated_ram_floor_without_changing_the_displayed_quote(self):
        self.qualify()
        self.publish("lium", [offer("too-small", ram_gib=98), offer("fits", ram_gib=100)])
        rows = self.project()["candidates"]
        self.assertEqual([row["offer_id"] for row in rows], ["fits"])
        self.assertEqual(rows[0]["ram_gib"], 100)
        self.assertEqual(self.resolve(rows[0]["selection"])["ram_gib"], 100)
        with self.assertRaisesRegex(OperatorError, "inventory_ram_below_minimum"):
            self.resolve({**rows[0]["selection"], "offer_id":"too-small"})
        raw = offer(ram_gib=1000)
        self.assertEqual(allocation_resources(raw)["ram_gib"], 990)
        self.assertEqual(raw["ram_gib"], 1000)
        targon = offer("sku", "targon", "RTX PRO 6000 Blackwell", ram_gib=98)
        self.assertEqual(allocation_resources(targon), targon)
        self.assertIsNone(allocation_resources(offer(ram_gib=None))["ram_gib"])

    def test_topology_is_pending_and_full_host_price_is_preserved(self):
        self.qualify()
        rows = [offer(str(count), gpu_count=count, hourly_cost_microusd=count*750000,
                      available_gpu_count=count) for count in (1, 2, 4)]
        self.publish("lium", rows)
        self.publish("targon")
        result = {row["gpu_count"]:row for row in self.project()["candidates"]}
        self.assertEqual(set(result), {1, 2, 4})
        self.assertEqual(result[1]["execution_slots"], 1)
        self.assertEqual(result[1]["qualification"], "qualified")
        for count in (2, 4):
            self.assertIsNone(result[count]["execution_slots"])
            self.assertIn("operator_topology_not_qualified", result[count]["blockers"])
            self.assertEqual(result[count]["hourly_cost_microusd"], count*750000)
            self.assertEqual(result[count]["selection"]["gpu_count"], count)

    def test_rank_deployable_then_whole_price_then_bandwidth_without_provider_priority(self):
        self.qualify()
        self.publish("lium", [offer("qualified", hourly_cost_microusd=900000),
            offer("slow", gpu=PRO, hourly_cost_microusd=800000, download_mbps=100),
            offer("unknown", gpu=PRO, hourly_cost_microusd=800000, download_mbps=None)])
        self.publish("targon", [offer("cheaper", "targon", "RTX PRO 6000 Blackwell",
            hourly_cost_microusd=700000), offer("fast", "targon", "RTX PRO 6000 Blackwell",
            hourly_cost_microusd=800000, download_mbps=1000)])
        rows = self.project()["candidates"]
        self.assertEqual([row["offer_id"] for row in rows], ["qualified", "cheaper", "fast", "slow", "unknown"])
        self.assertEqual(rows[1]["offer_kind"], "resource_sku")
        self.assertEqual(rows[0]["offer_kind"], "executor")

    def test_binding_filters_and_full_worker_count_are_required_for_qualification(self):
        self.qualify(filters={"min_ram_gib":96, "min_disk_gib":128, "min_download_mbps":500,
                              "max_price_per_gpu_hour_microusd":800000, "allowed_countries":["US"]})
        self.publish("lium", [offer("blocked", price_per_gpu_hour_microusd=900000, download_mbps=None)])
        candidate = self.project()["candidates"][0]
        self.assertFalse(candidate["deployment_qualified"])
        self.assertFalse(candidate["specs_confirmed"])
        self.assertIsNone(candidate["execution_slots"])
        self.assertTrue({"inventory_unknown_bandwidth", "inventory_price_above_limit", "inventory_country_mismatch"}
                        <= set(candidate["blockers"]))
        with self.assertRaisesRegex(OperatorError, "inventory_unknown_bandwidth"):
            self.resolve(candidate["selection"])

    def test_every_rented_gpu_requires_an_approved_execution_slot(self):
        self.qualify(PROFILE_IDS[1], PRO, gpu_count=2, execution_slots=1)
        self.publish("lium", [offer("two", gpu=PRO, gpu_count=2, ram_gib=279, disk_gib=400)])
        candidate = self.project(BASE)["candidates"][0]
        self.assertEqual(candidate["qualification"], "unqualified")
        self.assertIsNone(candidate["execution_slots"])
        self.assertIn("operator_execution_slots_unqualified", candidate["blockers"])
        with self.assertRaisesRegex(OperatorError, "operator_execution_slots_unqualified"):
            self.resolve(candidate["selection"])

    def test_targon_sku_and_fixed_lium_executor_must_match_the_approved_launch(self):
        for provider, profile_id, gpu, identity in (("lium", PROFILE_IDS[0], "RTX 5090", "executor-1"),
                ("targon", PROFILE_IDS[3], "RTX PRO 6000 Blackwell", "rtx6000b-small")):
            with self.subTest(provider=provider):
                self.registry = OperatorRegistry(catalog=public_catalog, qualified_providers=("lium", "targon"))
                self.qualify(profile_id, gpu, provider,
                    launch=LaunchSpec(provider, "candidate", PRUNED, offer_id=identity))
                self.publish("lium")
                self.publish("targon")
                self.publish(provider, [offer(identity, provider, gpu), offer("other", provider, gpu)])
                rows = {row["offer_id"]:row for row in self.project()["candidates"]}
                self.assertEqual(rows[identity]["qualification"], "qualified")
                self.assertEqual(self.resolve(rows[identity]["selection"])["offer_id"], identity)
                self.assertIn("operator_offer_selection_mismatch", rows["other"]["blockers"])
                self.assertFalse(rows["other"]["deployment_qualified"])
                with self.assertRaisesRegex(OperatorError, "operator_offer_selection_mismatch"):
                    self.resolve(rows["other"]["selection"])
                self.publish(provider, [offer(identity, provider, gpu, kind="bm")])
                wrong_kind = self.project()["candidates"][0]
                self.assertEqual(wrong_kind["qualification"], "unqualified")
                self.assertIn("operator_offer_selection_mismatch", wrong_kind["blockers"])
                with self.assertRaisesRegex(OperatorError, "operator_offer_selection_mismatch"):
                    self.resolve(wrong_kind["selection"])

    def test_fresh_failure_stale_missing_and_successful_empty_are_distinct(self):
        self.assertEqual(self.project()["reason_code"], "inventory_scan_unconfirmed")
        self.publish("lium", [offer()], observed=self.now-121)
        self.publish("targon", status="error")
        result = self.project()
        self.assertEqual([row["status"] for row in result["providers"]], ["stale", "error"])
        self.assertEqual(result["candidates"], [])
        self.publish("lium")
        self.publish("targon")
        self.assertEqual(self.project()["reason_code"], "inventory_no_matching_stock")
        self.assertEqual(self.project()["status"], "ok")

    def test_partial_executor_is_not_a_fake_available_slice_or_proof_of_absence(self):
        self.publish("lium", [offer("partial", gpu_count=8, available_count=0, available_gpu_count=4,
            min_gpu_count_for_rental=1, unverified_fields=["allocation"], hourly_cost_microusd=6000000)])
        self.publish("targon")
        result = self.project()
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["reason_code"], "inventory_specs_unconfirmed")

    def test_resolver_returns_exact_detached_row_and_rejects_changed_or_stale_stock(self):
        self.qualify()
        original = offer()
        self.publish("lium", [original])
        chosen = self.project()["candidates"][0]["selection"]
        self.assertEqual(self.resolve(chosen), original)
        for change, code in (({"offer_id":"other"}, "operator_offer_unavailable"),
                ({"gpu_count":2}, "operator_offer_selection_mismatch"),
                ({"gpu_type":PRO}, "operator_offer_selection_mismatch"),
                ({"node_count":2}, "operator_offer_quantity_invalid")):
            with self.subTest(change=change), self.assertRaisesRegex(OperatorError, code):
                self.resolve({**chosen, **change})
        self.now += 121
        with self.assertRaisesRegex(OperatorError, "operator_offer_observation_unavailable"):
            self.resolve(chosen)

    def test_resolver_rechecks_specs_quantity_approval_and_ttl(self):
        self.qualify(min_ttl_seconds=3780)
        self.publish("lium", [offer()])
        choice = self.project()["candidates"][0]["selection"]
        for changes, code in (({"available_count":0}, "operator_offer_unavailable"),
                ({"ram_gib":64}, "inventory_ram_below_minimum"),
                ({"ram_gib":None}, "inventory_unknown_ram"),
                ({"unverified_fields":["ram_gib"]}, "inventory_unknown_ram"),
                ({"hourly_cost_microusd":3000000}, "inventory_price_above_limit"),
                ({"unverified_fields":["allocation"]}, "inventory_unknown_allocation")):
            self.publish("lium", [offer(**changes)])
            with self.subTest(changes=changes), self.assertRaisesRegex(OperatorError, code):
                self.resolve(choice)
        self.publish("lium", [offer()])
        with self.assertRaisesRegex(OperatorError, "operator_ttl_below_provider_minimum"):
            self.resolve({**choice, "ttl_seconds":120})
        binding = next(iter(self.registry.bindings.values()))
        self.registry.bindings[binding.binding_id] = replace(binding, enabled=False)
        with self.assertRaisesRegex(OperatorError, "operator_deployment_not_qualified"):
            self.resolve(choice)

    def test_ambiguous_offer_ids_never_select_first_row(self):
        self.qualify()
        self.publish("lium", [offer()])
        choice = self.project()["candidates"][0]["selection"]
        self.publish("lium", [offer(), offer(hourly_cost_microusd=800000)])
        result = self.project()
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["providers"][0]["reason_code"], "inventory_offer_ambiguous")
        with self.assertRaisesRegex(OperatorError, "operator_offer_observation_unavailable"):
            self.resolve(choice)

    def test_http_is_authenticated_bounded_no_store_and_read_only(self):
        self.publish("lium", [offer()])
        service = OperatorCapacity(self.repo, SimpleNamespace(operator_capacity_owners=("superdan",)), self.registry)
        app, actors = FastAPI(), [Principal("superdan", "browser", auth_mode="password")]
        @app.middleware("http")
        async def principal(request: Request, call_next):
            request.state.principal = actors[0]
            return await call_next(request)
        register_routes(app, service=service)
        budget = copy.deepcopy(self.repo.get_budget("owner-budget"))
        query = {"model_id":PRUNED, "mode":"ref", "ttl_seconds":7200}
        with patch("studio_platform.lium_provider._central_loader", side_effect=AssertionError("credential load")), \
             patch("httpx.HTTPTransport.handle_request", side_effect=AssertionError("network")), TestClient(app) as client:
            response = client.get("/v1/operator/capacity/candidates", params=query)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.json()["candidates"][0]["selection"]["offer_id"], "executor-1")
            for invalid in ({"model_id":"unknown"}, {"mode":"other"}, {"ttl_seconds":119}, {"ttl_seconds":14401}):
                self.assertEqual(client.get("/v1/operator/capacity/candidates", params={**query, **invalid}).status_code, 422)
            actors[0] = Principal("supervan", "browser", auth_mode="password")
            self.assertEqual(client.get("/v1/operator/capacity/candidates", params=query).status_code, 403)
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)
