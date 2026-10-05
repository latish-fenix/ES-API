"""Shell: /api/v1/clusters/{clusterId}/shell (see shell.py)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from .identity import User, current_user, require_any_access
from .shell import ShellRequest

router = APIRouter(prefix="/api/v1/clusters/{cluster_id}/shell", tags=["shell"])


@router.post("", summary="Run one Dev Tools-style request (reads at once; writes through the console's change flow)")
def run(cluster_id: str, body: ShellRequest, request: Request, response: Response, user: User = Depends(current_user)):
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.shell.run(request, user, cluster_id, body)


@router.get("/history", summary="Your last 50 shell requests on this cluster")
def history(cluster_id: str, request: Request, response: Response, user: User = Depends(current_user)):
    response.headers["Cache-Control"] = "no-store"
    require_any_access(user, cluster_id)
    return request.app.state.shell.history(user, cluster_id)


@router.post("/history/_clear", summary="Forget your shell history")
def clear(cluster_id: str, request: Request, user: User = Depends(current_user)):
    return request.app.state.shell.clear_history(user)
