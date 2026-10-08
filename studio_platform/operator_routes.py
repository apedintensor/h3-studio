"""Cookie-only operator endpoints. Shared application middleware owns CSRF."""
from fastapi import Header, Query, Request
from fastapi.responses import JSONResponse

from .operator_capacity import OperatorCapacity, OperatorError, OperatorRegistry, selection

PREFIX = "/v1/operator/capacity"


def register_routes(app, *, registry=None, service=None):
    service = service or OperatorCapacity(app.state.repository, app.state.settings,
        registry if registry is not None else OperatorRegistry.from_environment())
    app.state.operator_capacity = service

    def principal(request):
        return getattr(request.state, "principal", None)

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @app.exception_handler(OperatorError)
    async def operator_error(_request, error):
        return JSONResponse({"code": error.code, "message": error.code},
            status_code=error.status, headers={"Cache-Control": "no-store"})

    @app.get(PREFIX+"/state")
    def state(request: Request):
        return response(service.state(principal(request)))

    @app.get(PREFIX+"/catalog")
    def catalog(request: Request):
        service.authorize(principal(request))
        return response(service.registry.catalog())

    @app.get(PREFIX+"/offers")
    def offers(request: Request, runtime_profile_id: str, gpu_type: str, mode: str,
               gpu_count: int = Query(1, ge=1, le=8)):
        service.authorize(principal(request))
        chosen=selection({"runtime_profile_id":runtime_profile_id,"gpu_type":gpu_type,
            "node_count":1,"gpu_count":gpu_count,"mode":mode,"ttl_seconds":120,"filters":{}})
        try:
            value=service.registry.offers(chosen,service.repo.clock())
        except Exception:
            # Never echo raw provider errors (which can include request URLs).
            value={"status":"unavailable","observed_at":service.repo.clock(),
                "offers":[],"reason_code":"operator_inventory_unavailable"}
        return response(value)

    @app.post(PREFIX+"/previews")
    def previews(request: Request, body: dict):
        return response(service.preview(principal(request),body))

    @app.post(PREFIX+"/starts", status_code=202)
    def starts(request: Request, body: dict, key: str=Header(...,alias="Idempotency-Key")):
        return JSONResponse(service.start(principal(request),body,key),status_code=202,
            headers={"Cache-Control":"no-store"})

    @app.post(PREFIX+"/nodes/{node_id}/drain", status_code=202)
    def drain(node_id: str, request: Request, body: dict, key: str=Header(...,alias="Idempotency-Key")):
        return JSONResponse(service.node_command(principal(request),node_id,body,key,"drain"),
            status_code=202,headers={"Cache-Control":"no-store"})

    @app.post(PREFIX+"/nodes/{node_id}/stop", status_code=202)
    def stop(node_id: str, request: Request, body: dict, key: str=Header(...,alias="Idempotency-Key")):
        return JSONResponse(service.node_command(principal(request),node_id,body,key,"stop"),
            status_code=202,headers={"Cache-Control":"no-store"})

    @app.put(PREFIX+"/policy")
    def policy(request: Request, body: dict):
        return response(service.update_policy(principal(request),body))

    return service
