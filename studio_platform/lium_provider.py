"""Explicit, fail-closed Lium adapter; importing it cannot rent or load a key.

This adapter is not wired into a CLI. The durable ScaleCoordinator must commit
the creation/deletion intent before calling it. No HTTP operation is retried.
Only a removed, exactly identified ledger statement proves removed capacity;
404, an empty list, VM failure and an accepted DELETE never do so by themselves.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import importlib
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
import uuid

import httpx

from .repository import Conflict, money
from .scaler import LaunchSpec, ProviderFact, _safe_id


SERVICE = "lium"
PROFILE = "lium--rig-root"
BASE_URL = "https://lium.io/api"
KEY_VARIABLE = "LIUM_API_KEY"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ROWS = 4096


class LiumError(Conflict):
    """Only static, non-secret error codes leave the adapter."""


def _uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value.lower():
            raise ValueError
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise LiumError("lium_invalid_identifier") from None


def _pod_name(tag):
    return "sixnine-" + _uuid(tag)


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise LiumError("lium_invalid_money")
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result > Decimal("9000000000"):
            raise ValueError
        return result
    except (InvalidOperation, ValueError):
        raise LiumError("lium_invalid_money") from None


def _microusd(value):
    # Ledger decimal amounts can have finer precision than our micro-USD unit.
    # Round upward by <1 microUSD; never replace the ledger with an estimate.
    return money(int((_decimal(value) * 1_000_000).to_integral_value(rounding=ROUND_CEILING)))


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 80:
        raise LiumError("lium_invalid_statement_time")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        stamp = result.timestamp()
        if not math.isfinite(stamp):
            raise ValueError
        return stamp
    except (ValueError, OverflowError):
        raise LiumError("lium_invalid_statement_time") from None


def _public_key(value):
    # Accept one public key, never a filename, private PEM or authorized_keys
    # options. The approved operator supplies public content, not private files.
    if (not isinstance(value, str) or len(value) > 16_384 or value != value.strip()
            or any(c in value for c in "\r\n\x00")):
        raise LiumError("lium_public_ssh_key_required")
    parts = value.split()
    if len(parts) < 2 or parts[0] not in {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256"}:
        raise LiumError("lium_public_ssh_key_required")
    try:
        raw = base64.b64decode(parts[1], validate=True)
        length = int.from_bytes(raw[:4], "big")
        if len(raw) < 20 or length < 1 or length > 80 or raw[4:4+length] != parts[0].encode("ascii"):
            raise ValueError
    except (ValueError, UnicodeError):
        raise LiumError("lium_public_ssh_key_required") from None
    return value


@dataclass(frozen=True)
class LiumManifest:
    """Trusted operator input, never inferred from a job's user payload.

    image_id is a Lium template UUID, not a model ID or a Docker image string.
    A fresh GET price cannot enforce an atomic rent price cap. Explicitly accept
    that limitation before enabling a manifest; the default cannot submit rent.
    """
    configuration_id: str
    model_id: str
    executor_id: str
    template_id: str
    gpu_count: int
    max_price_per_gpu_hour_microusd: int
    termination_hours: int
    user_public_key: str = field(repr=False)
    approved_until: float
    region: str = ""
    allow_preflight_only_price_cap: bool = False
    execution_slots: int = 1

    def __post_init__(self):
        _safe_id(self.configuration_id), _safe_id(self.model_id)
        if self.region:
            _safe_id(self.region)
        _uuid(self.executor_id), _uuid(self.template_id)
        if type(self.gpu_count) is not int or not 1 <= self.gpu_count <= 128:
            raise LiumError("lium_invalid_gpu_count")
        if type(self.execution_slots) is not int or not 1 <= self.execution_slots <= self.gpu_count:
            raise LiumError("lium_invalid_execution_slot_count")
        money(self.max_price_per_gpu_hour_microusd)
        if type(self.termination_hours) is not int or not 1 <= self.termination_hours <= 720:
            raise LiumError("lium_invalid_ttl")
        if (type(self.allow_preflight_only_price_cap) is not bool
                or isinstance(self.approved_until, bool)
                or not isinstance(self.approved_until, (int, float))
                or not math.isfinite(self.approved_until)):
            raise LiumError("lium_invalid_manifest")
        _public_key(self.user_public_key)


@dataclass(frozen=True)
class InferenceIdleProof:
    """A trusted probe checked the exact instance's inference queue separately."""
    instance_id: str
    observed_at: float
    idle_since: float
    idle: bool

    def __post_init__(self):
        _uuid(self.instance_id)
        if (type(self.idle) is not bool or any(isinstance(v, bool) or not isinstance(v, (int, float))
                or not math.isfinite(v) for v in (self.observed_at, self.idle_since))):
            raise LiumError("lium_invalid_idle_proof")


def _central_loader(service, *, profile):
    """Import the existing central implementation lazily, without copying it."""
    configured = os.environ.get("AI_REGISTRY_ROOT", "")
    if configured:
        root = Path(configured)
        if not root.is_absolute():
            raise LiumError("lium_registry_path_invalid")
    elif os.name == "nt":
        root = Path(r"C:\Users\danmo\Desktop\AI-Registry")
    else:
        # This known WSL mount must really exist; an AWS host gets no fallback.
        root = Path("/mnt/c/Users/danmo/Desktop/AI-Registry")
    source = root / "api_registry.py"
    if not source.is_file():
        raise LiumError("lium_central_loader_unavailable")
    try:
        existing = sys.modules.get("api_registry")
        if existing is not None and Path(existing.__file__).resolve() != source.resolve():
            raise ValueError
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        module = importlib.import_module("api_registry")
        if Path(module.__file__).resolve() != source.resolve():
            raise ValueError
        return module.load_api(service, profile=profile)
    except Exception:
        raise LiumError("lium_central_loader_unavailable") from None


class LiumProvider:
    provider_id = SERVICE

    def __init__(self, *, enabled=False, manifests=(), loader=None, transport=None,
                 idle_probe=None, clock=time.time, timeout_s=20, ttl_margin_s=60):
        if (type(enabled) is not bool or isinstance(timeout_s, bool) or not math.isfinite(timeout_s)
                or not 0 < timeout_s <= 30 or isinstance(ttl_margin_s, bool)
                or not math.isfinite(ttl_margin_s) or not 30 <= ttl_margin_s <= 300):
            raise LiumError("lium_invalid_settings")
        manifest_list = tuple(manifests)
        if (any(not isinstance(item, LiumManifest) for item in manifest_list)
                or len({item.configuration_id for item in manifest_list}) != len(manifest_list)):
            raise LiumError("lium_duplicate_or_invalid_manifest")
        self.enabled = enabled
        self._manifests = {item.configuration_id: item for item in manifest_list}
        self._loader = loader or _central_loader
        self._transport, self._idle_probe, self.clock = transport, idle_probe, clock
        self._timeout, self._ttl_margin = timeout_s, ttl_margin_s
        self._client = None
        self._initialization_lock, self._create_lock = threading.Lock(), threading.Lock()
        self._submitted_tags = set()
        self._destroy_submitted = set()

    def _http(self):
        if not self.enabled:
            raise LiumError("lium_provider_disabled")
        with self._initialization_lock:
            if self._client is None:
                try:
                    config = self._loader(SERVICE, profile=PROFILE)
                    if (config.service != SERVICE or config.profile != PROFILE
                            or config.base_url != BASE_URL or config.primary_key_variable != KEY_VARIABLE
                            or not isinstance(config.api_key, str) or not config.api_key
                            or any(c in config.api_key for c in "\r\n\x00")):
                        raise ValueError
                    self._client = httpx.Client(base_url=BASE_URL+"/", trust_env=False,
                        follow_redirects=False, timeout=self._timeout, transport=self._transport,
                        headers={"X-API-Key": config.api_key, "Accept": "application/json"})
                except Exception:
                    raise LiumError("lium_profile_unavailable_or_mismatched") from None
        return self._client

    def close(self):
        if self._client is not None:
            self._client.close()

    def _request(self, method, route, *, payload=None, missing_ok=False):
        # All routes are generated internally from validated UUIDs. Disable
        # redirects, proxies and retries; never propagate response body/errors.
        try:
            with self._http().stream(method, route, json=payload) as response:
                if missing_ok and response.status_code == 404:
                    return None
                if response.status_code < 200 or response.status_code >= 300:
                    raise LiumError("lium_request_unconfirmed")
                raw = bytearray()
                for part in response.iter_bytes():
                    raw.extend(part)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise LiumError("lium_response_too_large")
                # Preserve money's JSON decimal spelling; avoid binary floats.
                return json.loads(raw, parse_float=Decimal)
        except LiumError:
            raise
        except Exception:
            raise LiumError("lium_request_unconfirmed") from None

    def _rows(self, route):
        rows = self._request("GET", route)
        if not isinstance(rows, list) or len(rows) > MAX_ROWS or any(not isinstance(row, dict) for row in rows):
            raise LiumError("lium_invalid_listing")
        return rows

    @staticmethod
    def _name(row):
        name = row.get("name", row.get("pod_name"))
        if "name" in row and "pod_name" in row and row["name"] != row["pod_name"]:
            raise LiumError("lium_pod_name_conflict")
        return name

    def _exact_pod(self, tag, instance_id=None):
        name = _pod_name(tag)
        known = _uuid(instance_id) if instance_id is not None else None
        # Read every visible row rather than SDK's first name/prefix match.
        matches = [row for row in self._rows("pods") if self._name(row) == name]
        if len(matches) > 1:
            raise LiumError("lium_duplicate_tag_requires_operator")
        if not matches:
            return None
        pod_id = _uuid(matches[0].get("id"))
        if known is not None and known != pod_id:
            raise LiumError("lium_instance_identity_conflict")
        return matches[0]

    def _running_fact(self, tag, pod):
        pod_id = _uuid(pod.get("id"))
        if self._name(pod) != _pod_name(tag):
            raise LiumError("lium_instance_identity_conflict")
        status = pod.get("status")
        if not isinstance(status, str):
            raise LiumError("lium_invalid_pod_status")
        if status.upper() != "RUNNING":
            # FAILED/STOPPED may still reserve a billed node; not removed.
            return ProviderFact("starting", pod_id)
        proof = None
        if self._idle_probe is not None:
            try:
                proof = self._idle_probe(tag, pod_id)
            except Exception:
                pass
        now = self.clock()
        if (isinstance(proof, InferenceIdleProof) and proof.instance_id == pod_id and proof.idle
                and 0 <= now-proof.observed_at <= 30 and proof.idle_since <= proof.observed_at):
            return ProviderFact("running", pod_id, idle_confirmed=True, idle_since=proof.idle_since)
        return ProviderFact("running", pod_id)

    def _removed_statement(self, tag, instance_id):
        pod_id = _uuid(instance_id)
        statement = self._request("GET", f"pods/{pod_id}/statement", missing_ok=True)
        if statement is None:
            return None
        if (not isinstance(statement, dict) or _uuid(statement.get("pod_id")) != pod_id
                or statement.get("pod_name") != _pod_name(tag)):
            raise LiumError("lium_statement_identity_conflict")
        if statement.get("removed") is not True:
            return None
        removed_at, created_at = _timestamp(statement.get("removed_at")), _timestamp(statement.get("created_at"))
        if not created_at <= removed_at <= self.clock()+30:
            raise LiumError("lium_invalid_statement_time")
        # Absence of final money does not prevent a verified physical removal.
        # Missing/inconsistent accounting remains pending for a later read.
        actual = None
        try:
            actual = _microusd(statement["total"])
            if type(statement.get("billed_seconds")) is not int or statement["billed_seconds"] < 0:
                actual = None
        except (KeyError, LiumError):
            pass
        return ProviderFact("destroyed", pod_id, actual_cost_microusd=actual)

    def _manifest(self, launch):
        manifest = self._manifests.get(getattr(launch, "configuration_id", None))
        if (manifest is None or not isinstance(launch, LaunchSpec) or launch.provider != SERVICE
                or launch.model_id != manifest.model_id or launch.region != manifest.region
                or launch.offer_id != manifest.executor_id or launch.image_id != manifest.template_id
                or not manifest.allow_preflight_only_price_cap or manifest.approved_until <= self.clock()):
            raise LiumError("lium_launch_manifest_unapproved")
        return manifest

    def validate_launch(self, launch, *, physical_gpus, slots, reserved_cost_microusd, hard_deadline):
        """Pure pre-reservation check; no credentials/HTTP, no parallel budget.

        The coordinator's existing durable reservation must account for every
        approved physical GPU and the full approved TTL at the preflight cap.
        This remains a reservation, not a final invoice or an atomic price cap.
        """
        if not self.enabled:
            raise LiumError("lium_provider_disabled")
        manifest = self._manifest(launch)
        if (type(physical_gpus) is not int or physical_gpus != manifest.gpu_count
                or type(slots) is not int or slots != manifest.execution_slots):
            raise LiumError("lium_capacity_manifest_mismatch")
        reservation = money(reserved_cost_microusd)
        cap = money(manifest.max_price_per_gpu_hour_microusd * manifest.gpu_count * manifest.termination_hours)
        if reservation < cap:
            raise LiumError("lium_ttl_budget_not_reserved")
        now = self.clock()
        if (isinstance(hard_deadline, bool) or not isinstance(hard_deadline, (float, int))
                or not math.isfinite(hard_deadline) or hard_deadline-now-self._ttl_margin < 3600):
            raise LiumError("lium_insufficient_provider_ttl_window")
        return {"physical_gpus": manifest.gpu_count, "slots": manifest.execution_slots,
                "ttl_cap_reservation_microusd": cap}

    def create(self, tag, launch: LaunchSpec, *, hard_deadline):
        if not self.enabled:
            raise LiumError("lium_provider_disabled")
        tag = _uuid(tag)
        name = _pod_name(tag)
        manifest = self._manifest(launch)
        now = self.clock()
        if (isinstance(hard_deadline, bool) or not isinstance(hard_deadline, (float, int))
                or not math.isfinite(hard_deadline)):
            raise LiumError("lium_invalid_deadline")
        # The minimum provider TTL is one hour. Floor the available window and
        # include the bounded HTTP submission margin; never round TTL upward.
        hours = min(manifest.termination_hours, math.floor((hard_deadline-now-self._ttl_margin)/3600))
        if hours < 1:
            raise LiumError("lium_insufficient_provider_ttl_window")
        with self._create_lock:
            if tag in self._submitted_tags:
                raise LiumError("lium_creation_already_submitted_reconcile_only")
            existing = self._exact_pod(tag)
            if existing is not None:
                return self._running_fact(tag, existing)
            offers = [row for row in self._rows("executors") if row.get("id") == manifest.executor_id]
            templates = [row for row in self._rows("templates") if row.get("id") == manifest.template_id]
            if len(offers) != 1 or len(templates) != 1:
                raise LiumError("lium_exact_offer_or_template_unavailable")
            price = _microusd(offers[0].get("price_per_gpu"))
            count = offers[0].get("gpu_count")
            if (type(count) is not int or count < manifest.gpu_count
                    or price > manifest.max_price_per_gpu_hour_microusd):
                raise LiumError("lium_offer_outside_approved_limits")
            # Preflight may have consumed time. Recheck approval and absolute
            # deadline immediately before the single side-effecting request.
            now = self.clock()
            hours = min(hours, math.floor((hard_deadline-now-self._ttl_margin)/3600))
            if hours < 1 or manifest.approved_until <= now:
                raise LiumError("lium_launch_approval_expired")
            self._submitted_tags.add(tag)
            response = self._request("POST", f"executors/{manifest.executor_id}/rent", payload={
                "pod_name": name, "template_id": manifest.template_id,
                "user_public_key": manifest.user_public_key, "gpu_count": manifest.gpu_count,
                "termination_hours": hours,
            })
            if not isinstance(response, dict) or response.get("success") is not True:
                raise LiumError("lium_creation_response_unconfirmed")
            return ProviderFact("starting", _uuid(response.get("pod_id")))

    def reconcile(self, tag, instance_id=None):
        if not self.enabled:
            raise LiumError("lium_provider_disabled")
        pod = self._exact_pod(tag, instance_id)
        if pod is not None:
            return self._running_fact(tag, pod)
        if instance_id is not None:
            statement = self._removed_statement(tag, instance_id)
            if statement is not None:
                return statement
        # Key visibility / eventual consistency is not authoritative absence.
        return ProviderFact("unknown", _uuid(instance_id) if instance_id else None)

    def destroy(self, tag, instance_id):
        if not self.enabled:
            raise LiumError("lium_provider_disabled")
        pod_id = _uuid(instance_id)
        removed = self._removed_statement(tag, pod_id)
        if removed is not None:
            return removed
        pod = self._exact_pod(tag, pod_id)
        if pod is None:
            return ProviderFact("unknown", pod_id)
        # The coordinator already persisted destroy_started_at. DELETE is
        # issued once. A 2xx payload alone has no pinned terminal response
        # schema, so read the durable removed statement before releasing.
        with self._create_lock:
            if pod_id in self._destroy_submitted:
                raise LiumError("lium_destruction_already_submitted_reconcile_only")
            self._destroy_submitted.add(pod_id)
        self._request("DELETE", f"pods/{pod_id}")
        removed = self._removed_statement(tag, pod_id)
        return removed or ProviderFact("unknown", pod_id)

    def billing(self, tag, instance_id=None):
        if not self.enabled:
            raise LiumError("lium_provider_disabled")
        if instance_id is None:
            return None
        statement = self._removed_statement(tag, instance_id)
        return statement.actual_cost_microusd if statement is not None else None
