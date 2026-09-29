"""Small helpers: hashing, flattening, diffing, glob matching, time."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Any, Iterable


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def config_hash(exists: bool, config: dict | None) -> str:
    """Version of a config: stable hash of its normalised content."""
    payload = {"exists": bool(exists), "config": config if exists else None}
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()[:16]


def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts into dotted keys. Lists are treated as leaf values."""
    out: dict[str, Any] = {}
    if isinstance(obj, dict) and obj:
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict) and value:
                out.update(flatten(value, path))
            else:
                out[path] = value
    elif prefix:
        out[prefix] = obj
    return out


def diff(before: dict | None, after: dict | None) -> dict[str, list]:
    """Key-level diff of two (nested) configs."""
    b = flatten(before or {})
    a = flatten(after or {})
    added = [{"path": k, "after": a[k]} for k in sorted(a.keys() - b.keys())]
    removed = [{"path": k, "before": b[k]} for k in sorted(b.keys() - a.keys())]
    changed = [
        {"path": k, "before": b[k], "after": a[k]}
        for k in sorted(a.keys() & b.keys())
        if canonical_json(a[k]) != canonical_json(b[k])
    ]
    return {"added": added, "removed": removed, "changed": changed}


def diff_is_empty(d: dict[str, list]) -> bool:
    return not (d["added"] or d["removed"] or d["changed"])


def matches_any(value: str, patterns: Iterable[str]) -> bool:
    return any(fnmatchcase(value, p) for p in patterns)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_id() -> str:
    return uuid.uuid4().hex
