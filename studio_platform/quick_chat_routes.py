"""Thin authenticated chat routes. State and side effects live in one service."""
from fastapi import BackgroundTasks, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from .quick_chat import QuickChatService, QuickChatError, default_next_settings
from .quick_chat_assistant import QuickChatAssistant, model_schema
from .quick_chat_titles import TITLE_MODEL
from .capabilities import capabilities
from .upload_route import QuickChatAssetUploadRoute

PREFIX = "/v1/quick-chat/sessions"


def agent_contract():
    """Static same-origin authoring instructions; no credential or live-state read."""
    return {"schema_url": "/v1/quick-chat/schema", "sessions_url": PREFIX,
        "authentication": "Use an existing owner PAT in the Authorization: Bearer header. Never put it in URLs or prompts.",
        "new_session_scopes": ["projects:create", "projects:read", "projects:write"],
        "new_session_all_projects": True,
        "media_scopes": ["assets:read", "assets:write"], "generation_scopes": ["jobs:read", "jobs:write"],
        "generation_authority": "A card or preflight does not start generation. Confirm only within the user's authorization; an HTTP202 is not inference success.",
        "same_session": "Materials and next_settings are visible authoring state. Revisions freeze their own explicit inputs and seeds. Different sessions are isolated.",
        "history_titles": "Omit title when creating a session to allow optional Gemma 4 short history naming from the first meaningful accepted description. Any explicit title is manual, even the default label. Naming is independent of the conversational assistant and video engine; consult schema.history_titles.enabled. It sends only bounded first text, never media/full history, and failure keeps the original label without automatic retry.",
        "deployment_profiles": "Read capabilities.deployment_profiles and the session's complete next_settings. Preserve the user's selected deployment_profile_id and use that profile's generation_support[fl|ref] controls, limits and joint_cases. Turn-to-card inherits next_settings. Direct card creation/revision does not inherit an omitted profile: include top-level deployment_profile_id and explicit controls, copied from session next_settings for a new card or from the current revision when revising. Null or omission retains legacy routing; never drop a selected profile to bypass a blocker. Explicit session changes use PATCH with expected_version and complete next_settings. Catalog measurements are historical; preflight checks current matching workers. Ordinary generation keys do not grant operator rental access.",
        "replay": "Persist each write body and Idempotency-Key. Replaying the same revision's initial confirmation across Agents returns its original submission/items/jobs, not new variations.",
        "examples": {
            "session": {"method": "POST", "path": PREFIX, "body": {},
                "require": "Omit title for optional automatic history naming. Supply title only when intentionally choosing a manual name."},
            "upload": {"method": "POST", "path": PREFIX+"/{session_id}/assets",
                "multipart": {"client_asset_id": "stable-input-001", "file": "{local_file}"},
                "require": "Use returned ready asset_id. A lost response retains client_asset_id; query this session's assets and resume the same receipt."},
            "turn_to_card": {"method": "POST", "path": PREFIX+"/{session_id}/turns",
                "body": {"expected_version": "{current_session_version}", "model_id": "{session_model_id}",
                    "assistant_mode": "none", "create_card": True, "text": "{complete_prompt}"},
                "require": "For external Agents writing their own prompt; no website conversational assistant call. Optional Gemma history naming can still run once for an eligible new session. Inherits the session's complete next_settings including deployment_profile_id and selected materials. Read card_id then the card's current_revision_id. Model selection here does not select or call a video engine."},
            "card": {"method": "POST", "path": PREFIX+"/{session_id}/cards",
                "body": {"deployment_profile_id": "{selected_deployment_profile_id}",
                    "recipe_id": "{selected_recipe_id}", "prompt": "{complete_prompt}",
                    "controls": "{selected_controls_object}", "inputs": {}, "copies": 1},
                "require": "Alternative to turn_to_card when explicit settings/references are needed. Replace controls with the selected JSON object, not a string. Copy profile/recipe/controls from session next_settings for a new card, or the current revision for a revision; preserve copies unless explicitly changed. Direct cards do not inherit an omitted deployment_profile_id. Read selected profile generation_support[fl|ref] for current limits and tested joint cases; inputs can include first_frame:{asset_id:...} or last_frame:{asset_id:...}. Do not mix FL and REF. Null is only for an intentional legacy route. Authoring support does not qualify a recipe for execution."},
            "preflight": {"method": "POST", "path": PREFIX+"/{session_id}/revisions/{revision_id}/preflights",
                "body": {"revision_hash": "{revision.input_hash}", "capabilities_version": "{current_capabilities_version}"}},
            "confirm": {"method": "POST", "path": PREFIX+"/{session_id}/revisions/{revision_id}/submissions",
                "body": {"revision_hash": "{revision.input_hash}", "preflight_id": "{ready_preflight_id}", "confirmed": True}},
            "observe": {"method": "GET", "path": PREFIX+"/{session_id}/submissions/{submission_id}",
                "read_response": "items[].job_id and items[].job are the existing shared jobs. Download returned artifacts[].content_url/download_url with the same owner authentication; verify size_bytes and sha256."}},
        "placeholder_rules": "Replace placeholders with returned IDs/values and use a stable Idempotency-Key for every POST. expected_version is an integer. Never edit hidden project/shot projections or submit them through legacy routes.",
        "release_notice": "This is the backend contract. Quick Chat frontend integration and public onboarding have separate release gates; discovery does not prove a deployed UI, enabled conversational assistant or GPU readiness."}


def register_routes(app, *, hooks=None, assistant=None, assistant_enabled=False, title_generator=None):
    settings = app.state.settings
    service = QuickChatService(app.state.repository, app.state.assets, settings, app.state.storage,
        hooks=hooks, assistant=assistant or QuickChatAssistant(app.state.assets, app.state.storage),
        assistant_enabled=assistant_enabled, title_generator=title_generator)
    app.state.quick_chat = service

    @app.exception_handler(QuickChatError)
    async def error(_request, value):
        return JSONResponse({"code": value.code, "message": value.message, "retryable": value.retryable}, status_code=value.status)

    @app.get("/v1/quick-chat/schema")
    def schema():
        return {"version": 1, "agent_contract": agent_contract(),
            "models": model_schema(enabled=assistant_enabled), "capabilities": capabilities(settings),
            "assistant_enabled": assistant_enabled, "copies": {"minimum": 1, "maximum": 4},
            "history_titles": {"enabled": title_generator is not None, "model_id": TITLE_MODEL,
                "input": "first meaningful accepted turn or card prompt; at most 2000 characters; no media or full history",
                "timing": "background after authoring response", "maximum_calls_per_session": 1,
                "manual_title_preserved": True, "authoring_version_bump": False,
                "failure": "keep the original title; no automatic paid retry",
                "existing_sessions_backfilled": False},
            "default_next_settings": default_next_settings(settings),
            "turn_creation": {"create_card": {"type": "boolean", "default": False,
                "allowed_assistant_modes": ["none"], "atomic_with_turn": True,
                "starts_generation": False, "result_field": "card_id"}},
            "material_participation": {"basis": "next_settings.recipe_id", "catalog_selection": "enabled",
                "actual_participation": "effective_enabled", "inactive_reasons": ["mode_incompatible", "not_selected"]},
            "timeline": {"events": "creation_commands_only", "execution_polling": "active_submissions"}}

    @app.get(PREFIX)
    def sessions(request: Request, limit: int = Query(20, ge=1, le=50), cursor: str | None = None):
        return service.list_sessions(request.state.principal, limit=limit, cursor=cursor)

    @app.post(PREFIX, status_code=201)
    def create_session(request: Request, body: dict, key: str = Header(..., alias="Idempotency-Key")):
        return service.create_session(request.state.principal, body, key)

    @app.get(PREFIX+"/{session_id}")
    def session(session_id: str, request: Request):
        return service.get_session(request.state.principal, session_id)

    @app.patch(PREFIX+"/{session_id}")
    def patch(session_id: str, request: Request, body: dict, key: str = Header(..., alias="Idempotency-Key")):
        return service.patch_session(request.state.principal, session_id, body, key)

    def upload(session_id: str, request: Request, file: UploadFile = File(...), client_asset_id: str = Form(...)):
        return service.upload(request.state.principal, session_id, file.file, file.filename or "file", client_asset_id)

    app.router.add_api_route(PREFIX+"/{session_id}/assets", upload, methods=["POST"], status_code=201,
        route_class_override=QuickChatAssetUploadRoute)

    @app.get(PREFIX+"/{session_id}/assets")
    def asset_catalog(session_id: str, request: Request, client_asset_id: str | None = None,
            limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0, le=500)):
        session = service._access(request.state.principal, session_id, "assets:read")
        values = app.state.assets.list(request.state.principal.owner, session["payload"]["project_id"])
        if client_asset_id is not None:
            values = [v for v in values if v.get("client_asset_id") == client_asset_id]
        return {"assets": values[offset:offset+limit], "session_id": session_id,
            "limit": limit, "offset": offset, "has_more": offset+limit < len(values)}

    @app.post(PREFIX+"/{session_id}/assets/{asset_id}/resume")
    def resume_asset(session_id: str, asset_id: str, request: Request):
        session = service._access(request.state.principal, session_id, "assets:write")
        app.state.assets.get(request.state.principal.owner, asset_id, session["payload"]["project_id"])
        return app.state.assets.resume(request.state.principal.owner, asset_id)

    @app.get(PREFIX+"/{session_id}/materials")
    def materials(session_id: str, request: Request):
        return service.materials(request.state.principal, session_id)

    @app.put(PREFIX+"/{session_id}/materials")
    def set_materials(session_id: str, request: Request, body: dict, key: str = Header(..., alias="Idempotency-Key")):
        return service.put_materials(request.state.principal, session_id, body, key)

    @app.get(PREFIX+"/{session_id}/timeline")
    def timeline(session_id: str, request: Request, limit: int = Query(20, ge=1, le=50),
                 cursor: str | None = None, direction: str = "older", before_cursor: str | None = None, after_cursor: str | None = None):
        if sum(v is not None for v in (cursor, before_cursor, after_cursor))>1:
            raise QuickChatError("invalid_cursor", "只能指定一个分页位置。", 422)
        return service.timeline(request.state.principal, session_id, limit=limit,
            cursor=after_cursor or before_cursor or cursor, direction="newer" if after_cursor else "older" if before_cursor else direction)

    @app.post(PREFIX+"/{session_id}/turns", status_code=201)
    def create_turn(session_id: str, request: Request, body: dict, background_tasks: BackgroundTasks,
                    key: str = Header(..., alias="Idempotency-Key")):
        value = service.create_turn(request.state.principal, session_id, body, key)
        background_tasks.add_task(service.generate_title, request.state.principal, session_id)
        return value

    @app.get(PREFIX+"/{session_id}/turns/{turn_id}")
    def turn(session_id: str, turn_id: str, request: Request):
        return service.get_turn(request.state.principal, session_id, turn_id)

    @app.post(PREFIX+"/{session_id}/turns/{turn_id}/acknowledge-unknown")
    def acknowledge(session_id: str, turn_id: str, request: Request, body: dict, key: str = Header(..., alias="Idempotency-Key")):
        return service.acknowledge_unknown(request.state.principal, session_id, turn_id, body, key)

    @app.post(PREFIX+"/{session_id}/cards", status_code=201)
    def create_card(session_id: str, request: Request, body: dict, background_tasks: BackgroundTasks,
                    key: str = Header(..., alias="Idempotency-Key")):
        value = service.save_card(request.state.principal, session_id, body, key)
        background_tasks.add_task(service.generate_title, request.state.principal, session_id)
        return value

    @app.get(PREFIX+"/{session_id}/cards/{card_id}")
    def card(session_id: str, card_id: str, request: Request):
        return service.get_card(request.state.principal, session_id, card_id)

    @app.post(PREFIX+"/{session_id}/cards/{card_id}/revisions", status_code=201)
    def revision(session_id: str, card_id: str, request: Request, body: dict, background_tasks: BackgroundTasks,
                 key: str = Header(..., alias="Idempotency-Key")):
        value = service.save_card(request.state.principal, session_id, body, key, card_id=card_id)
        background_tasks.add_task(service.generate_title, request.state.principal, session_id)
        return value

    @app.get(PREFIX+"/{session_id}/revisions/{revision_id}")
    def get_revision(session_id: str, revision_id: str, request: Request):
        return service.get_revision(request.state.principal, session_id, revision_id)

    @app.post(PREFIX+"/{session_id}/revisions/{revision_id}/preflights", status_code=201)
    def preflight(session_id: str, revision_id: str, request: Request, body: dict, key: str = Header(..., alias="Idempotency-Key")):
        return service.preflight(request.state.principal, session_id, revision_id, body, key)

    @app.get(PREFIX+"/{session_id}/preflights/{preflight_id}")
    def get_preflight(session_id: str, preflight_id: str, request: Request):
        return service.get_preflight(request.state.principal, session_id, preflight_id)

    @app.post(PREFIX+"/{session_id}/revisions/{revision_id}/submissions", status_code=202)
    def submit(session_id: str, revision_id: str, request: Request, body: dict, key: str = Header(..., alias="Idempotency-Key")):
        return service.submit(request.state.principal, session_id, revision_id, body, key)

    @app.get(PREFIX+"/{session_id}/submissions/{submission_id}")
    def submission(session_id: str, submission_id: str, request: Request):
        return service.get_submission(request.state.principal, session_id, submission_id)

    @app.post(PREFIX+"/{session_id}/submissions/{submission_id}/cancel")
    def cancel(session_id: str, submission_id: str, request: Request, body: dict, key: str = Header(..., alias="Idempotency-Key")):
        return service.cancel(request.state.principal, session_id, submission_id, body, key)

    @app.post(PREFIX+"/{session_id}/submissions/{submission_id}/resume-admission")
    def resume(session_id: str, submission_id: str, request: Request, body: dict, key: str = Header(..., alias="Idempotency-Key")):
        return service.resume_admission(request.state.principal, session_id, submission_id, body, key)

    @app.post(PREFIX+"/{session_id}/submissions/{submission_id}/items/{item_id}/retry", status_code=202)
    def retry(session_id: str, submission_id: str, item_id: str, request: Request, body: dict, key: str = Header(..., alias="Idempotency-Key")):
        return service.retry(request.state.principal, session_id, submission_id, item_id, body, key)

    @app.post(PREFIX+"/{session_id}/result-imports", status_code=201)
    def result_import(session_id: str, request: Request, body: dict, key: str = Header(..., alias="Idempotency-Key")):
        return service.result_import(request.state.principal, session_id, body, key)

    @app.get(PREFIX+"/{session_id}/result-imports/{import_id}")
    def get_import(session_id: str, import_id: str, request: Request):
        return service.get_result_import(request.state.principal, session_id, import_id)

    return service
