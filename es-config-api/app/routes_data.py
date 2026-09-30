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


# ------------------------------------------------------------------ writes (data_edit.py)
from fastapi import Query  # noqa: E402

from .data_edit import BulkBody, DocCreate, DocRestore, DocWrite, RestoreBody  # noqa: E402


def _ed(request: Request):
    return request.app.state.data_edit


@router.get("/{index}/_recent", summary="Newest document and bulk changes on an index or pattern, for rolling back")
def recent_changes(cluster_id: str, index: str, request: Request, response: Response,
                   limit: int = Query(10, ge=1, le=50), user: User = Depends(current_user)):
    response.headers.update(NO_STORE)
    return _ed(request).recent(user, cluster_id, index, limit)


@router.get("/_changes", summary="Bulk changes on this cluster (newest first), with restore info")
def bulk_changes(cluster_id: str, request: Request, response: Response, user: User = Depends(current_user)):
    response.headers.update(NO_STORE)
    from .identity import require_any_access
    require_any_access(user, cluster_id)
    return _ed(request).changes(user, cluster_id)


@router.post("/_changes/{change_id}/_restore", summary="Put back every document a bulk change touched "
             "(dry run first: returns a dryRunToken)")
def bulk_restore(cluster_id: str, change_id: str, request: Request, response: Response,
                 body: RestoreBody = RestoreBody(), dryRun: bool = Query(False),
                 user: User = Depends(current_user)):
    response.headers.update(NO_STORE)
    return _ed(request).restore_bulk(user, meta(request), cluster_id, change_id, body, dryRun)


@router.post("/{index}/_doc", status_code=201, summary="Create a document (edit access; reason required)")
def create_doc(cluster_id: str, index: str, body: DocCreate, request: Request, response: Response,
               dryRun: bool = Query(False), user: User = Depends(current_user)):
    response.headers.update(NO_STORE)
    if dryRun:
        response.status_code = 200
    return _ed(request).create(user, meta(request), cluster_id, index, body, dryRun)


@router.put("/{index}/_doc/{doc_id}", summary="Replace a document (edit access; dry run shows the diff; "
            "the previous version is kept for undo)")
def update_doc(cluster_id: str, index: str, doc_id: str, body: DocWrite, request: Request,
               response: Response, dryRun: bool = Query(False), user: User = Depends(current_user)):
    response.headers.update(NO_STORE)
    return _ed(request).update(user, meta(request), cluster_id, index, doc_id, body, dryRun)


@router.delete("/{index}/_doc/{doc_id}", summary="Delete a document (edit access; confirm = the id; kept for undo)")
def delete_doc(cluster_id: str, index: str, doc_id: str, request: Request, response: Response,
               confirm: str | None = Query(None), reason: str | None = Query(None),
               dryRun: bool = Query(False), user: User = Depends(current_user)):
    response.headers.update(NO_STORE)
    return _ed(request).delete(user, meta(request), cluster_id, index, doc_id, confirm, reason, dryRun)


@router.get("/{index}/_doc/{doc_id}/_history", summary="Saved versions of a document (from edits made here)")
def doc_history(cluster_id: str, index: str, doc_id: str, request: Request, response: Response,
                user: User = Depends(current_user)):
    response.headers.update(NO_STORE)
    return _ed(request).history(user, cluster_id, index, doc_id)


@router.post("/{index}/_doc/{doc_id}/_restore", summary="Put a document back to a saved version")
def doc_restore(cluster_id: str, index: str, doc_id: str, body: DocRestore, request: Request,
                response: Response, dryRun: bool = Query(False), user: User = Depends(current_user)):
    response.headers.update(NO_STORE)
    return _ed(request).restore(user, meta(request), cluster_id, index, doc_id, body, dryRun)


@router.post("/{index}/_bulk_update", summary="Set / remove fields on every matching document "
             "(dry run is mandatory: it returns the count and a dryRunToken)")
def bulk_update(cluster_id: str, index: str, body: BulkBody, request: Request, response: Response,
                dryRun: bool = Query(False), user: User = Depends(current_user)):
    response.headers.update(NO_STORE)
    return _ed(request).bulk("update", user, meta(request), cluster_id, index, body, dryRun)


@router.post("/{index}/_bulk_delete", summary="Delete every matching document (delete access; dry run "
             "is mandatory; a backup is kept for restore)")
def bulk_delete(cluster_id: str, index: str, body: BulkBody, request: Request, response: Response,
                dryRun: bool = Query(False), user: User = Depends(current_user)):
    response.headers.update(NO_STORE)
    return _ed(request).bulk("delete", user, meta(request), cluster_id, index, body, dryRun)
