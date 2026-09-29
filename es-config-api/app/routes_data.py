"""Data browser: /api/v1/clusters/{clusterId}/data/{index}/... (read-only, view access)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from .data_browser import ExportBody, SearchBody
from .identity import User, current_user
from .routes_config import meta

router = APIRouter(prefix="/api/v1/clusters/{cluster_id}/data", tags=["data"])

NO_STORE = {"Cache-Control": "no-store"}


def _svc(request: Request):
    return request.app.state.data


@router.get("/{index}/_fields", summary="Fields of an index or pattern (for columns and filters)")
def fields(cluster_id: str, index: str, request: Request, response: Response,
           user: User = Depends(current_user)):
    response.headers.update(NO_STORE)
    return _svc(request).fields(user, cluster_id, index)


@router.post("/{index}/_search", summary="Search documents (query string, filters, sort, paging)")
def search(cluster_id: str, index: str, body: SearchBody, request: Request, response: Response,
           user: User = Depends(current_user)):
    response.headers.update(NO_STORE)
    return _svc(request).search(user, meta(request), cluster_id, index, body)


@router.get("/{index}/_doc/{doc_id}", summary="One document by its concrete index and id")
def document(cluster_id: str, index: str, doc_id: str, request: Request, response: Response,
             user: User = Depends(current_user)):
    response.headers.update(NO_STORE)
    return _svc(request).document(user, meta(request), cluster_id, index, doc_id)


@router.post("/{index}/_export", summary="Download matching documents as CSV, JSON or NDJSON (max 10,000)")
def export(cluster_id: str, index: str, body: ExportBody, request: Request,
           user: User = Depends(current_user)):
    data, media, name, rows = _svc(request).export(user, meta(request), cluster_id, index, body)
    return Response(content=data, media_type=media, headers={
        **NO_STORE, "Content-Disposition": f'attachment; filename="{name}"', "X-Export-Rows": str(rows)})
