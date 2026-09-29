"""FastAPI entry point: `uvicorn app.main:create_app --factory`."""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import routes_admin, routes_auth, routes_clusters, routes_config, routes_data
from .auth import hash_password, password_problems
from .clusters import ClusterRegistry, load_clusters
from .errors import ApiError
from .data_browser import DataBrowser
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
    registry = registry or ClusterRegistry(load_clusters(settings.clusters_file),
                                           settings.managed_clusters_file or None)

    app = FastAPI(
        title="ES Config API",
        version="1.0.0",
        description="Get, update, dry-run and roll back configuration on self-managed "
                    "Elasticsearch clusters. "
                    + ("Sign in with `POST /api/v1/auth/login`, then click **Authorize** and paste "
                       "the token." if settings.auth_mode == "password" else
                       f"Dev mode: identify yourself with the `{settings.user_header}` header.")
                    + " The web console is at [/ui/](/ui/).",
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
    app.state.data = DataBrowser(registry, app.state.audit)
    app.state.index_delete = IndexDeleteService(registry, store, locks, app.state.allowlist,
                                                app.state.audit)
    if settings.auth_mode == "password":
        _bootstrap_passwords(settings, app.state.users)

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith("/ui"):
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = UI_CSP
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

    app.include_router(routes_auth.router)
    app.include_router(routes_data.router)  # before routes_config: its /{type}/{name} paths are generic
    app.include_router(routes_config.router)
    app.include_router(routes_admin.router)
    app.include_router(routes_clusters.router)
    _mount_ui(app)
    return app


log = logging.getLogger("es_config_api")


def _bootstrap_passwords(settings: Settings, users: UsersRepo) -> None:
    """Give each BOOTSTRAP_ADMINS user BOOTSTRAP_ADMIN_PASSWORD, once (if they have none yet)."""
    for name in settings.bootstrap_admins:
        rec = users.get_auth(name)
        if rec and rec.get("passwordHash"):
            continue
        pw = settings.bootstrap_admin_password
        if not pw:
            raise RuntimeError(f"Bootstrap admin '{name}' has no password yet: set "
                               "BOOTSTRAP_ADMIN_PASSWORD in .env (used only until they change it)")
        problems = password_problems(pw, name)
        if problems:
            raise RuntimeError("BOOTSTRAP_ADMIN_PASSWORD " + "; ".join(problems))
        users.set_password(name, hash_password(pw), True, "bootstrap")
        log.warning("Set the initial password for bootstrap admin %s from BOOTSTRAP_ADMIN_PASSWORD", name)


UI_DIR = Path(__file__).parent / "static" / "ui"
# The UI is fully self-hosted (fonts included). The editor injects <style> tags, hence
# 'unsafe-inline' for styles only; scripts are same-origin files only.
UI_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
          "img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; "
          "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


def _mount_ui(app: FastAPI) -> None:
    """Serve the built web UI (ui/ -> app/static/ui) at /ui, with an SPA fallback."""
    index = UI_DIR / "index.html"

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/ui/" if index.exists() else "/docs")

    @app.get("/ui", include_in_schema=False)
    def ui_slash():
        return RedirectResponse("/ui/")

    @app.get("/ui/{path:path}", include_in_schema=False)
    def ui(path: str):
        if not index.exists():
            return JSONResponse(status_code=404, content={"error": {
                "code": "UI_NOT_BUILT", "message": "The web UI has not been built (see ui/README.md)"}})
        target = (UI_DIR / path).resolve()
        if path and UI_DIR.resolve() in target.parents and target.is_file():
            cache = ("public, max-age=31536000, immutable" if "/assets/" in f"/{path}"
                     else "no-cache")
            return FileResponse(target, headers={"Cache-Control": cache})
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
