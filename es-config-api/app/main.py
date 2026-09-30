"""FastAPI entry point: `uvicorn app.main:create_app --factory`."""
from __future__ import annotations

import dataclasses
import logging
import os
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import routes_admin, routes_auth, routes_clusters, routes_config, routes_data, routes_history
from .auth import generate_password, hash_password, password_problems
from .clusters import ClusterRegistry, load_clusters
from .errors import ApiError
from .data_browser import DataBrowser
from .data_edit import DataEditor
from .index_delete import IndexDeleteService
from .repos import AllowlistRepo, AuditRepo, LockRepo, SnapshotRepo, UsersRepo
from .secret_store import SecretStore, build_secret_store, resolve_app_secrets
from .service import ChangeService
from .settings import Settings
from .storage import ObjectStore, build_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def create_app(settings: Settings | None = None, store: ObjectStore | None = None,
               registry: ClusterRegistry | None = None, secrets: SecretStore | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if store is None:
        settings.validate()
        store = build_store(settings)
    secrets = secrets or build_secret_store(settings)
    # Several API workers start at the same moment. On a fresh install each would otherwise
    # generate its own session key and first-admin password; one lock in S3 lets the first
    # worker create them and the others read them.
    locks = LockRepo(store, STARTUP_LOCK_SECONDS)
    token = _acquire_startup_lock(locks)
    try:
        return _create_app(settings, store, registry, secrets)
    finally:
        locks.release(*STARTUP_LOCK, token)


STARTUP_LOCK = ("_app", "startup", "init")
STARTUP_LOCK_SECONDS = 120


def _acquire_startup_lock(locks: LockRepo, wait_seconds: float = 90) -> str:
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            return locks.acquire(*STARTUP_LOCK, f"api worker {os.getpid()}")
        except ApiError as e:
            if e.code != "CHANGE_IN_PROGRESS" or time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def _create_app(settings: Settings, store: ObjectStore, registry: ClusterRegistry | None,
                secrets: SecretStore) -> FastAPI:
    bootstrap_pw = settings.bootstrap_admin_password
    if settings.auth_mode == "password":
        session_secret, bootstrap_pw = resolve_app_secrets(
            secrets, settings.session_secret, settings.bootstrap_admin_password,
            need_bootstrap_password=bool(settings.bootstrap_admins))
        settings = dataclasses.replace(settings, session_secret=session_secret)
    if registry is None:
        registry = ClusterRegistry(load_clusters(settings.clusters_file, secrets),
                                   settings.managed_clusters_file or None, secrets)
    elif registry.secrets is None:
        registry.attach_secrets(secrets)

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
    app.state.secrets = secrets
    app.state.registry = registry
    app.state.users = UsersRepo(store, settings.bootstrap_admins, secrets)
    moved = app.state.users.migrate_hashes()
    if moved:
        log.warning("Moved %d password hash(es) from state/users.json into %s", len(moved),
                    "AWS Secrets Manager" if secrets.backend == "aws" else "the local secret store")
    app.state.allowlist = AllowlistRepo(store)
    app.state.audit = AuditRepo(store)
    locks = LockRepo(store, settings.lock_ttl_seconds)
    app.state.service = ChangeService(
        registry, SnapshotRepo(store), locks, app.state.allowlist, app.state.audit,
    )
    app.state.data = DataBrowser(registry, app.state.audit)
    app.state.data_edit = DataEditor(registry, store, app.state.audit, settings.session_secret)
    app.state.index_delete = IndexDeleteService(registry, store, locks, app.state.allowlist,
                                                app.state.audit)
    if settings.auth_mode == "password":
        _bootstrap_passwords(settings, app.state.users, bootstrap_pw, secrets)

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
    app.include_router(routes_history.router)
    app.include_router(routes_config.router)
    app.include_router(routes_admin.router)
    app.include_router(routes_clusters.router)
    _mount_ui(app)
    return app


log = logging.getLogger("es_config_api")


def _bootstrap_passwords(settings: Settings, users: UsersRepo, pw: str | None,
                         secrets: SecretStore) -> None:
    """Give each BOOTSTRAP_ADMINS user the first-admin password, once (if they have none yet).

    The password comes from the app secret (field bootstrapAdminPassword); if neither it nor
    BOOTSTRAP_ADMIN_PASSWORD is set, one is generated and stored there for an admin to read."""
    for name in settings.bootstrap_admins:
        rec = users.get_auth(name)
        if rec and rec.get("hasPassword"):
            continue
        if not pw:
            pw = generate_password()
            doc = dict(secrets.get("app") or {})
            doc["bootstrapAdminPassword"] = pw
            secrets.put("app", doc)
            log.warning("Generated the first-admin password; read it from %sapp "
                        "(field bootstrapAdminPassword) and change it after signing in",
                        secrets.full_name(""))
        problems = password_problems(pw, name)
        if problems:
            raise RuntimeError("BOOTSTRAP_ADMIN_PASSWORD " + "; ".join(problems))
        users.set_password(name, hash_password(pw), True, "bootstrap")
        log.warning("Set the first-admin password for bootstrap admin %s (sign in and change it)", name)


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
