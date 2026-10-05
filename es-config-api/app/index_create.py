"""Create an index from the console, and undo that while it is still empty.

* Who: Edit (write) access on the cluster as a whole, and an index rule that lowers access
  for that name (View / No access) still blocks it. No allowlist.
* What: settings, mappings and aliases (JSON). Elasticsearch adds what the matching index
  template gives; the dry run (required) shows the combined result, checked by Elasticsearch
  (`_index_template/_simulate`), before anything is created.
* Names: one concrete, lowercase name; not a system (dot) name; not taken by an index,
  alias or data stream; not a name a data-stream template claims.
* Every create is audited (INDEX_CREATE, setting keys and field names, never values) and
  recorded in ``created-indices/<cluster>/<index>/<changeId>.json`` so it can be undone:
  undo deletes the index only while it holds no documents (and is still the same index),
  saving its definition like any delete, so it can be recreated too.
"""
from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any

from pydantic import BaseModel, Field

from .clusters import es_call
from .errors import ApiError, bad_request, conflict, forbidden, not_found, unprocessable
from .handlers.base import Handler
from .identity import User, require_cluster
from .util import iso, new_id

SIM_PRIORITY = 2_147_483_646     # above any real template, so the simulation uses our merge
_names = Handler()
_names.resource_kind = "index"


class CreateIndexBody(BaseModel):
    settings: dict[str, Any] = Field(default_factory=dict,
                                     description='e.g. {"number_of_shards": 1, "number_of_replicas": 1}')
    mappings: dict[str, Any] = Field(default_factory=dict,
                                     description='e.g. {"properties": {"sku": {"type": "keyword"}}}')
    aliases: dict[str, Any] = Field(default_factory=dict, description='e.g. {"orders-current": {}}')
    reason: str | None = Field(None, max_length=1000, description="Required unless dryRun")

    model_config = {"json_schema_extra": {"examples": [{
        "settings": {"number_of_shards": 1, "number_of_replicas": 1},
        "mappings": {"properties": {"order_id": {"type": "keyword"}, "created": {"type": "date"}}},
        "aliases": {}, "reason": "Returns import for October"}]}}


class UndoCreateBody(BaseModel):
    changeId: str | None = Field(None, description="The INDEX_CREATE change; default: the latest one")
    reason: str | None = Field(None, max_length=1000)


# ------------------------------------------------------------------ helpers
def flat_settings(d: dict, prefix: str = "") -> dict[str, Any]:
    """{"index": {"number_of_shards": 1}} / {"number_of_shards": 1} -> {"index.number_of_shards": 1}."""
    out: dict[str, Any] = {}
    for k, v in (d or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and k not in ("analysis",) and not key.endswith(".analysis"):
            out.update(flat_settings(v, key + "."))
        else:
            out[key] = v
    return {(k if k.startswith("index.") else f"index.{k}"): v for k, v in out.items()} if not prefix else out


def merge_mappings(base: dict, over: dict) -> dict:
    """Request mappings on top of the template's (fields merge; the request wins per field)."""
    out = dict(base or {})
    for k, v in (over or {}).items():
        both_objects = isinstance(v, dict) and isinstance(out.get(k), dict)
        # a field given with its own "type" replaces the template's; containers merge
        if both_objects and (k in ("properties", "fields", "_meta") or "type" not in v):
            out[k] = merge_mappings(out[k], v)
        else:
            out[k] = v
    return out


def _fields(mappings: dict, prefix: str = "") -> list[str]:
    out = []
    for name, spec in ((mappings or {}).get("properties") or {}).items():
        out.append(prefix + name)
        if isinstance(spec, dict) and spec.get("properties"):
            out += _fields(spec, f"{prefix}{name}.")
    return out


def _exists(es, name: str) -> str | None:
    """'index' / 'alias' / 'data stream' if the name is taken, else None."""
    try:
        r = es_call(es, "GET", f"/_resolve/index/{name}", params={"expand_wildcards": "all"})
    except ApiError as e:
        if e.code == "ES_NOT_FOUND":
            return None
        raise
    for part, label in (("indices", "index"), ("aliases", "alias"), ("data_streams", "data stream")):
        if any(x.get("name") == name for x in r.get(part, [])):
            return label
    return None


def _matching_template(es, name: str) -> dict | None:
    """The index template Elasticsearch would apply to this name (highest priority)."""
    try:
        r = es_call(es, "GET", "/_index_template")
    except ApiError:
        return None
    best = None
    for t in r.get("index_templates", []):
        body = t["index_template"]
        if any(fnmatchcase(name, p) for p in body.get("index_patterns", [])):
            if best is None or body.get("priority", 0) > best["priority"]:
                best = {"name": t["name"], "priority": body.get("priority", 0),
                        "indexPatterns": body.get("index_patterns", []),
                        "composedOf": body.get("composed_of", []),
                        "dataStream": "data_stream" in body}
    return best


def _template_result(es, name: str) -> dict:
    r = es_call(es, "POST", f"/_index_template/_simulate_index/{name}")
    t = r.get("template") or {}
    return {"settings": flat_settings(t.get("settings") or {}), "mappings": t.get("mappings") or {},
            "aliases": t.get("aliases") or {}}


# ------------------------------------------------------------------ service
class IndexCreateService:
    def __init__(self, registry, store, audit):
        self.registry = registry
        self.store = store
        self.audit = audit

    def _check(self, user: User, cluster_id: str, index: str) -> None:
        _names.check_resource(index)
        if index.startswith((".", "-", "_", "+")) or index in (".", ".."):
            raise bad_request("INVALID_INDEX_NAME", "Index names can't start with '.', '-', '_' or '+' "
                              "(dot names are system indices)")
        self.registry.get(cluster_id)
        require_cluster(user, cluster_id, "edit")
        if not user.can_index(cluster_id, index, "edit"):
            raise forbidden("PERMISSION_DENIED", f"An index rule limits your access to '{index}': "
                            "you can't create an index with that name", {"index": index})

    def preview(self, user: User, cluster_id: str, index: str) -> dict:
        """What the matching templates would give this name (to pre-fill the create form)."""
        self._check(user, cluster_id, index)
        es = self.registry.client(cluster_id)
        return {"index": index, "exists": _exists(es, index), "template": _matching_template(es, index),
                "fromTemplates": _template_result(es, index)}

    def create(self, user: User, meta, cluster_id: str, index: str, body: CreateIndexBody,
               dry_run: bool) -> dict:
        change_id = new_id()
        audit_base = {"changeId": change_id, "action": "DRY_RUN" if dry_run else "INDEX_CREATE",
                      "requestedAction": "INDEX_CREATE", "actor": user.username, "sourceIp": meta.source_ip,
                      "requestId": meta.request_id, "clusterId": cluster_id, "configType": "index-create",
                      "resource": index, "reason": body.reason}
        try:
            self._check(user, cluster_id, index)
            es = self.registry.client(cluster_id)
            taken = _exists(es, index)
            if taken:
                raise conflict("INDEX_EXISTS", f"'{index}' already exists as {'an' if taken[0] in 'ai' else 'a'} {taken}")
            tpl = _matching_template(es, index)
            if tpl and tpl["dataStream"]:
                raise unprocessable("DATA_STREAM_TEMPLATE",
                                    f"'{index}' matches template '{tpl['name']}', which creates data streams; "
                                    "pick a name that doesn't match it", {"template": tpl["name"]})
            for part, value in (("settings", body.settings), ("mappings", body.mappings), ("aliases", body.aliases)):
                if not isinstance(value, dict):
                    raise bad_request("INVALID_BODY", f"'{part}' must be a JSON object")
            if any(a.startswith(".") for a in body.aliases):
                raise bad_request("INVALID_BODY", "Aliases can't start with a dot")
            own = flat_settings(body.settings)
            base = _template_result(es, index)
            final = {"settings": {**base["settings"], **own},
                     "mappings": merge_mappings(base["mappings"], body.mappings),
                     "aliases": {**base["aliases"], **body.aliases}}
            # let Elasticsearch check the combined result (mapping types, setting names and values)
            try:
                es_call(es, "POST", "/_index_template/_simulate", body={
                    "index_patterns": [index], "priority": SIM_PRIORITY,
                    "template": {k: v for k, v in final.items() if v}})
            except ApiError as e:
                raise bad_request("INVALID_INDEX_BODY", "Elasticsearch would refuse this index: " + e.message,
                                  (e.details or {}).get("esError") if isinstance(e.details, dict) else None) from e
            warnings = []
            reps = final["settings"].get("index.number_of_replicas")
            if str(reps) == "0":
                warnings.append("No replicas (number_of_replicas 0): losing one node loses this index's data")
            shards = final["settings"].get("index.number_of_shards")
            if shards is not None and int(shards) > 10:
                warnings.append(f"{shards} primary shards is a lot for one index; it can't be changed later "
                                "without reindexing")
            if tpl:
                warnings.append(f"Template '{tpl['name']}' (priority {tpl['priority']}) also applies to this name: "
                                "its settings and mappings are included below; yours win where they differ")
            for a in body.aliases:
                if _exists(es, a) == "index":
                    raise conflict("ALIAS_IS_INDEX", f"'{a}' is an index, so it can't be used as an alias")
            audit_extra = {"settingKeys": sorted(own), "mappingFields": _fields(body.mappings)[:200],
                           "aliases": sorted(body.aliases), "template": tpl["name"] if tpl else None}
            result = {"index": index, "dryRun": dry_run, "template": tpl, "result": final,
                      "warnings": warnings, "undo": "Roll back deletes it again, as long as it has no documents"}
            if dry_run:
                self.audit.write({**audit_base, "outcome": "SUCCESS", **audit_extra})
                return {**result, "applied": False}
            if not (body.reason and body.reason.strip()):
                raise bad_request("REASON_REQUIRED", "Give a 'reason' (it is audited)")
            req = {k: v for k, v in (("settings", own), ("mappings", body.mappings), ("aliases", body.aliases)) if v}
            es_call(es, "PUT", f"/{index}", body=req or None)
            uuid = None
            try:
                s = es_call(es, "GET", f"/{index}/_settings/index.uuid")
                uuid = s[index]["settings"]["index"]["uuid"]
            except Exception:
                pass
            key = f"created-indices/{cluster_id}/{index}/{change_id}.json"
            self.store.put_json(key, {"changeId": change_id, "clusterId": cluster_id, "index": index,
                                      "uuid": uuid, "at": iso(), "by": user.username,
                                      "reason": body.reason.strip(), "request": req})
            self.audit.write({**audit_base, "outcome": "SUCCESS", **audit_extra, "createdKey": key})
            return {**result, "applied": True, "changeId": change_id}
        except ApiError as e:
            self.audit.write({**audit_base, "outcome": "REJECTED" if e.status < 500 and e.code != "ES_REJECTED" else "FAILED",
                              "error": {"status": e.status, "code": e.code, "message": e.message}})
            raise

    # -- undo -----------------------------------------------------------------
    def _record(self, cluster_id: str, index: str, change_id: str | None) -> dict | None:
        keys = self.store.list_keys(f"created-indices/{cluster_id}/{index}/")
        recs = [d for d in (self.store.get_json(k)[0] for k in keys) if d]
        if change_id:
            recs = [r for r in recs if r["changeId"] == change_id]
        return max(recs, key=lambda r: r["at"]) if recs else None

    def _state(self, es, index: str, rec: dict) -> dict:
        """Can this create still be undone: same index, and no documents in it."""
        if not _exists(es, index) == "index":
            return {"canUndo": False, "why": "gone", "docs": None}
        try:
            uuid = es_call(es, "GET", f"/{index}/_settings/index.uuid")[index]["settings"]["index"]["uuid"]
        except Exception:
            uuid = None
        if rec.get("uuid") and uuid and uuid != rec["uuid"]:
            return {"canUndo": False, "why": "replaced", "docs": None}
        try:
            es_call(es, "POST", f"/{index}/_refresh")
        except ApiError:
            pass
        docs = es_call(es, "GET", f"/{index}/_count").get("count", 0)
        return {"canUndo": docs == 0, "why": None if docs == 0 else "not-empty", "docs": docs}

    def created(self, user: User, cluster_id: str, index: str) -> dict:
        """Whether this index was created in the console, and whether that can still be undone."""
        self.registry.get(cluster_id)
        if not user.index_level(cluster_id, index):
            raise forbidden("PERMISSION_DENIED", f"No access to '{index}'")
        rec = self._record(cluster_id, index, None)
        if not rec:
            return {"index": index, "created": False, "canUndo": False}
        es = self.registry.client(cluster_id)
        st = self._state(es, index, rec)
        allowed = user.can(cluster_id, "edit") and user.can_index(cluster_id, index, "edit")
        return {"index": index, "created": True, "changeId": rec["changeId"], "at": rec["at"], "by": rec["by"],
                "reason": rec.get("reason"), **st, "canUndo": st["canUndo"] and allowed}

    def undo(self, user: User, meta, cluster_id: str, index: str, body: UndoCreateBody, dry_run: bool) -> dict:
        change_id = new_id()
        audit_base = {"changeId": change_id, "action": "DRY_RUN" if dry_run else "INDEX_DELETE",
                      "requestedAction": "INDEX_CREATE_UNDO", "actor": user.username, "sourceIp": meta.source_ip,
                      "requestId": meta.request_id, "clusterId": cluster_id, "configType": "index-delete",
                      "resource": index, "reason": body.reason}
        try:
            self._check(user, cluster_id, index)
            rec = self._record(cluster_id, index, body.changeId)
            if not rec:
                raise not_found("CHANGE_NOT_FOUND", f"No create of '{index}' through the console to undo")
            audit_base["restoreOf"] = rec["changeId"]
            es = self.registry.client(cluster_id)
            st = self._state(es, index, rec)
            if st["why"] == "gone":
                raise not_found("INDEX_NOT_FOUND", f"'{index}' doesn't exist any more")
            if st["why"] == "replaced":
                raise conflict("INDEX_REPLACED", f"'{index}' was deleted and created again since; "
                               "this undo no longer applies")
            if st["why"] == "not-empty":
                raise conflict("INDEX_NOT_EMPTY", f"'{index}' already holds {st['docs']:,} document"
                               f"{'' if st['docs'] == 1 else 's'}; use Delete index instead (it has its own checks)",
                               {"docs": st["docs"]})
            out = {"index": index, "dryRun": dry_run, "docs": 0, "createdBy": rec["by"], "createdAt": rec["at"],
                   "restoreOf": rec["changeId"],
                   "note": "The empty index is deleted; its settings and mappings are kept, so it can be recreated"}
            if dry_run:
                self.audit.write({**audit_base, "outcome": "SUCCESS"})
                return out
            if not (body.reason and body.reason.strip()):
                raise bad_request("REASON_REQUIRED", "Give a 'reason' (it is audited)")
            definition = es_call(es, "GET", f"/{index}").get(index, {})
            tomb_key = f"deleted-indices/{cluster_id}/{index}/{iso().replace(':', '')}_{change_id}.json"
            self.store.put_json(tomb_key, {
                "clusterId": cluster_id, "index": index, "deletedBy": user.username, "deletedAt": iso(),
                "reason": body.reason.strip(), "changeId": change_id, "undoOfCreate": rec["changeId"],
                "summary": {"docsCount": 0, "storeSizeBytes": None}, "definition": definition}, if_none_match=True)
            es_call(es, "DELETE", f"/{index}")
            for ctype in ("index-settings", "index-mappings"):
                try:
                    self.store.delete(f"snapshots/{cluster_id}/{ctype}/{index}.json")
                except Exception:
                    pass
            self.audit.write({**audit_base, "outcome": "SUCCESS", "tombstoneKey": tomb_key})
            return {**out, "applied": True, "changeId": change_id, "tombstoneKey": tomb_key}
        except ApiError as e:
            self.audit.write({**audit_base, "outcome": "REJECTED" if e.status < 500 and e.code != "ES_REJECTED" else "FAILED",
                              "error": {"status": e.status, "code": e.code, "message": e.message}})
            raise
