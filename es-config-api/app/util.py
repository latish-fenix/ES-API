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


# --------------------------------------------------------- query DSL inspection
SCRIPT_KEYS = {"script", "script_fields", "runtime_mappings", "scripted_metric", "script_score"}
_FIELD_QUERIES = {"term", "terms", "match", "match_phrase", "match_phrase_prefix", "match_bool_prefix", "range",
                  "prefix", "wildcard", "regexp", "fuzzy", "term_set", "intervals", "geo_distance",
                  "geo_bounding_box", "geo_shape", "span_term"}
_NOT_FIELDS = {"boost", "_name", "minimum_should_match", "analyzer", "format", "time_zone", "relation",
               "case_insensitive", "rewrite", "distance", "distance_type", "validation_method", "ignore_unmapped"}


def script_paths(obj: Any, path: str = "") -> list[str]:
    """Where a request body uses a script (painless), e.g. ['aggs.x.scripted_metric']."""
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if k in SCRIPT_KEYS:
                out.append(p)
            else:
                out += script_paths(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += script_paths(v, f"{path}[{i}]")
    return out


def index_refs(obj: Any, path: str = "") -> list[str]:
    """Where a query body names another index to fetch documents from (terms lookup, more_like_this
    documents, indexed shapes, percolate by id): any "index" / "_index" key."""
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if k in ("index", "_index") and isinstance(v, (str, list)):
                out.append(p)
            else:
                out += index_refs(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += index_refs(v, f"{path}[{i}]")
    return out


def dsl_names(obj: Any) -> dict[str, list[str]]:
    """Field and aggregation names used in a query DSL body, never the values (for the audit log)."""
    fields: set[str] = set()
    aggs: set[str] = set()

    def walk(o: Any, parent: str | None = None) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if parent in _FIELD_QUERIES and k not in _NOT_FIELDS:
                    fields.add(str(k))
                if k in ("field", "fields", "default_field", "path", "_source") and isinstance(v, (str, list)):
                    for f in ([v] if isinstance(v, str) else v):
                        if isinstance(f, str):
                            fields.add(f)
                if k == "sort":
                    for s in (v if isinstance(v, list) else [v]):
                        if isinstance(s, str):
                            fields.add(s)
                        elif isinstance(s, dict):
                            fields.update(str(x) for x in s)
                if k in ("aggs", "aggregations") and isinstance(v, dict):
                    for name, spec in v.items():
                        kinds = [t for t in (spec or {}) if t not in ("aggs", "aggregations", "meta")] \
                            if isinstance(spec, dict) else []
                        aggs.add(f"{name}:{kinds[0]}" if kinds else str(name))
                        if isinstance(spec, dict):
                            for t, tv in spec.items():
                                walk({t: tv} if t in ("aggs", "aggregations") else tv, None)
                    continue
                if k == "query_string" or k == "simple_query_string":
                    fields.add("(query string)")
                    if isinstance(v, dict):
                        walk({kk: vv for kk, vv in v.items() if kk != "query"}, k)
                    continue
                walk(v, k)
        elif isinstance(o, list):
            for v in o:
                walk(v, parent)
    walk(obj)
    return {"fields": sorted(fields)[:200], "aggs": sorted(aggs)[:100]}
