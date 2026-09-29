"""Admin API: /api/v1/admin/... (admin users only)."""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import BaseModel, Field

from .clusters import es_call
from .errors import ApiError, not_found
from .identity import User, require_admin
from .repos import validate_permissions

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class UserBody(BaseModel):
    admin: bool = False
    clusters: dict[str, str] = Field(default_factory=dict,
                                     description="{clusterId or '*': 'view' | 'edit' | 'delete'}")


def _audit(request: Request, admin: User, action: str, **fields: Any) -> None:
    fwd = request.headers.get("x-forwarded-for")
    request.app.state.audit.write({
        "action": action, "actor": admin.username, "outcome": "SUCCESS",
        "requestId": request.state.request_id,
        "sourceIp": fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None),
        **fields,
    })


# ------------------------------------------------------------------ clusters
@router.get("/clusters", summary="All registered clusters with reachability")
def admin_clusters(request: Request, check: bool = Query(True, description="Ping each cluster"),
                   admin: User = Depends(require_admin)):
    registry = request.app.state.registry
    items = []
    for c in registry.all():
        item = {**c.public(), "hosts": c.hosts, "authType": c.auth_type}
        if check:
            try:
                info = es_call(registry.client(c.id), "GET", "/")
                item.update(reachable=True, version=info.get("version", {}).get("number"),
                            clusterName=info.get("cluster_name"))
            except ApiError as e:
                item.update(reachable=False, error=e.message)
        items.append(item)
    return {"items": items}


# --------------------------------------------------------------------- users
@router.get("/users", summary="All users")
def list_users(request: Request, admin: User = Depends(require_admin)):
    return {"items": request.app.state.users.list()}


@router.get("/users/{username}", summary="One user")
def get_user(username: str, request: Request, admin: User = Depends(require_admin)):
    rec = request.app.state.users.get(username)
    if rec is None:
        raise not_found("USER_NOT_FOUND", f"Unknown user '{username}'")
    return rec


@router.put("/users/{username}", summary="Create or replace a user")
def put_user(username: str, body: UserBody, request: Request, admin: User = Depends(require_admin)):
    clusters = validate_permissions(body.clusters, request.app.state.registry.ids())
    change = request.app.state.users.put(username, body.admin, clusters, admin.username)
    _audit(request, admin, "ADMIN_USER_UPDATE", targetUser=username,
           before=change["before"], after={"admin": body.admin, "clusters": clusters})
    return change["after"]


@router.put("/users/{username}/permissions", summary="Set per-cluster permissions")
def put_permissions(username: str, request: Request,
                    body: dict[str, str] = Body(..., examples=[{"prod-us": "edit", "prod-eu": "view"}]),
                    admin: User = Depends(require_admin)):
    clusters = validate_permissions(body, request.app.state.registry.ids())
    change = request.app.state.users.set_permissions(username, clusters, admin.username)
    _audit(request, admin, "ADMIN_PERMISSIONS_UPDATE", targetUser=username, **change)
    return request.app.state.users.get(username)


@router.delete("/users/{username}", summary="Remove a user")
def delete_user(username: str, request: Request, admin: User = Depends(require_admin)):
    before = request.app.state.users.delete(username)
    _audit(request, admin, "ADMIN_USER_DELETE", targetUser=username, before=before)
    return {"deleted": username}


# ----------------------------------------------------------------- allowlist
@router.get("/allowlist", summary="Global allowlist")
def get_allowlist(request: Request, admin: User = Depends(require_admin)):
    doc = request.app.state.allowlist.get(None)
    return doc or {"rules": {}, "note": "No allowlist yet: every change is blocked"}


@router.put("/allowlist", summary="Replace the global allowlist")
def put_allowlist(request: Request, body: dict[str, Any] = Body(..., examples=[{
    "cluster-settings": {"allow": ["cluster.routing.allocation.*"],
                         "deny": ["cluster.routing.allocation.enable"]},
    "index-templates": {"allow": ["logs-*"]}}]), admin: User = Depends(require_admin)):
    change = request.app.state.allowlist.put(body, admin.username)
    _audit(request, admin, "ADMIN_ALLOWLIST_UPDATE", allowlist="global", **change)
    return {"rules": change["after"]}


@router.get("/allowlist/{cluster_id}", summary="Per-cluster allowlist override")
def get_cluster_allowlist(cluster_id: str, request: Request, admin: User = Depends(require_admin)):
    request.app.state.registry.get(cluster_id)
    rules, source = request.app.state.allowlist.effective(cluster_id)
    return {"clusterId": cluster_id, "effectiveSource": source, "rules": rules}


@router.put("/allowlist/{cluster_id}", summary="Set a per-cluster override (replaces global)")
def put_cluster_allowlist(cluster_id: str, request: Request, body: dict[str, Any] = Body(...),
                          admin: User = Depends(require_admin)):
    request.app.state.registry.get(cluster_id)
    change = request.app.state.allowlist.put(body, admin.username, cluster_id)
    _audit(request, admin, "ADMIN_ALLOWLIST_UPDATE", allowlist=cluster_id, clusterId=cluster_id,
           **change)
    return {"clusterId": cluster_id, "rules": change["after"]}


@router.delete("/allowlist/{cluster_id}", summary="Remove the override (global applies again)")
def delete_cluster_allowlist(cluster_id: str, request: Request, admin: User = Depends(require_admin)):
    request.app.state.registry.get(cluster_id)
    request.app.state.allowlist.delete(cluster_id)
    _audit(request, admin, "ADMIN_ALLOWLIST_DELETE", allowlist=cluster_id, clusterId=cluster_id)
    return {"clusterId": cluster_id, "deleted": True}


# --------------------------------------------------------------------- audit
@router.get("/audit", summary="Audit events for one day (UTC), newest first")
def audit(request: Request, day: date = Query(..., alias="date"),
          clusterId: str | None = None, user: str | None = None, action: str | None = None,
          limit: int = Query(200, ge=1, le=1000), admin: User = Depends(require_admin)):
    limit = min(limit, request.app.state.settings.audit_query_limit)
    items = request.app.state.audit.query(day, clusterId, user, action, limit)
    return {"date": day.isoformat(), "count": len(items), "items": items}
