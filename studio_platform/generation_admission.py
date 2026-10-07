"""Shared generation admission; HTTP and scenario callers retain their identity.

Extracted from the existing API and aligned with the preserved Quick Chat
admission boundary. This module does not own a queue, permissions or GPU calls.
"""
from .capabilities import capabilities
from .generation_draft import plan_body, read_draft
from .repository import Conflict
from .render_plans import RECIPE as RENDER_RECIPE
from .source_snapshot import source_snapshot


class GenerationAdmission:
    def __init__(self, *, repo, assets, settings, policies, scope,
                 authorized_project, owned_plan, plan_response, check_source,
                 capabilities_provider=None):
        self.repo, self.assets, self.settings, self.policies = repo, assets, settings, policies
        self.scope, self.authorized_project = scope, authorized_project
        self.owned_plan, self.plan_response, self.check_source = owned_plan, plan_response, check_source
        self.capabilities_provider = capabilities_provider or (lambda: capabilities(self.settings))

    def preflight(self, principal, project, shot_id, *, expected_version):
        project_id = project["id"]
        record = self.authorized_project(principal, project_id, "jobs:write")
        if record["version"] != expected_version:
            raise Conflict("document_version_conflict")
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
                raise Conflict("asset_not_ready")
            return receipt["asset_id"]

        body = plan_body(project, shot_id, self.capabilities_provider(), derive)
        latest = self.authorized_project(principal, project_id, "jobs:write")
        if latest["version"] != expected_version:
            raise Conflict("document_version_conflict")
        return self.plan_response(principal, body, latest["payload"])

    def create(self, principal, plan_id, key, *, initial_status=None):
        plan = self.owned_plan(principal, plan_id)
        task_scope = self.scope(principal, plan["project_id"])
        # Replaying a lost response keeps the immutable original even if the
        # editable source has since changed. The ledger compares request hashes.
        existing = self.repo.lookup_job_by_idempotency(task_scope, key)
        if existing:
            return self.repo.create_job(task_scope, plan_id, key)
        project = self.authorized_project(principal, plan["project_id"], "jobs:write")["payload"]
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

    def enqueue(self, principal, job):
        task_scope = self.scope(principal, job["project_id"])
        plan = self.owned_plan(principal, job["plan_id"])
        project = self.authorized_project(principal, job["project_id"], "jobs:write")["payload"]
        self.check_source(project, plan["request"])
        budgets = self.policies.ensure_current(plan, task_scope)
        return self.repo.enqueue(task_scope, job["id"], budget_account_ids=budgets)
