"""Versioned video business API. No import of the legacy single-worker server."""
from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import quote, urlsplit
import uuid

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from .assets import AssetService, AssetNotFound
from .upload_route import AssetUploadRoute
from .auth import Auth, AuthenticationError, LoginLimited
from .capabilities import VERSION, capabilities, compile_request
from .media import MediaError, MediaBusy
from .project_validation import validate_project
from .repository import Repository, Scope, NotFound, Conflict, BudgetExceeded, plans, artifacts, jobs
from .settings import Settings
from .storage import LocalObjectStore, StorageError, S3ObjectStore
from .storage_config import S3StorageConfig, R2_CREDENTIAL_FIELDS, load_storage_credentials
from .source_snapshot import source_snapshot, validate_source_ref
from .http_limits import (ADMISSION_SCOPE_KEY, BodyLimitMiddleware, UploadAdmission,
                         RequestAdmission, RequestAdmissionMiddleware, admission_rejected)
from .execution_policy import ExecutionPolicies
from .render_plans import RECIPE as RENDER_RECIPE, compile_render, validate_render_source
from .agent_discovery import PUBLIC_PATHS as AGENT_PUBLIC_PATHS, DISCOVERY_LINK

COOKIE = "sixnine_session"
TERMINAL = {"succeeded", "failed", "cancelled"}


def create_app(settings: Settings, *, repository=None, storage=None):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    repo = repository or Repository(settings.database_url)
    repo.create_schema()
    if storage is None:
        if settings.storage_provider == "local":
            storage = LocalObjectStore(settings.data_dir / "objects")
        elif settings.storage_provider == "r2":
            import os
            cfg = S3StorageConfig("r2", settings.storage_endpoint, settings.storage_region,
                settings.storage_bucket, "cloudflare-r2", settings.storage_profile, enabled=True)
            credentials = load_storage_credentials(cfg, fields=R2_CREDENTIAL_FIELDS,
                registry_root=os.environ.get("AI_REGISTRY_ROOT"))
            storage = S3ObjectStore(cfg, credentials)
        else:
            raise ValueError("此远端存储需要显式注入已审阅的store与凭据入口；不使用默认AWS账户")
    auth = Auth(repo.engine, tenant=settings.tenant_id, mode=settings.auth_mode, session_seconds=settings.session_seconds)
    asset_service = AssetService(repo.engine, storage, settings.data_dir,
        tenant=settings.tenant_id, max_bytes=settings.max_upload_bytes)
    execution_policies = ExecutionPolicies(settings, repo)
    upload_admission = UploadAdmission()
    request_admission = RequestAdmission()

    @asynccontextmanager
    async def lifespan(app):
        yield
        if repository is None:
            repo.close()

    app = FastAPI(title="Sixnine video platform", version="1.0.0-dev", lifespan=lifespan)
    app.add_middleware(BodyLimitMiddleware, project_bytes=settings.max_project_bytes,
                       upload_bytes=settings.max_upload_bytes)
    app.state.repository, app.state.auth, app.state.storage = repo, auth, storage
    app.state.assets, app.state.settings = asset_service, settings
    app.state.upload_admission = upload_admission
    app.state.request_admission = request_admission

    def scope(principal, project_id):
        return Scope(settings.tenant_id, principal.owner, project_id, principal.actor_id)

    def project_scope(principal):
        return scope(principal, "__projects")

    def authorized_project(principal, project_id, operation="projects:read"):
        if not isinstance(project_id, str) or not principal.allows(project_id, operation):
            raise NotFound("project_not_found")
        return repo.get_document(project_scope(principal), "project", project_id)

    def project_response(record):
        return {"id": record["document_id"], "version": record["version"],
                "updated_at": record["updated_at"], "project": record["payload"]}

    def public_execution(execution):
        # Expose user-relevant admission, not operator approval/budget identities.
        return {"admission_state": execution.get("admission_state", "queued" if execution.get("enabled") else "blocked"),
                "enabled": bool(execution.get("enabled")), "quote_known": bool(execution.get("quote_known")),
                "backend": execution.get("backend")}

    def owned_plan(principal, plan_id):
        with repo.engine.connect() as conn:
            row = conn.execute(select(plans).where(plans.c.id == plan_id,
                plans.c.tenant_id == settings.tenant_id, plans.c.owner_id == principal.owner)).mappings().first()
        if not row:
            raise NotFound("plan_not_found")
        authorized_project(principal, row["project_id"], "jobs:write")
        return dict(row)

    def owned_job(principal, job_id, operation="jobs:read"):
        job = repo.get_job_for_owner(settings.tenant_id, principal.owner, job_id)
        authorized_project(principal, job["project_id"], operation)
        return job

    def public_artifact(record):
        value = record["metadata"]
        return {"id": record["id"], "job_id": record["job_id"], "kind": value["kind"],
                "mime": value.get("mime", value.get("content_type", "application/octet-stream")),
                "size_bytes": value["size_bytes"], "sha256": value["sha256"],
                "metadata": {k: v for k, v in value.items() if k not in {"object_key", "provider", "storage_profile"}},
                "content_url": f'/v1/artifacts/{record["id"]}/content',
                "download_url": f'/v1/artifacts/{record["id"]}/content?download=1'}

    def public_job(job, *, artifact_records=None):
        stored_request = job["request"]
        visible = {k: job.get(k) for k in ("id", "status", "created_at", "updated_at", "request_hash", "error_code", "result", "created")}
        visible.update(client_ref=stored_request.get("client_ref", {}), phase=job["status"],
            recipe_id=stored_request.get("recipe_id"), effective_request=stored_request.get("request", {}),
            simulation=job["execution_plan"].get("backend") == "mock" or stored_request.get("simulation") is True, plan_id=job["plan_id"],
            project_id=job["project_id"], artifacts=[])
        if job["status"] == "succeeded":
            if artifact_records is None:
                artifact_records = repo.list_artifacts(Scope(settings.tenant_id, job["owner_id"], job["project_id"]), job["id"])
            for art in artifact_records:
                visible["artifacts"].append(public_artifact(art))
        return visible

    @app.exception_handler(NotFound)
    @app.exception_handler(AssetNotFound)
    async def missing(_request, _error):
        return JSONResponse({"detail": "资源不存在或无权访问"}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(BudgetExceeded)
    async def quota(_request, _error):
        return JSONResponse({"detail": "预算或容量不足，未新增付费执行"}, status_code=429)

    @app.exception_handler(ValueError)
    async def invalid(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.exception_handler(MediaBusy)
    async def media_busy(_request, _error):
        return JSONResponse({"detail": "素材处理繁忙；原件已保留，请在素材列表恢复同一素材"},
                            status_code=503, headers={"Retry-After": "5", "Cache-Control": "no-store"})

    @app.exception_handler(StorageError)
    async def storage_error(_request, _error):
        return JSONResponse({"detail": "素材存储不可用，未创建生成任务"}, status_code=503)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(_request, _error):
        # SQLAlchemy errors may include a DSN or user data. Do not serialize them.
        return JSONResponse({"detail": "持久存储暂时不可用，请保留当前编辑后重试"}, status_code=503)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "").lower()
        if settings.public_origin:
            expected = urlsplit(settings.public_origin).netloc.lower()
            if host != expected and not (request.url.path == "/healthz" and host.split(":")[0] in {"localhost", "127.0.0.1"}):
                return JSONResponse({"detail": "无效访问域名"}, status_code=400)
        elif host.split(":")[0] not in {"localhost", "127.0.0.1", "testserver"}:
            return JSONResponse({"detail": "本地服务仅支持loopback入口"}, status_code=400)
        maximum = settings.max_upload_bytes + 1024*1024 if request.url.path == "/v1/assets" else settings.max_project_bytes
        length = request.headers.get("content-length")
        if length and (len(length) > 20 or not length.isascii() or not length.isdigit() or int(length) > maximum):
            return JSONResponse({"detail": "请求体超过限制"}, status_code=413)
        principal = None
        authorization = request.headers.get("authorization", "")
        lease = request.scope[ADMISSION_SCOPE_KEY]
        if not lease.acquire("authentication"):
            return admission_rejected()
        try:
            if authorization:
                if authorization.startswith("Bearer "):
                    principal = await run_in_threadpool(auth.bearer, authorization[7:])
                if not principal:
                    return JSONResponse({"detail": "服务身份无效"}, status_code=401)
            else:
                principal = await run_in_threadpool(auth.session, request.cookies.get(COOKIE))
        except SQLAlchemyError:
            # Middleware runs outside FastAPI's endpoint exception handlers.
            # Connection errors must not escape with a DSN or bound token hash.
            return JSONResponse({"detail": "身份服务暂时不可用，请稍后重试"}, status_code=503,
                                headers={"Cache-Control": "no-store", "Retry-After": "3"})
        finally:
            lease.release("authentication")
        expected_account = request.headers.get("x-expected-account")
        if (principal and not principal.machine and request.url.path.startswith("/v1/")
                and expected_account is not None and expected_account != principal.owner):
            # Cookies are shared across tabs and ports. An old tab must not
            # accidentally apply its draft to a new owner's same project ID.
            return JSONResponse({"detail": "登录账户已改变，已阻止本次操作；请先核对当前账户",
                                 "code": "account_context_changed"}, status_code=409,
                headers={"Cache-Control": "no-store", "X-Authenticated-Account": principal.owner})
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not (principal and principal.machine):
            origin = request.headers.get("origin")
            expected = settings.public_origin or str(request.base_url).rstrip("/")
            allowed_origins = {expected, *settings.local_ui_origins}
            if origin and origin.rstrip("/") not in allowed_origins:
                return JSONResponse({"detail": "跨站写入请求被拒绝"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "跨站写入请求被拒绝"}, status_code=403)
        public_paths = {"/healthz", "/api/auth/config", "/api/auth/login"}
        public_frontend = (settings.frontend_dir is not None and request.method in {"GET", "HEAD"}
            and (request.url.path in {"/", "/index.html", "/freestyle", "/freestyle/"} or request.url.path.startswith("/assets/")))
        public_discovery = request.method in {"GET", "HEAD"} and request.url.path in AGENT_PUBLIC_PATHS
        if request.url.path not in public_paths and not public_frontend and not public_discovery and not principal:
            return JSONResponse({"detail": "请先登录"}, status_code=401)
        request.state.principal = principal
        if principal and not lease.acquire("owner", principal.owner):
            return admission_rejected()
        downloading = (principal is not None and request.method in {"GET", "HEAD"}
            and request.url.path.endswith("/content")
            and request.url.path.startswith(("/v1/artifacts/", "/v1/assets/")))
        if downloading and (not lease.acquire("downloads")
                or not lease.acquire("owner_downloads", principal.owner)):
            return admission_rejected()
        uploading = request.method == "POST" and request.url.path == "/v1/assets" and principal is not None
        if uploading and not upload_admission.acquire(principal.owner):
            return JSONResponse({"detail": "同时上传的文件已达到上限，请等当前上传完成再重试"}, status_code=429,
                                headers={"Retry-After": "3"})
        if uploading:
            lease.release_when_finished(lambda: upload_admission.release(principal.owner))
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path in {"/", "/index.html", "/for-agents", "/for-agents/"}:
            response.headers["Link"] = DISCOVERY_LINK
        if principal and request.url.path.startswith("/v1/"):
            response.headers["X-Authenticated-Account"] = principal.owner
        return response

    @app.get("/healthz", include_in_schema=False)
    def health():
        with repo.engine.connect() as connection:
            connection.execute(select(1)).scalar_one()
        return {"status": "ok", "generation_enabled": settings.generation_enabled,
                "execution_backend": settings.execution_backend, "auth_ready": auth.ready(),
                "render_enabled": settings.render_enabled,
                "cloud_creation_enabled": False}

    @app.get("/api/auth/config")
    def auth_config():
        return {"authentication": "password" if settings.auth_mode == "password" else "username-only-test",
                "auth_ready": auth.ready(), "users": ["superdan", "supervan"] if settings.auth_mode == "local-test" else []}

    @app.post("/api/auth/login")
    async def login(request: Request):
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("username"), str) or not isinstance(body.get("password", ""), str):
            raise HTTPException(422, "登录信息格式无效")
        try:
            token = await run_in_threadpool(auth.login, body["username"], body.get("password", ""), request.client.host if request.client else "unknown")
        except LoginLimited as error:
            raise HTTPException(429, str(error), headers={"Retry-After": str(error.retry_after)}) from None
        except AuthenticationError as error:
            raise HTTPException(401, str(error)) from None
        response = JSONResponse({"username": body["username"], "authentication": settings.auth_mode})
        response.set_cookie(COOKIE, token, max_age=settings.session_seconds, httponly=True,
            secure=bool(settings.public_origin), samesite="lax", path="/")
        return response

    @app.get("/api/auth/me")
    def whoami(request: Request):
        principal = request.state.principal
        return {"username": principal.owner, "actor_id": principal.actor_id, "machine": principal.machine,
                "authentication": settings.auth_mode, "scopes": list(principal.scopes),
                "all_projects": principal.all_projects, "project_ids": list(principal.project_ids)}

    @app.post("/api/auth/logout")
    def logout(request: Request):
        auth.logout(request.cookies.get(COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/v1/capabilities")
    def get_capabilities():
        result = capabilities(settings)
        result["upload_max_bytes"] = settings.max_upload_bytes
        result["chapter_render"] = {"recipe_id": RENDER_RECIPE, "implemented": True,
            "enabled": settings.render_enabled, "resolutions": ["480P", "720P"],
            "aspects": ["16:9", "9:16", "1:1", "4:3", "3:4"], "fps": 24,
            "max_shots": 50, "max_duration": 600, "max_audio_tracks": 32,
            "video_original_audio": False, "subtitles": True, "transitions": False,
            "source_trim": True, "generated_audio": True, "render_contract_version": 3}
        return result

    @app.get("/v1/projects")
    def list_projects(request: Request, limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0, le=1000000)):
        principal = request.state.principal
        allowed = ((None if principal.all_projects else principal.project_ids) if "projects:read" in principal.scopes else ()) if principal.machine else None
        values = repo.list_documents(project_scope(principal), "project", limit=limit, offset=offset,
                                     allowed_ids=allowed, summary=True)
        return {"projects": [{"id": v["document_id"], "title": v["payload"]["title"],
                              "version": v["version"], "updated_at": v["updated_at"]}
                for v in values if principal.allows(v["document_id"], "projects:read")]}

    @app.post("/v1/projects", status_code=201)
    def create_project(request: Request, body: dict, idempotency_key: str | None = Header(None)):
        principal = request.state.principal
        if principal.machine and not (principal.all_projects and "projects:create" in principal.scopes):
            raise HTTPException(403, "创建故事需要projects:create及全部本人项目授权")
        from .guided import empty_project
        if "project" in body:
            if set(body) != {"project"}:
                raise HTTPException(422, "导入故事仅接受project字段")
            project = body["project"]
        else:
            values = dict(body)
            if idempotency_key and "id" not in values:
                values["id"] = "project-"+uuid.uuid5(uuid.NAMESPACE_URL,
                    f"{settings.tenant_id}:{principal.owner}:{principal.actor_id}:{idempotency_key}").hex
            project = empty_project(values)
        validate_project(project, settings.max_project_bytes)
        return app.state.guided.mutate(principal, project["id"], project, idempotency_key, create=True)

    @app.get("/v1/projects/{project_id}")
    def get_project(project_id: str, request: Request):
        return project_response(authorized_project(request.state.principal, project_id))

    @app.put("/v1/projects/{project_id}")
    def save_project(project_id: str, request: Request, body: dict):
        principal = request.state.principal
        if principal.machine and not principal.allows(project_id, "projects:write"):
            raise HTTPException(403, "保存故事需要projects:write授权")
        authorized_project(principal, project_id, "projects:write")
        project = validate_project(body.get("project"), settings.max_project_bytes)
        if project["id"] != project_id or type(body.get("expected_version")) is not int:
            raise HTTPException(422, "需要匹配的项目ID与版本")
        return app.state.guided.save(principal, project_id, project, body["expected_version"])

    def upload_asset(request: Request, file: UploadFile = File(...), client_project_id: str = Form(...), client_asset_id: str | None = Form(None)):
        principal = request.state.principal
        authorized_project(principal, client_project_id, "assets:write")
        return asset_service.upload(principal.owner, client_project_id, file.file, file.filename or "file", client_asset_id=client_asset_id)

    app.router.add_api_route("/v1/assets", upload_asset, methods=["POST"],
        status_code=201, route_class_override=AssetUploadRoute)

    @app.get("/v1/assets")
    def list_assets(request: Request, client_project_id: str):
        principal = request.state.principal
        authorized_project(principal, client_project_id, "assets:read")
        return {"assets": asset_service.list(principal.owner, client_project_id)}

    @app.get("/v1/storage-usage")
    def storage_usage(request: Request):
        if request.state.principal.machine:
            raise HTTPException(403, "服务身份不能查询账户整体存储用量")
        return asset_service.usage(request.state.principal.owner)

    @app.get("/v1/assets/{asset_id}")
    def get_asset(asset_id: str, request: Request):
        principal = request.state.principal
        value = asset_service.get(principal.owner, asset_id)
        authorized_project(principal, value["project_id"], "assets:read")
        return asset_service.public(value)

    @app.post("/v1/assets/{asset_id}/resume")
    def resume_asset(asset_id: str, request: Request):
        principal = request.state.principal
        value = asset_service.get(principal.owner, asset_id)
        authorized_project(principal, value["project_id"], "assets:write")
        # Browser callers cannot override an active/stale worker lease. They
        # resume the same durable receipt, never create a fresh storage key.
        return asset_service.resume(principal.owner, asset_id)

    def object_content(key, mime, filename, request, *, download=False):
        headers = {"Accept-Ranges": "bytes", "Content-Disposition": ("attachment" if download else "inline")+"; filename*=UTF-8''"+quote(filename, safe="")}
        if not isinstance(storage, LocalObjectStore):
            if request.method == "HEAD":
                # A GET presign cannot authenticate a redirected HEAD request.
                info = storage.stat(key)
                headers["Content-Length"] = str(info.size_bytes)
                return StreamingResponse(iter(()), media_type=mime, headers=headers)
            options = {"download_filename": filename} if download else {}
            url = storage.presign_download(key, expires_seconds=300, **options)
            return RedirectResponse(url.reveal(), status_code=307,
                headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"})
        info = storage.stat(key)
        start, end, status = 0, info.size_bytes-1, 200
        requested_range = request.headers.get("range")
        if requested_range:
            match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", requested_range)
            if not match or not any(match.groups()):
                raise HTTPException(416, "无效字节范围", headers={"Content-Range": f"bytes */{info.size_bytes}"})
            left, right = match.groups()
            if left:
                start = int(left)
                end = min(int(right) if right else end, end)
            else:
                start = max(0, info.size_bytes-int(right))
            if start > end or start >= info.size_bytes:
                raise HTTPException(416, "字节范围越界", headers={"Content-Range": f"bytes */{info.size_bytes}"})
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{info.size_bytes}"
        headers["Content-Length"] = str(end-start+1)
        def stream():
            with storage.open(key) as source:
                # Starlette's threaded iterator may remain suspended at yield
                # after a disconnected/expired response. The outer ASGI lease
                # deterministically closes the handle instead of relying on
                # generator garbage collection or a success-only background task.
                request.scope[ADMISSION_SCOPE_KEY].release_when_finished(source.close)
                source.seek(start)
                remaining = end-start+1
                while remaining:
                    chunk = source.read(min(1024*1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
        return StreamingResponse(iter(()) if request.method == "HEAD" else stream(), status_code=status, media_type=mime, headers=headers)

    @app.api_route("/v1/assets/{asset_id}/content", methods=["GET", "HEAD"])
    def asset_content(asset_id: str, request: Request, download: bool = False):
        principal = request.state.principal
        value = asset_service.get(principal.owner, asset_id)
        authorized_project(principal, value["project_id"], "assets:read")
        if value["status"] != "ready":
            raise HTTPException(409, "素材尚未就绪")
        return object_content(value["original"]["key"], value["mime"], value["file_name"], request, download=download)

    @app.post("/v1/assets/{asset_id}/derivatives", status_code=201)
    def derivative(asset_id: str, request: Request, body: dict):
        principal = request.state.principal
        parent = asset_service.get(principal.owner, asset_id)
        authorized_project(principal, parent["project_id"], "assets:write")
        if set(body) - {"start", "end"}:
            raise HTTPException(422, "仅支持明确的时间选段")
        return asset_service.derive(principal.owner, asset_id, body.get("start"), body.get("end"))

    @app.post("/v1/generation-plans", status_code=201)
    def make_plan(request: Request, body: dict):
        principal = request.state.principal
        project_id = body.get("client_ref", {}).get("project_id") if isinstance(body.get("client_ref"), dict) else None
        project = authorized_project(principal, project_id, "jobs:write")["payload"]
        compiled, fingerprint = compile_request(body, lambda asset_id: asset_service.model_snapshot(principal.owner, project_id, asset_id))
        ref = compiled["client_ref"]
        if not validate_source_ref(project, ref):
            raise Conflict("shot_version_conflict")
        compiled["server_source_hash"] = source_snapshot(project, ref["shot_id"])
        admission = execution_policies.evaluate(compiled, scope(principal, project_id), fingerprint)
        execution = admission.execution
        enabled, blockers = execution["enabled"], execution["blockers"]
        simulation = settings.execution_backend == "mock"
        plan = repo.create_plan(scope(principal, project_id), compiled, execution,
            expires_at=admission.expires_at, estimated_cost_microusd=admission.cost)
        return {"plan_id": plan["id"], "status": "ready" if enabled else "blocked", "request_hash": plan["request_hash"],
                "effective_request": compiled["request"], "output_spec": compiled["output_spec"],
                "client_ref": ref, "expires_at": plan["expires_at"], "blockers": blockers,
                "warnings": ["本地模拟：不调用模型，不代表H3速度或质量"] if simulation else [],
                "estimate": admission.estimate, "execution": public_execution(execution), "simulation": simulation}

    def resolve_render_source(principal, project_id, entity, kind):
        data = entity.get("data", {})
        artifact_id, asset_id = data.get("cloudArtifactId"), data.get("cloudAssetId")
        if artifact_id:
            if not isinstance(artifact_id, str):
                raise NotFound("source_not_found")
            with repo.engine.connect() as connection:
                row = connection.execute(select(artifacts).join(jobs, artifacts.c.job_id == jobs.c.id).where(
                    artifacts.c.id == artifact_id, jobs.c.tenant_id == settings.tenant_id,
                    jobs.c.owner_id == principal.owner, jobs.c.project_id == project_id,
                    jobs.c.status == "succeeded")).mappings().first()
            if row is None:
                raise NotFound("source_not_found")
            metadata = row["metadata"]
            job = owned_job(principal, row["job_id"])
            if metadata.get("kind") != kind or not metadata.get("validated"):
                raise ValueError("成片来源尚未校验")
            return {"kind": kind, "duration": metadata.get("duration_s"),
                "source_job_id": row["job_id"], "artifact_id": row["id"],
                "object": {"key": metadata["object_key"], "size_bytes": metadata["size_bytes"],
                    "sha256": metadata["sha256"], "content_type": metadata.get("content_type", metadata.get("mime"))},
                "simulation": job["execution_plan"].get("backend") == "mock" or job["request"].get("simulation") is True}
        if not isinstance(asset_id, str):
            raise ValueError("需要先把所选本机素材上传到当前云项目")
        try:
            asset = asset_service.get(principal.owner, asset_id, project_id)
        except AssetNotFound:
            raise NotFound("source_not_found") from None
        if asset.get("status") != "ready" or asset.get("kind") != kind:
            raise ValueError("素材尚未就绪或类型不符")
        return {"kind": kind, "duration": asset["metadata"].get("source_duration"),
                "source_job_id": None, "artifact_id": None,
                "object": asset["original"], "simulation": False}

    @app.post("/v1/render-plans", status_code=201)
    def make_render_plan(request: Request, body: dict):
        principal = request.state.principal
        project_id = body.get("client_ref", {}).get("project_id") if isinstance(body.get("client_ref"), dict) else None
        project = authorized_project(principal, project_id, "jobs:write")["payload"]
        compiled, timeline, blockers, warnings = compile_render(body, project,
            lambda entity, kind: resolve_render_source(principal, project_id, entity, kind))
        compiled["render_blockers"] = blockers
        from .repository import request_hash
        fingerprint = request_hash(compiled)
        admission = execution_policies.evaluate(compiled, scope(principal, project_id), fingerprint)
        plan = repo.create_plan(scope(principal, project_id), compiled, admission.execution,
            expires_at=admission.expires_at, estimated_cost_microusd=0)
        return {"plan_id": plan["id"], "status": "ready" if admission.execution["enabled"] else "blocked",
            "request_hash": plan["request_hash"], "client_ref": compiled["client_ref"],
            "output_spec": compiled["output_spec"], "timeline": timeline,
            "expires_at": plan["expires_at"], "blockers": admission.execution["blockers"],
            "warnings": warnings, "estimate": admission.estimate, "execution": public_execution(admission.execution),
            "simulation": compiled["simulation"]}

    def check_plan_source(project, compiled):
        if compiled["recipe_id"] == RENDER_RECIPE:
            if not validate_render_source(project, compiled):
                raise Conflict("chapter_timeline_changed")
        else:
            ref = compiled["client_ref"]
            if (not validate_source_ref(project, ref)
                    or compiled.get("server_source_hash") != source_snapshot(project, ref["shot_id"])):
                raise Conflict("shot_version_conflict")

    def create_from_plan(principal, plan_id, idempotency_key, initial_status=None):
        plan = owned_plan(principal, plan_id)
        # A lost HTTP response can be retried after editing the shot. The original
        # immutable task still belongs to this key; let the ledger compare hashes.
        existing = repo.lookup_job_by_idempotency(scope(principal, plan["project_id"]), idempotency_key)
        if existing:
            return repo.create_job(scope(principal, plan["project_id"]), plan_id, idempotency_key)
        project = authorized_project(principal, plan["project_id"], "jobs:write")["payload"]
        check_plan_source(project, plan["request"])
        execution = plan["execution_plan"]
        enabled_setting = settings.render_enabled if plan["request"]["recipe_id"] == RENDER_RECIPE else settings.generation_enabled
        ready = enabled_setting and execution.get("enabled") and execution.get("quote_known")
        status = initial_status or (execution.get("admission_state", "queued") if ready else "blocked")
        if status in {"queued", "planned", "waiting_capacity"} and not ready:
            status = "blocked"
        task_scope = scope(principal, plan["project_id"])
        budgets = execution_policies.ensure_current(plan, task_scope) if ready else ()
        return repo.create_job(task_scope, plan_id, idempotency_key, initial_status=status, budget_account_ids=budgets)

    def enqueue_planned(principal, job):
        task_scope = scope(principal, job["project_id"])
        plan = owned_plan(principal, job["plan_id"])
        project = authorized_project(principal, job["project_id"], "jobs:write")["payload"]
        check_plan_source(project, plan["request"])
        budgets = execution_policies.ensure_current(plan, task_scope)
        return repo.enqueue(task_scope, job["id"], budget_account_ids=budgets)

    @app.post("/v1/jobs", status_code=202)
    def create_job(request: Request, body: dict, idempotency_key: str = Header(..., alias="Idempotency-Key")):
        if set(body) != {"plan_id"} or not isinstance(body["plan_id"], str):
            raise HTTPException(422, "只接受已确认的plan_id")
        return public_job(create_from_plan(request.state.principal, body["plan_id"], idempotency_key))

    @app.get("/v1/jobs")
    def list_jobs(request: Request, client_project_id: str | None = None, limit: int = Query(100, ge=1, le=100),
                  offset: int = Query(0, ge=0, le=1000000)):
        principal = request.state.principal
        if client_project_id:
            authorized_project(principal, client_project_id, "jobs:read")
        allowed = ((None if principal.all_projects else principal.project_ids) if "jobs:read" in principal.scopes else ()) if principal.machine else None
        values = repo.list_jobs_for_owner(settings.tenant_id, principal.owner, project_id=client_project_id, project_ids=allowed,
                                         limit=limit, offset=offset, summary=True)
        visible = [v for v in values if principal.allows(v["project_id"], "jobs:read")]
        media = repo.list_artifacts_for_jobs(settings.tenant_id, principal.owner,
            {v["id"]: v["project_id"] for v in visible if v["status"] == "succeeded"})
        return {"jobs": [public_job(v, artifact_records=media.get(v["id"], [])) for v in visible]}

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str, request: Request):
        return public_job(owned_job(request.state.principal, job_id))

    @app.get("/v1/activity-summary")
    def activity_summary(request: Request, client_project_id: str):
        principal = request.state.principal
        authorized_project(principal, client_project_id, "jobs:read")
        from .diagnostics import job_activity
        return job_activity(repo, tenant_id=settings.tenant_id, owner_id=principal.owner,
                            project_id=client_project_id)

    @app.post("/v1/jobs/{job_id}/cancel")
    def cancel(job_id: str, request: Request):
        principal = request.state.principal
        job = owned_job(principal, job_id, "jobs:write")
        return public_job(repo.request_cancel(scope(principal, job["project_id"]), job_id))

    @app.get("/v1/jobs/{job_id}/artifacts")
    def job_artifacts(job_id: str, request: Request):
        job = owned_job(request.state.principal, job_id)
        return {"artifacts": public_job(job)["artifacts"]}

    @app.api_route("/v1/artifacts/{artifact_id}/content", methods=["GET", "HEAD"])
    def artifact_content(artifact_id: str, request: Request, download: bool = False):
        principal = request.state.principal
        with repo.engine.connect() as conn:
            row = conn.execute(select(artifacts).join(jobs, artifacts.c.job_id == jobs.c.id).where(
                artifacts.c.id == artifact_id, jobs.c.tenant_id == settings.tenant_id,
                jobs.c.owner_id == principal.owner)).mappings().first()
        if not row:
            raise NotFound("artifact_not_found")
        owned_job(principal, row["job_id"])
        value = row["metadata"]
        extension = {"video": "mp4", "audio": "flac", "image": "png"}.get(value.get("kind"), "bin")
        filename = value.get("filename", f'sixnine-{row["job_id"][:8]}-{value.get("kind", "result")}.{extension}')
        return object_content(value["object_key"], value.get("mime", value.get("content_type", "application/octet-stream")),
                              filename, request, download=download)

    # Helpers for the batch/worker integration without exposing internal state to clients.
    app.state.scope = scope
    app.state.authorized_project = authorized_project
    app.state.owned_plan = owned_plan
    app.state.create_from_plan = create_from_plan
    app.state.enqueue_planned = enqueue_planned
    app.state.public_job = public_job
    from .batches import register_routes
    register_routes(app)
    from .guided import register_routes as register_guided_routes
    register_guided_routes(app)
    from .agent_discovery import register_routes as register_agent_discovery_routes
    register_agent_discovery_routes(app)
    if settings.frontend_dir is not None:
        if not (settings.frontend_dir / "index.html").is_file():
            raise ValueError("Configured frontend build is missing; build the reviewed Yingxu source snapshot first")

        @app.api_route("/freestyle", methods=["GET", "HEAD"], include_in_schema=False)
        @app.api_route("/freestyle/", methods=["GET", "HEAD"], include_in_schema=False)
        def freestyle():
            return FileResponse(settings.frontend_dir / "index.html")

        app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="yingxu")
    # Last-added middleware is outermost, including the authentication guard.
    app.add_middleware(RequestAdmissionMiddleware, admission=request_admission)
    return app
