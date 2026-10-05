"""Document writes from the data browser: create / edit / delete one document, restore a
saved version, and bulk update / delete by query.

Safety, in order:
* Index-level access: `edit` on the index for create / edit / delete / restore and bulk
  update; `delete` for bulk delete. A pattern must be fully covered (no silent skipping).
* Every write needs a reason and is audited (the field names that changed, never values).
* Every write first saves the document's previous state to S3, so it can be restored:
  ``doc-versions/<cluster>/<index>/<id>/<time>_<changeId>.json`` for single documents,
  ``bulk-backups/<cluster>/<time>_<changeId>.json`` for bulk changes.
* Single edits use optimistic concurrency (if_seq_no / if_primary_term): a document that
  changed since it was read is refused (DOCUMENT_CHANGED), never overwritten.
* Bulk changes need a dry run first: it returns the exact count, a sample of before/after
  and a signed token (15 minutes). The real run must send that token, the same request and
  the same count; if the matching documents changed in the meantime it is refused. Each
  document is then written with its own seq_no check, so a document changed in between
  is reported as a conflict instead of being overwritten. At most BULK_MAX documents.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, Field, field_validator

from .clusters import NDJSON_HEADERS
from .data_browser import SearchBody, _expr, _read, build_query, check_target, resolve_scope
from .errors import ApiError, bad_request, conflict, forbidden, not_found, unprocessable
from .identity import User
from .util import canonical_json, diff, diff_is_empty, iso, new_id, utcnow

BULK_MAX = 10_000
BULK_CHUNK = 500
SAMPLE = 5
TOKEN_TTL = 15 * 60
MAX_DOC_BYTES = 5 * 1024 * 1024


# ------------------------------------------------------------------ bodies
class DocWrite(BaseModel):
    document: dict = Field(..., description="The whole document (_source) as it should be saved")
    reason: str | None = Field(None, max_length=1000, description="Required unless dryRun")
    ifSeqNo: int | None = Field(None, description="_seq_no you read; refused if the document changed since")
    ifPrimaryTerm: int | None = None


class DocCreate(BaseModel):
    document: dict
    id: str | None = Field(None, max_length=512, description="Leave out to let Elasticsearch pick one")
    reason: str | None = Field(None, max_length=1000)


class DocRestore(BaseModel):
    versionKey: str = Field(..., description="A key from GET …/_history")
    reason: str | None = Field(None, max_length=1000)


class BulkBody(SearchBody):
    set: dict[str, Any] = Field(default_factory=dict,
                                description="Bulk update: dotted field -> new value, e.g. {\"status\": \"delivered\"}")
    remove: list[str] = Field(default_factory=list, max_length=100,
                              description="Bulk update: dotted fields to remove")
    reason: str | None = Field(None, max_length=1000)
    dryRunToken: str | None = Field(None, description="From the dry run of this exact request")
    expectedCount: int | None = Field(None, description="The count the dry run reported")
    dsl: dict | None = Field(None, description="Elasticsearch query DSL (the 'query' object) used instead of "
                                               "query/filters/timeRange; no scripts")

    @field_validator("dsl")
    @classmethod
    def _dsl(cls, v: dict | None) -> dict | None:
        if v is not None:
            from .util import script_paths
            if not v:
                raise ValueError("dsl must be a non-empty query object, e.g. {\"term\": {\"status\": \"new\"}}")
            if script_paths(v):
                raise ValueError(f"scripts are not allowed in bulk changes ({', '.join(script_paths(v)[:3])})")
            from .util import index_refs
            if index_refs(v):
                raise ValueError("queries that fetch documents from another index (terms lookup, more_like_this "
                                 f"documents…) are not allowed in bulk changes ({', '.join(index_refs(v)[:3])})")
        return v

    @field_validator("remove")
    @classmethod
    def _rm(cls, v: list[str]) -> list[str]:
        return [x.strip() for x in v if x.strip()]


class RestoreBody(BaseModel):
    reason: str | None = Field(None, max_length=1000)
    dryRunToken: str | None = None
    expectedCount: int | None = None


# ------------------------------------------------------------------ helpers
def _q(s: str) -> str:
    return quote(s, safe="")


def _doc_prefix(cluster_id: str, index: str, doc_id: str) -> str:
    return f"doc-versions/{cluster_id}/{index}/{_q(doc_id)}/"


def _stamp() -> str:
    return utcnow().strftime("%Y%m%dT%H%M%S%f")


FEED_PREFIX = "doc-changes"
FEED_SCAN = 2000   # newest feed entries looked at per request


def _feed_key(cluster_id: str, index: str, change_id: str) -> str:
    """Newest first in key order (inverted milliseconds); the index is in the key so a page
    can be filtered by index before any entry is read."""
    inv = 10 ** 14 - int(time.time() * 1000)
    return f"{FEED_PREFIX}/{cluster_id}/{inv:014d}_{change_id}_{_q(index)}.json"


def _change_of(version_key: str | None) -> str | None:
    """The change id in a doc-versions key (<stamp>_<changeId>.json)."""
    if not version_key:
        return None
    return version_key.rsplit("/", 1)[-1].removesuffix(".json").split("_")[-1] or None


def _check_field(path: str) -> str:
    p = path.strip()
    if not p or p.startswith("_") or ".." in p or p.startswith(".") or p.endswith(".") or len(p) > 512:
        raise bad_request("INVALID_FIELD", f"'{path}' is not a field path (e.g. order_info.status)")
    return p


def apply_changes(src: dict, set_: dict[str, Any], remove: list[str]) -> dict:
    """New _source with dotted fields set / removed. ValueError if a path goes through a
    non-object value (e.g. an array)."""
    out = json.loads(json.dumps(src))
    for path, value in set_.items():
        parts = path.split(".")
        cur = out
        for p in parts[:-1]:
            nxt = cur.get(p)
            if nxt is None:
                nxt = cur[p] = {}
            if not isinstance(nxt, dict):
                raise ValueError(f"'{path}': '{p}' is not an object in this document")
            cur = nxt
        cur[parts[-1]] = value
    for path in remove:
        parts = path.split(".")
        cur = out
        for p in parts[:-1]:
            cur = cur.get(p) if isinstance(cur, dict) else None
            if not isinstance(cur, dict):
                break
        else:
            if isinstance(cur, dict):
                cur.pop(parts[-1], None)
    return out


def changed_fields(d: dict) -> list[str]:
    return sorted({x["path"] for k in ("added", "removed", "changed") for x in d[k]})


def _size_ok(doc: dict) -> None:
    if len(canonical_json(doc).encode()) > MAX_DOC_BYTES:
        raise bad_request("DOCUMENT_TOO_LARGE", "Documents over 5 MB can't be edited here")


def _need_reason(reason: str | None) -> str:
    if not (reason and reason.strip()):
        raise bad_request("REASON_REQUIRED", "Give a 'reason' for the change (it is audited)")
    return reason.strip()


# ------------------------------------------------------------------ service
class DataEditor:
    def __init__(self, registry, store, audit, token_key: str):
        self.registry = registry
        self.store = store
        self.audit = audit
        self.key = (token_key or "es-config-api-dev-token-key").encode()

    # -- audit -------------------------------------------------------------
    def _audit(self, action: str, user: User, meta, cluster_id: str, index: str, outcome: str,
               dry_run: bool = False, **extra) -> None:
        self.audit.write({"action": "DRY_RUN" if dry_run else action, "requestedAction": action,
                          "actor": user.username, "outcome": outcome, "requestId": meta.request_id,
                          "sourceIp": meta.source_ip, "clusterId": cluster_id, "configType": "data",
                          "resource": index, **extra})

    def _guard(self, action, user, meta, cluster_id, index, dry_run, fn, **ctx):
        try:
            return fn()
        except ApiError as e:
            self._audit(action, user, meta, cluster_id, index,
                        "REJECTED" if e.status < 500 and e.code != "ES_REJECTED" else "FAILED",
                        dry_run, error={"status": e.status, "code": e.code, "message": e.message}, **ctx)
            raise

    # -- single documents ----------------------------------------------------
    def _concrete(self, es, user: User, cluster_id: str, index: str, needed: str,
                  allow_data_stream: bool = False) -> str:
        index = check_target(index)
        if "*" in index or "?" in index:
            raise bad_request("INVALID_INDEX_NAME", "Use one concrete index name, not a pattern")
        scope = resolve_scope(es, user, cluster_id, index, needed, require_all=True)
        perms = set(scope.names.values())
        if set(scope.names) != {index} and not (allow_data_stream and perms == {index}):
            raise unprocessable("NOT_A_CONCRETE_INDEX",
                                f"'{index}' is an alias or data stream; use the document's own index")
        return index

    def _current(self, es, index: str, doc_id: str) -> dict | None:
        try:
            r = _read(es, "GET", f"/{index}/_doc/{_q(doc_id)}")
        except ApiError as e:
            if e.code == "ES_NOT_FOUND":
                return None
            raise
        return r if r.get("found") else None

    def _save_version(self, cluster_id: str, index: str, doc_id: str, action: str, current: dict | None,
                      user: User, reason: str, change_id: str, fields: list[str]) -> str:
        key = f"{_doc_prefix(cluster_id, index, doc_id)}{_stamp()}_{change_id}.json"
        self.store.put_json(key, {
            "clusterId": cluster_id, "index": index, "id": doc_id, "action": action,
            "changeId": change_id, "at": iso(), "by": user.username, "reason": reason,
            "changedFields": fields, "applied": False,
            "before": {"exists": current is not None,
                       "source": (current or {}).get("_source"),
                       "seqNo": (current or {}).get("_seq_no"),
                       "primaryTerm": (current or {}).get("_primary_term")}})
        return key

    def _mark_applied(self, key: str) -> None:
        v, etag = self.store.get_json(key)
        if v:
            v["applied"] = True
            self.store.put_json(key, v)

    def _feed(self, cluster_id: str, indices: list[str], entry: dict) -> None:
        """Recent-changes feed for the Data page (one entry per index touched). Best effort:
        the change is done; a missing feed entry only hides it from that list."""
        for idx in indices[:200]:
            try:
                self.store.put_json(_feed_key(cluster_id, idx, entry["changeId"]),
                                    {**entry, "index": idx, "clusterId": cluster_id})
            except Exception:
                pass

    def recent(self, user: User, cluster_id: str, target: str, limit: int = 10) -> dict:
        """Newest single-document and bulk changes on the indices `target` covers that the user
        may see, each with what's needed to roll it back and whether the user may."""
        es = self.registry.client(cluster_id)
        scope = resolve_scope(es, user, cluster_id, target, "view")
        allowed = {_q(i) for i in scope.permitted}
        items: list[dict] = []
        seen: set[str] = set()
        for key in self.store.list_keys(f"{FEED_PREFIX}/{cluster_id}/", limit=FEED_SCAN):
            name = key.rsplit("/", 1)[-1].removesuffix(".json")
            parts = name.split("_", 2)
            if len(parts) != 3 or parts[2] not in allowed or parts[1] in seen:
                continue
            e, _ = self.store.get_json(key)
            if not e:
                continue
            seen.add(parts[1])
            items.append(e)
            if len(items) >= limit:
                break
        undone = {e.get("restoreOf") for e in items if e.get("restoreOf")}
        out = []
        for e in items:
            kind = e.get("kind")
            need = "edit"
            idx_list = e.get("indices") or [e["index"]]
            out.append({**{k: e.get(k) for k in ("kind", "changeId", "action", "index", "id", "at", "by",
                                                  "reason", "fields", "count", "versionKey", "restoreOf", "op")},
                        "rolledBack": e["changeId"] in undone,
                        "canRollBack": all(user.can_index(cluster_id, scope.names.get(i, i), need) for i in idx_list)
                                       and (kind != "doc" or bool(e.get("versionKey")))})
        return {"clusterId": cluster_id, "target": target, "items": out}

    @staticmethod
    def _write_error(e: ApiError, doc_id: str) -> ApiError:
        status = (e.details or {}).get("esStatus") if isinstance(e.details, dict) else None
        if status == 409:
            return conflict("DOCUMENT_CHANGED", f"Document '{doc_id}' was changed (or created) by someone "
                            "else since you read it. Reload it and try again")
        return e

    def update(self, user, meta, cluster_id, index, doc_id, body: DocWrite, dry_run: bool) -> dict:
        def run():
            es = self.registry.client(cluster_id)
            idx = self._concrete(es, user, cluster_id, index, "edit")
            _size_ok(body.document)
            cur = self._current(es, idx, doc_id)
            if cur is None:
                raise not_found("DOCUMENT_NOT_FOUND", f"No document '{doc_id}' in '{idx}'")
            if body.ifSeqNo is not None and (cur["_seq_no"], cur["_primary_term"]) != (body.ifSeqNo, body.ifPrimaryTerm):
                raise conflict("DOCUMENT_CHANGED", f"Document '{doc_id}' changed since you read it. "
                               "Reload it and try again")
            d = diff(cur["_source"], body.document)
            fields = changed_fields(d)
            out = {"index": idx, "id": doc_id, "diff": d, "noChange": diff_is_empty(d), "dryRun": dry_run,
                   "seqNo": cur["_seq_no"], "primaryTerm": cur["_primary_term"]}
            if dry_run or out["noChange"]:
                self._audit("DATA_DOC_UPDATE", user, meta, cluster_id, idx, "SUCCESS" if dry_run else "NO_CHANGE",
                            dry_run, documentId=doc_id, changedFields=fields)
                return out
            reason = _need_reason(body.reason)
            change_id = new_id()
            vkey = self._save_version(cluster_id, idx, doc_id, "UPDATE", cur, user, reason, change_id, fields)
            try:
                r = _read(es, "PUT", f"/{idx}/_doc/{_q(doc_id)}", body=body.document,
                          params={"if_seq_no": cur["_seq_no"], "if_primary_term": cur["_primary_term"],
                                  "refresh": "wait_for"})
            except ApiError as e:
                raise self._write_error(e, doc_id) from e
            self._mark_applied(vkey)
            self._audit("DATA_DOC_UPDATE", user, meta, cluster_id, idx, "SUCCESS", documentId=doc_id,
                        changeId=change_id, reason=reason, changedFields=fields, versionKey=vkey)
            self._feed(cluster_id, [idx], {"kind": "doc", "changeId": change_id, "action": "UPDATE", "id": doc_id,
                                           "at": iso(), "by": user.username, "reason": reason,
                                           "fields": fields, "versionKey": vkey})
            return {**out, "applied": True, "changeId": change_id, "versionKey": vkey,
                    "seqNo": r.get("_seq_no"), "primaryTerm": r.get("_primary_term")}
        return self._guard("DATA_DOC_UPDATE", user, meta, cluster_id, index, dry_run, run, documentId=doc_id)

    def create(self, user, meta, cluster_id, index, body: DocCreate, dry_run: bool) -> dict:
        def run():
            es = self.registry.client(cluster_id)
            idx = self._concrete(es, user, cluster_id, index, "edit", allow_data_stream=True)
            _size_ok(body.document)
            if body.id is not None and not body.id.strip():
                raise bad_request("INVALID_ID", "Leave 'id' out, or give a non-empty id")
            fields = changed_fields(diff({}, body.document))
            if body.id and self._current(es, idx, body.id) is not None:
                raise conflict("DOCUMENT_EXISTS", f"A document with id '{body.id}' already exists in '{idx}'")
            if dry_run:
                self._audit("DATA_DOC_CREATE", user, meta, cluster_id, idx, "SUCCESS", True,
                            documentId=body.id, changedFields=fields)
                return {"index": idx, "id": body.id, "dryRun": True, "diff": diff({}, body.document)}
            reason = _need_reason(body.reason)
            try:
                if body.id:
                    r = _read(es, "PUT", f"/{idx}/_create/{_q(body.id)}", body=body.document,
                              params={"refresh": "wait_for"})
                else:
                    r = _read(es, "POST", f"/{idx}/_doc", body=body.document,
                              params={"op_type": "create", "refresh": "wait_for"})
            except ApiError as e:
                if isinstance(e.details, dict) and e.details.get("esStatus") == 409:
                    raise conflict("DOCUMENT_EXISTS", f"A document with id '{body.id}' already exists") from e
                raise
            doc_id, real_index = r["_id"], r.get("_index", idx)
            change_id = new_id()
            vkey = self._save_version(cluster_id, real_index, doc_id, "CREATE", None, user, reason, change_id, fields)
            self._mark_applied(vkey)
            self._audit("DATA_DOC_CREATE", user, meta, cluster_id, real_index, "SUCCESS", documentId=doc_id,
                        changeId=change_id, reason=reason, changedFields=fields, versionKey=vkey)
            self._feed(cluster_id, [real_index], {"kind": "doc", "changeId": change_id, "action": "CREATE",
                                                  "id": doc_id, "at": iso(), "by": user.username,
                                                  "reason": reason, "fields": fields, "versionKey": vkey})
            return {"index": real_index, "id": doc_id, "applied": True, "changeId": change_id,
                    "versionKey": vkey, "seqNo": r.get("_seq_no"), "primaryTerm": r.get("_primary_term")}
        return self._guard("DATA_DOC_CREATE", user, meta, cluster_id, index, dry_run, run, documentId=body.id)

    def delete(self, user, meta, cluster_id, index, doc_id, confirm: str | None, reason: str | None,
               dry_run: bool) -> dict:
        def run():
            es = self.registry.client(cluster_id)
            idx = self._concrete(es, user, cluster_id, index, "edit")
            cur = self._current(es, idx, doc_id)
            if cur is None:
                raise not_found("DOCUMENT_NOT_FOUND", f"No document '{doc_id}' in '{idx}'")
            fields = sorted((cur.get("_source") or {}).keys())
            if dry_run:
                self._audit("DATA_DOC_DELETE", user, meta, cluster_id, idx, "SUCCESS", True, documentId=doc_id)
                return {"index": idx, "id": doc_id, "dryRun": True, "confirmRequired": doc_id,
                        "source": cur["_source"], "note": "A copy is kept so the delete can be undone"}
            if confirm != doc_id:
                raise bad_request("CONFIRMATION_MISMATCH", "Set 'confirm' to the document id to delete it",
                                  {"expected": doc_id, "got": confirm})
            why = _need_reason(reason)
            change_id = new_id()
            vkey = self._save_version(cluster_id, idx, doc_id, "DELETE", cur, user, why, change_id, fields)
            try:
                _read(es, "DELETE", f"/{idx}/_doc/{_q(doc_id)}",
                      params={"if_seq_no": cur["_seq_no"], "if_primary_term": cur["_primary_term"],
                              "refresh": "wait_for"})
            except ApiError as e:
                raise self._write_error(e, doc_id) from e
            self._mark_applied(vkey)
            self._audit("DATA_DOC_DELETE", user, meta, cluster_id, idx, "SUCCESS", documentId=doc_id,
                        changeId=change_id, reason=why, versionKey=vkey)
            self._feed(cluster_id, [idx], {"kind": "doc", "changeId": change_id, "action": "DELETE", "id": doc_id,
                                           "at": iso(), "by": user.username, "reason": why, "versionKey": vkey})
            return {"index": idx, "id": doc_id, "applied": True, "deleted": True, "changeId": change_id,
                    "versionKey": vkey}
        return self._guard("DATA_DOC_DELETE", user, meta, cluster_id, index, dry_run, run, documentId=doc_id)

    def history(self, user, cluster_id, index, doc_id) -> dict:
        es = self.registry.client(cluster_id)
        idx = self._concrete(es, user, cluster_id, index, "view")
        items = []
        for key in reversed(self.store.list_keys(_doc_prefix(cluster_id, idx, doc_id))):
            v, _ = self.store.get_json(key)
            if v and v.get("applied", True):
                items.append({"key": key, "action": v["action"], "at": v["at"], "by": v["by"],
                              "reason": v.get("reason"), "changedFields": v.get("changedFields", []),
                              "changeId": v.get("changeId"), "before": v["before"]})
        return {"index": idx, "id": doc_id, "items": items}

    def restore(self, user, meta, cluster_id, index, doc_id, body: DocRestore, dry_run: bool) -> dict:
        def run():
            es = self.registry.client(cluster_id)
            idx = self._concrete(es, user, cluster_id, index, "edit")
            if not body.versionKey.startswith(_doc_prefix(cluster_id, idx, doc_id)):
                raise bad_request("INVALID_VERSION", "That version belongs to another document")
            v, _ = self.store.get_json(body.versionKey)
            if not v:
                raise not_found("VERSION_NOT_FOUND", "No such saved version")
            target = v["before"]
            cur = self._current(es, idx, doc_id)
            d = diff((cur or {}).get("_source") or {}, target.get("source") or {}) if target["exists"] else \
                diff((cur or {}).get("_source") or {}, {})
            plan = ("recreate" if target["exists"] and cur is None else
                    "overwrite" if target["exists"] else "delete" if cur is not None else "nothing")
            out = {"index": idx, "id": doc_id, "dryRun": dry_run, "plan": plan, "diff": d,
                   "restoresTo": {"at": v["at"], "by": v["by"], "action": v["action"]}}
            if dry_run or plan == "nothing":
                self._audit("DATA_DOC_RESTORE", user, meta, cluster_id, idx, "SUCCESS" if dry_run else "NO_CHANGE",
                            dry_run, documentId=doc_id, versionKey=body.versionKey)
                return out
            why = _need_reason(body.reason)
            change_id = new_id()
            vkey = self._save_version(cluster_id, idx, doc_id, "RESTORE", cur, user, why, change_id, changed_fields(d))
            params = {"refresh": "wait_for"}
            if cur is not None:
                params.update(if_seq_no=cur["_seq_no"], if_primary_term=cur["_primary_term"])
            try:
                if plan == "delete":
                    _read(es, "DELETE", f"/{idx}/_doc/{_q(doc_id)}", params=params)
                elif plan == "recreate":
                    _read(es, "PUT", f"/{idx}/_create/{_q(doc_id)}", body=target["source"], params={"refresh": "wait_for"})
                else:
                    _read(es, "PUT", f"/{idx}/_doc/{_q(doc_id)}", body=target["source"], params=params)
            except ApiError as e:
                raise self._write_error(e, doc_id) from e
            self._mark_applied(vkey)
            self._audit("DATA_DOC_RESTORE", user, meta, cluster_id, idx, "SUCCESS", documentId=doc_id,
                        changeId=change_id, reason=why, restoredFrom=body.versionKey, versionKey=vkey,
                        changedFields=changed_fields(d), restoreOf=_change_of(body.versionKey))
            self._feed(cluster_id, [idx], {"kind": "doc", "changeId": change_id, "action": "RESTORE", "id": doc_id,
                                           "at": iso(), "by": user.username, "reason": why,
                                           "fields": changed_fields(d), "versionKey": vkey,
                                           "restoreOf": _change_of(body.versionKey)})
            return {**out, "applied": True, "changeId": change_id, "versionKey": vkey}
        return self._guard("DATA_DOC_RESTORE", user, meta, cluster_id, index, dry_run, run, documentId=doc_id)

    # -- dry-run tokens ------------------------------------------------------
    def _sign(self, payload: dict) -> str:
        raw = canonical_json(payload).encode()
        sig = hmac.new(self.key, raw, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." + \
            base64.urlsafe_b64encode(sig).decode().rstrip("=")

    def _verify(self, token: str | None, expect: dict) -> dict:
        if not token:
            raise bad_request("DRY_RUN_REQUIRED", "Run this as a dry run first (dryRun=true) and send "
                              "back its dryRunToken, expectedCount and a reason")
        try:
            a, b = token.split(".")
            raw = base64.urlsafe_b64decode(a + "=" * (-len(a) % 4))
            sig = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4))
        except Exception as e:
            raise bad_request("DRY_RUN_REQUIRED", "The dryRunToken is not valid; run the dry run again") from e
        if not hmac.compare_digest(sig, hmac.new(self.key, raw, hashlib.sha256).digest()):
            raise bad_request("DRY_RUN_REQUIRED", "The dryRunToken is not valid; run the dry run again")
        p = json.loads(raw)
        if p.get("exp", 0) < time.time():
            raise bad_request("DRY_RUN_EXPIRED", "The dry run is older than 15 minutes; run it again")
        for k, v in expect.items():
            if p.get(k) != v:
                raise bad_request("DRY_RUN_MISMATCH", "The request differs from the dry run (query, filters, "
                                  "changes, index or user); run the dry run again", {"field": k})
        return p

    # -- bulk ----------------------------------------------------------------
    @staticmethod
    def _spec(op: str, cluster_id: str, target: str, body: BulkBody) -> dict:
        return {"op": op, "cluster": cluster_id, "target": target, "query": body.query, "dsl": body.dsl,
                "filters": [f.model_dump(exclude_none=True) for f in body.filters],
                "timeRange": body.timeRange.model_dump(exclude_none=True) if body.timeRange else None,
                "set": body.set if op == "update" else None, "remove": body.remove if op == "update" else None}

    def bulk(self, op: Literal["update", "delete"], user, meta, cluster_id, target, body: BulkBody,
             dry_run: bool) -> dict:
        action = "DATA_BULK_UPDATE" if op == "update" else "DATA_BULK_DELETE"
        spec_h = None

        def run():
            nonlocal spec_h
            t = check_target(target)
            es = self.registry.client(cluster_id)
            needed = "edit" if op == "update" else "delete"
            scope = resolve_scope(es, user, cluster_id, t, needed, require_all=True)
            if op == "update":
                if not body.set and not body.remove:
                    raise bad_request("NOTHING_TO_CHANGE", "Give 'set' and/or 'remove'")
                for f in list(body.set) + body.remove:
                    _check_field(f)
            spec = self._spec(op, cluster_id, t, body)
            spec_h = hashlib.sha256(canonical_json(spec).encode()).hexdigest()
            query = body.dsl if body.dsl else build_query(body)
            r = _read(es, "POST", f"/{_expr(t)}/_search", body={
                "query": scope.narrow(query), "size": BULK_MAX, "track_total_hits": True,
                "seq_no_primary_term": True, "sort": ["_doc"], "timeout": "60s"},
                params={"expand_wildcards": "open", "ignore_unavailable": "true", "allow_no_indices": "true"})
            total = (r.get("hits", {}).get("total") or {}).get("value", 0)
            hits = [h for h in r.get("hits", {}).get("hits", []) if h.get("_index") in scope.names]
            if total > BULK_MAX:
                raise unprocessable("TOO_MANY_DOCUMENTS", f"{total:,} documents match; bulk changes are limited "
                                    f"to {BULK_MAX:,}. Narrow the query or filters", {"count": total})
            plans, skipped = [], []
            for h in hits:
                if op == "delete":
                    plans.append((h, None))
                    continue
                try:
                    new = apply_changes(h.get("_source") or {}, body.set, body.remove)
                except ValueError as e:
                    skipped.append({"_index": h["_index"], "_id": h["_id"], "reason": str(e)})
                    continue
                if canonical_json(new) != canonical_json(h.get("_source") or {}):
                    plans.append((h, new))
            warnings = []
            if (body.dsl == {"match_all": {}}) or (not body.dsl and not body.query.strip() and not body.filters
                                                   and not body.timeRange):
                warnings.append("No query, filter or time range: this matches every document in "
                                f"'{t}'")
            sample = [{"_index": h["_index"], "_id": h["_id"],
                       "diff": diff(h.get("_source") or {}, new) if op == "update" else None,
                       "source": h.get("_source") if op == "delete" else None}
                      for h, new in plans[:SAMPLE]]
            summary = {"op": op, "index": t, "count": total, "willChange": len(plans),
                       "unchanged": len(hits) - len(plans) - len(skipped), "skipped": skipped[:20],
                       "skippedCount": len(skipped), "indices": scope.permission_names(), "warnings": warnings}
            fields = {"set": sorted(body.set), "remove": body.remove} if op == "update" else None
            if dry_run:
                token = self._sign({"spec": spec_h, "user": user.username, "count": total,
                                    "change": len(plans), "exp": int(time.time()) + TOKEN_TTL})
                self._audit(action, user, meta, cluster_id, t, "SUCCESS", True, count=total,
                            willChange=len(plans), fields=fields, search=_search_audit(body))
                return {**summary, "dryRun": True, "sample": sample, "dryRunToken": token,
                        "expiresInSeconds": TOKEN_TTL,
                        "confirm": f"Send the same request with dryRunToken, expectedCount={total} and a reason"}
            tok = self._verify(body.dryRunToken, {"spec": spec_h, "user": user.username})
            why = _need_reason(body.reason)
            if body.expectedCount != tok["count"]:
                raise bad_request("COUNT_MISMATCH", f"expectedCount must be {tok['count']} (from the dry run)")
            if total != tok["count"] or len(plans) != tok["change"]:
                raise conflict("COUNT_CHANGED", f"The dry run matched {tok['count']:,} documents, now "
                               f"{total:,} match. Run the dry run again", {"was": tok["count"], "now": total})
            change_id = new_id()
            stamp = _stamp()
            backup_key = f"bulk-backups/{cluster_id}/{stamp}_{change_id}.json"
            meta_key = f"bulk-changes/{cluster_id}/{stamp}_{change_id}.json"
            record = {"changeId": change_id, "clusterId": cluster_id, "op": op, "index": t, "at": iso(),
                      "by": user.username, "reason": why, "count": len(plans), "fields": fields,
                      "search": _search_audit(body), "backupKey": backup_key, "status": "RUNNING",
                      "indices": scope.permission_names()}
            self.store.put_json(backup_key, {"changeId": change_id, "docs": [
                {"_index": h["_index"], "_id": h["_id"], "_source": h.get("_source")} for h, _ in plans]})
            self.store.put_json(meta_key, record)
            result = self._run_bulk(es, [
                ({"delete": {"_index": h["_index"], "_id": h["_id"], "if_seq_no": h["_seq_no"],
                             "if_primary_term": h["_primary_term"]}}, None) if op == "delete" else
                ({"index": {"_index": h["_index"], "_id": h["_id"], "if_seq_no": h["_seq_no"],
                            "if_primary_term": h["_primary_term"]}}, new)
                for h, new in plans])
            record.update(status="DONE", result=result, finishedAt=iso())
            self.store.put_json(meta_key, record)
            touched = sorted({h["_index"] for h, _ in plans})
            self._feed(cluster_id, touched, {"kind": "bulk", "changeId": change_id, "op": op,
                                             "action": "BULK_" + op.upper(), "at": iso(), "by": user.username,
                                             "reason": why, "count": result["succeeded"], "fields": fields,
                                             "indices": touched, "target": t})
            self._audit(action, user, meta, cluster_id, t, "SUCCESS" if not result["failed"] else "FAILED",
                        changeId=change_id, reason=why, count=total, changed=result["succeeded"],
                        conflicts=result["conflicts"], failed=result["failed"], fields=fields,
                        backupKey=backup_key, search=_search_audit(body))
            return {**summary, "applied": True, "changeId": change_id, "backupKey": backup_key, **result}
        return self._guard(action, user, meta, cluster_id, target, dry_run, run)

    def _run_bulk(self, es, actions: list[tuple[dict, dict | None]]) -> dict:
        ok = conflicts = failed = 0
        errors: list[dict] = []
        for i in range(0, len(actions), BULK_CHUNK):
            chunk = actions[i:i + BULK_CHUNK]
            lines = []
            for meta_line, src in chunk:
                lines.append(json.dumps(meta_line))
                if src is not None:
                    lines.append(json.dumps(src, default=str))
            last = i + BULK_CHUNK >= len(actions)
            r = _read(es, "POST", "/_bulk", body="\n".join(lines) + "\n",
                      params={"refresh": "wait_for"} if last else None, headers=NDJSON_HEADERS)
            for item in r.get("items", []):
                res = next(iter(item.values()))
                st = res.get("status", 500)
                if st < 300:
                    ok += 1
                elif st == 409:
                    conflicts += 1
                    if len(errors) < 20:
                        errors.append({"_index": res.get("_index"), "_id": res.get("_id"),
                                       "error": "changed by someone else since the dry run; left as is"})
                else:
                    failed += 1
                    if len(errors) < 20:
                        errors.append({"_index": res.get("_index"), "_id": res.get("_id"),
                                       "error": (res.get("error") or {}).get("reason")})
        return {"succeeded": ok, "conflicts": conflicts, "failed": failed, "errors": errors}

    def changes(self, user: User, cluster_id: str) -> dict:
        items = []
        for key in reversed(self.store.list_keys(f"bulk-changes/{cluster_id}/")):
            rec, _ = self.store.get_json(key)
            if rec and all(user.index_level(cluster_id, n) for n in rec.get("indices", [])):
                items.append({k: rec.get(k) for k in ("changeId", "op", "index", "at", "by", "reason", "count",
                                                         "fields", "status", "result", "restoredBy", "restoredAt",
                                                         "restoreOf", "search")})
        return {"clusterId": cluster_id, "items": items[:200]}

    def restore_bulk(self, user, meta, cluster_id, change_id, body: RestoreBody, dry_run: bool) -> dict:
        def run():
            keys = [k for k in self.store.list_keys(f"bulk-changes/{cluster_id}/") if k.endswith(f"_{change_id}.json")]
            if not keys:
                raise not_found("CHANGE_NOT_FOUND", f"No bulk change '{change_id}' on this cluster")
            rec, _ = self.store.get_json(keys[0])
            backup, _ = self.store.get_json(rec["backupKey"])
            docs = (backup or {}).get("docs", [])
            # documents that did not exist before that change (a restore recreated them): deleted again
            gone = [d for d in (backup or {}).get("missing", []) if d.get("_id")]
            denied = sorted({d["_index"] for d in docs + gone if not user.can_index(cluster_id, d["_index"], "edit")})
            if denied:
                raise forbidden("PERMISSION_DENIED", "You need 'edit' on every index this change touched",
                                {"indices": denied[:50]})
            es = self.registry.client(cluster_id)
            if dry_run:
                token = self._sign({"restore": change_id, "user": user.username, "count": len(docs) + len(gone),
                                    "exp": int(time.time()) + TOKEN_TTL})
                self._audit("DATA_BULK_RESTORE", user, meta, cluster_id, rec["index"], "SUCCESS", True,
                            restoreOf=change_id, count=len(docs) + len(gone))
                return {"dryRun": True, "changeId": change_id, "count": len(docs) + len(gone), "op": rec["op"],
                        "at": rec["at"], "by": rec["by"], "dryRunToken": token,
                        "note": "Each document is put back exactly as it was before that change; later "
                                "edits to those documents are overwritten (and backed up first)"}
            tok = self._verify(body.dryRunToken, {"restore": change_id, "user": user.username})
            why = _need_reason(body.reason)
            if body.expectedCount != tok["count"]:
                raise bad_request("COUNT_MISMATCH", f"expectedCount must be {tok['count']}")
            # back up the current state of those documents first, so the restore can be undone too
            current = []
            for i in range(0, len(docs), 1000):
                part = docs[i:i + 1000]
                r = _read(es, "POST", "/_mget", body={"docs": [{"_index": d["_index"], "_id": d["_id"]} for d in part]})
                current += [{"_index": x["_index"], "_id": x["_id"], "_source": x.get("_source")}
                            for x in r.get("docs", []) if x.get("found")]
            new_id_ = new_id()
            stamp = _stamp()
            backup_key = f"bulk-backups/{cluster_id}/{stamp}_{new_id_}.json"
            self.store.put_json(backup_key, {"changeId": new_id_, "docs": current,
                                             "missing": [{"_index": d["_index"], "_id": d["_id"]} for d in docs
                                                         if not any(c["_id"] == d["_id"] and c["_index"] == d["_index"] for c in current)][:10_000]})
            result = self._run_bulk(es, [({"index": {"_index": d["_index"], "_id": d["_id"]}}, d["_source"])
                                         for d in docs] +
                                    [({"delete": {"_index": d["_index"], "_id": d["_id"]}}, None) for d in gone])
            self.store.put_json(f"bulk-changes/{cluster_id}/{stamp}_{new_id_}.json", {
                "changeId": new_id_, "clusterId": cluster_id, "op": "restore", "index": rec["index"],
                "at": iso(), "by": user.username, "reason": why, "count": len(docs), "restoreOf": change_id,
                "backupKey": backup_key, "status": "DONE", "result": result, "indices": rec.get("indices", [])})
            rec.update(restoredBy=user.username, restoredAt=iso())
            self.store.put_json(keys[0], rec)
            touched = sorted({d["_index"] for d in docs + gone})
            self._feed(cluster_id, touched, {"kind": "bulk", "changeId": new_id_, "op": "restore",
                                             "action": "BULK_RESTORE", "at": iso(), "by": user.username,
                                             "reason": why, "count": result["succeeded"], "restoreOf": change_id,
                                             "indices": touched, "target": rec["index"]})
            self._audit("DATA_BULK_RESTORE", user, meta, cluster_id, rec["index"],
                        "SUCCESS" if not result["failed"] else "FAILED", changeId=new_id_, reason=why,
                        restoreOf=change_id, count=len(docs), changed=result["succeeded"], failed=result["failed"],
                        backupKey=backup_key)
            return {"applied": True, "changeId": new_id_, "restoreOf": change_id, "backupKey": backup_key, **result}
        return self._guard("DATA_BULK_RESTORE", user, meta, cluster_id, change_id, dry_run, run)


def _search_audit(body: SearchBody) -> dict:
    dsl = getattr(body, "dsl", None)
    if dsl:
        from .util import dsl_names
        return {"dsl": dsl_names(dsl)["fields"]}
    return {"query": body.query or None,
            "filters": [f.model_dump(exclude_none=True) for f in body.filters] or None,
            "timeRange": body.timeRange.model_dump(exclude_none=True) if body.timeRange else None}
