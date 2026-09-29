"""Everything the API keeps in S3: users, allowlists, snapshots, locks, audit events."""
from __future__ import annotations

import json
import logging
import time
from datetime import date
from typing import Any

from .errors import ApiError, bad_request, conflict, forbidden, not_found
from .storage import ObjectStore, PreconditionFailed
from .util import iso, matches_any, new_id, utcnow

log = logging.getLogger("es_config_api.audit")

LEVELS = ("view", "edit", "delete")   # each level includes the ones before it

CONFIG_TYPES = (
    "cluster-settings", "index-settings", "index-mappings", "index-templates",
    "component-templates", "ilm-policies", "ingest-pipelines", "index-delete",
)


def _update_with_retry(store: ObjectStore, key: str, mutate, default: dict, attempts: int = 5):
    """Read-modify-write with S3 conditional writes; retries on concurrent writers."""
    for _ in range(attempts):
        data, etag = store.get_json(key)
        current = data if data is not None else json.loads(json.dumps(default))
        result = mutate(current)
        try:
            if etag:
                store.put_json(key, current, if_match=etag)
            else:
                store.put_json(key, current, if_none_match=True)
            return result
        except PreconditionFailed:
            time.sleep(0.05)
    raise conflict("CONCURRENT_UPDATE", f"'{key}' kept changing; please retry")


# --------------------------------------------------------------------------- users
class UsersRepo:
    KEY = "state/users.json"
    SECRET_FIELDS = ("passwordHash",)

    def __init__(self, store: ObjectStore, bootstrap_admins: list[str]):
        self.store = store
        self.bootstrap_admins = set(bootstrap_admins)

    def _all(self) -> dict[str, dict]:
        data, _ = self.store.get_json(self.KEY)
        return (data or {}).get("users", {})

    def _full(self, username: str, rec: dict | None) -> dict | None:
        if rec is None and username not in self.bootstrap_admins:
            return None
        rec = dict(rec or {"admin": False, "clusters": {}})
        rec["username"] = username
        rec["bootstrap"] = username in self.bootstrap_admins
        if rec["bootstrap"]:
            rec["admin"] = True
        return rec

    @classmethod
    def public(cls, rec: dict | None) -> dict | None:
        """A user record safe to return from the API: no password hash."""
        if rec is None:
            return None
        out = {k: v for k, v in rec.items() if k not in cls.SECRET_FIELDS}
        out["hasPassword"] = bool(rec.get("passwordHash"))
        out["usingGeneratedPassword"] = bool(rec.get("passwordHash") and rec.get("passwordGenerated"))
        return out

    def list(self) -> list[dict]:
        users = self._all()
        names = sorted(set(users) | self.bootstrap_admins)
        return [self.public(self._full(n, users.get(n))) for n in names]

    def get(self, username: str) -> dict | None:
        return self.public(self._full(username, self._all().get(username)))

    def get_auth(self, username: str) -> dict | None:
        """Full record, including the password hash. Never return this from the API."""
        return self._full(username, self._all().get(username))

    def exists(self, username: str) -> bool:
        return username in self._all()

    def put(self, username: str, admin: bool, clusters: dict[str, str], by: str,
            password_hash: str | None = None, generated: bool = False) -> dict:
        """Create or update admin flag + permissions. Password fields are kept unless
        a new hash is given (used when creating a user)."""
        def mutate(doc):
            prev = doc["users"].get(username)
            rec = dict(prev or {"createdAt": iso(), "createdBy": by, "tokenVersion": 0})
            rec.update({"admin": bool(admin), "clusters": clusters, "updatedAt": iso(),
                        "updatedBy": by})
            if password_hash:
                rec.update(passwordHash=password_hash, passwordGenerated=generated,
                           passwordSetAt=iso(), failedLogins=0, lockedUntil=None,
                           tokenVersion=int(rec.get("tokenVersion", 0)) + 1)
            doc["users"][username] = rec
            return prev
        prev = _update_with_retry(self.store, self.KEY, mutate, {"users": {}})
        return {"before": self.public(prev), "after": self.get(username)}

    def set_password(self, username: str, password_hash: str, generated: bool, by: str) -> int:
        """Set a password; bumps tokenVersion so every older session stops working."""
        def mutate(doc):
            rec = doc["users"].get(username)
            if rec is None:
                if username not in self.bootstrap_admins:
                    raise not_found("USER_NOT_FOUND", f"Unknown user '{username}'")
                rec = {"admin": True, "clusters": {}, "createdAt": iso(), "tokenVersion": 0}
            version = int(rec.get("tokenVersion", 0)) + 1
            rec.update(passwordHash=password_hash, passwordGenerated=generated,
                       passwordSetAt=iso(), passwordSetBy=by, failedLogins=0,
                       lockedUntil=None, tokenVersion=version)
            doc["users"][username] = rec
            return version
        return _update_with_retry(self.store, self.KEY, mutate, {"users": {}})

    def bump_token_version(self, username: str) -> int:
        def mutate(doc):
            rec = doc["users"].get(username)
            if rec is None:
                raise not_found("USER_NOT_FOUND", f"Unknown user '{username}'")
            rec["tokenVersion"] = int(rec.get("tokenVersion", 0)) + 1
            return rec["tokenVersion"]
        return _update_with_retry(self.store, self.KEY, mutate, {"users": {}})

    def record_login(self, username: str, success: bool, max_attempts: int,
                     lock_minutes: int) -> dict:
        """Count failures; lock after max_attempts. Returns {locked, lockedUntil, failed}."""
        def mutate(doc):
            rec = doc["users"].get(username)
            if rec is None:
                return {"locked": False, "lockedUntil": None, "failed": 0}
            if success:
                rec.update(failedLogins=0, lockedUntil=None, lastLoginAt=iso())
                return {"locked": False, "lockedUntil": None, "failed": 0}
            if rec.get("lockedUntil") and rec["lockedUntil"] < time.time():
                rec["lockedUntil"] = None
            failed = int(rec.get("failedLogins", 0)) + 1
            rec["failedLogins"] = failed
            if failed >= max_attempts:
                rec["lockedUntil"] = time.time() + lock_minutes * 60
                rec["failedLogins"] = 0
            locked = bool(rec.get("lockedUntil") and rec["lockedUntil"] > time.time())
            return {"locked": locked, "lockedUntil": rec.get("lockedUntil"), "failed": failed}
        return _update_with_retry(self.store, self.KEY, mutate, {"users": {}})

    def set_permissions(self, username: str, clusters: dict[str, str], by: str) -> dict:
        def mutate(doc):
            rec = doc["users"].get(username)
            if rec is None:
                if username not in self.bootstrap_admins:
                    raise not_found("USER_NOT_FOUND", f"Unknown user '{username}'")
                rec = {"admin": True, "clusters": {}, "createdAt": iso(), "tokenVersion": 0}
            before = dict(rec.get("clusters", {}))
            rec.update({"clusters": clusters, "updatedAt": iso(), "updatedBy": by})
            doc["users"][username] = rec
            return before
        before = _update_with_retry(self.store, self.KEY, mutate, {"users": {}})
        return {"before": before, "after": clusters}

    def delete(self, username: str) -> dict:
        if username in self.bootstrap_admins:
            raise bad_request("BOOTSTRAP_ADMIN", "Bootstrap admins are set by BOOTSTRAP_ADMINS "
                              "and cannot be deleted through the API")

        def mutate(doc):
            if username not in doc["users"]:
                raise not_found("USER_NOT_FOUND", f"Unknown user '{username}'")
            return doc["users"].pop(username)
        return self.public(_update_with_retry(self.store, self.KEY, mutate, {"users": {}}))


# ----------------------------------------------------------------------- allowlist
class AllowlistRepo:
    GLOBAL = "state/allowlist/global.json"

    def __init__(self, store: ObjectStore):
        self.store = store

    def _key(self, cluster_id: str | None) -> str:
        return self.GLOBAL if cluster_id is None else f"state/allowlist/{cluster_id}.json"

    # Extra rule keys some types accept, beyond allow / deny.
    EXTRA_KEYS = {"index-settings": {"indices"}, "index-templates": {"indexPatterns"}}

    @staticmethod
    def validate(doc: Any) -> dict:
        if not isinstance(doc, dict):
            raise bad_request("INVALID_ALLOWLIST", "Allowlist must be an object keyed by config type")
        clean: dict[str, dict] = {}
        for ctype, rule in doc.items():
            if ctype not in CONFIG_TYPES:
                raise bad_request("INVALID_ALLOWLIST", f"Unknown config type '{ctype}'",
                                  {"validTypes": list(CONFIG_TYPES)})
            allowed_keys = {"allow", "deny"} | AllowlistRepo.EXTRA_KEYS.get(ctype, set())
            if not isinstance(rule, dict) or set(rule) - allowed_keys:
                raise bad_request("INVALID_ALLOWLIST",
                                  f"'{ctype}' accepts only these keys: {sorted(allowed_keys)}")
            out: dict[str, list[str]] = {}
            for key in allowed_keys:
                if key not in rule:
                    continue
                patterns = rule[key]
                if not isinstance(patterns, list) or not all(
                        isinstance(p, str) and p.strip() for p in patterns):
                    raise bad_request("INVALID_ALLOWLIST",
                                      f"'{ctype}.{key}' must be a list of non-empty strings")
                out[key] = list(patterns)
            out.setdefault("allow", [])
            out.setdefault("deny", [])
            clean[ctype] = out
        return clean

    def get(self, cluster_id: str | None = None) -> dict | None:
        data, _ = self.store.get_json(self._key(cluster_id))
        return data

    def put(self, rules: dict, by: str, cluster_id: str | None = None) -> dict:
        clean = self.validate(rules)
        key = self._key(cluster_id)
        before, _ = self.store.get_json(key)
        self.store.put_json(key, {"rules": clean, "updatedAt": iso(), "updatedBy": by})
        return {"before": (before or {}).get("rules"), "after": clean}

    def delete(self, cluster_id: str) -> None:
        self.store.delete(self._key(cluster_id))

    def effective(self, cluster_id: str) -> tuple[dict, str]:
        override = self.get(cluster_id)
        if override is not None:
            return override.get("rules", {}), f"cluster:{cluster_id}"
        glob = self.get(None)
        return (glob or {}).get("rules", {}), "global"

    def blocked(self, cluster_id: str, config_type: str, names: list[str]) -> tuple[list[str], str]:
        rules, source = self.effective(cluster_id)
        rule = rules.get(config_type)
        if not rule:
            return list(names), source
        allow, deny = rule.get("allow", []), rule.get("deny", [])
        out = []
        for n in names:
            ok = matches_any(n, allow) and not matches_any(n, deny)
            if ok and config_type in ("index-mappings", "index-delete"):
                ok = _dot_ok(n, allow)
            if not ok:
                out.append(n)
        return out, source

    def check_scope(self, cluster_id: str, config_type: str, resource: str,
                    target_config: dict | None) -> None:
        """Second-level checks: which index a setting change hits, and which indices a
        template would apply to."""
        rules, source = self.effective(cluster_id)
        rule = rules.get(config_type) or {}
        if config_type == "index-settings":
            patterns = rule.get("indices", ["*"])
            if not (matches_any(resource, patterns) and _dot_ok(resource, patterns)):
                raise forbidden("NOT_ALLOWLISTED",
                                f"Index '{resource}' is not in the {source} allowlist's "
                                "index-settings.indices (dot/system indices need an explicit "
                                "pattern starting with '.')",
                                {"blocked": [resource], "allowlist": source})
        if config_type == "index-templates" and target_config:
            wanted = target_config.get("index_patterns") or []
            wanted = [wanted] if isinstance(wanted, str) else list(wanted)
            allowed = rule.get("indexPatterns")
            if allowed is None:
                bad = [p for p in wanted if p.strip() in ("*", "**") or p.startswith(".")]
                reason = ("Templates matching every index or dot/system indices need "
                          "index-templates.indexPatterns in the allowlist")
            else:
                bad = [p for p in wanted if not matches_any(p, allowed)]
                reason = f"index_patterns not covered by the {source} allowlist's indexPatterns"
            if bad:
                raise forbidden("NOT_ALLOWLISTED", reason, {"blocked": bad, "allowlist": source})


def _dot_ok(name: str, patterns: list[str]) -> bool:
    """Dot (system/hidden) indices only match patterns that themselves start with '.'."""
    return not name.startswith(".") or any(
        p.startswith(".") and matches_any(name, [p]) for p in patterns)


# ----------------------------------------------------------------------- snapshots
class SnapshotRepo:
    """One object per resource: previous config, pending change, last applied hash."""

    def __init__(self, store: ObjectStore):
        self.store = store

    @staticmethod
    def key(cluster_id: str, config_type: str, resource: str) -> str:
        return f"snapshots/{cluster_id}/{config_type}/{resource}.json"

    def get(self, cluster_id: str, config_type: str, resource: str) -> tuple[dict | None, str | None]:
        return self.store.get_json(self.key(cluster_id, config_type, resource))

    def put(self, cluster_id: str, config_type: str, resource: str, doc: dict,
            etag: str | None) -> str:
        key = self.key(cluster_id, config_type, resource)
        try:
            if etag:
                return self.store.put_json(key, doc, if_match=etag)
            return self.store.put_json(key, doc, if_none_match=True)
        except PreconditionFailed as e:
            raise conflict("CONCURRENT_CHANGE",
                           "The snapshot changed while this request was running; retry") from e


# --------------------------------------------------------------------------- locks
class LockRepo:
    def __init__(self, store: ObjectStore, ttl_seconds: int):
        self.store = store
        self.ttl = ttl_seconds

    @staticmethod
    def key(cluster_id: str, config_type: str, resource: str) -> str:
        return f"locks/{cluster_id}/{config_type}/{resource}.lock"

    def acquire(self, cluster_id: str, config_type: str, resource: str, owner: str) -> str:
        key = self.key(cluster_id, config_type, resource)
        token = new_id()
        body = {"owner": owner, "token": token, "acquiredAt": iso(),
                "expiresAtEpoch": time.time() + self.ttl}
        try:
            self.store.put_json(key, body, if_none_match=True)
            return token
        except PreconditionFailed:
            pass
        existing, etag = self.store.get_json(key)
        if existing is None:  # released in between
            try:
                self.store.put_json(key, body, if_none_match=True)
                return token
            except PreconditionFailed:
                existing, etag = self.store.get_json(key)
        if existing and existing.get("expiresAtEpoch", 0) < time.time():
            try:  # take over an expired lock
                self.store.put_json(key, body, if_match=etag)
                return token
            except PreconditionFailed:
                existing, _ = self.store.get_json(key)
        owner_now = (existing or {}).get("owner", "another request")
        raise conflict("CHANGE_IN_PROGRESS",
                       f"{owner_now} is changing this resource right now; retry shortly",
                       {"lockedBy": owner_now, "since": (existing or {}).get("acquiredAt")})

    def verify(self, cluster_id: str, config_type: str, resource: str, token: str | None) -> None:
        """Abort if our lock expired and someone else took it."""
        existing, _ = self.store.get_json(self.key(cluster_id, config_type, resource))
        if not existing or existing.get("token") != token:
            raise conflict("LOCK_LOST", "This change took longer than LOCK_TTL_SECONDS and its "
                           "lock was taken over; check the resource and retry")

    def release(self, cluster_id: str, config_type: str, resource: str, token: str) -> None:
        key = self.key(cluster_id, config_type, resource)
        existing, _ = self.store.get_json(key)
        if existing and existing.get("token") == token:
            self.store.delete(key)


# --------------------------------------------------------------------------- audit
class AuditRepo:
    """Append-only: one object per event, under audit/yyyy/mm/dd/."""

    def __init__(self, store: ObjectStore):
        self.store = store

    def write(self, event: dict) -> dict:
        now = utcnow()
        event = {"eventId": new_id(), "timestamp": iso(now), **event}
        key = f"audit/{now:%Y/%m/%d}/{now:%Y%m%dT%H%M%S%f}_{event['eventId']}.json"
        event["auditKey"] = key
        log.info(json.dumps(event, default=str))
        try:
            self.store.put_json(key, event, if_none_match=True)
        except Exception:  # never lose the request result because audit storage hiccuped
            log.exception("failed to persist audit event %s", event["eventId"])
        return event

    def query(self, day: date, cluster_id: str | None = None, user: str | None = None,
              action: str | None = None, limit: int = 500) -> list[dict]:
        keys = self.store.list_keys(f"audit/{day:%Y/%m/%d}/")
        out: list[dict] = []
        for key in reversed(keys):  # newest first
            ev, _ = self.store.get_json(key)
            if not ev:
                continue
            if cluster_id and ev.get("clusterId") != cluster_id:
                continue
            if user and ev.get("actor") != user:
                continue
            if action and ev.get("action") != action:
                continue
            out.append(ev)
            if len(out) >= limit:
                break
        return out


def validate_permissions(clusters: Any, known: set[str]) -> dict[str, str]:
    if not isinstance(clusters, dict):
        raise bad_request("INVALID_PERMISSIONS", "Permissions must be {clusterId: 'view'|'edit'}")
    unknown = sorted(set(clusters) - known - {"*"})
    if unknown:
        raise bad_request("UNKNOWN_CLUSTER", f"Unknown cluster ids: {unknown}",
                          {"knownClusters": sorted(known)})
    bad = {k: v for k, v in clusters.items() if v not in LEVELS}
    if bad:
        raise bad_request("INVALID_PERMISSIONS", "Permission level must be 'view', 'edit' or 'delete'", bad)
    return dict(clusters)


__all__ = ["UsersRepo", "AllowlistRepo", "SnapshotRepo", "LockRepo", "AuditRepo",
           "validate_permissions", "CONFIG_TYPES", "LEVELS", "ApiError"]
