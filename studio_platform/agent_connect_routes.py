"""Explicit browser authorization and a single anonymous JSON exchange route."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .agent_connect import AgentConnect, ConnectError
from .auth import AuthenticationError, Principal

EXCHANGE_PATH = "/v1/agent-connect/exchange"
HELPER_PATH = "/for-agents/connect.py"
MANIFEST_PATH = "/for-agents/connect-manifest.json"
PUBLIC_GET_PATHS = frozenset({HELPER_PATH, MANIFEST_PATH})
HELPER_SOURCE = Path(__file__).resolve().parent.parent / "skills" / "sixnine-yingxu" / "scripts" / "connect.py"
MAX_BODY_BYTES = 8192


def helper_manifest():
    import hashlib
    raw = HELPER_SOURCE.read_bytes()
    return {"version": 1, "helper": HELPER_PATH, "sha256": hashlib.sha256(raw).hexdigest(),
        "protocol": "client-held-pat-v1", "credential_storage": ["windows-user-dpapi", "linux-secret-service"],
        "missing_storage_behavior": "stop_before_exchange", "manual_pat": "advanced_fallback",
        "exchange": EXCHANGE_PATH, "raw_api_key_response": False, "discovery_is_authorization": False}


def register_routes(app, *, service=None):
    service = service or AgentConnect(app.state.auth)
    app.state.agent_connect = service
    settings = app.state.settings

    def origin(request):
        return service.origin(settings.public_origin or str(request.base_url).rstrip("/"))

    def source_guard(request, *, browser=False):
        expected = origin(request)
        permitted = {expected, *settings.local_ui_origins}
        provided = request.headers.get("origin")
        if provided is not None and provided.rstrip("/") not in permitted:
            raise ConnectError("connection_cross_site_rejected", 403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise ConnectError("connection_cross_site_rejected", 403)
        if browser and not provided:
            # Browsers always supply Origin on these credential-issuing writes.
            # Keep their proof stronger than the existing legacy API fallback.
            raise ConnectError("connection_origin_required", 403)
        return expected

    def browser(request):
        principal = getattr(request.state, "principal", None)
        if not isinstance(principal, Principal):
            raise ConnectError("connection_login_required", 401)
        if principal.machine:
            raise ConnectError("connection_browser_required", 403)
        return principal

    async def body(request, *, allowed, required=()):
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise ConnectError("connection_json_required", 415)
        data = await request.body()
        if len(data) > MAX_BODY_BYTES:
            raise ConnectError("connection_body_too_large", 413)
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError):
            raise ConnectError("connection_body_invalid", 422) from None
        if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
            raise ConnectError("connection_fields_invalid", 422)
        return value

    @app.exception_handler(ConnectError)
    async def connection_error(_request, error):
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        if error.retry_after:
            headers["Retry-After"] = str(error.retry_after)
        return JSONResponse({"code": error.code, "message": error.code,
            "retryable": error.status_code in {429, 503}}, status_code=error.status_code, headers=headers)

    @app.post("/v1/account/agent-connections", status_code=201)
    async def issue(request: Request):
        source_guard(request, browser=True)
        principal = browser(request)
        value = await body(request, allowed={"name", "authorization_profile_id", "authorization_profile_version"},
            required={"authorization_profile_id", "authorization_profile_version"})
        try:
            return await run_in_threadpool(service.issue, principal, name=value.get("name", "Codex"),
                authorization_profile_id=value["authorization_profile_id"],
                authorization_profile_version=value["authorization_profile_version"],
                idempotency_key=request.headers.get("idempotency-key"), origin=origin(request))
        except AuthenticationError:
            raise ConnectError("connection_login_required", 401) from None

    @app.get("/v1/account/agent-connections")
    def list_connections(request: Request, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=10000)):
        try:
            return service.list(browser(request), limit=limit, offset=offset)
        except AuthenticationError:
            raise ConnectError("connection_login_required", 401) from None

    @app.get("/v1/account/agent-connections/{connection_id}")
    def get_connection(connection_id: str, request: Request):
        try:
            return service.get(browser(request), connection_id)
        except AuthenticationError:
            raise ConnectError("connection_login_required", 401) from None

    @app.delete("/v1/account/agent-connections/{connection_id}")
    def revoke(connection_id: str, request: Request):
        source_guard(request, browser=True)
        try:
            return service.revoke(browser(request), connection_id)
        except AuthenticationError:
            raise ConnectError("connection_login_required", 401) from None

    @app.post(EXCHANGE_PATH)
    async def exchange(request: Request):
        expected_origin = source_guard(request)
        value = await body(request, allowed={"code", "client_challenge", "token_hash", "key_prefix",
            "expected_authorization_fingerprint", "recovery_verifier"},
            required={"code", "client_challenge", "token_hash", "key_prefix", "expected_authorization_fingerprint"})
        # ASGI client address is supplied by the configured trusted proxy layer.
        # Never directly trust a request's X-Forwarded-For string here.
        return await run_in_threadpool(service.exchange, **value, origin=expected_origin,
            source=request.client.host if request.client else "unknown")

