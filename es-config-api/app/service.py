"""Orchestrates get / update / rollback / dry run for every config type.

Order of operations for a real (non dry-run) change:
  permission -> lock -> resolve leftover pending -> read live + health -> drift / If-Match
  -> validate + diff -> allowlist -> save PENDING snapshot -> apply to ES
  -> promote snapshot + audit -> unlock
Nothing touches Elasticsearch until the snapshot is safely in S3.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from .clusters import ClusterRegistry, es_call
from .errors import ApiError, bad_request, conflict, forbidden, not_found, precondition_failed, unprocessable
from .handlers import Handler, Plan, State, get_handler
from .identity import (User, require_admin_user, require_any_access, require_cluster,
                       require_index)

INDEX_TYPES = ("index-settings", "index-mappings")


def authorize(user: User, cluster_id: str, config_type: str, resource: str, needed: str) -> None:
    """Cluster settings: admins only. Index settings/mappings: the user's level on that index.
    Templates, ILM policies, pipelines: the user's cluster-wide level."""
    if config_type == "cluster-settings":
        require_admin_user(user)
    elif config_type in INDEX_TYPES:
        require_index(user, cluster_id, resource, needed)
    else:
        require_cluster(user, cluster_id, needed)
from .repos import AllowlistRepo, AuditRepo, ConfigHistoryRepo, LockRepo, SnapshotRepo
from .util import diff, diff_is_empty, iso, new_id

CLUSTER_RESOURCE = "_cluster"
log = logging.getLogger("es_config_api.service")


@dataclass
class RequestMeta:
    request_id: str
    source_ip: str | None


class ChangeService:
    def __init__(self, registry: ClusterRegistry, snapshots: SnapshotRepo, locks: LockRepo,
                 allowlist: AllowlistRepo, audit: AuditRepo, history: ConfigHistoryRepo | None = None):
        self.registry = registry
        self.snapshots = snapshots
        self.history = history or ConfigHistoryRepo(snapshots.store)
        self.locks = locks
        self.allowlist = allowlist
        self.audit = audit

    # ------------------------------------------------------------------ reads
    def health(self, user: User, cluster_id: str) -> dict:
        require_any_access(user, cluster_id)
        es = self.registry.client(cluster_id)
        h = es_call(es, "GET", "/_cluster/health")
        return {"clusterId": cluster_id, "status": h.get("status"),
                "numberOfNodes": h.get("number_of_nodes"),
                "unassignedShards": h.get("unassigned_shards"),
                "clusterName": h.get("cluster_name")}

    def nodes(self, user: User, cluster_id: str) -> dict:
        from .nodes import node_stats
        require_any_access(user, cluster_id)
        return node_stats(self.registry.client(cluster_id), cluster_id)

    def list_resources(self, user: User, cluster_id: str, config_type: str) -> dict:
        require_cluster(user, cluster_id, "view")
        handler = get_handler(config_type)
        es = self.registry.client(cluster_id)
        return {"clusterId": cluster_id, "configType": config_type, "items": handler.list(es)}

    def list_indices(self, user: User, cluster_id: str) -> dict:
        """Indices the user may see (index rules applied), each with their level on it."""
        require_any_access(user, cluster_id)
        es = self.registry.client(cluster_id)
        rows = es_call(es, "GET", "/_cat/indices",
                       params={"format": "json", "h": "index,health,status,docs.count",
                               "expand_wildcards": "open,closed"})
        items = []
        for r in rows:
            if r["index"].startswith("."):
                continue
            level = user.index_level(cluster_id, r["index"])
            if level:
                items.append({**r, "permission": level})
        return {"clusterId": cluster_id, "items": sorted(items, key=lambda r: r["index"])}

    def get(self, user: User, cluster_id: str, config_type: str, resource: str) -> dict:
        authorize(user, cluster_id, config_type, resource, "view")
        handler = get_handler(config_type)
        handler.check_resource(resource)
        es = self.registry.client(cluster_id)
        current = handler.fetch(es, resource)
        if not current.exists:
            raise not_found("RESOURCE_NOT_FOUND", f"{handler.label} '{resource}' does not exist")
        doc, _ = self.snapshots.get(cluster_id, config_type, resource)
        last = (doc or {}).get("lastApplied")
        return {
            "clusterId": cluster_id, "configType": config_type, "resource": resource,
            "version": current.version, "config": current.config,
            "rollbackAvailable": bool(handler.rollback_supported and (doc or {}).get("previous")),
            "driftDetected": bool(last and last.get("version") != current.version),
            "lastApplied": last,
            "warnings": handler.read_warnings(es, resource),
        }

    def previous(self, user: User, cluster_id: str, config_type: str, resource: str) -> dict:
        authorize(user, cluster_id, config_type, resource, "view")
        handler = get_handler(config_type)
        handler.check_resource(resource)
        doc, _ = self.snapshots.get(cluster_id, config_type, resource)
        if not doc or not doc.get("previous"):
            raise not_found("NO_SNAPSHOT", "No stored snapshot for this resource")
        prev = doc["previous"]
        return {"clusterId": cluster_id, "configType": config_type, "resource": resource,
                "rollbackSupported": handler.rollback_supported, **prev}

    # ----------------------------------------------------------------- writes
    def update(self, user: User, meta: RequestMeta, cluster_id: str, config_type: str,
               resource: str, config: Any, reason: str | None, sample_docs: list | None,
               dry_run: bool, force: bool, if_match: str | None) -> dict:
        return self._change("UPDATE", user, meta, cluster_id, config_type, resource,
                            config=config, reason=reason, sample_docs=sample_docs,
                            dry_run=dry_run, force=force, if_match=if_match)

    def rollback(self, user: User, meta: RequestMeta, cluster_id: str, config_type: str,
                 resource: str, reason: str | None, dry_run: bool, force: bool,
                 if_match: str | None) -> dict:
        return self._change("ROLLBACK", user, meta, cluster_id, config_type, resource,
                            config=None, reason=reason, sample_docs=None,
                            dry_run=dry_run, force=force, if_match=if_match)

    def restore(self, user: User, meta: RequestMeta, cluster_id: str, config_type: str,
                resource: str, restore_of: str, reason: str | None, dry_run: bool, force: bool,
                if_match: str | None) -> dict:
        """Put a resource back to the state it had just before change `restore_of` (any past
        change, from the audit log). Later changes to it are undone too; the diff shows them."""
        return self._change("RESTORE", user, meta, cluster_id, config_type, resource,
                            config=None, reason=reason, sample_docs=None, dry_run=dry_run,
                            force=force, if_match=if_match, restore_of=restore_of)

    def change_history(self, user: User, cluster_id: str, config_type: str, resource: str) -> dict:
        authorize(user, cluster_id, config_type, resource, "view")
        handler = get_handler(config_type)
        handler.check_resource(resource)
        items = [{k: e.get(k) for k in ("changeId", "action", "at", "by", "reason", "beforeVersion",
                                          "afterVersion", "restoreOf")}
                 | {"createdResource": not (e.get("before") or {}).get("exists"),
                    "restorable": handler.rollback_supported}
                 for e in self.history.list(cluster_id, config_type, resource)]
        return {"clusterId": cluster_id, "configType": config_type, "resource": resource, "items": items}

    # --------------------------------------------------------------- internals
    def _change(self, action: str, user: User, meta: RequestMeta, cluster_id: str,
                config_type: str, resource: str, *, config: Any, reason: str | None,
                sample_docs: list | None, dry_run: bool, force: bool,
                if_match: str | None, restore_of: str | None = None) -> dict:
        change_id = new_id()
        audit_base = {
            "changeId": change_id, "action": "DRY_RUN" if dry_run else action,
            "requestedAction": action, "actor": user.username, "sourceIp": meta.source_ip,
            "requestId": meta.request_id, "clusterId": cluster_id, "configType": config_type,
            "resource": resource, "reason": reason, "forced": force,
            **({"restoreOf": restore_of} if restore_of else {}),
        }
        token = None
        ctx: dict[str, Any] = {}
        try:
            handler = get_handler(config_type)
            handler.check_resource(resource)
            self.registry.get(cluster_id)
            authorize(user, cluster_id, config_type, resource, "edit")
            if not dry_run and not (reason and reason.strip()):
                raise bad_request("REASON_REQUIRED", "Give a 'reason' for the change (it is audited)")
            if action in ("ROLLBACK", "RESTORE") and not handler.rollback_supported:
                raise unprocessable("ROLLBACK_NOT_SUPPORTED",
                                    f"{handler.label} changes are permanent and cannot be rolled back")
            es = self.registry.client(cluster_id)

            if not dry_run:
                token = self.locks.acquire(cluster_id, config_type, resource, user.username)

            doc, etag = self.snapshots.get(cluster_id, config_type, resource)
            current = handler.fetch(es, resource)
            doc, etag = self._resolve_pending(doc, etag, current, cluster_id, config_type,
                                              resource, persist=not dry_run)

            version_before = current.version
            ctx["versionBefore"] = version_before
            if if_match and if_match.strip('"') != version_before:
                raise precondition_failed("VERSION_MISMATCH",
                                          "The config changed since you read it; GET it again",
                                          {"expected": if_match, "current": version_before})

            warnings: list[str] = []
            last = (doc or {}).get("lastApplied")
            drift = bool(last and last.get("version") != version_before)
            ctx["driftDetected"] = drift
            if drift:
                msg = ("Drift: this config was changed outside the API since "
                       f"{last.get('by')} applied it at {last.get('at')}")
                if action == "ROLLBACK" and not force:
                    raise conflict("DRIFT_DETECTED", msg + ". Rolling back would wipe that change; "
                                   "re-run with force=true if you are sure",
                                   {"lastAppliedVersion": last.get("version"),
                                    "liveVersion": version_before})
                warnings.append(msg)

            health = es_call(es, "GET", "/_cluster/health").get("status")
            ctx["clusterHealth"] = health
            if health == "red" and not force:
                raise conflict("CLUSTER_UNHEALTHY", "Cluster health is red; re-run with force=true "
                               "to change config anyway")
            if health in ("yellow", "red"):
                warnings.append(f"Cluster health is {health}")

            # -- target state
            if action == "UPDATE":
                if current.exists is False and handler.resource_kind == "index":
                    raise not_found("INDEX_NOT_FOUND", f"Index '{resource}' does not exist")
                plan = handler.plan_update(es, resource, current, config)
            elif action == "RESTORE":
                if current.exists is False and handler.resource_kind == "index":
                    raise not_found("INDEX_NOT_FOUND", f"Index '{resource}' does not exist any more")
                entry = self.history.get(cluster_id, config_type, resource, restore_of or "")
                prev = (doc or {}).get("previous")
                if entry:
                    before = entry["before"]
                    if entry.get("afterVersion") and entry["afterVersion"] != version_before:
                        warnings.append("This resource changed again after that change (later changes "
                                        "or edits outside the API). Restoring the state from before "
                                        "it undoes those too: check the diff")
                elif prev and prev.get("changeId") == restore_of:
                    before = prev["state"]   # made before change history was kept: the snapshot has it
                else:
                    raise not_found("CHANGE_NOT_FOUND",
                                    "No saved state for that change (it may be older than change "
                                    "history, or not a change to this resource)")
                ctx["restoreOf"] = restore_of
                plan = Plan(target=State.from_dict(before))
            else:
                prev = (doc or {}).get("previous")
                if not prev:
                    raise not_found("NO_SNAPSHOT", "Nothing to roll back to for this resource")
                plan = Plan(target=State.from_dict(prev["state"]))
            warnings = plan.warnings + warnings + handler.read_warnings(es, resource)
            target = plan.target

            change_diff = diff(current.config if current.exists else None,
                               target.config if target.exists else None)
            ctx["diff"] = change_diff
            changed_paths = sorted({i["path"] for part in change_diff.values() for i in part})

            # -- allowlist
            if not diff_is_empty(change_diff) or current.exists != target.exists:
                names = handler.allowlist_names(resource, changed_paths)
                blocked, source = self.allowlist.blocked(cluster_id, config_type, names)
                if blocked:
                    raise forbidden("NOT_ALLOWLISTED",
                                    f"Not on the {source} allowlist for {config_type}",
                                    {"blocked": blocked, "allowlist": source})
                self.allowlist.check_scope(cluster_id, config_type, resource, target.config
                                           if target.exists else None)

            result = {
                "changeId": change_id, "action": action, "dryRun": dry_run,
                "clusterId": cluster_id, "configType": config_type, "resource": resource,
                "versionBefore": version_before, "diff": change_diff,
                "createsResource": not current.exists and target.exists,
                "deletesResource": current.exists and not target.exists,
                "warnings": warnings, "permanent": plan.permanent,
                "driftDetected": drift, "clusterHealth": health,
            }

            no_change = diff_is_empty(change_diff) and current.exists == target.exists
            if no_change:
                result.update(applied=False, noChange=True, versionAfter=version_before,
                              rollbackAvailable=bool(handler.rollback_supported and (doc or {}).get("previous")))
                self.audit.write({**audit_base, "outcome": "NO_CHANGE", **ctx})
                return result

            if dry_run:
                sim = None
                try:
                    sim = handler.simulate(es, resource, target, sample_docs)
                except ApiError as e:
                    result["valid"] = False
                    result["errors"] = [e.to_dict()["error"]]
                result.update(applied=False, simulation=sim,
                              valid=result.get("valid", True),
                              rollbackAvailableAfter=handler.rollback_supported)
                self.audit.write({**audit_base, "outcome": "SUCCESS", **ctx})
                return result

            # -- 1) pending snapshot
            self.locks.verify(cluster_id, config_type, resource, token)
            pending = {"changeId": change_id, "action": action, "by": user.username,
                       "at": iso(), "atEpoch": time.time(), "beforeVersion": version_before,
                       "state": current.to_dict()}
            new_doc = dict(doc or {})
            new_doc["pending"] = pending
            etag = self.snapshots.put(cluster_id, config_type, resource, new_doc, etag)

            # -- 2) apply
            apply_error: ApiError | None = None
            try:
                handler.apply(es, resource, current, target, plan)
            except ApiError as e:
                apply_error = e

            # -- 3) what does the cluster hold now?
            try:
                after = handler.fetch(es, resource)
            except ApiError:
                # Can't tell whether ES changed: keep 'pending' so the next request decides.
                raise apply_error or ApiError(502, "CLUSTER_UNREACHABLE",
                                              "Change sent but the result could not be read back")
            self.locks.verify(cluster_id, config_type, resource, token)

            if after.version == version_before:
                # Nothing changed in ES (rejected, or ES normalised the request to what it
                # already had): drop 'pending', keep the existing rollback target.
                new_doc.pop("pending", None)
                self.snapshots.put(cluster_id, config_type, resource, new_doc, etag)
                if apply_error:
                    raise apply_error
                result.update(applied=False, noChange=True, versionAfter=version_before,
                              rollbackAvailable=bool(handler.rollback_supported
                                                     and new_doc.get("previous")))
                self.audit.write({**audit_base, "outcome": "NO_CHANGE", **ctx})
                return result

            # -- 4) promote: the state we replaced becomes the rollback target
            final = {
                "previous": {"state": current.to_dict(), "version": version_before,
                             "capturedAt": pending["at"], "capturedBy": user.username,
                             "changeId": change_id, "replacedBy": action},
                "lastApplied": {"version": after.version, "at": iso(), "by": user.username,
                                "changeId": change_id, "action": action},
            }
            self.snapshots.put(cluster_id, config_type, resource, final, etag)
            snap_key = SnapshotRepo.key(cluster_id, config_type, resource)
            # -- 5) change history: the state before this change, so it can be undone later
            try:
                self.history.put({
                    "changeId": change_id, "action": action, "clusterId": cluster_id,
                    "configType": config_type, "resource": resource, "by": user.username,
                    "at": pending["at"], "reason": reason, "before": current.to_dict(),
                    "beforeVersion": version_before, "afterVersion": after.version,
                    **({"restoreOf": restore_of} if restore_of else {})})
                ctx["restorable"] = handler.rollback_supported
            except Exception:  # the change is applied; a missing history entry must not fail it
                log.exception("Could not save change history for %s %s %s", cluster_id, config_type, resource)
            if apply_error:
                result["warnings"] = result["warnings"] + [
                    f"Elasticsearch returned an error ({apply_error.message}) but the config did "
                    "change; check the result below"]
            if after.version != target.version and not plan.permanent:
                result["warnings"] = result["warnings"] + [
                    "Elasticsearch stored a normalised form of the requested config"]
            result.update(applied=True, versionAfter=after.version, config=after.config,
                          rollbackAvailable=handler.rollback_supported, snapshotKey=snap_key)
            self.audit.write({**audit_base, "outcome": "SUCCESS", **ctx,
                              "versionAfter": after.version, "snapshotKey": snap_key,
                              **({"esError": apply_error.to_dict()["error"]} if apply_error else {})})
            return result

        except ApiError as e:
            outcome = "REJECTED" if e.status < 500 and e.code not in ("ES_REJECTED",) else "FAILED"
            self.audit.write({**audit_base, "outcome": outcome,
                              "error": {"status": e.status, "code": e.code, "message": e.message},
                              **ctx})
            raise
        finally:
            if token:
                try:
                    self.locks.release(cluster_id, config_type, resource, token)
                except Exception:
                    pass  # expires on its own

    def _resolve_pending(self, doc: dict | None, etag: str | None, live: State, cluster_id: str,
                         config_type: str, resource: str, persist: bool):
        """Finish or discard a change interrupted between 'pending' and 'promote'."""
        if not doc or not doc.get("pending"):
            return doc, etag
        pending = doc["pending"]
        age = time.time() - float(pending.get("atEpoch", 0))
        if age < self.locks.ttl:
            raise conflict("CHANGE_IN_PROGRESS",
                           f"{pending.get('by')} started a change {int(age)}s ago that has not "
                           "finished; retry shortly", {"lockedBy": pending.get("by")})
        new_doc = {k: v for k, v in doc.items() if k != "pending"}
        if live.version != pending.get("beforeVersion"):
            # ES did change: the pending state is the real previous config.
            new_doc["previous"] = {"state": pending["state"], "version": pending["beforeVersion"],
                                   "capturedAt": pending["at"], "capturedBy": pending["by"],
                                   "changeId": pending["changeId"], "replacedBy": pending["action"],
                                   "recovered": True}
            new_doc["lastApplied"] = {"version": live.version, "at": iso(), "by": pending["by"],
                                      "changeId": pending["changeId"], "action": pending["action"],
                                      "recovered": True}
        if persist:
            etag = self.snapshots.put(cluster_id, config_type, resource, new_doc, etag)
        return new_doc, etag
