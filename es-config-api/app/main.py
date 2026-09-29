"""FastAPI entry point: `uvicorn app.main:create_app --factory`."""
from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import routes_admin, routes_config
from .clusters import ClusterRegistry, load_clusters
from .errors import ApiError
from .index_delete import IndexDeleteService
from .repos import AllowlistRepo, AuditRepo, LockRepo, SnapshotRepo, UsersRepo
from .service import ChangeService
from .settings import Settings
from .storage import ObjectStore, build_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def create_app(settings: Settings | None = None, store: ObjectStore | None = None,
               registry: ClusterRegistry | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if store is None:
        settings.validate()
        store = build_store(settings)
    registry = registry or ClusterRegistry(load_clusters(settings.clusters_file))

    app = FastAPI(
        title="ES Config API",
        version="1.0.0",
        description="Get, update, dry-run and roll back configuration on self-managed "
                    "Elasticsearch clusters. Identify yourself with the "
                    f"`{settings.user_header}` header.",
    )
    app.state.settings = settings
    app.state.registry = registry
    app.state.users = UsersRepo(store, settings.bootstrap_admins)
    app.state.allowlist = AllowlistRepo(store)
    app.state.audit = AuditRepo(store)
    locks = LockRepo(store, settings.lock_ttl_seconds)
    app.state.service = ChangeService(
        registry, SnapshotRepo(store), locks, app.state.allowlist, app.state.audit,
    )
    app.state.index_delete = IndexDeleteService(registry, store, locks, app.state.allowlist,
                                                app.state.audit)

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=400, content={"error": {
            "code": "INVALID_REQUEST", "message": "Request body or parameters are invalid",
            "details": exc.errors()}})

    @app.get("/healthz", tags=["ops"], summary="Liveness probe")
    def healthz():
        return {"status": "ok", "clusters": len(registry.all())}

    app.include_router(routes_config.router)
    app.include_router(routes_admin.router)
    return app
