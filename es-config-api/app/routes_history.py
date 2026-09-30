"""Undo any past change: /api/v1/clusters/{clusterId}/config-history/... and
/api/v1/clusters/{clusterId}/deleted-indices/_recreate.

Registered before routes_config, whose /clusters/{id}/{type}/{name} paths are generic."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Header, Query, Request
from pydantic import BaseModel, Field

from .identity import User, current_user
from .routes_config import meta, svc

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
    return svc(request).restore(user, meta(request), cluster_id, body.configType, body.resource,
                                change_id, body.reason, dryRun, force, if_match)


@router.post("/deleted-indices/_recreate",
             summary="Recreate a deleted index, empty, from its saved settings, mappings and aliases")
def recreate_index(cluster_id: str, request: Request, body: RecreateBody = Body(...),
                   dryRun: bool = Query(False), user: User = Depends(current_user)):
    return request.app.state.index_delete.recreate(user, meta(request), cluster_id, body.key,
                                                   body.reason, dryRun)
