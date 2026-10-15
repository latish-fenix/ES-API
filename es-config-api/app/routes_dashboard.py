"""Dashboard: /api/v1/clusters/{id}/summary and /trends (anyone with access to the cluster)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from .identity import User, current_user

router = APIRouter(prefix="/api/v1", tags=["dashboard"])


@router.get("/clusters/{cluster_id}/summary",
            summary="One dashboard row: health, alerts, resources (average and worst node), shards, size")
def summary(cluster_id: str, request: Request, user: User = Depends(current_user)):
    return request.app.state.dashboard.summary(user, cluster_id)


@router.get("/clusters/{cluster_id}/trends",
            summary="Worst-node disk / RAM / heap / CPU and shards % over 24 hours or 7 days")
def trends(cluster_id: str, request: Request, hours: int = Query(24, description="24 or 168"),
           user: User = Depends(current_user)):
    return request.app.state.dashboard.trends(user, cluster_id, hours)
