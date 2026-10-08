"""Bounded, operator-only cloud helper for a named standalone GPU qualification.

This private experiment journal is subordinate evidence, not the production
capacity/job ledger. Use one shared --run-dir for the entire approved pilot;
never reset/copy it to retry an uncertain rental. No imports perform HTTP.
Only `rent --execute`, `ensure-ttl --execute` and
`destroy --execute --idle-confirmed` mutate Lium.
The existing provider may also shorten the new rental's removal schedule while
renting. Status/reconcile/connection are strictly GET-only at the provider.
Provider TTL is independent of this local process; no local watchdog is claimed.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import ipaddress
import json
import os
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from studio_platform.lium_provider import LiumError, LiumManifest, LiumProvider
from studio_platform.rent_journal import RentJournal
from studio_platform.scaler import LaunchSpec

PUBLIC_URL = "https://lium.io/api/public/v1/nodes"
LOCK_TAG = "b8e72577-1d22-4b86-b5de-a83936482dcb"
PRICE_CAP = 850_000
RESERVATION = PRICE_CAP * 2
BUDGET = 5_000_000
MAX_RECORDS = 2
MAX_BYTES = 4 * 1024 * 1024
GPU_NAMES = {"RTX 5090", "NVIDIA GeForce RTX 5090", "NVIDIA RTX 5090"}


@dataclass(frozen=True)
class PilotPolicy:
    name: str
    purpose: str
    pool_id: str
    capability: str
    gpu_names: tuple[str, ...]
    price_cap_microusd: int
    budget_microusd: int
    max_records: int
    host_gpu_count: int
    rental_gpu_count: int
    min_ram_gib: int
    preferred_ram_gib: int
    min_disk_gib: int
    min_public_vram_gb: int
    min_authenticated_vram_mib: int
    termination_hours: int = 2

    @property
    def reservation_microusd(self):
        return self.price_cap_microusd * self.rental_gpu_count * self.termination_hours

    def snapshot(self):
        result = asdict(self)
        result["gpu_names"] = list(self.gpu_names)
        return result


POLICY_5090 = PilotPolicy(
    "5090", "standalone-5090-qualification-not-production", "h3-5090-pilot", "h3-pruned-int8",
    tuple(sorted(GPU_NAMES)), PRICE_CAP, BUDGET, MAX_RECORDS, 1, 1, 96, 96, 250, 31, 31_000)
PRO6000_NAMES = tuple(sorted(
    prefix + "RTX PRO 6000 Blackwell " + edition + " Edition"
    for prefix in ("", "NVIDIA ") for edition in ("Server", "Workstation")))
# Named policies are fixed envelopes, not spending authorization. The root
# operator must select the approved topology; a multi-card host is one node.
POLICY_PRO6000 = PilotPolicy(
    "pro6000-comparison", "standalone-pro6000-dual-card-comparison-not-production",
    "h3-pro6000-comparison", "h3-unpruned-comparison", PRO6000_NAMES,
    2_000_000, 8_000_000, 1, 2, 2, 256, 256, 350, 95, 95_000)
POLICIES = {policy.name: policy for policy in (POLICY_5090, POLICY_PRO6000)}


class PilotError(LiumError):
    """Only static codes are returned to the terminal."""


def policy_named(name):
    if not isinstance(name, str) or name not in POLICIES:
        raise PilotError("unknown_pilot_policy")
    return POLICIES[name]


def identity(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
        return value
    except (ValueError, TypeError, AttributeError):
        raise PilotError("invalid_uuid") from None


def number(value):
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError
        return result
    except (ValueError, InvalidOperation):
        raise PilotError("invalid_numeric_metadata") from None


def stamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    except (ValueError, TypeError, AttributeError):
        raise PilotError("invalid_provider_timestamp") from None


def public_fetch():
    """Unauthenticated bounded GET; no redirects, proxy environment or retry."""
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=20) as client:
            with client.stream("GET", PUBLIC_URL) as response:
                if response.status_code != 200:
                    raise ValueError
                data = bytearray()
                for part in response.iter_bytes():
                    data.extend(part)
                    if len(data) > MAX_BYTES:
                        raise ValueError
        return json.loads(data)
    except Exception:
        raise PilotError("public_inventory_unavailable") from None


def public_rows(feed, now):
    if (not isinstance(feed, dict) or not isinstance(feed.get("nodes"), list)
            or len(feed["nodes"]) > 4096 or not -30 <= now - stamp(feed.get("generated_at")) <= 180):
        raise PilotError("public_inventory_stale_or_invalid")
    return feed["nodes"]


def node_id(row):
    # Accept the two explicitly named identity fields only when consistent.
    values = [row[key] for key in ("id", "node_id") if key in row]
    if not values or any(value != values[0] for value in values):
        raise PilotError("public_node_identity_unconfirmed")
    return identity(values[0])


def qualify(row, policy=POLICY_5090):
    """Conservative decimal-GB conversion and exact approved host topology.

    This is advertised capacity, NOT a cgroup allocation or runtime proof. Check
    actual RAM/cgroup, CPUs and the cache mount before downloading any weights.
    """
    reasons = []
    checks = (
        (row.get("gpu_model") in policy.gpu_names, "gpu_model_outside_policy"),
        (type(row.get("gpu_count")) is int and row["gpu_count"] == policy.host_gpu_count,
         "not_single_gpu_host" if policy.host_gpu_count == 1 else "host_gpu_count_outside_policy"),
        (type(row.get("available_gpu_count")) is int
         and row["available_gpu_count"] >= policy.rental_gpu_count, "no_free_gpu"),
        (type(row.get("min_rentable_gpu_count")) is int
         and row["min_rentable_gpu_count"] == policy.rental_gpu_count, "rental_granularity_unconfirmed"),
    )
    reasons.extend(reason for valid, reason in checks if not valid)
    for field, minimum, code in (("cpu_count", 12, "cpu_below_12"),
            ("ram_gb", Decimal(policy.min_ram_gib * 1024**3) / 10**9, f"ram_below_{policy.min_ram_gib}_gib"),
            ("disk_free_gb", Decimal(policy.min_disk_gib * 1024**3) / 10**9, f"free_disk_below_{policy.min_disk_gib}_gib"),
            ("network_download_mbps", 500, "download_below_500_mbps"),
            ("gpu_memory_gb", policy.min_public_vram_gb, "vram_below_policy_class")):
        try:
            if number(row.get(field)) < minimum:
                reasons.append(code)
        except PilotError:
            reasons.append("unverified_" + field)
    try:
        if not 0 < number(row.get("price_per_gpu_hour")) <= Decimal(policy.price_cap_microusd) / 1_000_000:
            reasons.append("price_above_cap_or_invalid")
    except PilotError:
        reasons.append("unverified_price")
    return reasons


class ExactPilotProvider(LiumProvider):
    def __init__(self, *, fetch=public_fetch, policy="5090", **kwargs):
        self.policy = policy_named(policy)
        super().__init__(**kwargs)
        self.fetch = fetch

    def _select_offer(self, manifest):
        """Recheck both feeds immediately before the original provider rent POST.

        Do not call the general selector: its exact executor is only a preference
        when GPU filters are enabled, and its direct path does not gate RAM/disk.
        """
        if (manifest.configuration_id != self.policy.pool_id or manifest.model_id != self.policy.capability
                or manifest.gpu_count != self.policy.rental_gpu_count
                or manifest.execution_slots != self.policy.rental_gpu_count
                or manifest.max_price_per_gpu_hour_microusd != self.policy.price_cap_microusd
                or manifest.termination_hours != self.policy.termination_hours):
            raise PilotError("manifest_policy_mismatch")
        matches = [row for row in public_rows(self.fetch(), self.clock())
                   if node_id(row) == manifest.executor_id]
        if len(matches) != 1 or qualify(matches[0], self.policy):
            raise PilotError("selected_public_node_outside_limits")
        rows = [row for row in self._rows("executors?available=true")
                if row.get("id") == manifest.executor_id]
        if len(rows) != 1:
            raise PilotError("selected_executor_unavailable")
        row = rows[0]
        details = row.get("specs", {}).get("gpu", {}).get("details", [])
        if (type(row.get("gpu_count")) is not int or row["gpu_count"] != self.policy.host_gpu_count
                or type(row.get("available_gpu_count")) is not int
                or row["available_gpu_count"] != self.policy.rental_gpu_count
                or not 0 < number(row.get("price_per_gpu")) <= Decimal(self.policy.price_cap_microusd) / 1_000_000
                or not isinstance(details, list) or len(details) != self.policy.host_gpu_count
                or any(gpu.get("name") not in self.policy.gpu_names
                       or number(gpu.get("capacity")) < self.policy.min_authenticated_vram_mib for gpu in details)):
            raise PilotError("selected_executor_identity_or_price_unconfirmed")
        templates = [row for row in self._rows("templates") if row.get("id") == manifest.template_id]
        if len(templates) != 1:
            raise PilotError("selected_template_unavailable")
        return manifest.executor_id


class Pilot:
    def __init__(self, directory, *, policy="5090", fetch=public_fetch, factory=ExactPilotProvider, clock=time.time):
        self.policy = policy_named(policy)
        self.directory = Path(directory)
        if not self.directory.is_absolute() or self.directory.is_symlink():
            raise PilotError("absolute_private_run_directory_required")
        self.directory = self.directory.resolve()
        self.path = self.directory / "pilot.json"
        self.fetch, self.factory, self.clock = fetch, factory, clock
        self.lock = RentJournal(self.directory / "locks")

    def _check_policy_binding(self, value):
        if (value.get("version") == 1 and self.policy == POLICY_5090
                and value.get("purpose") == POLICY_5090.purpose and "policy" not in value):
            return  # Historical 5090 journals remain bound to their original envelope.
        if value.get("version") != 2:
            raise PilotError("pilot_policy_mismatch")
        if (value.get("purpose") != self.policy.purpose
                or json.dumps(value.get("policy"), sort_keys=True, allow_nan=False)
                != json.dumps(self.policy.snapshot(), sort_keys=True, allow_nan=False)):
            raise PilotError("pilot_policy_mismatch")

    def read(self):
        if not self.path.exists():
            return {"version": 2, "purpose": self.policy.purpose,
                    "policy": self.policy.snapshot(), "records": []}
        if self.path.is_symlink() or self.path.stat().st_size > 131072:
            raise PilotError("pilot_state_invalid")
        value = json.loads(self.path.read_text(encoding="utf-8"))
        self._check_policy_binding(value)
        if (not isinstance(value.get("records"), list) or len(value["records"]) > self.policy.max_records):
            raise PilotError("pilot_state_invalid")
        tags = set()
        for record in value["records"]:
            tag = identity(record["tag"])
            if tag in tags or record["deadline"] != record["created_at"] + self.policy.termination_hours * 3600:
                raise PilotError("pilot_state_invalid")
            if (value["version"] == 2 and (record.get("gpu_count") != self.policy.rental_gpu_count
                    or record.get("reservation_microusd") != self.policy.reservation_microusd)):
                raise PilotError("pilot_record_policy_mismatch")
            tags.add(tag)
            identity(record["executor_id"]); identity(record["template_id"])
            if record.get("pod_id"):
                identity(record["pod_id"])
        return value

    def save(self, state):
        self._check_policy_binding(state)
        if self.path.exists():
            self.read()  # Refuse overwriting a directory bound to another policy.
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.directory / (".pilot-" + uuid.uuid4().hex + ".next")
        data = json.dumps(state, sort_keys=True, allow_nan=False).encode()
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(target, self.path)
        if os.name != "nt":
            descriptor = os.open(self.directory, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def provider(self, record=None, public_key=None):
        manifests = ()
        if record is not None and public_key is not None:
            manifests = (LiumManifest(self.policy.pool_id, self.policy.capability, record["executor_id"],
                record["template_id"], self.policy.rental_gpu_count, self.policy.price_cap_microusd,
                self.policy.termination_hours, public_key, record["deadline"] + 180,
                allow_preflight_only_price_cap=True, execution_slots=self.policy.rental_gpu_count),)
        return self.factory(enabled=True, manifests=manifests, fetch=self.fetch, clock=self.clock,
                            policy=self.policy.name, journal_dir=self.directory / "rent-journal")

    def inventory(self):
        result = []
        for row in public_rows(self.fetch(), self.clock()):
            if row.get("gpu_model") not in self.policy.gpu_names:
                continue
            rejections = qualify(row, self.policy)
            preferred_ram = ("unverified_ram_gb" not in rejections
                and number(row.get("ram_gb")) * 10**9 >= self.policy.preferred_ram_gib * 1024**3)
            result.append({"executor_id": node_id(row), "rejections": rejections,
                "preferred_ram_met": preferred_ram,
                **{key: row.get(key) for key in ("gpu_model", "gpu_count", "available_gpu_count", "cpu_count",
                    "min_rentable_gpu_count", "ram_gb", "disk_free_gb", "network_download_mbps", "price_per_gpu_hour")}})
        return {"policy": self.policy.snapshot(), "candidates": result, "allocation_verified": False}

    def overview(self):
        provider = self.provider()
        try:
            # No raw pod/template payloads, arbitrary commands, env or URLs.
            return {"pods": [{"id": identity(row.get("id")), "name": provider._name(row),
                             "status": row.get("status")} for row in provider._rows("pods")],
                    "templates": [{"id": identity(row.get("id")), "name": row.get("name")}
                                  for row in provider._rows("templates")]}
        finally:
            provider.close()

    @staticmethod
    def summary(record):
        return {key: record.get(key) for key in ("tag", "executor_id", "template_id", "pod_id", "phase",
            "created_at", "deadline", "collection_deadline", "ttl_verified", "destroy_started_at",
            "gpu_count", "reservation_microusd", "actual_cost_microusd", "last_error")}

    def rent(self, executor_id, template_id, public_key):
        identity(executor_id); identity(template_id)
        with self.lock.ttl_lock(LOCK_TAG):
            state = self.read()
            # Reusing the same executor never means another attempt after an
            # uncertain POST or a process crash. It returns its original record.
            old = next((row for row in state["records"] if row["executor_id"] == executor_id), None)
            if old:
                return {"replay_refused": True, **self.summary(old)}
            if len(state["records"]) >= self.policy.max_records:
                raise PilotError("pilot_rental_count_limit")
            unresolved = [row for row in state["records"]
                          if row["phase"] not in ("destroyed", "not_created")
                          or row.get("actual_cost_microusd") is None]
            if unresolved:
                # A PENDING -> RUNNING transition can reset the provider TTL.
                # Cached readiness never authorizes another rental. Invalidate
                # it durably before GET-only refresh, including when GET fails.
                for prior in unresolved:
                    prior["ttl_verified"] = False
                    prior.pop("connection", None)
                self.save(state)
                prior_provider = self.provider()
                try:
                    for prior in unresolved:
                        self._refresh(state, prior, prior_provider)
                finally:
                    prior_provider.close()
            held = sum(row.get("actual_cost_microusd") if row.get("actual_cost_microusd") is not None
                       else self.policy.reservation_microusd for row in state["records"])
            if held + self.policy.reservation_microusd > self.policy.budget_microusd or any(
                    row["phase"] in ("creating", "unknown", "destroy_unknown")
                    or row["phase"] not in ("destroyed", "not_created")
                        and (row.get("destroy_started_at") or not row.get("ttl_verified"))
                    for row in state["records"]):
                raise PilotError("pilot_budget_or_unknown_outcome_hold")
            created = self.clock()
            deadline = created + self.policy.termination_hours * 3600
            record = {"tag": str(uuid.uuid4()), "executor_id": executor_id, "template_id": template_id,
                      "created_at": created, "deadline": deadline, "collection_deadline": deadline - 600,
                      "phase": "creating", "pod_id": None, "ttl_verified": False,
                      "gpu_count": self.policy.rental_gpu_count,
                      "reservation_microusd": self.policy.reservation_microusd, "actual_cost_microusd": None}
            provider = self.provider(record, public_key)
            try:
                owned = {row.get("pod_id") for row in state["records"]}
                for pod in provider._rows("pods"):
                    name = provider._name(pod) or ""
                    if name.startswith("sixnine-") and pod.get("id") not in owned:
                        raise PilotError("existing_sixnine_rental_requires_reconciliation")
                manifest = next(iter(provider._manifests.values()))
                provider._select_offer(manifest)  # cheap preflight before consuming an attempt
                launch = LaunchSpec("lium", self.policy.pool_id, self.policy.capability,
                                    offer_id=executor_id, image_id=template_id)
                provider.validate_launch(launch, physical_gpus=self.policy.rental_gpu_count,
                    slots=self.policy.rental_gpu_count, reserved_cost_microusd=self.policy.reservation_microusd,
                    hard_deadline=record["deadline"] + 180)
                state["records"].append(record)
                self.save(state)  # durable single intent BEFORE any mutating request
                try:
                    # Extra coordinator margin prevents the provider's integer
                    # hour floor turning 2h into1h. The absolute TTL remains the
                    # ORIGINAL intent_created_at+2h, never this margin.
                    fact = provider.create_for_intent(record["tag"], launch,
                        hard_deadline=record["deadline"] + 180, intent_created_at=created)
                    record["pod_id"] = fact.instance_id
                    record["phase"] = fact.state
                    if fact.state == "not_created":
                        record["actual_cost_microusd"] = 0
                    self.save(state)
                except Exception:
                    record["phase"] = "unknown"; record["last_error"] = "rent_outcome_requires_reconciliation"
                    self.save(state)
                self._refresh(state, record, provider)
                return self.summary(record)
            finally:
                provider.close()

    def _refresh(self, state, record, provider):
        """GET-only. Never call provider.reconcile/lifetime/ssh_connection here:
        those may shorten a newly created rental's removal schedule.
        """
        tag = record["tag"]
        marker = provider._journal_read(tag)
        if marker and marker.get("instance_id"):
            if record["pod_id"] not in (None, marker["instance_id"]):
                raise PilotError("journal_pod_identity_conflict")
            record["pod_id"] = marker["instance_id"]
        if marker and marker["phase"] in ("not_submitted", "rejected") and record["pod_id"] is None:
            record.update(phase="not_created", actual_cost_microusd=0)
            self.save(state)
            return
        pod = provider._exact_pod(tag, record["pod_id"])
        if pod is not None:
            record["pod_id"] = identity(pod["id"])
            self.save(state)  # preserve identity even if subsequent GET fails
        if record["pod_id"]:
            removed = provider._removed_statement(tag, record["pod_id"])
            if removed:
                record.update(phase="destroyed", ttl_verified=False,
                              actual_cost_microusd=removed.actual_cost_microusd)
                record.pop("connection", None)
                self.save(state)
                return
        if pod is None:
            if record.get("phase") != "not_created":
                record["phase"] = "unknown"
            self.save(state)
            return
        detail = provider._request("GET", "pods/" + record["pod_id"])
        if detail.get("id") != record["pod_id"] or provider._name(detail) != "sixnine-" + tag:
            raise PilotError("pod_detail_identity_conflict")
        record["phase"] = {"RUNNING": "running", "PENDING": "starting", "FAILED": "provider_failed",
                           "STOPPED": "provider_stopped"}.get(detail.get("status"), "unknown")
        record["ttl_verified"] = False
        try:
            start, end = stamp(detail["created_at"]), stamp(detail["removal_scheduled_at"])
            record["ttl_verified"] = (abs(start-record["created_at"]) <= 300
                and self.clock() < end <= record["deadline"] and end > start)
            if record["ttl_verified"]:
                record["verified_removal_at"] = end
                record["collection_deadline"] = min(record["collection_deadline"], end - 600)
        except (KeyError, PilotError):
            pass
        if detail.get("status") == "RUNNING" and record["ttl_verified"]:
            try:
                host = str(ipaddress.ip_address(detail["executor"]["executor_ip_address"]))
                raw_port = detail["ports_mapping"]["22"]
                port = int(raw_port)
                if isinstance(raw_port, bool) or not ipaddress.ip_address(host).is_global or not 1 <= port <= 65535:
                    raise ValueError
                record["connection"] = {"host": host, "port": port, "username": "root", "instance_id": record["pod_id"]}
            except (KeyError, ValueError, TypeError):
                record.pop("connection", None)
        else:
            record.pop("connection", None)
        self.save(state)

    def reconcile(self, tag=None, *, connection=False):
        with self.lock.ttl_lock(LOCK_TAG):
            state = self.read()
            records = [row for row in state["records"] if tag is None or row["tag"] == identity(tag)]
            if tag and not records:
                raise PilotError("pilot_tag_not_owned")
            provider = self.provider()
            try:
                for record in records:
                    self._refresh(state, record, provider)
                if connection:
                    if len(records) != 1 or not records[0].get("ttl_verified") or not records[0].get("connection"):
                        raise PilotError("ttl_verified_connection_not_ready")
                    return {**records[0]["connection"], "allocation_verified": False,
                            "collection_deadline": records[0]["collection_deadline"]}
                return {"records": [self.summary(row) for row in records]}
            finally:
                provider.close()

    def ensure_ttl(self, tag):
        """Explicit mutation: only shorten the original provider TTL binding."""
        with self.lock.ttl_lock(LOCK_TAG):
            state = self.read()
            record = next((row for row in state["records"] if row["tag"] == identity(tag)), None)
            if record is None:
                raise PilotError("pilot_tag_not_owned")
            provider = self.provider()
            try:
                self._refresh(state, record, provider)
                if record["phase"] in ("destroyed", "not_created"):
                    return self.summary(record)
                if not record["pod_id"] or record.get("destroy_started_at"):
                    raise PilotError("ttl_identity_or_retirement_hold")
                provider._ensure_absolute_ttl(tag, record["pod_id"])
                self._refresh(state, record, provider)
                return self.summary(record)
            finally:
                provider.close()

    def destroy(self, tag, *, idle_confirmed):
        if idle_confirmed is not True:
            raise PilotError("operator_must_confirm_no_inference_or_collection")
        with self.lock.ttl_lock(LOCK_TAG):
            state = self.read()
            record = next((row for row in state["records"] if row["tag"] == identity(tag)), None)
            if record is None:
                raise PilotError("pilot_tag_not_owned")
            provider = self.provider()
            try:
                self._refresh(state, record, provider)
                if record["phase"] in ("destroyed", "not_created"):
                    return self.summary(record)
                if record.get("destroy_started_at"):
                    return {"replay_refused": True, **self.summary(record)}
                if record["pod_id"] is None:
                    raise PilotError("unknown_rental_identity_reconcile_only")
                record["destroy_started_at"] = self.clock()
                record["phase"] = "destroy_unknown"
                self.save(state)  # persist intent before the sole DELETE
                try:
                    fact = provider.destroy(tag, record["pod_id"])
                    if fact.state == "destroyed":
                        record.update(phase="destroyed", actual_cost_microusd=fact.actual_cost_microusd)
                        self.save(state)
                except Exception:
                    record["last_error"] = "destroy_outcome_requires_reconciliation"
                    self.save(state)
                self._refresh(state, record, provider)
                return self.summary(record)
            finally:
                provider.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True, help="one private durable directory for the entire pilot")
    parser.add_argument("--policy", choices=tuple(POLICIES), default="5090",
                        help="fixed approved envelope; use a distinct run directory for each policy")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("inventory")
    sub.add_parser("overview", help="GET-only safe pod/template identities for the root operator")
    for action in ("status", "reconcile", "connection"):
        command = sub.add_parser(action)
        command.add_argument("--tag", required=action == "connection")
    rent = sub.add_parser("rent")
    rent.add_argument("--executor-id", required=True)
    rent.add_argument("--template-id", required=True)
    rent.add_argument("--public-key-file", type=Path, required=True)
    rent.add_argument("--execute", action="store_true")
    destroy = sub.add_parser("destroy")
    destroy.add_argument("--tag", required=True)
    destroy.add_argument("--execute", action="store_true")
    destroy.add_argument("--idle-confirmed", action="store_true")
    ttl = sub.add_parser("ensure-ttl")
    ttl.add_argument("--tag", required=True)
    ttl.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        pilot = Pilot(args.run_dir, policy=args.policy)
        if args.action == "inventory":
            result = pilot.inventory()
        elif args.action == "overview":
            result = pilot.overview()
        elif args.action in ("status", "reconcile", "connection"):
            result = pilot.reconcile(args.tag, connection=args.action == "connection")
        elif not args.execute:
            raise PilotError("mutation_requires_explicit_execute")
        elif args.action == "rent":
            if args.public_key_file.stat().st_size > 8192:
                raise PilotError("invalid_public_key_file")
            result = pilot.rent(args.executor_id, args.template_id, args.public_key_file.read_text().strip())
        elif args.action == "ensure-ttl":
            result = pilot.ensure_ttl(args.tag)
        else:
            result = pilot.destroy(args.tag, idle_confirmed=args.idle_confirmed)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, PilotError) else "pilot_operation_failed_reconcile_original_state"
        print(json.dumps({"error": code, "automatic_retry": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
