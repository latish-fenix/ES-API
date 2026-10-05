"""Undo any past change: /api/v1/clusters/{clusterId}/config-history/... and
/api/v1/clusters/{clusterId}/deleted-indices/_recreate.

Registered before routes_config, whose /clusters/{id}/{type}/{name} paths are generic."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Header, Query, Request
from pydantic import BaseModel, Field

from .identity import User, current_user
from .routes_config import gate, meta, svc

router = APIRouter(prefix="/api/v1/clusters/{cluster_id}", tags=["history"])


class ConfigRestoreBody(BaseModel):
    configType: str = Field(..., description="From the audit entry, e.g. cluster-settings, ilm-policies")
    resource: str = Field(..., description="From the audit entry: _cluster, an index or a name")
    reason: str | None = Field(None, max_length=1000, description="Required unless dryRun")

    model_config = {"json_schema_extra": {"examples": [{
        "configType": "ilm-policies", "resource": "delest-logs-policy",
        "reason": "Undo the retention change from Monday"}]}}


class RecreateBody(BaseModel):
    key: str = Field(..., description="The deleted index's `key` from GET …/deleted-indices")
    reason: str | None = Field(None, max_length=1000, description="Required unless dryRun")


@router.get("/config-history", summary="Every applied change to one config resource, newest first")
def config_history(cluster_id: str, request: Request,
                   configType: str = Query(..., description="e.g. cluster-settings, index-settings, ilm-policies"),
                   resource: str = Query(..., description="_cluster for cluster settings, else the index or name"),
                   user: User = Depends(current_user)):
    return svc(request).change_history(user, cluster_id, configType, resource)


@router.post("/config-history/{change_id}/_restore",
             summary="Put a config back to the state from just before a past change")
def restore_config(cluster_id: str, change_id: str, request: Request,
                   body: ConfigRestoreBody = Body(...),
                   dryRun: bool = Query(False, description="Show the diff; change nothing"),
                   force: bool = Query(False, description="Also when cluster health is red"),
                   if_match: str | None = Header(None, alias="If-Match"),
                   user: User = Depends(current_user)):
    return gate(request, user, "config.restore", {"clusterId": cluster_id, "changeId": change_id,
                "force": force, "ifMatch": if_match}, body.model_dump(exclude_none=True), dryRun)


@router.post("/deleted-indices/_recreate",
             summary="Recreate a deleted index, empty, from its saved settings, mappings and aliases")
def recreate_index(cluster_id: str, request: Request, body: RecreateBody = Body(...),
                   dryRun: bool = Query(False), user: User = Depends(current_user)):
    return gate(request, user, "index.recreate", {"clusterId": cluster_id}, body.model_dump(exclude_none=True),
                dryRun)


# ------------------------------------------------------------------ create an index
from .index_create import CreateIndexBody, UndoCreateBody  # noqa: E402


def _create(request: Request):
    return request.app.state.index_create


@router.get("/indices/{index}/_create-preview",
            summary="What the matching index templates would give a new index of this name")
def create_preview(cluster_id: str, index: str, request: Request, user: User = Depends(current_user)):
    return _create(request).preview(user, cluster_id, index)


@router.post("/indices/{index}", status_code=200,
             summary="Create an index (Edit on the cluster; dry run shows the combined result)")
def create_index(cluster_id: str, index: str, request: Request,
                 body: CreateIndexBody = Body(default_factory=CreateIndexBody),
                 dryRun: bool = Query(False, description="Check and show the result; create nothing"),
                 user: User = Depends(current_user)):
    return gate(request, user, "index.create", {"clusterId": cluster_id, "index": index},
                body.model_dump(exclude_none=True), dryRun)


@router.get("/indices/{index}/_created",
            summary="Whether this index was created in the console, and whether that can still be undone")
def index_created(cluster_id: str, index: str, request: Request, user: User = Depends(current_user)):
    return _create(request).created(user, cluster_id, index)


@router.post("/indices/{index}/_undo-create",
             summary="Undo a create: deletes the index, only while it holds no documents")
def undo_create(cluster_id: str, index: str, request: Request,
                body: UndoCreateBody = Body(default_factory=UndoCreateBody),
                dryRun: bool = Query(False), user: User = Depends(current_user)):
    return gate(request, user, "index.undo_create", {"clusterId": cluster_id, "index": index},
                body.model_dump(exclude_none=True), dryRun)
