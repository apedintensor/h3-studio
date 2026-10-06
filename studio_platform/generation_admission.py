"""Shared admission boundary; callers retain their real authorization identity.

The optional business namespace is only a durable queue idempotency namespace.
It never grants permissions, mints a Principal, or changes existing jobs.
"""
from .capabilities import capabilities
from .generation_draft import plan_body, read_draft
from .repository import Scope, Conflict
from .render_plans import RECIPE as RENDER_RECIPE
from .source_snapshot import source_snapshot


def managed_project(project):
    return (project.get("integration_kind") == "quick_chat"
            or project.get("journey", {}).get("integration_kind") == "quick_chat")


def reject_managed(project):
    if managed_project(project):
        raise Conflict("quick_chat_managed_resource")


class GenerationAdmission:
    def __init__(self, *, repo, assets, settings, policies, scope,
                 authorized_project, owned_plan, plan_response, check_source):
        self.repo, self.assets, self.settings, self.policies = repo, assets, settings, policies
        self.scope, self.authorized_project = scope, authorized_project
        self.owned_plan, self.plan_response, self.check_source = owned_plan, plan_response, check_source

    def task_scope(self, principal, project_id, business_identity=None):
        if business_identity is None:
            return self.scope(principal, project_id)
        # Actual caller is checked independently and audited by the submission.
        return Scope(self.settings.tenant_id, principal.owner, project_id, "quick-chat-execution")

    def preflight(self, principal, project, shot_id):
        project_id = project["id"]
        record = self.authorized_project(principal, project_id, "jobs:write")
        if source_snapshot(project, shot_id) != source_snapshot(record["payload"], shot_id):
            raise Conflict("shot_version_conflict")
        draft, _ = read_draft(project, shot_id)
        if any(draft["inputs"].values()):
            self.authorized_project(principal, project_id, "assets:read")

        def derive(asset_id, start, end):
            self.authorized_project(principal, project_id, "assets:write")
            self.assets.get(principal.owner, asset_id, project_id)
            receipt = self.assets.derive(principal.owner, asset_id, start, end)
            if receipt["status"] != "ready":
                raise Conflict("reference_not_ready")
            return receipt["asset_id"]

        body = plan_body(project, shot_id, capabilities(self.settings), derive)
        latest = self.authorized_project(principal, project_id, "jobs:write")
        if source_snapshot(project, shot_id) != source_snapshot(latest["payload"], shot_id):
            raise Conflict("shot_version_conflict")
        return self.plan_response(principal, body, latest["payload"])

    def create(self, principal, plan_id, key, *, initial_status=None, business_identity=None):
        plan = self.owned_plan(principal, plan_id)
        project = self.authorized_project(principal, plan["project_id"], "jobs:write")["payload"]
        if business_identity is None:
            reject_managed(project)
        elif not managed_project(project):
            raise Conflict("quick_chat_projection_required")
        task_scope = self.task_scope(principal, plan["project_id"], business_identity)
        existing = self.repo.lookup_job_by_idempotency(task_scope, key)
        if existing:
            return self.repo.create_job(task_scope, plan_id, key)
        self.check_source(project, plan["request"])
        execution = plan["execution_plan"]
        enabled_setting = (self.settings.render_enabled if plan["request"]["recipe_id"] == RENDER_RECIPE
                           else self.settings.generation_enabled)
        ready = enabled_setting and execution.get("enabled") and execution.get("quote_known")
        status = initial_status or (execution.get("admission_state", "queued") if ready else "blocked")
        if status in {"queued", "planned", "waiting_capacity"} and not ready:
            status = "blocked"
        budgets = self.policies.ensure_current(plan, task_scope) if ready else ()
        return self.repo.create_job(task_scope, plan_id, key, initial_status=status,
                                    budget_account_ids=budgets)

    def create_planned(self, principal, plan_id, business_identity):
        return self.create(principal, plan_id, business_identity, initial_status="planned",
                           business_identity=business_identity)

    def enqueue(self, principal, job, *, business=False):
        plan = self.owned_plan(principal, job["plan_id"])
        project = self.authorized_project(principal, job["project_id"], "jobs:write")["payload"]
        if not business:
            reject_managed(project)
        elif (not managed_project(project) or job["actor_id"] != "quick-chat-execution"):
            raise Conflict("quick_chat_execution_required")
        self.check_source(project, plan["request"])
        task_scope = self.task_scope(principal, job["project_id"], job["id"] if business else None)
        budgets = self.policies.ensure_current(plan, task_scope)
        return self.repo.enqueue(task_scope, job["id"], budget_account_ids=budgets)

    def refresh_planned(self, principal, job_id, plan_id):
        job = self.repo.get_job_for_owner(self.settings.tenant_id, principal.owner, job_id)
        project = self.authorized_project(principal, job["project_id"], "jobs:write")["payload"]
        if not managed_project(project) or job["actor_id"] != "quick-chat-execution":
            raise Conflict("quick_chat_execution_required")
        plan = self.owned_plan(principal, plan_id)
        if plan["project_id"] != job["project_id"]:
            raise Conflict("plan_not_available")
        self.check_source(project, plan["request"])
        task_scope = self.task_scope(principal, job["project_id"], job_id)
        self.policies.ensure_current(plan, task_scope)
        return self.repo.refresh_unadmitted_plan(task_scope, job_id, plan_id)
