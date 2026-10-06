"""Loopback-only model comparison. Run directly; never mounts the GPU API."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import threading
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from studio_platform.google_chat import GoogleChatClient, ChatError, DEFAULT_INSTRUCTION, MODELS


def create_app(client=None, port=8864):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    provider = client or GoogleChatClient()
    lock, slots, receipts = threading.Lock(), threading.BoundedSemaphore(2), {}
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        if request.headers.get("host") not in hosts:
            return JSONResponse({"message": "仅允许本机访问。"}, status_code=403)
        if request.method != "GET" and (request.headers.get("origin") not in origins
                or request.headers.get("content-type", "").split(";")[0] != "application/json"):
            return JSONResponse({"message": "拒绝跨站请求。"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        return response

    @app.get("/")
    def page():
        return HTMLResponse((ROOT / "web/chat-model-lab.html").read_text(encoding="utf-8"))

    @app.get("/lab/models")
    def models():
        # Opening the page only reads local configuration; no provider request.
        return {"models": [{"id": key, "label": label, "configured": True}
                           for key, label in MODELS.items()],
                "instruction": DEFAULT_INSTRUCTION, "mode": "local-text-comparison"}

    @app.post("/lab/chat")
    async def chat(request: Request):
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > 300000:
                return JSONResponse({"message": "对话请求过大。"}, status_code=413)
        try:
            body = json.loads(raw)
            if not isinstance(body, dict) or set(body) != {"request_id", "model", "messages", "instruction"}:
                raise ValueError()
            ident = str(uuid.UUID(body["request_id"]))
        except Exception:
            return JSONResponse({"message": "请求格式不正确。"}, status_code=422)
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with lock:
            prior = receipts.get(ident)
            if prior:
                if prior[0] != fingerprint:
                    return JSONResponse({"message": "同一请求ID不能用于不同内容。"}, status_code=409)
                if prior[1] is None:
                    return JSONResponse({"message": "原请求仍在执行，请勿重复提交。"}, status_code=409)
                return JSONResponse(prior[1], status_code=prior[2])
            if len(receipts) >= 200 or not slots.acquire(blocking=False):
                return JSONResponse({"message": "本地试聊请求已满或正在执行，请稍后再试。"}, status_code=429)
            receipts[ident] = (fingerprint, None, 0)
        from starlette.concurrency import run_in_threadpool
        try:
            result = await run_in_threadpool(provider.generate, body["model"], body["messages"], body["instruction"])
            status = 200
        except ChatError as error:
            result, status = {"error": error.code, "message": error.message}, error.http_status
        except Exception:
            result, status = {"error": "request_failed", "message": "本次调用未能完成；没有自动重试。"}, 502
        finally:
            slots.release()
        with lock:
            receipts[ident] = (fingerprint, result, status)
        return JSONResponse(result, status_code=status)
    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=8864, access_log=False, log_level="warning")
