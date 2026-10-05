"""Approval requests: /api/v1/approvals/... (see approvals.py)."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import BaseModel, Field

from .identity import User, current_user, require_admin
from .routes_config import meta

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])


class DecisionBody(BaseModel):
    comment: str | None = Field(None, max_length=1000, description="Why: required on approve and on reject")


def _svc(request: Request):
    return request.app.state.approvals


@router.get("", summary="Requests: mine (anyone), pending or all (admins)")
def list_requests(request: Request, scope: Literal["mine", "pending", "all"] = Query("mine"),
                  status: str | None = Query(None, description="PENDING, APPLIED, REJECTED, FAILED, CANCELLED, "
                                                               "EXPIRED, OUTDATED"),
                  clusterId: str | None = Query(None), requestedBy: str | None = Query(None),
                  limit: int = Query(200, ge=1, le=500), user: User = Depends(current_user)):
    return _svc(request).list(user, scope, status, clusterId, requestedBy, limit)


@router.get("/_count", summary="Pending counts for the top bar: waiting for you (admins), and your own")
def count(request: Request, user: User = Depends(current_user)):
    return _svc(request).count(user)


@router.get("/{request_id}", summary="One request with its full change and dry-run result")
def get_request(request_id: str, request: Request, user: User = Depends(current_user)):
    rec, _ = _svc(request).get(request_id)
    return _svc(request).view(rec, user)


@router.post("/{request_id}/_recheck", summary="Admins: dry run it again now, as the requester")
def recheck(request_id: str, request: Request, user: User = Depends(require_admin)):
    return _svc(request).recheck(request.app, user, meta(request), request_id)


@router.post("/{request_id}/_approve", summary="Admins: approve and apply (as the requester; comment required)")
def approve(request_id: str, request: Request, body: DecisionBody = Body(default_factory=DecisionBody),
            user: User = Depends(require_admin)):
    return _svc(request).approve(request.app, user, meta(request), request_id, body.comment)


@router.post("/{request_id}/_reject", summary="Admins: reject (comment required; the requester is emailed)")
def reject(request_id: str, request: Request, body: DecisionBody = Body(...),
           user: User = Depends(require_admin)):
    return _svc(request).reject(user, meta(request), request_id, body.comment)


@router.post("/{request_id}/_cancel", summary="The requester withdraws a pending request")
def cancel(request_id: str, request: Request, user: User = Depends(current_user)):
    return _svc(request).cancel(user, meta(request), request_id)
