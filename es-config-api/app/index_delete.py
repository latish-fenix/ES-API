"""Delete an index, with guards.

Deleting an index destroys its documents; nothing in this API can bring them back.
Before deleting, the index's settings, mappings and aliases are saved to S3
(``deleted-indices/...``) so the *empty* index can be recreated. Guards:

* the caller needs the ``delete`` permission level on the cluster (above ``edit``)
* the index must match the ``index-delete`` allowlist (dot/system indices only via
  patterns that start with ``.``); a missing ``index-delete`` entry blocks all deletes
* ``confirm`` must repeat the index name exactly, and ``reason`` is required
* only concrete indices: aliases, data-stream names and wildcards are refused
* the current write index of a data stream is refused
* one delete at a time per index (S3 lock); every attempt is audited
"""
from __future__ import annotations

from typing import Any

from .clusters import es_call
from .errors import ApiError, bad_request, forbidden, not_found, unprocessable
from .handlers.base import Handler
from .identity import User, require_cluster
from .util import iso, new_id

CONFIG_TYPE = "index-delete"
_name_checker = Handler()
_name_checker.resource_kind = "index"


def _index_summary(es, index: str) -> dict:
    resolved = es_call(es, "GET", f"/_resolve/index/{index}", params={"expand_wildcards": "all"})
    if any(a.get("name") == index for a in resolved.get("aliases", [])):
        raise unprocessable("NOT_A_CONCRETE_INDEX",
                            f"'{index}' is an alias; delete the index it points to by its own name",
                            {"indices": next(a["indices"] for a in resolved["aliases"]
                                             if a["name"] == index)})
    if any(d.get("name") == index for d in resolved.get("data_streams", [])):
        raise unprocessable("NOT_A_CONCRETE_INDEX",
                            f"'{index}' is a data stream, not an index; this endpoint deletes "
                            "single indices only")
    entry = next((i for i in resolved.get("indices", []) if i.get("name") == index), None)
    if entry is None:
        raise not_found("INDEX_NOT_FOUND", f"Index '{index}' does not exist")

    rows = es_call(es, "GET", f"/_cat/indices/{index}",
                   params={"format": "json", "bytes": "b", "expand_wildcards": "all",
                           "h": "index,health,status,docs.count,store.size,pri,rep,creation.date.string"})
    row = rows[0] if rows else {}
    definition = es_call(es, "GET", f"/{index}", params={"expand_wildcards": "all"}).get(index, {})

    def _int(v: Any) -> int | None:
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return {
        "index": index,
        "status": row.get("status") or ("open" if "open" in entry.get("attributes", []) else "closed"),
        "health": row.get("health"),
        "docsCount": _int(row.get("docs.count")),
        "storeSizeBytes": _int(row.get("store.size")),
        "primaryShards": _int(row.get("pri")),
        "replicas": _int(row.get("rep")),
        "createdAt": row.get("creation.date.string"),
        "hidden": "hidden" in entry.get("attributes", []),
        "aliases": entry.get("aliases", []),
        "dataStream": entry.get("data_stream"),
        "definition": definition,  # settings + mappings + aliases, for the tombstone
    }


class IndexDeleteService:
    def __init__(self, registry, store, locks, allowlist, audit):
        self.registry = registry
        self.store = store
        self.locks = locks
        self.allowlist = allowlist
        self.audit = audit

    def delete(self, user: User, meta, cluster_id: str, index: str, confirm: str | None,
               reason: str | None, dry_run: bool) -> dict:
        change_id = new_id()
        audit_base = {
            "changeId": change_id, "action": "DRY_RUN" if dry_run else "INDEX_DELETE",
            "requestedAction": "INDEX_DELETE", "actor": user.username,
            "sourceIp": meta.source_ip, "requestId": meta.request_id, "clusterId": cluster_id,
            "configType": CONFIG_TYPE, "resource": index, "reason": reason,
        }
        token = None
        ctx: dict[str, Any] = {}
        try:
            _name_checker.check_resource(index)
            self.registry.get(cluster_id)
            require_cluster(user, cluster_id, "delete")
            blocked, source = self.allowlist.blocked(cluster_id, CONFIG_TYPE, [index])
            if blocked:
                raise forbidden("NOT_ALLOWLISTED",
                                f"Index '{index}' is not on the {source} allowlist for index-delete "
                                "(dot/system indices need a pattern that starts with '.')",
                                {"blocked": blocked, "allowlist": source})
            if not dry_run:
                if not (reason and reason.strip()):
                    raise bad_request("REASON_REQUIRED", "Give a 'reason' for the delete (it is audited)")
                if confirm != index:
                    raise bad_request("CONFIRMATION_MISMATCH",
                                      "Set 'confirm' to the exact index name to delete it",
                                      {"expected": index, "got": confirm})

            es = self.registry.client(cluster_id)
            if not dry_run:
                token = self.locks.acquire(cluster_id, CONFIG_TYPE, index, user.username)

            info = _index_summary(es, index)
            definition = info.pop("definition")
            ctx["index"] = {k: v for k, v in info.items()}

            warnings: list[str] = [
                "Deleting an index permanently destroys its documents. The API keeps only the "
                "settings, mappings and aliases, so it can recreate an EMPTY index, not the data."]
            if info["dataStream"]:
                ds = es_call(es, "GET", f"/_data_stream/{info['dataStream']}")
                backing = [i["index_name"] for i in ds["data_streams"][0]["indices"]]
                if backing and backing[-1] == index:
                    raise unprocessable("DATA_STREAM_WRITE_INDEX",
                                        f"'{index}' is the current write index of data stream "
                                        f"'{info['dataStream']}'; roll the data stream over first")
                warnings.append(f"This is an older backing index of data stream '{info['dataStream']}'")
            if info["aliases"]:
                warnings.append(f"Aliases that point to this index will lose it: {info['aliases']}")
            if info["docsCount"]:
                warnings.append(f"{info['docsCount']:,} documents will be deleted")
            health = es_call(es, "GET", "/_cluster/health").get("status")

            result = {
                "changeId": change_id, "action": "INDEX_DELETE", "dryRun": dry_run,
                "clusterId": cluster_id, "index": index, **info, "clusterHealth": health,
                "warnings": warnings, "permanent": True, "confirmRequired": index,
            }
            if dry_run:
                result["applied"] = False
                self.audit.write({**audit_base, "outcome": "SUCCESS", **ctx})
                return result

            # 1) tombstone first: nothing is deleted unless this write succeeds
            tomb_key = f"deleted-indices/{cluster_id}/{index}/{iso().replace(':', '')}_{change_id}.json"
            self.store.put_json(tomb_key, {
                "clusterId": cluster_id, "index": index, "deletedBy": user.username,
                "deletedAt": iso(), "reason": reason, "changeId": change_id,
                "summary": info, "definition": definition,
            }, if_none_match=True)
            self.locks.verify(cluster_id, CONFIG_TYPE, index, token)

            # 2) delete
            es_call(es, "DELETE", f"/{index}")

            # 3) the index's settings/mapping snapshots no longer apply to anything
            for ctype in ("index-settings", "index-mappings"):
                try:
                    self.store.delete(f"snapshots/{cluster_id}/{ctype}/{index}.json")
                except Exception:
                    pass

            result.update(applied=True, tombstoneKey=tomb_key)
            self.audit.write({**audit_base, "outcome": "SUCCESS", **ctx, "tombstoneKey": tomb_key})
            return result
        except ApiError as e:
            outcome = "REJECTED" if e.status < 500 and e.code != "ES_REJECTED" else "FAILED"
            self.audit.write({**audit_base, "outcome": outcome,
                              "error": {"status": e.status, "code": e.code, "message": e.message},
                              **ctx})
            raise
        finally:
            if token:
                try:
                    self.locks.release(cluster_id, CONFIG_TYPE, index, token)
                except Exception:
                    pass

    def list_tombstones(self, user: User, cluster_id: str, index: str | None = None) -> dict:
        require_cluster(user, cluster_id, "view")
        self.registry.get(cluster_id)
        prefix = f"deleted-indices/{cluster_id}/" + (f"{index}/" if index else "")
        items = []
        for key in reversed(self.store.list_keys(prefix)):
            doc, _ = self.store.get_json(key)
            if doc:
                items.append({"index": doc["index"], "deletedAt": doc["deletedAt"],
                              "deletedBy": doc["deletedBy"], "reason": doc.get("reason"),
                              "docsCount": doc["summary"].get("docsCount"),
                              "storeSizeBytes": doc["summary"].get("storeSizeBytes"),
                              "key": key, "definition": doc["definition"]})
        return {"clusterId": cluster_id, "items": items}
