"""Operator-owned admission policy; never accept routing, price or budgets from users.

This file describes approved configuration envelopes, not automatic performance
discovery. A historical benchmark cannot create one. No cloud resources or SDK
requests are made by policy evaluation.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import stat
import string

from .capabilities import MODEL, RECIPES
from .control import WorkerControl
from .repository import BudgetExceeded, NotFound, identifier, request_hash


POLICY = "self-hosted-default"
FIELDS = {"id", "revision", "enabled", "model_id", "backend", "pool", "configuration_id",
          "recipe_ids", "qualification", "envelope", "reservation", "budget_accounts"}
ENVELOPE = {"max_pixels", "max_duration_seconds", "max_steps", "max_reference_files",
            "max_guides", "allow_first_last", "allow_audio", "controls"}


def positive(value, maximum):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= maximum


def validate_policy(value):
    """Reject ambiguous/misspelled operator settings rather than broadening them."""
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("Invalid execution policy fields")
    if (value["id"] != POLICY or value["model_id"] != MODEL or value["backend"] != "comfy-worker"
            or type(value["enabled"]) is not bool):
        raise ValueError("Invalid execution policy identity")
    for field in ("revision", "pool", "configuration_id"):
        identifier(value[field])
    recipes = value["recipe_ids"]
    if not isinstance(recipes, list) or not recipes or len(set(recipes)) != len(recipes) or any(r not in RECIPES for r in recipes):
        raise ValueError("Invalid execution policy recipes")
    qualification = value["qualification"]
    if (not isinstance(qualification, dict) or set(qualification) != {"status", "evidence_id", "verified_at", "expires_at"}
            or qualification["status"] not in {"unverified", "accepted"}
            or not positive(qualification["verified_at"], 1e12)
            or not positive(qualification["expires_at"], 1e12)
            or qualification["verified_at"] >= qualification["expires_at"]):
        raise ValueError("Invalid execution qualification")
    identifier(qualification["evidence_id"])
    envelope = value["envelope"]
    if not isinstance(envelope, dict) or set(envelope) != ENVELOPE:
        raise ValueError("Invalid execution envelope")
    for field, maximum in (("max_pixels", 768*1344), ("max_duration_seconds", 16), ("max_steps", 1000)):
        if not positive(envelope[field], maximum):
            raise ValueError("Invalid execution envelope limit")
    for field, maximum in (("max_reference_files", 12), ("max_guides", 8)):
        if type(envelope[field]) is not int or not 0 <= envelope[field] <= maximum:
            raise ValueError("Invalid execution input envelope")
    if any(type(envelope[field]) is not bool for field in ("allow_first_last", "allow_audio")):
        raise ValueError("Invalid execution feature envelope")
    # Every non-numeric generation family must be explicitly qualified. Numeric
    # decoder tiling/export parameters remain visible and are never overridden.
    controls = envelope["controls"]
    required_controls = {"sampler_name", "scheduler", "video_decode", "audio_decode", "encoder_device", "ref_image_size"}
    if not isinstance(controls, dict) or set(controls) != required_controls:
        raise ValueError("Explicit execution control families required")
    if any(not isinstance(options, list) or not options or any(not isinstance(x, str) or len(x)>80 for x in options) for options in controls.values()):
        raise ValueError("Invalid execution control values")
    quote = value["reservation"]
    if (not isinstance(quote, dict) or set(quote) != {"cost_microusd", "expected_runtime_s", "expires_at", "source_id"}
            or type(quote["cost_microusd"]) is not int or not 0 < quote["cost_microusd"] <= 1000000000
            or not positive(quote["expected_runtime_s"], 86400) or not positive(quote["expires_at"], 1e12)):
        raise ValueError("Invalid execution reservation")
    identifier(quote["source_id"])
    accounts = value["budget_accounts"]
    if not isinstance(accounts, list) or not 1 <= len(accounts) <= 8 or len(set(accounts)) != len(accounts):
        raise ValueError("Explicit execution budget accounts required")
    for template in accounts:
        if not isinstance(template, str) or len(template) > 200:
            raise ValueError("Invalid budget account template")
        try:
            for _, field, format_spec, conversion in string.Formatter().parse(template):
                if field is not None and (field not in {"tenant_id", "owner_id", "project_id"} or format_spec or conversion):
                    raise ValueError("Invalid budget account template")
            identifier(template.format(tenant_id="tenant", owner_id="owner", project_id="project"))
        except (KeyError, IndexError):
            raise ValueError("Invalid budget account template") from None
    return value


def read_policy(path):
    if path is None:
        return None
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Execution policy path must be absolute")
    try:
        with path.open("rb") as source:
            meta = os.fstat(source.fileno())
            if not stat.S_ISREG(meta.st_mode) or os.name != "nt" and meta.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise ValueError("Execution policy must be an operator-owned file")
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ValueError("Execution policy file exceeds limit")
        return validate_policy(json.loads(raw))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, KeyError):
        raise ValueError("Execution policy file is unavailable or invalid") from None


@dataclass(frozen=True)
class Admission:
    execution: dict
    cost: int
    expires_at: float
    estimate: dict


class ExecutionPolicies:
    def __init__(self, settings, repository):
        self.settings, self.repo = settings, repository
        self.control = WorkerControl(repository)

    def evaluate(self, compiled, scope, fingerprint):
        from .render_plans import RECIPE, MODEL as RENDER_MODEL, configuration_for, POOL
        if compiled["recipe_id"] == RECIPE:
            configuration = configuration_for(compiled)
            blockers = list(compiled.get("render_blockers", []))
            if not self.settings.render_enabled:
                blockers.append("章节粗剪执行尚未开启，时间线和素材仍可保存")
            else:
                capacity = self.control.pool_status(POOL, model_id=RENDER_MODEL,
                    configuration_id=configuration, recipe_id=RECIPE, backend="cpu-render")
                if not capacity["ready"]+capacity["busy"]:
                    blockers.append("章节粗剪工作机尚未就绪，请稍后重试")
            execution = {"pool": POOL, "backend": "cpu-render", "configuration_id": configuration,
                "quote_known": True, "enabled": not blockers, "blockers": blockers,
                "admission_state": "blocked" if blockers else "queued",
                "fingerprint": fingerprint, "budget_account_ids": [], "expected_runtime_s": 1800}
            return Admission(execution, 0, self.repo.clock()+900,
                {"currency": "USD", "cost_microusd": 0, "source": "operator-included-cpu-render",
                 "kind": "included", "description": "当前粗剪不单独计费；服务器资源仍有运行成本"})
        now = self.repo.clock()
        backend = self.settings.execution_backend
        base = {"pool": compiled["recipe_id"], "backend": backend, "expected_runtime_s": 600,
                "quote_known": False, "enabled": False, "blockers": [], "fingerprint": fingerprint,
                "budget_account_ids": [], "admission_state": "blocked"}
        unknown = {"currency": "USD", "cost_microusd": None, "source": "unknown", "kind": "unknown"}
        if not self.settings.generation_enabled:
            base["blockers"].append("生成执行尚未开启；项目和素材可以继续保存")
            return Admission(base, 0, now+900, unknown)
        if backend == "mock":
            base.update(pool="mock", expected_runtime_s=1, quote_known=True, enabled=True, admission_state="queued")
            return Admission(base, 0, now+900,
                {"currency": "USD", "cost_microusd": 0, "source": "mock", "kind": "simulation"})
        if backend != "comfy-worker":
            base["blockers"].append("尚未接入此执行方式")
            return Admission(base, 0, now+900, unknown)
        try:
            policy = read_policy(self.settings.execution_policy_file)
        except ValueError:
            policy = None
        if policy is None:
            base["blockers"].append("缺少有效的执行池验收与费用策略，暂不发起生成")
            return Admission(base, 0, now+900, unknown)
        qualification, quote, envelope = policy["qualification"], policy["reservation"], policy["envelope"]
        blockers = base["blockers"]
        if not policy["enabled"]:
            blockers.append("操作员已暂停此执行策略")
        if qualification["status"] != "accepted" or not qualification["verified_at"] <= now < qualification["expires_at"]:
            blockers.append("此执行配置尚未验收或验收记录已过期")
        if quote["expires_at"] <= now:
            blockers.append("费用预留策略已过期，请等待更新")
        if compiled["recipe_id"] not in policy["recipe_ids"] or compiled["request"]["model"] != policy["model_id"]:
            blockers.append("执行池未验收此模型或配方")
        request, output = compiled["request"], compiled["output_spec"]
        refs = len(set(a for a in compiled["assets"]))
        if (output["width"]*output["height"] > envelope["max_pixels"]
                or output["actual_duration"] > envelope["max_duration_seconds"]
                or request["steps"] > envelope["max_steps"] or refs > envelope["max_reference_files"]
                or len(request["guides"]) > envelope["max_guides"]
                or request["generate_audio"] and not envelope["allow_audio"]
                or (request["inputs"]["first_frame"] or request["inputs"]["last_frame"]) and not envelope["allow_first_last"]
                or any(request[field] not in options for field, options in envelope["controls"].items())):
            blockers.append("当前输入或控制项超出此执行池的验收范围；请保留配方等待相符配置")
        capacity = self.control.pool_status(policy["pool"], model_id=policy["model_id"],
            configuration_id=policy["configuration_id"], recipe_id=compiled["recipe_id"], backend=backend)
        account_ids = [value.format(**scope.__dict__) for value in policy["budget_accounts"]]
        for account_id in account_ids:
            try:
                identifier(account_id)
                account = self.repo.get_budget(account_id)
                if (account["tenant_id"] != scope.tenant_id
                        or account["owner_id"] is not None and account["owner_id"] != scope.owner_id
                        or account["project_id"] is not None and account["project_id"] != scope.project_id):
                    raise NotFound("budget_not_found")
                if account["spent_microusd"] + account["reserved_microusd"] + quote["cost_microusd"] > account["limit_microusd"]:
                    raise BudgetExceeded("budget_exceeded")
            except (NotFound, BudgetExceeded, ValueError):
                blockers.append("当前项目的生成预算未配置或可用预留额度不足")
                break
        approval = None
        if capacity["ready"] + capacity["busy"] == 0:
            # Only an independently approved, current launch can admit a wait.
            # Empty approvals / gates=0 retain the original blocked behavior.
            if not blockers:
                approval = self.repo.find_capacity_approval(scope, pool=policy["pool"], model_id=policy["model_id"],
                    configuration_id=policy["configuration_id"], recipe_id=compiled["recipe_id"], policy_hash=request_hash(policy))
                if approval is not None and not self.capacity_approval_current(approval["payload"]):
                    approval = None
            if approval is None:
                blockers.append("暂无已登记且心跳有效的匹配工作机，也无有效的独立冷启动审批")
        quote_known = quote["expires_at"] > now
        base.update(pool=policy["pool"], configuration_id=policy["configuration_id"], policy_revision=policy["revision"],
            policy_hash=request_hash(policy), expected_runtime_s=quote["expected_runtime_s"], quote_known=quote_known,
            enabled=not blockers, budget_account_ids=account_ids, qualification_evidence_id=qualification["evidence_id"],
            qualification_expires_at=qualification["expires_at"], quote_expires_at=quote["expires_at"],
            registered_healthy_slots=capacity["ready"]+capacity["busy"])
        base["admission_state"] = "blocked" if blockers else "waiting_capacity" if approval else "queued"
        if approval:
            base.update(capacity_approval_id=approval["id"], capacity_approval_hash=approval["approval_hash"])
        expiry = min(now+900, quote["expires_at"], qualification["expires_at"]) if not blockers else now+900
        if approval:
            expiry = min(expiry, approval["expires_at"],
                approval["payload"]["scale_policy"]["hard_deadline"]-quote["expected_runtime_s"])
            if expiry <= now:
                base.update(enabled=False, admission_state="blocked")
                blockers.append("冷启动硬截止不足以完成此配方")
                expiry = now+900
        estimate = {"currency": "USD", "cost_microusd": quote["cost_microusd"] if quote_known else None,
                    "source": quote["source_id"] if quote_known else "unknown", "kind": "budget_reservation",
                    "actual_charge_known": False, "description": "运营配置的预算预留额；实际费用另行核对，不代表最终账单"}
        return Admission(base, quote["cost_microusd"] if quote_known else 0, expiry, estimate)

    def ensure_current(self, plan, scope):
        previous = plan["execution_plan"]
        current = self.evaluate(plan["request"], scope, previous["fingerprint"])
        if (not previous.get("enabled") or not current.execution["enabled"]
                or previous.get("policy_hash") != current.execution.get("policy_hash")
                or previous.get("backend") != current.execution["backend"]):
            from .repository import Conflict
            raise Conflict("execution_policy_changed_or_unavailable")
        if previous.get("capacity_approval_id") and not self._capacity_reference_current(previous, scope.tenant_id):
            from .repository import Conflict
            raise Conflict("capacity_approval_unavailable")
        return tuple(current.execution["budget_account_ids"])

    def capacity_approval_current(self, payload):
        """Pure current operator-file check; safe inside a ledger transaction."""
        if not self.settings.generation_enabled or self.settings.execution_backend != "comfy-worker":
            return False
        try:
            policy = read_policy(self.settings.execution_policy_file)
            if policy is None:
                return False
            now, qualification, quote = self.repo.clock(), policy["qualification"], policy["reservation"]
            return bool(policy["enabled"] and request_hash(policy) == payload["policy_hash"]
                and policy["model_id"] == payload["model_id"] and policy["pool"] == payload["pool"]
                and policy["configuration_id"] == payload["configuration_id"]
                and set(payload["recipe_ids"]) <= set(policy["recipe_ids"])
                and qualification["status"] == "accepted" and qualification["verified_at"] <= now < qualification["expires_at"]
                and qualification["evidence_id"] == payload["qualification_evidence_id"]
                and qualification["expires_at"] == payload["qualification_expires_at"]
                and now < quote["expires_at"] == payload["quote_expires_at"]
                and payload["expires_at"] > now)
        except (ValueError, KeyError, TypeError):
            return False

    def _capacity_reference_current(self, execution, tenant_id):
        from sqlalchemy import select
        from .repository import capacity_approvals
        with self.repo.engine.connect() as connection:
            approval = connection.execute(select(capacity_approvals).where(
                capacity_approvals.c.id == execution["capacity_approval_id"], capacity_approvals.c.tenant_id == tenant_id)).mappings().first()
        return bool(approval and approval["enabled"] == 1
            and approval["approval_hash"] == execution.get("capacity_approval_hash")
            and approval["expires_at"] > self.repo.clock() and self.capacity_approval_current(approval["payload"]))

    def submission_allowed(self, job):
        """New submission guard; cold-start revocation also applies after queueing."""
        return bool(self.activation_allowed(job) and (not job["execution_plan"].get("capacity_approval_id")
            or self._capacity_reference_current(job["execution_plan"], job["tenant_id"])))

    def activation_allowed(self, job):
        """Re-check revocation immediately before a NEW upstream submission.

        Running/unknown submissions must still reconcile after policy expiry.
        The caller only uses this guard on a proven, unsubmitted attempt. Budget
        is already reserved and is not counted a second time here.
        """
        execution = job.get("execution_plan", {})
        from .render_plans import RECIPE, MODEL as RENDER_MODEL, configuration_for, POOL
        if job.get("request", {}).get("recipe_id") == RECIPE:
            try:
                configuration = configuration_for(job["request"])
            except (ValueError, AttributeError, TypeError):
                return False
            return bool(self.settings.render_enabled and execution.get("enabled") is True
                and execution.get("backend") == "cpu-render" and execution.get("pool") == POOL
                and execution.get("configuration_id") == configuration
                and job["request"]["request"].get("model") == RENDER_MODEL
                and not job["request"].get("render_blockers"))
        if (not self.settings.generation_enabled or self.settings.execution_backend != execution.get("backend")
                or execution.get("enabled") is not True):
            return False
        if self.settings.execution_backend == "mock":
            return True
        if self.settings.execution_backend != "comfy-worker":
            return False
        try:
            policy = read_policy(self.settings.execution_policy_file)
            if policy is None:
                return False
            qualification, quote = policy["qualification"], policy["reservation"]
            now = self.repo.clock()
            return bool(policy["enabled"] and execution.get("policy_hash") == request_hash(policy)
                and qualification["status"] == "accepted"
                and qualification["verified_at"] <= now < qualification["expires_at"]
                and now < quote["expires_at"]
                and job["request"]["recipe_id"] in policy["recipe_ids"]
                and job["request"]["request"]["model"] == policy["model_id"]
                and execution.get("pool") == policy["pool"]
                and execution.get("configuration_id") == policy["configuration_id"])
        except (ValueError, KeyError, TypeError):
            return False
