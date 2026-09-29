"""Config API: /api/v1/clusters/..."""
from __future__ import annotations

from enum import Enum
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field

from .identity import User, current_user
from .service import CLUSTER_RESOURCE, ChangeService, RequestMeta

router = APIRouter(prefix="/api/v1", tags=["config"])


class NamedType(str, Enum):
    index_templates = "index-templates"
    component_templates = "component-templates"
    ilm_policies = "ilm-policies"
    ingest_pipelines = "ingest-pipelines"


class IndexPart(str, Enum):
    settings = "settings"
    mapping = "mapping"


INDEX_PART_TYPE = {IndexPart.settings: "index-settings", IndexPart.mapping: "index-mappings"}


class UpdateRequest(BaseModel):
    config: Any = Field(..., description="Partial settings map for settings types; full ES body "
                                         "for templates, ILM policies and pipelines; "
                                         "{properties: {...}} for mappings")
    reason: str | None = Field(None, description="Why the change is made. Required unless dryRun")
    sampleDocs: list[dict] | None = Field(None, description="Ingest-pipeline dry run only")

    model_config = {"json_schema_extra": {"examples": [{
        "config": {"cluster.routing.allocation.enable": "primaries"},
        "reason": "Pause shard moves during node patching"}]}}


class RollbackRequest(BaseModel):
    reason: str | None = None


def svc(request: Request) -> ChangeService:
    return request.app.state.service


def meta(request: Request) -> RequestMeta:
    fwd = request.headers.get("x-forwarded-for")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)
    return RequestMeta(request_id=request.state.request_id, source_ip=ip)


def _etag(response: Response, body: dict) -> dict:
    if body.get("version"):
        response.headers["ETag"] = f'"{body["version"]}"'
    return body


# ------------------------------------------------------------------ identity
@router.get("/me", summary="Who am I: username, admin flag, per-cluster access")
def me(request: Request, user: User = Depends(current_user)):
    registry = request.app.state.registry
    rec = request.app.state.users.get(user.username) or {}
    return {"username": user.username, "admin": user.admin,
            "authMode": request.app.state.settings.auth_mode,
            "usingGeneratedPassword": rec.get("usingGeneratedPassword", False),
            "lastLoginAt": rec.get("lastLoginAt"),
            "clusters": {c.id: user.level(c.id) for c in registry.all() if user.level(c.id)}}


# ------------------------------------------------------------------ clusters
@router.get("/clusters", summary="Clusters you can see")
def list_clusters(request: Request, user: User = Depends(current_user)):
    registry = request.app.state.registry
    items = []
    for c in registry.all():
        level = user.level(c.id)
        if level:
            items.append({**c.public(), "permission": level})
    return {"items": items}


@router.get("/clusters/{cluster_id}/health", summary="Cluster health")
def cluster_health(cluster_id: str, request: Request, user: User = Depends(current_user)):
    return svc(request).health(user, cluster_id)


# ----------------------------------------------------------- cluster settings
@router.get("/clusters/{cluster_id}/cluster-settings", summary="Get persistent cluster settings")
def get_cluster_settings(cluster_id: str, request: Request, response: Response,
                         user: User = Depends(current_user)):
    return _etag(response, svc(request).get(user, cluster_id, "cluster-settings", CLUSTER_RESOURCE))


@router.put("/clusters/{cluster_id}/cluster-settings", summary="Update cluster settings")
def put_cluster_settings(cluster_id: str, request: Request, body: UpdateRequest,
                         dryRun: bool = Query(False), force: bool = Query(False),
                         if_match: str | None = Header(None, alias="If-Match"),
                         user: User = Depends(current_user)):
    return svc(request).update(user, meta(request), cluster_id, "cluster-settings",
                               CLUSTER_RESOURCE, body.config, body.reason, body.sampleDocs,
                               dryRun, force, if_match)


@router.post("/clusters/{cluster_id}/cluster-settings/rollback", summary="Roll back cluster settings")
def rollback_cluster_settings(cluster_id: str, request: Request,
                              body: RollbackRequest = Body(default_factory=RollbackRequest),
                              dryRun: bool = Query(False), force: bool = Query(False),
                              if_match: str | None = Header(None, alias="If-Match"),
                              user: User = Depends(current_user)):
    return svc(request).rollback(user, meta(request), cluster_id, "cluster-settings",
                                 CLUSTER_RESOURCE, body.reason, dryRun, force, if_match)


@router.get("/clusters/{cluster_id}/cluster-settings/previous", summary="Stored snapshot")
def previous_cluster_settings(cluster_id: str, request: Request, user: User = Depends(current_user)):
    return svc(request).previous(user, cluster_id, "cluster-settings", CLUSTER_RESOURCE)


# -------------------------------------------------------------------- indices
@router.get("/clusters/{cluster_id}/indices", summary="List indices")
def list_indices(cluster_id: str, request: Request, user: User = Depends(current_user)):
    return svc(request).list_indices(user, cluster_id)


@router.get("/clusters/{cluster_id}/indices/{index}/{part}", summary="Get index settings or mapping")
def get_index_part(cluster_id: str, index: str, part: IndexPart, request: Request,
                   response: Response, user: User = Depends(current_user)):
    return _etag(response, svc(request).get(user, cluster_id, INDEX_PART_TYPE[part], index))


@router.put("/clusters/{cluster_id}/indices/{index}/{part}",
            summary="Update index settings (dynamic only) or add mapping fields")
def put_index_part(cluster_id: str, index: str, part: IndexPart, request: Request,
                   body: UpdateRequest, dryRun: bool = Query(False), force: bool = Query(False),
                   if_match: str | None = Header(None, alias="If-Match"),
                   user: User = Depends(current_user)):
    return svc(request).update(user, meta(request), cluster_id, INDEX_PART_TYPE[part], index,
                               body.config, body.reason, body.sampleDocs, dryRun, force, if_match)


@router.post("/clusters/{cluster_id}/indices/{index}/{part}/rollback",
             summary="Roll back index settings (mappings cannot be rolled back)")
def rollback_index_part(cluster_id: str, index: str, part: IndexPart, request: Request,
                        body: RollbackRequest = Body(default_factory=RollbackRequest),
                        dryRun: bool = Query(False), force: bool = Query(False),
                        if_match: str | None = Header(None, alias="If-Match"),
                        user: User = Depends(current_user)):
    return svc(request).rollback(user, meta(request), cluster_id, INDEX_PART_TYPE[part], index,
                                 body.reason, dryRun, force, if_match)


@router.get("/clusters/{cluster_id}/indices/{index}/{part}/previous", summary="Stored snapshot")
def previous_index_part(cluster_id: str, index: str, part: IndexPart, request: Request,
                        user: User = Depends(current_user)):
    return svc(request).previous(user, cluster_id, INDEX_PART_TYPE[part], index)


@router.delete("/clusters/{cluster_id}/indices/{index}",
               summary="Delete an index (permanent: documents cannot be recovered)")
def delete_index(cluster_id: str, index: str, request: Request,
                 confirm: str | None = Query(None, description="Repeat the index name exactly"),
                 reason: str | None = Query(None, description="Why; required unless dryRun"),
                 dryRun: bool = Query(False, description="Show what would be deleted"),
                 user: User = Depends(current_user)):
    return request.app.state.index_delete.delete(user, meta(request), cluster_id, index,
                                                 confirm, reason, dryRun)


@router.get("/clusters/{cluster_id}/deleted-indices",
            summary="Indices deleted through the API, with their saved settings and mappings")
def deleted_indices(cluster_id: str, request: Request, index: str | None = Query(None),
                    user: User = Depends(current_user)):
    return request.app.state.index_delete.list_tombstones(user, cluster_id, index)


# ------------------------------------------ templates, ILM policies, pipelines
@router.get("/clusters/{cluster_id}/{config_type}", summary="List resource names")
def list_named(cluster_id: str, config_type: NamedType, request: Request,
               user: User = Depends(current_user)):
    return svc(request).list_resources(user, cluster_id, config_type.value)


@router.get("/clusters/{cluster_id}/{config_type}/{name}", summary="Get one resource")
def get_named(cluster_id: str, config_type: NamedType, name: str, request: Request,
              response: Response, user: User = Depends(current_user)):
    return _etag(response, svc(request).get(user, cluster_id, config_type.value, name))


@router.put("/clusters/{cluster_id}/{config_type}/{name}", summary="Create or replace a resource")
def put_named(cluster_id: str, config_type: NamedType, name: str, request: Request,
              body: UpdateRequest, dryRun: bool = Query(False), force: bool = Query(False),
              if_match: str | None = Header(None, alias="If-Match"),
              user: User = Depends(current_user)):
    return svc(request).update(user, meta(request), cluster_id, config_type.value, name,
                               body.config, body.reason, body.sampleDocs, dryRun, force, if_match)


@router.post("/clusters/{cluster_id}/{config_type}/{name}/rollback", summary="Roll back a resource")
def rollback_named(cluster_id: str, config_type: NamedType, name: str, request: Request,
                   body: RollbackRequest = Body(default_factory=RollbackRequest),
                   dryRun: bool = Query(False), force: bool = Query(False),
                   if_match: str | None = Header(None, alias="If-Match"),
                   user: User = Depends(current_user)):
    return svc(request).rollback(user, meta(request), cluster_id, config_type.value, name,
                                 body.reason, dryRun, force, if_match)


@router.get("/clusters/{cluster_id}/{config_type}/{name}/previous", summary="Stored snapshot")
def previous_named(cluster_id: str, config_type: NamedType, name: str, request: Request,
                   user: User = Depends(current_user)):
    return svc(request).previous(user, cluster_id, config_type.value, name)
