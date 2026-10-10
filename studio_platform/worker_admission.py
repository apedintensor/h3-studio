"""Read-only eligibility shared by readiness, claim and the late submit guard.

These gates apply only to new inference. Existing attempts retain their original
worker binding and can reconcile or collect after the admission window closes.
"""
import math

from sqlalchemy import inspect, select

from .repository import instance_intents

STOP_NEW_SECONDS = 300
COMPLETION_MARGIN_SECONDS = 120
BROKER_HEARTBEAT_SECONDS = 60


def managed_window_reason(intent, node, now, *, expected_runtime_s=None,
                          deployment_profile_id=None, binding=None):
    from .operator_controller import provider_lifetime_current
    deadline = min(intent["hard_deadline"], binding.expires_at) if binding else intent["hard_deadline"]
    if node["desired_state"] != "running" or intent["state"] not in {"starting", "ready", "busy"}:
        return "worker_capacity_stopping"
    if binding and (not binding.enabled or node["binding_hash"] != binding.fingerprint):
        return "managed_binding_changed"
    if node.get("runtime_state") in {"blocked", "failed", "bootstrap_unconfigured", "observation_failed", "provider_execution_unverified", "removal_pending", "stopped", "draining"}:
        return "managed_runtime_unavailable"
    if now >= deadline - STOP_NEW_SECONDS:
        return "worker_window_closing"
    payload = node.get("payload", {})
    if payload.get("capacity_backend") == "dstack-v1":
        # dstack max_duration starts at RUNNING. The original absolute business
        # deadline remains binding; do not fabricate a supplier lease guarantee.
        proof = payload.get("dstack_observation", {})
        original = payload.get("dstack", {})
        observed = proof.get("observed_at")
        if (type(observed) not in (int, float) or not math.isfinite(observed)
                or not 0 <= now-observed <= payload.get("observation_fresh_seconds", 30) or proof.get("status") != "running"
                or not original.get("run_id") or proof.get("run_id") != original.get("run_id")
                or proof.get("instance_id") != intent["provider_instance_id"]
                or node.get("runtime_state") not in {"ready", "busy"}
                or not original.get("runtime_incarnation")):
            return "managed_dstack_readiness_unconfirmed"
    elif not provider_lifetime_current(payload, intent, now):
        return "managed_provider_lifetime_unverified"
    if binding is None and node.get("payload", {}).get("selection", {}).get("runtime_profile_id") != deployment_profile_id:
        return "matching_profile_unavailable"
    if expected_runtime_s is not None:
        if (type(expected_runtime_s) not in (int, float) or not math.isfinite(expected_runtime_s)
                or expected_runtime_s <= 0):
            return "job_runtime_unconfirmed"
        if now + expected_runtime_s + COMPLETION_MARGIN_SECONDS >= deadline:
            return "job_exceeds_worker_window"
    return None


def worker_window_reason(connection, worker, now, *, expected_runtime_s=None, deployment_profile_id=None):
    """Unmanaged slots retain the existing intent TTL gate; managed slots add proof."""
    from .operator_capacity import operator_nodes
    managed = operator_nodes.name in inspect(connection).get_table_names()
    intents = connection.execute(select(instance_intents).where(
        instance_intents.c.provider == worker["provider"],
        instance_intents.c.provider_instance_id == worker["instance_id"])).mappings()
    for intent in intents:
        node = connection.execute(select(operator_nodes).where(
            operator_nodes.c.intent_id == intent["id"])).mappings().first() if managed else None
        if node is not None:
            if node.get("payload", {}).get("capacity_backend") == "dstack-v1":
                original = node["payload"].get("dstack", {})
                spec = worker["spec"]
                if (spec.get("dispatch_backend") != "hatchet-v1"
                        or spec.get("engine_manifest_digest") != original.get("manifest_digest")
                        or spec.get("configuration_id") != original.get("configuration_id")
                        or spec.get("model_id") != original.get("model_id")):
                    return "managed_dstack_binding_mismatch"
                broker = original.get("broker_observation", {})
                observed = broker.get("observed_at")
                heartbeat = broker.get("heartbeat_at")
                freshness = node["payload"].get("observation_fresh_seconds", 30)
                if (broker.get("ready") is not True or broker.get("worker_id") != worker["id"]
                        or type(observed) not in (int,float) or not math.isfinite(observed)
                        or not 0 <= now-observed <= freshness
                        or type(heartbeat) not in (int,float) or not math.isfinite(heartbeat)
                        or not 0 <= now-heartbeat <= BROKER_HEARTBEAT_SECONDS):
                    return "managed_hatchet_consumer_unconfirmed"
            reason = managed_window_reason(intent, node, now, expected_runtime_s=expected_runtime_s,
                deployment_profile_id=deployment_profile_id)
            if reason:
                return reason
        elif intent["state"] not in {"starting", "ready", "busy"} or intent["hard_deadline"] <= now:
            return "worker_capacity_stopping"
        elif expected_runtime_s is not None and now + expected_runtime_s + COMPLETION_MARGIN_SECONDS >= intent["hard_deadline"]:
            return "job_exceeds_worker_window"
    return None
