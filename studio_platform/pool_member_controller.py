"""Explicit two-member lifecycle within the existing scaler and rental ledger.

This module supplies no provider, operating authority or alternate task queue.
The caller owns the same fenced pool leader and injects its current policy guard.
"""
from __future__ import annotations

from dataclasses import asdict
import json

from sqlalchemy import insert, select

from .capacity import pool_member_ids, reserve_capacity_member
from .repository import (Conflict, capacity_approvals, capacity_pool_members,
    capacity_waiters, instance_intents, jobs, scaler_actions, canonical)
from .scaler import LaunchSpec


def port_for_member(config, repo, intent_id):
    """Stable ports belong to approved member IDs, never offer aliases or order."""
    from .production_scaler import ScalerError, save
    from .service_policy import service_member_ids
    members = service_member_ids(config)
    if not members:
        raise ScalerError("capacity_pool_members_required")
    with repo.engine.connect() as connection:
        approval = connection.execute(select(capacity_approvals).where(
            capacity_approvals.c.id == config.capacity_approval_id)).mappings().one()
        bindings = list(connection.execute(select(capacity_pool_members).where(
            capacity_pool_members.c.approval_id == approval["id"])).mappings())
    if (pool_member_ids(approval["payload"]) != members
            or any(r["approval_hash"] != approval["approval_hash"] or r["member_id"] not in members for r in bindings)):
        raise ScalerError("capacity_pool_member_identity_mismatch")
    expected = {r["intent_id"]: config.port_start+members.index(r["member_id"]) for r in bindings}
    if intent_id not in expected:
        raise ScalerError("capacity_pool_unknown_member")
    path = config.work_dir/"cycle-state.json"
    receipt = json.loads(path.read_text())
    ports = receipt.get("ports")
    if (receipt.get("config_hash") != config.fingerprint() or not isinstance(ports, dict)
            or any(key not in expected or type(value) is not int or value != expected[key]
                   for key, value in ports.items())):
        raise ScalerError("capacity_pool_port_identity_mismatch")
    if intent_id not in ports:
        ports[intent_id] = expected[intent_id]
        save(path, receipt)
    return ports[intent_id]


class MemberLaunchCoordinator:
    """Atomically attach an approved original member to its only create intent.

    A return after a persisted action is never permission to repeat its POST.
    The existing ScaleCoordinator reconciles that exact intent after uncertainty.
    No replacement or rebound membership is provided by this primitive.
    """

    def __init__(self, scaler, *, approval_guard, job_guard):
        self.scaler, self.repo = scaler, scaler.repo
        self.approval_guard, self.job_guard = approval_guard, job_guard

    def managed(self, connection, approval):
        p = approval["payload"]
        members = pool_member_ids(p)
        if not members:
            raise Conflict("capacity_pool_members_required")
        bindings = list(connection.execute(select(capacity_pool_members).where(
            capacity_pool_members.c.approval_id == approval["id"])).mappings())
        if any(r["approval_hash"] != approval["approval_hash"] or r["member_id"] not in members for r in bindings):
            raise Conflict("capacity_pool_member_identity_mismatch")
        ids = {r["intent_id"] for r in bindings}
        rows = list(connection.execute(select(instance_intents).where(
            instance_intents.c.pool == p["pool"])).mappings())
        if (any(r["state"] != "destroyed" and r["id"] not in ids for r in rows)
                or not ids <= {r["id"] for r in rows}):
            raise Conflict("capacity_pool_unknown_member")
        selected = [r for r in rows if r["id"] in ids]
        actions = {r["intent_id"]: dict(r) for r in connection.execute(select(scaler_actions).where(
            scaler_actions.c.intent_id.in_(ids))).mappings()}
        if any(r["provider"] != p["launch"]["provider"] or r["id"] not in actions
                or actions[r["id"]]["pool"] != p["pool"]
                or actions[r["id"]]["launch_spec"] != p["launch"] for r in selected):
            raise Conflict("capacity_pool_member_action_mismatch")
        return selected, actions, {r["member_id"]: r["intent_id"] for r in bindings}

    def _demand(self, connection, approval):
        p, now = approval["payload"], self.repo.clock()
        if (not approval["enabled"] or min(approval["expires_at"], p["qualification_expires_at"],
                p["quote_expires_at"], p["scale_policy"]["hard_deadline"]) <= now
                or self.approval_guard(p) is not True):
            return False
        rows = connection.execute(select(jobs, capacity_waiters.c.deadline.label("wait_deadline"))
            .join(capacity_waiters, capacity_waiters.c.job_id == jobs.c.id).where(
                capacity_waiters.c.approval_id == approval["id"],
                capacity_waiters.c.approval_hash == approval["approval_hash"],
                capacity_waiters.c.intent_id.is_(None), jobs.c.tenant_id == approval["tenant_id"],
                jobs.c.pool == p["pool"],
                jobs.c.status.not_in(("planned", "blocked", "succeeded", "failed", "cancelled")))
            .order_by(jobs.c.id).limit(4097)).mappings()
        values = list(rows)
        if len(values) > 4096:
            raise Conflict("capacity_pool_demand_window_exceeded")
        for job in values:
            execution = job["execution_plan"]
            if (execution.get("capacity_binding") != "pool-members-v1"
                    or execution.get("capacity_approval_id") != approval["id"]
                    or execution.get("capacity_approval_hash") != approval["approval_hash"]
                    or self.job_guard(job) is not True):
                continue
            if job["status"] in ("waiting_capacity", "queued") and job["wait_deadline"] <= now:
                continue
            return True
        return False

    def before_create(self, lease, approval_id, member_id, intent_id):
        """A bounded read before the sole POST; never renew or reacquire a lease."""
        try:
            with self.repo.transaction() as connection:
                self.scaler._leader(connection, lease)
                approval = connection.execute(select(capacity_approvals).where(
                    capacity_approvals.c.id == approval_id)).mappings().one()
                _, _, mapping = self.managed(connection, approval)
                return (approval["pool"] == lease.pool and mapping.get(member_id) == intent_id
                    and self._demand(connection, approval))
        except Exception:
            return False

    def create_once(self, lease, approval_id, member_id):
        """Reserve/bind/action/barrier commit precedes exactly one provider call.

        Only *original* approved members can be created; an unknown A still
        consumes its reservation while an unbound, separately approved B can
        proceed. Foreign/unbound live intents fail closed before any reservation.
        """
        if self.scaler.enabled is not True or self.scaler.provider.enabled is not True:
            return {"state": "disabled"}
        with self.repo.transaction() as connection:
            self.scaler._leader(connection, lease)
            self.repo._lock_capacity(connection)
            approval = self.repo._locked(connection, select(capacity_approvals).where(
                capacity_approvals.c.id == approval_id))
            if approval is None or approval["pool"] != lease.pool:
                raise Conflict("capacity_pool_approval_mismatch")
            p = approval["payload"]
            if member_id not in pool_member_ids(p):
                raise Conflict("capacity_pool_member_not_approved")
            _, _, mapping = self.managed(connection, approval)
            if member_id in mapping:
                return {"state": "member_already_bound", "intent_id": mapping[member_id]}
            if not self._demand(connection, approval):
                return {"state": "no_confirmed_pool_demand"}
            launch = LaunchSpec(**p["launch"])
            if launch.provider != getattr(self.scaler.provider, "provider_id", None):
                raise Conflict("scaler_provider_identity_mismatch")
            validate = getattr(self.scaler.provider, "validate_launch", None)
            if validate is not None:
                validate(launch, physical_gpus=1, slots=1,
                    reserved_cost_microusd=p["scale_policy"]["instance_reservation_microusd"],
                    hard_deadline=min(p["scale_policy"]["hard_deadline"], approval["expires_at"]))
            intent = reserve_capacity_member(self.repo, approval_id, member_id, connection=connection)
            if not intent["created"]:
                raise Conflict("capacity_pool_member_action_missing")
            connection.execute(insert(scaler_actions).values(intent_id=intent["id"], pool=p["pool"],
                launch_spec=canonical(asdict(launch)), create_started_at=self.repo.clock()))
            if self.scaler.preparation_timeout_s is not None:
                self.scaler._preparation_receipt(connection, intent, "awaiting_provider")
            self.repo.update_instance(intent["id"], "creating", connection=connection)
        fact, observed_at = self.scaler._call(intent, "create", launch=launch,
            before_create=lambda: self.before_create(lease, approval_id, member_id, intent["id"]))
        self.scaler._apply(lease, intent["id"], fact, observed_at)
        return {"state": "creation_observed", "intent_id": intent["id"], "provider_state": fact.state}
