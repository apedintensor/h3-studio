"""Read-only eligibility shared by readiness, claim and the late submit guard.

These gates apply only to new inference. Existing attempts retain their original
worker binding and can reconcile or collect after the admission window closes.
"""
import math

from sqlalchemy import inspect, select

from .repository import instance_intents

STOP_NEW_SECONDS = 300
COMPLETION_MARGIN_SECONDS = 120


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
    if not provider_lifetime_current(node.get("payload", {}), intent, now):
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
            reason = managed_window_reason(intent, node, now, expected_runtime_s=expected_runtime_s,
                deployment_profile_id=deployment_profile_id)
            if reason:
                return reason
        elif intent["state"] not in {"starting", "ready", "busy"} or intent["hard_deadline"] <= now:
            return "worker_capacity_stopping"
        elif expected_runtime_s is not None and now + expected_runtime_s + COMPLETION_MARGIN_SECONDS >= intent["hard_deadline"]:
            return "job_exceeds_worker_window"
    return None
