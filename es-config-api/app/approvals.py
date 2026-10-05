"""Approvals: a change made by a non-admin waits for an admin.

Flow:
  1. The user sends the change as usual (after its dry run). Instead of applying it, the API
     runs the dry run again as that user (same permission checks, same validation), saves
     the request with that result, emails every admin and answers 202 ``pendingApproval``.
  2. An admin approves or rejects it (Requests page, or the API). On approve the dry run is
     run once more, as the requester, and must give the same result (same diff, same
     version, same document count...). If the resource changed meanwhile the request is
     closed as OUTDATED and nothing is applied. Otherwise the change runs as the requester,
     with the usual locks, snapshots, backups and audit; each audit entry also carries
     ``approvalId`` and ``approvedBy``.
  3. The requester is emailed the outcome. Pending requests expire after APPROVAL_EXPIRY_DAYS.

Admins' own changes apply directly. Requests are kept in S3 under ``approvals/`` (they hold
the full change, e.g. document values, like doc-versions/ and bulk-backups/ do; never
secrets). The audit log gets APPROVAL_* events with names only.
"""
from __future__ import annotations

import contextvars
import logging
import secrets as pysecrets
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi.responses import JSONResponse

from .errors import ApiError, bad_request, conflict, forbidden, not_found
from .identity import User
from .mailer import Mailer, render
from .repos import _update_with_retry
from .storage import PreconditionFailed
from .util import canonical_json, iso, utcnow

log = logging.getLogger("es_config_api.approvals")

# Merged into every audit event written while an approved change runs (see AuditRepo.write).
APPROVAL_CONTEXT: contextvars.ContextVar[dict | None] = contextvars.ContextVar("approval", default=None)

PENDING_INDEX = "state/approvals-pending.json"
PREFIX = "approvals/"
BY_USER = "approvals-by-user/"
MAIL = "approvals-mail/"
APPLYING_STALE = timedelta(minutes=15)
OPEN = ("PENDING", "APPLYING")

TYPE_LABEL = {"cluster-settings": "cluster settings", "index-settings": "index settings",
              "index-mappings": "index mapping", "index-templates": "index template",
              "component-templates": "component template", "ilm-policies": "ILM policy",
              "ingest-pipelines": "ingest pipeline"}


# ------------------------------------------------------------------ operations
@dataclass
class Op:
    label: Callable[[dict, dict], str]
    run: Callable[[Any, User, Any, dict, dict, bool], dict]
    resource: Callable[[dict, dict, dict], str]
    fingerprint: tuple[str, ...]
    check: Callable[[dict, dict, dict], None] | None = None     # extra checks at submit
    prepare: Callable[[dict, dict, dict], None] | None = None   # before the approved real run
    kind: str = "config"


def _reason(p: dict, b: dict) -> str | None:
    r = b.get("reason") if b.get("reason") is not None else p.get("reason")
    return r.strip() if isinstance(r, str) and r.strip() else None


def _cfg_prepare(p, b, preview):
    if not p.get("ifMatch") and preview.get("versionBefore"):
        p["ifMatch"] = preview["versionBefore"]


def _bulk_check(p, b, preview):
    if not b.get("dryRunToken"):
        raise bad_request("DRY_RUN_REQUIRED", "Run this as a dry run first (dryRun=true) and send back its "
                          "dryRunToken, expectedCount and a reason")
    if b.get("expectedCount") != preview.get("count"):
        raise conflict("COUNT_CHANGED", f"The dry run you ran matched {b.get('expectedCount')} documents, now "
                       f"{preview.get('count')} match. Run the dry run again",
                       {"was": b.get("expectedCount"), "now": preview.get("count")})


def _bulk_prepare(p, b, preview):
    b["dryRunToken"] = preview.get("dryRunToken")
    b["expectedCount"] = preview.get("count")


def _confirm(field_: str, key: str):
    def check(p, b, preview):
        if p.get("confirm") != p.get(key):
            raise bad_request("CONFIRMATION_MISMATCH", f"Set 'confirm' to the {field_} to request this",
                              {"expected": p.get(key), "got": p.get("confirm")})
    return check


def _valid(p, b, preview):
    if preview.get("valid") is False:
        raise ApiError(400, "INVALID_CHANGE", "Elasticsearch rejects this change in the dry run; fix it first",
                       {"errors": preview.get("errors")})


def _svc(app):
    return app.state.service


def _ed(app):
    return app.state.data_edit


def _run_config_update(app, u, m, p, b, dry):
    return _svc(app).update(u, m, p["clusterId"], p["configType"], p["resource"], b.get("config"),
                            b.get("reason"), b.get("sampleDocs"), dry, bool(p.get("force")), p.get("ifMatch"))


def _run_config_rollback(app, u, m, p, b, dry):
    return _svc(app).rollback(u, m, p["clusterId"], p["configType"], p["resource"], b.get("reason"),
                              dry, bool(p.get("force")), p.get("ifMatch"))


def _run_config_restore(app, u, m, p, b, dry):
    return _svc(app).restore(u, m, p["clusterId"], b["configType"], b["resource"], p["changeId"],
                             b.get("reason"), dry, bool(p.get("force")), p.get("ifMatch"))


def _run_index_delete(app, u, m, p, b, dry):
    return app.state.index_delete.delete(u, m, p["clusterId"], p["index"], p.get("confirm"),
                                         p.get("reason"), dry)


def _run_index_recreate(app, u, m, p, b, dry):
    return app.state.index_delete.recreate(u, m, p["clusterId"], b["key"], b.get("reason"), dry)


def _run_index_create(app, u, m, p, b, dry):
    from .index_create import CreateIndexBody
    return app.state.index_create.create(u, m, p["clusterId"], p["index"], CreateIndexBody(**b), dry)


def _run_index_undo(app, u, m, p, b, dry):
    from .index_create import UndoCreateBody
    return app.state.index_create.undo(u, m, p["clusterId"], p["index"], UndoCreateBody(**b), dry)


def _run_doc_create(app, u, m, p, b, dry):
    from .data_edit import DocCreate
    return _ed(app).create(u, m, p["clusterId"], p["index"], DocCreate(**b), dry)


def _run_doc_update(app, u, m, p, b, dry):
    from .data_edit import DocWrite
    return _ed(app).update(u, m, p["clusterId"], p["index"], p["id"], DocWrite(**b), dry)


def _run_doc_delete(app, u, m, p, b, dry):
    return _ed(app).delete(u, m, p["clusterId"], p["index"], p["id"], p.get("confirm"), p.get("reason"), dry)


def _run_doc_restore(app, u, m, p, b, dry):
    from .data_edit import DocRestore
    return _ed(app).restore(u, m, p["clusterId"], p["index"], p["id"], DocRestore(**b), dry)


def _run_bulk(op):
    def run(app, u, m, p, b, dry):
        from .data_edit import BulkBody
        return _ed(app).bulk(op, u, m, p["clusterId"], p["index"], BulkBody(**b), dry)
    return run


def _run_bulk_restore(app, u, m, p, b, dry):
    from .data_edit import RestoreBody
    return _ed(app).restore_bulk(u, m, p["clusterId"], p["changeId"], RestoreBody(**b), dry)


def _cfg_res(p, b, prev):
    r = p.get("resource") or b.get("resource")
    return "cluster settings" if r == "_cluster" else r


def _tl(p, b):
    return TYPE_LABEL.get(p.get("configType") or b.get("configType"), p.get("configType") or b.get("configType") or "config")


_CFG_FP = ("diff", "versionBefore", "createsResource", "deletesResource")
OPS: dict[str, Op] = {
    "config.update": Op(lambda p, b: f"Change {_tl(p, b)}", _run_config_update, _cfg_res, _CFG_FP,
                        _valid, _cfg_prepare),
    "config.rollback": Op(lambda p, b: f"Roll back {_tl(p, b)}", _run_config_rollback, _cfg_res, _CFG_FP,
                          _valid, _cfg_prepare),
    "config.restore": Op(lambda p, b: f"Undo a past change to {_tl(p, b)}", _run_config_restore, _cfg_res,
                         _CFG_FP + ("restoreOf",), _valid, _cfg_prepare),
    "index.delete": Op(lambda p, b: "Delete index", _run_index_delete, lambda p, b, v: p["index"],
                       ("index", "createdAt", "dataStream", "aliases"), _confirm("index name", "index"),
                       kind="index"),
    "index.recreate": Op(lambda p, b: "Recreate deleted index", _run_index_recreate,
                         lambda p, b, v: v.get("index") or b.get("key", ""), ("index", "body"), kind="index"),
    "index.create": Op(lambda p, b: "Create index", _run_index_create, lambda p, b, v: p["index"],
                       ("index", "result", "template"), kind="index"),
    "index.undo_create": Op(lambda p, b: "Undo index creation", _run_index_undo, lambda p, b, v: p["index"],
                            ("index", "restoreOf", "docs"), kind="index"),
    "doc.create": Op(lambda p, b: "Add document", _run_doc_create,
                     lambda p, b, v: f"{v.get('index') or p['index']}/{b.get('id') or '(new id)'}",
                     ("index", "id", "diff"), kind="doc"),
    "doc.update": Op(lambda p, b: "Edit document", _run_doc_update, lambda p, b, v: f"{v.get('index') or p['index']}/{p['id']}",
                     ("index", "id", "diff", "seqNo", "primaryTerm"), kind="doc"),
    "doc.delete": Op(lambda p, b: "Delete document", _run_doc_delete, lambda p, b, v: f"{v.get('index') or p['index']}/{p['id']}",
                     ("index", "id", "source"), _confirm("document id", "id"), kind="doc"),
    "doc.restore": Op(lambda p, b: "Restore document version", _run_doc_restore,
                      lambda p, b, v: f"{v.get('index') or p['index']}/{p['id']}", ("index", "id", "plan", "diff"),
                      kind="doc"),
    "bulk.update": Op(lambda p, b: "Bulk update documents", _run_bulk("update"), lambda p, b, v: p["index"],
                      ("index", "count", "willChange", "indices"), _bulk_check, _bulk_prepare, kind="bulk"),
    "bulk.delete": Op(lambda p, b: "Bulk delete documents", _run_bulk("delete"), lambda p, b, v: p["index"],
                      ("index", "count", "willChange", "indices"), _bulk_check, _bulk_prepare, kind="bulk"),
    "bulk.restore": Op(lambda p, b: "Undo a bulk change", _run_bulk_restore,
                       lambda p, b, v: f"bulk change {p['changeId'][:8]}", ("changeId", "count", "op"),
                       _bulk_check, _bulk_prepare, kind="bulk"),
}


def _fingerprint(op: Op, preview: dict) -> str:
    return canonical_json({k: preview.get(k) for k in op.fingerprint})


def _paths(d: Any) -> list[str]:
    if not isinstance(d, dict):
        return []
    out = set()
    for part in d.values():
        if isinstance(part, list):
            out.update(i.get("path") for i in part if isinstance(i, dict) and i.get("path"))
    return sorted(out)


def _summary(op_name: str, p: dict, b: dict, preview: dict) -> dict:
    """Names and counts only: what lists, emails and the audit log show."""
    s: dict[str, Any] = {}
    fields = _paths(preview.get("diff"))
    if op_name.startswith("bulk.") and op_name != "bulk.restore":
        s["count"] = preview.get("willChange", preview.get("count"))
        if op_name == "bulk.update":
            fields = sorted(set(b.get("set") or {}) | set(b.get("remove") or []))
    elif op_name == "bulk.restore":
        s["count"] = preview.get("count")
    elif op_name == "index.delete":
        s["docs"] = preview.get("docsCount")
    elif op_name == "index.create":
        res = preview.get("result") or {}
        s["settings"] = sorted((res.get("settings") or {}).keys())[:30]
    if fields:
        s["fields"] = fields[:60]
        s["fieldCount"] = len(fields)
    return s


def _describe(summary: dict) -> str:
    bits = []
    if summary.get("count") is not None:
        bits.append(f"{summary['count']:,} document{'s' if summary['count'] != 1 else ''}")
    if summary.get("docs"):
        bits.append(f"{summary['docs']:,} documents will be deleted")
    if summary.get("fields"):
        f = summary["fields"]
        bits.append("fields: " + ", ".join(f[:12]) + (f" and {len(f) - 12} more" if len(f) > 12 else ""))
    return "; ".join(bits)


def _strip(preview: dict) -> dict:
    out = {k: v for k, v in preview.items() if k not in ("dryRunToken", "expiresInSeconds", "confirm", "changeId")}
    return out


def _new_request_id() -> str:
    return utcnow().strftime("%Y%m%dT%H%M%S") + "-" + pysecrets.token_hex(4)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ------------------------------------------------------------------ service
@dataclass
class ApprovalService:
    store: Any
    users: Any
    audit: Any
    mailer: Mailer
    settings: Any
    async_mail: bool = True
    _pool: ThreadPoolExecutor | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- policy
    def required(self, user: User) -> bool:
        return bool(getattr(self.settings, "approvals_required", True)) and not user.admin

    @staticmethod
    def key(rid: str) -> str:
        if not rid or "/" in rid or ".." in rid:
            raise not_found("REQUEST_NOT_FOUND", "No such request")
        return f"{PREFIX}{rid}.json"

    # -- submit
    def submit(self, app, user: User, meta, op_name: str, params: dict, body: dict) -> dict:
        op = OPS[op_name]
        reason = _reason(params, body)
        if not reason:
            raise bad_request("REASON_REQUIRED", "Give a 'reason' for the change (admins see it when they approve)")
        preview = op.run(app, user, meta, dict(params), dict(body), True)     # permission + validation
        if preview.get("noChange"):
            return {**preview, "applied": False, "pendingApproval": False}
        if op.check:
            op.check(params, body, preview)
        now = utcnow()
        rid = _new_request_id()
        p = {k: v for k, v in params.items() if v is not None}
        b = {k: v for k, v in body.items() if k not in ("dryRunToken",)}
        summary = _summary(op_name, p, b, preview)
        rec = {
            "id": rid, "status": "PENDING", "op": op_name, "kind": op.kind, "label": op.label(p, b),
            "clusterId": p.get("clusterId"), "resource": op.resource(p, b, preview), "reason": reason,
            "requestedBy": user.username, "requestedAt": iso(now),
            "expiresAt": iso(now + timedelta(days=int(getattr(self.settings, "approval_expiry_days", 7)))),
            "params": p, "body": b, "preview": _strip(preview), "fingerprint": _fingerprint(op, preview),
            "summary": summary, "events": [{"at": iso(now), "by": user.username, "event": "REQUESTED"}],
        }
        self.store.put_json(self.key(rid), rec, if_none_match=True)
        self.store.put_json(f"{BY_USER}{user.username}/{rid}.json", {"id": rid})
        self._index_add(rec)
        self._audit("APPROVAL_REQUESTED", user.username, meta, rec)
        self._notify_admins(rec)
        return self.view(rec, user)

    # -- read
    def get(self, rid: str) -> tuple[dict, str]:
        rec, etag = self.store.get_json(self.key(rid))
        if not rec:
            raise not_found("REQUEST_NOT_FOUND", f"No request '{rid}'")
        return self._lazy(rec, etag)

    def _lazy(self, rec: dict, etag: str) -> tuple[dict, str]:
        """Expire / un-stick on read (no background jobs)."""
        now = utcnow()
        if rec["status"] == "PENDING" and _parse(rec["expiresAt"]) < now:
            rec2 = self._transition(rec, etag, "EXPIRED", "system", None, {})
            if rec2:
                self._audit("APPROVAL_EXPIRED", "system", None, rec2)
                self._notify_requester(rec2)
                return self.get(rec["id"])
        if rec["status"] == "APPLYING" and _parse(rec.get("decidedAt") or rec["requestedAt"]) + APPLYING_STALE < now:
            rec2 = self._transition(rec, etag, "FAILED", rec.get("decidedBy") or "system", rec.get("comment"),
                                    {"error": {"code": "INTERRUPTED", "message": "The API stopped while applying "
                                               "this change; check the audit log for what was done"}})
            if rec2:
                return self.get(rec["id"])
        return rec, etag

    def view(self, rec: dict, user: User, full: bool = True) -> dict:
        if not (user.admin or rec["requestedBy"] == user.username):
            raise forbidden("PERMISSION_DENIED", "Only admins and the requester can see this request")
        keys = ("id", "status", "op", "kind", "label", "clusterId", "resource", "reason", "requestedBy",
                "requestedAt", "expiresAt", "summary", "decidedBy", "decidedAt", "comment", "result", "error",
                "finishedAt")
        out = {k: rec.get(k) for k in keys if rec.get(k) is not None}
        out["canApprove"] = bool(user.admin and rec["status"] == "PENDING" and rec["requestedBy"] != user.username)
        out["canCancel"] = bool(rec["status"] == "PENDING" and rec["requestedBy"] == user.username)
        if full:
            out.update(params=rec.get("params"), body=rec.get("body"), preview=rec.get("preview"),
                       events=rec.get("events", []))
            for k in ("previewNow",):
                if rec.get(k) is not None:
                    out[k] = rec[k]
            if user.admin:
                mail, _ = self.store.get_json(f"{MAIL}{rec['id']}.json")
                if mail:
                    out["mail"] = mail
        return out

    def list(self, user: User, scope: str, status: str | None = None, cluster_id: str | None = None,
             requester: str | None = None, limit: int = 200) -> dict:
        if scope in ("pending", "all") and not user.admin:
            raise forbidden("ADMIN_REQUIRED", "Only admins see other people's requests")
        if scope == "pending":
            ids = sorted(self._index().keys(), reverse=True)
        elif scope == "mine":
            ids = sorted((k.rsplit("/", 1)[-1][:-5] for k in self.store.list_keys(f"{BY_USER}{user.username}/")),
                         reverse=True)
        else:
            ids = sorted((k[len(PREFIX):-5] for k in self.store.list_keys(PREFIX)), reverse=True)
        items = []
        for rid in ids:
            if len(items) >= limit:
                break
            try:
                rec, _ = self.get(rid)
            except ApiError:
                continue
            if scope == "pending" and rec["status"] != "PENDING":
                self._index_remove(rid)
                continue
            if status and rec["status"] != status:
                continue
            if cluster_id and rec.get("clusterId") != cluster_id:
                continue
            if requester and rec["requestedBy"] != requester:
                continue
            items.append(self.view(rec, user, full=False))
        return {"scope": scope, "items": items}

    def count(self, user: User) -> dict:
        idx = self._index()
        now = utcnow()
        live = {k: v for k, v in idx.items() if _parse(v["expiresAt"]) >= now}
        mine = sum(1 for v in live.values() if v["requestedBy"] == user.username)
        return {"pending": len([v for v in live.values() if v["requestedBy"] != user.username]) if user.admin else 0,
                "mine": mine}

    # -- decide
    def cancel(self, user: User, meta, rid: str) -> dict:
        rec, etag = self.get(rid)
        if rec["requestedBy"] != user.username:
            raise forbidden("PERMISSION_DENIED", "Only the requester can cancel a request")
        if rec["status"] != "PENDING":
            raise conflict("REQUEST_CLOSED", f"This request is already {rec['status'].lower()}")
        rec2 = self._transition(rec, etag, "CANCELLED", user.username, None, {})
        if not rec2:
            raise conflict("REQUEST_BUSY", "Someone acted on this request at the same time; reload it")
        self._audit("APPROVAL_CANCELLED", user.username, meta, rec2)
        return self.view(rec2, user)

    def reject(self, admin: User, meta, rid: str, comment: str | None) -> dict:
        if not (comment and comment.strip()):
            raise bad_request("COMMENT_REQUIRED", "Say why it is rejected (the requester gets it by email)")
        rec, etag = self.get(rid)
        self._can_decide(admin, rec)
        rec2 = self._transition(rec, etag, "REJECTED", admin.username, comment.strip(), {})
        if not rec2:
            raise conflict("REQUEST_BUSY", "Another admin acted on this request at the same time; reload it")
        self._audit("APPROVAL_REJECTED", admin.username, meta, rec2)
        self._notify_requester(rec2)
        return self.view(rec2, admin)

    def recheck(self, app, admin: User, meta, rid: str) -> dict:
        """Dry run now, as the requester: is it still the change that was asked for?"""
        rec, _ = self.get(rid)
        if not admin.admin:
            raise forbidden("ADMIN_REQUIRED", "Only admins can check requests")
        op = OPS[rec["op"]]
        requester = self._requester(rec)
        try:
            now = op.run(app, requester, meta, dict(rec["params"]), dict(rec["body"]), True)
        except ApiError as e:
            return {"id": rid, "upToDate": False, "error": e.to_dict()["error"]}
        return {"id": rid, "upToDate": _fingerprint(op, now) == rec["fingerprint"], "preview": _strip(now)}

    def approve(self, app, admin: User, meta, rid: str, comment: str | None) -> dict:
        rec, etag = self.get(rid)
        self._can_decide(admin, rec)
        op = OPS[rec["op"]]
        claimed = self._transition(rec, etag, "APPLYING", admin.username, (comment or "").strip() or None, {})
        if not claimed:
            raise conflict("REQUEST_BUSY", "Another admin acted on this request at the same time; reload it")
        rec, etag = self.store.get_json(self.key(rid))
        try:
            requester = self._requester(rec)
            params, body = dict(rec["params"]), dict(rec["body"])
            fresh = op.run(app, requester, meta, dict(params), dict(body), True)
            if _fingerprint(op, fresh) != rec["fingerprint"]:
                out = self._finish(rec, etag, "OUTDATED", {"error": {
                    "code": "RESOURCE_CHANGED", "message": "It changed since the request was made (or the "
                    "dry run now gives a different result), so nothing was applied. The requester can send "
                    "it again"}, "preview": rec["preview"], "previewNow": _strip(fresh)})
                self._audit("APPROVAL_OUTDATED", admin.username, meta, out)
                self._notify_requester(out)
                raise conflict("RESOURCE_CHANGED", out["error"]["message"],
                               {"request": self.view(out, admin, full=False)})
            if op.prepare:
                op.prepare(params, body, fresh)
            ctx = APPROVAL_CONTEXT.set({"approvalId": rid, "approvedBy": admin.username})
            try:
                result = op.run(app, requester, meta, params, body, False)
            finally:
                APPROVAL_CONTEXT.reset(ctx)
        except ApiError as e:
            if e.code == "RESOURCE_CHANGED":
                raise
            out = self._finish(rec, etag, "FAILED", {"error": e.to_dict()["error"]})
            self._audit("APPROVAL_FAILED", admin.username, meta, out)
            self._notify_requester(out)
            raise
        except Exception as e:  # unexpected: record it, never leave the request APPLYING
            log.exception("Applying approved request %s failed", rid)
            out = self._finish(rec, etag, "FAILED", {"error": {"code": "INTERNAL_ERROR", "message": str(e)[:300]}})
            self._audit("APPROVAL_FAILED", admin.username, meta, out)
            self._notify_requester(out)
            raise
        res = {k: result.get(k) for k in ("changeId", "applied", "noChange", "versionAfter", "index", "id",
                                          "succeeded", "conflicts", "failed", "backupKey", "tombstoneKey",
                                          "versionKey") if result.get(k) is not None}
        out = self._finish(rec, etag, "APPLIED", {"result": res})
        self._audit("APPROVAL_APPROVED", admin.username, meta, out, changeId=res.get("changeId"))
        self._notify_requester(out)
        return {**self.view(out, admin), "applyResult": result}

    # -- helpers
    def _can_decide(self, admin: User, rec: dict) -> None:
        if not admin.admin:
            raise forbidden("ADMIN_REQUIRED", "Only admins can approve or reject")
        if rec["requestedBy"] == admin.username:
            raise forbidden("SELF_APPROVAL", "You can't approve or reject your own request")
        if rec["status"] != "PENDING":
            raise conflict("REQUEST_CLOSED", f"This request is already {rec['status'].lower()}")

    def _requester(self, rec: dict) -> User:
        r = self.users.get_auth(rec["requestedBy"])
        if r is None:
            raise ApiError(409, "REQUESTER_GONE", f"User '{rec['requestedBy']}' no longer exists")
        return User(username=rec["requestedBy"], admin=bool(r.get("admin")), clusters=r.get("clusters", {}))

    def _transition(self, rec, etag, status, by, comment, extra) -> dict | None:
        new = {**rec, **extra, "status": status, "decidedBy": by, "decidedAt": iso()}
        if comment:
            new["comment"] = comment
        new["events"] = rec.get("events", []) + [{"at": iso(), "by": by, "event": status,
                                                   **({"comment": comment} if comment else {})}]
        try:
            self.store.put_json(self.key(rec["id"]), new, if_match=etag)
        except PreconditionFailed:
            return None
        if status not in OPEN:
            self._index_remove(rec["id"])
        return new

    def _finish(self, rec, etag, status, extra) -> dict:
        """APPLYING -> final. Only the admin who claimed the request gets here."""
        out: dict = {}

        def m(doc):
            doc.update({**extra, "status": status, "finishedAt": iso()})
            doc["events"] = doc.get("events", []) + [{"at": iso(), "by": rec.get("decidedBy"), "event": status}]
            out.update(doc)
        _update_with_retry(self.store, self.key(rec["id"]), m, {})
        self._index_remove(rec["id"])
        return out

    def _index(self) -> dict:
        d, _ = self.store.get_json(PENDING_INDEX)
        return (d or {}).get("items", {})

    def _index_add(self, rec: dict) -> None:
        def m(doc):
            doc["items"][rec["id"]] = {"requestedBy": rec["requestedBy"], "expiresAt": rec["expiresAt"],
                                       "clusterId": rec.get("clusterId")}
        _update_with_retry(self.store, PENDING_INDEX, m, {"items": {}})

    def _index_remove(self, rid: str) -> None:
        def m(doc):
            doc["items"].pop(rid, None)
        try:
            _update_with_retry(self.store, PENDING_INDEX, m, {"items": {}})
        except Exception:
            log.exception("could not update the pending-approvals index")

    def _audit(self, action: str, actor: str, meta, rec: dict, **extra) -> None:
        self.audit.write({"action": action, "actor": actor, "outcome": "SUCCESS",
                          "requestId": getattr(meta, "request_id", None), "sourceIp": getattr(meta, "source_ip", None),
                          "clusterId": rec.get("clusterId"), "configType": "approval", "resource": rec.get("resource"),
                          "approvalId": rec["id"], "approvalOp": rec["op"], "requestedBy": rec["requestedBy"],
                          "reason": rec.get("reason"), "comment": rec.get("comment"),
                          "summary": rec.get("summary"), **({"error": rec["error"]} if rec.get("error") else {}),
                          **extra})

    # -- email
    def _link(self, rid: str) -> str | None:
        base = (getattr(self.settings, "public_url", "") or "").rstrip("/")
        return f"{base}/ui/requests/{rid}" if base else None

    def _admins(self, exclude: str) -> list[str]:
        names = set(getattr(self.users, "bootstrap_admins", set()))
        for u in self.users.list():
            if u.get("admin"):
                names.add(u["username"])
        return sorted(n for n in names if "@" in n and n != exclude)

    def _send(self, rid: str, who: str, to: list[str], subject: str, text: str, html_body: str) -> None:
        def job():
            try:
                res = self.mailer.send(to, subject, text, html_body)
            except Exception as e:  # never fail the request because mail failed
                log.exception("mail for request %s failed", rid)
                res = {"sent": [], "failed": [{"to": ",".join(to), "error": str(e)[:300]}]}

            def m(doc):
                doc[who] = {"at": iso(), **res}
            try:
                _update_with_retry(self.store, f"{MAIL}{rid}.json", m, {})
            except Exception:
                log.exception("could not record mail result for %s", rid)
        if not self.mailer.enabled or not to:
            return
        if self.async_mail:
            with self._lock:
                if self._pool is None:
                    self._pool = ThreadPoolExecutor(2, thread_name_prefix="mail")
            self._pool.submit(job)
        else:
            job()

    def _notify_admins(self, rec: dict) -> None:
        to = self._admins(rec["requestedBy"])
        rows = [("Requested by", rec["requestedBy"]), ("Cluster", rec.get("clusterId") or ""),
                ("Change", rec["label"]), ("Resource", rec["resource"]), ("Reason", rec["reason"]),
                ("Details", _describe(rec["summary"])), ("Expires", rec["expiresAt"][:16].replace("T", " ") + " UTC")]
        intro = (f"{rec['requestedBy']} asked to {rec['label'][0].lower() + rec['label'][1:]} on "
                 f"{rec.get('clusterId')}. It runs only after an admin approves it.")
        text, body = render("Approval needed", rows, intro, self._link(rec["id"]), "Review the request")
        subject = f"Approval needed: {rec['label']} {rec['resource']} on {rec.get('clusterId')} ({rec['requestedBy']})"
        self._send(rec["id"], "admins", to, subject, text, body)

    def _notify_requester(self, rec: dict) -> None:
        if "@" not in rec["requestedBy"]:
            return
        st = rec["status"]
        title = {"APPLIED": "Approved and applied", "REJECTED": "Request rejected",
                 "FAILED": "Approved, but it failed", "EXPIRED": "Request expired",
                 "OUTDATED": "Not applied: it changed since you asked"}.get(st, st.title())
        intro = {"APPLIED": f"{rec.get('decidedBy')} approved your request and it was applied.",
                 "REJECTED": f"{rec.get('decidedBy')} rejected your request.",
                 "FAILED": f"{rec.get('decidedBy')} approved your request, but applying it failed. Nothing "
                           "was changed unless the details say so.",
                 "EXPIRED": "Nobody approved your request in time, so it was closed. Send it again if it is "
                            "still needed.",
                 "OUTDATED": "The resource changed after you made the request, so it was not applied. Run the "
                             "dry run again and send a new request."}.get(st, "")
        err = (rec.get("error") or {}).get("message")
        rows = [("Change", rec["label"]), ("Cluster", rec.get("clusterId") or ""), ("Resource", rec["resource"]),
                ("Your reason", rec["reason"]), ("Comment", rec.get("comment") or ""), ("Error", err or ""),
                ("Change id", (rec.get("result") or {}).get("changeId") or "")]
        text, body = render(title, rows, intro, self._link(rec["id"]))
        self._send(rec["id"], "requester", [rec["requestedBy"]],
                   f"{title}: {rec['label']} {rec['resource']} on {rec.get('clusterId')}", text, body)


# ------------------------------------------------------------------ route helper
def gate(request, user: User, op_name: str, params: dict, body: dict, dry_run: bool):
    """Run a write now (dry runs, admins, approvals off) or turn it into an approval request."""
    from .routes_config import meta
    svc: ApprovalService = request.app.state.approvals
    m = meta(request)
    if dry_run or not svc.required(user):
        return OPS[op_name].run(request.app, user, m, dict(params), dict(body), dry_run)
    out = svc.submit(request.app, user, m, op_name, params, body)
    if out.get("pendingApproval") is False:
        return out
    return JSONResponse(status_code=202, content={
        "applied": False, "dryRun": False, "pendingApproval": True, "approval": out,
        "message": "Sent to the admins for approval. It is applied when an admin approves it."})
