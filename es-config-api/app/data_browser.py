"""Read-only data browser: look at the documents in non-system indices.

* Anyone with `view` (or more) on a cluster can use it.
* Only non-system indices: no name or pattern starting with '.', wildcards never expand to
  dot or hidden indices, and hits from a dot index are dropped.
* Every search, document read and export is audited with the query, never the documents.
* Searches are capped: at most 100 rows a page, 10,000 rows deep (ES's result window), a 30 s
  timeout; exports at most 10,000 rows.
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field, field_validator

from .clusters import es_call
from .errors import ApiError, bad_request, forbidden, not_found
from .identity import User, require_cluster

MAX_PAGE = 100
MAX_WINDOW = 10_000
MAX_EXPORT = 10_000
SEARCH_TIMEOUT = "30s"
# Index names/patterns: letters, digits and . _ - + * : (for remote-free local names), no commas.
_TARGET_RE = re.compile(r"^[A-Za-z0-9*][A-Za-z0-9._\-+*:@]{0,254}$")
_METADATA = {"_id", "_index", "_source", "_routing", "_ignored", "_seq_no", "_version",
             "_primary_term", "_tier", "_field_names", "_doc_count", "_data_stream_timestamp",
             "_feature", "_nested_path", "_timestamp"}


# ------------------------------------------------------------------ request models
class Filter(BaseModel):
    field: str = Field(..., min_length=1, max_length=512)
    op: Literal["is", "is_not", "one_of", "not_one_of", "exists", "not_exists", "between",
                "contains"] = "is"
    value: Any = None
    values: list[Any] | None = Field(None, max_length=200)
    gte: Any = None
    lte: Any = None


class TimeRange(BaseModel):
    field: str = Field(..., min_length=1, max_length=512)
    gte: str | None = Field(None, description="ISO date/time or date math, e.g. now-24h")
    lte: str | None = None


class SortItem(BaseModel):
    field: str = Field(..., min_length=1, max_length=512)
    order: Literal["asc", "desc"] = "desc"
    unmappedType: str | None = Field(None, max_length=32,
                                     description="Field type, for patterns where some indices lack it")


class SearchBody(BaseModel):
    query: str = Field("", max_length=10_000,
                       description="Lucene query string, e.g. vendor:2593 AND order_info.order_number:SP0286*")
    filters: list[Filter] = Field(default_factory=list, max_length=50)
    timeRange: TimeRange | None = None
    sort: list[SortItem] = Field(default_factory=list, max_length=5)
    from_: int = Field(0, ge=0, alias="from")
    size: int = Field(25, ge=0, le=MAX_PAGE)

    model_config = {"populate_by_name": True, "json_schema_extra": {"examples": [{
        "query": "vendor:2593", "filters": [{"field": "order_info.order_number", "op": "contains", "value": "SP0286"}],
        "timeRange": {"field": "created_date", "gte": "now-7d"},
        "sort": [{"field": "created_date", "order": "desc"}], "from": 0, "size": 25}]}}


class ExportBody(SearchBody):
    format: Literal["csv", "json", "ndjson"] = "csv"
    columns: list[str] = Field(default_factory=list, max_length=200,
                               description="CSV columns (dotted field paths). Empty = every field found")
    limit: int = Field(MAX_EXPORT, ge=1, le=MAX_EXPORT)

    @field_validator("columns")
    @classmethod
    def _cols(cls, v: list[str]) -> list[str]:
        return [c for c in (x.strip() for x in v) if c][:200]


# ------------------------------------------------------------------ helpers
def check_target(target: str) -> str:
    """A concrete index, alias or wildcard pattern; never a system index."""
    t = (target or "").strip()
    if t.startswith((".", "_", "-")):
        raise forbidden("SYSTEM_INDEX", "System and hidden indices (names starting with '.') "
                        "can't be browsed")
    if not t or not _TARGET_RE.match(t):
        raise bad_request("INVALID_INDEX_NAME",
                          "Give one index name, alias or pattern (e.g. orders-2024.* ), without commas")
    return t


def _expr(target: str) -> str:
    # Never let a wildcard reach dot indices (even non-hidden ones like .kibana_1).
    return f"{target},-.*" if "*" in target else target


def _search_params() -> dict:
    return {"expand_wildcards": "open", "ignore_unavailable": "true", "allow_no_indices": "true"}


def _nested_wrap(field: str, query: dict, nested_paths: Iterable[str]) -> dict:
    """Wrap a query on a field inside nested objects, innermost path first."""
    paths = sorted((p for p in nested_paths if field.startswith(p + ".")), key=len)
    for p in reversed(paths):
        query = {"nested": {"path": p, "query": query, "ignore_unmapped": True}}
    return query


def _escape_wildcard(v: str) -> str:
    return re.sub(r"([*?\\])", r"\\\1", v)


def _filter_clause(f: Filter, nested: Iterable[str]) -> tuple[str, dict]:
    """(bool occurrence, clause) for one filter."""
    fld = f.field.strip()
    if f.op in ("is", "is_not"):
        if f.value is None or f.value == "":
            raise bad_request("INVALID_FILTER", f"Filter on '{fld}' needs a value")
        q = {"match_phrase": {fld: f.value}}
    elif f.op in ("one_of", "not_one_of"):
        vals = [v for v in (f.values or []) if v not in (None, "")]
        if not vals:
            raise bad_request("INVALID_FILTER", f"Filter on '{fld}' needs at least one value")
        q = {"bool": {"should": [{"match_phrase": {fld: v}} for v in vals], "minimum_should_match": 1}}
    elif f.op in ("exists", "not_exists"):
        q = {"exists": {"field": fld}}
    elif f.op == "between":
        rng = {k: v for k, v in (("gte", f.gte), ("lte", f.lte)) if v not in (None, "")}
        if not rng:
            raise bad_request("INVALID_FILTER", f"Range filter on '{fld}' needs a from and/or to value")
        q = {"range": {fld: rng}}
    else:  # contains
        if f.value in (None, ""):
            raise bad_request("INVALID_FILTER", f"Filter on '{fld}' needs a value")
        q = {"wildcard": {fld: {"value": f"*{_escape_wildcard(str(f.value))}*",
                                "case_insensitive": True}}}
    q = _nested_wrap(fld, q, nested)
    negative = f.op in ("is_not", "not_one_of", "not_exists")
    return ("must_not" if negative else "filter"), q


def build_query(body: SearchBody, nested: Iterable[str] = ()) -> dict:
    nested = list(nested)
    must: list[dict] = []
    filt: list[dict] = []
    must_not: list[dict] = []
    if body.query.strip():
        must.append({"query_string": {"query": body.query.strip(), "default_operator": "AND",
                                      "analyze_wildcard": True, "lenient": True}})
    for f in body.filters:
        occ, clause = _filter_clause(f, nested)
        (must_not if occ == "must_not" else filt).append(clause)
    if body.timeRange and (body.timeRange.gte or body.timeRange.lte):
        rng = {k: v for k, v in (("gte", body.timeRange.gte), ("lte", body.timeRange.lte)) if v}
        filt.append({"range": {body.timeRange.field.strip(): rng}})
    if not (must or filt or must_not):
        return {"match_all": {}}
    return {"bool": {k: v for k, v in (("must", must), ("filter", filt), ("must_not", must_not)) if v}}


def build_sort(items: list[SortItem]) -> list:
    out: list = []
    for s in items:
        spec: dict = {"order": s.order, "missing": "_last"}
        if s.unmappedType:
            spec["unmapped_type"] = s.unmappedType
        out.append({s.field.strip(): spec})
    return out


def _hit(h: dict) -> dict:
    return {"_index": h.get("_index"), "_id": h.get("_id"), "_score": h.get("_score"),
            "_source": h.get("_source") or {}, "sort": h.get("sort")}


def get_path(src: Any, path: str) -> Any:
    """Value at a dotted path; arrays of objects give a list; supports literal dotted keys."""
    if not isinstance(src, dict):
        return None
    if path in src:
        return src[path]
    if "." not in path:
        return None
    # try the longest literal key prefix first ("a.b" stored as a key)
    parts = path.split(".")
    for i in range(len(parts) - 1, 0, -1):
        key = ".".join(parts[:i])
        if key in src:
            sub = src[key]
            remainder = ".".join(parts[i:])
            if isinstance(sub, list):  # array of objects: collect the field from each
                vals = [get_path(x, remainder) for x in sub]
                vals = [v for v in vals if v is not None]
                return vals or None
            return get_path(sub, remainder)
    return None


def leaf_paths(src: Any, prefix: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(src, dict):
        for k, v in src.items():
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict) and v:
                out.extend(leaf_paths(v, p))
            else:
                out.append(p)
    return out


def csv_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        s = json.dumps(v, ensure_ascii=False, default=str)
    elif isinstance(v, bool):
        s = "true" if v else "false"
    else:
        s = str(v)
    # Keep spreadsheet apps from running a cell as a formula.
    if isinstance(v, str) and s[:1] in ("=", "+", "-", "@", "\t", "\r"):
        s = "'" + s
    return s


def _read(es, method: str, path: str, body: Any = None, params: dict | None = None) -> Any:
    """es_call, with a clear message when the service account lacks the `read` privilege."""
    try:
        return es_call(es, method, path, body=body, params=params)
    except ApiError as e:
        if e.code == "ES_AUTH_FAILED" and (e.details or {}).get("esStatus") == 403:
            raise ApiError(502, "ES_READ_NOT_ALLOWED",
                           "The API's Elasticsearch account may not read documents here. Add the "
                           "'read' index privilege to its role (see docs/es-lockdown.md). "
                           f"Elasticsearch said: {e.message}", e.details) from e
        raise


# ------------------------------------------------------------------ service
class DataBrowser:
    def __init__(self, registry, audit):
        self.registry = registry
        self.audit = audit

    # -- fields
    def fields(self, user: User, cluster_id: str, target: str) -> dict:
        require_cluster(user, cluster_id, "view")
        target = check_target(target)
        es = self.registry.client(cluster_id)
        caps = _read(es, "GET", f"/{_expr(target)}/_field_caps",
                     params={"fields": "*", **_search_params()})
        indices = [i for i in caps.get("indices", []) if not i.startswith(".")]
        if not indices:
            raise not_found("INDEX_NOT_FOUND", f"No index matches '{target}'")
        items, nested = [], []
        for name, types in sorted(caps.get("fields", {}).items()):
            if name in _METADATA or name.startswith("_") or any(
                    v.get("metadata_field") for v in types.values()):
                continue
            kinds = sorted(types)
            t = kinds[0] if len(kinds) == 1 else "conflict"
            info = types[kinds[0]]
            if t == "nested":
                nested.append(name)
            items.append({
                "name": name, "type": t, "types": kinds if t == "conflict" else None,
                "searchable": all(v.get("searchable") for v in types.values()),
                "aggregatable": all(v.get("aggregatable") for v in types.values()),
                "object": t in ("object", "nested"),
                "metadata": bool(info.get("metadata_field")),
            })
        return {"clusterId": cluster_id, "index": target, "indices": sorted(indices),
                "fields": items, "nestedPaths": nested,
                "dateFields": [f["name"] for f in items if f["type"] in ("date", "date_nanos")]}

    def _nested_paths(self, es, target: str) -> list[str]:
        try:
            caps = es_call(es, "GET", f"/{_expr(target)}/_field_caps",
                           params={"fields": "*", **_search_params()})
            return sorted(n for n, t in caps.get("fields", {}).items() if "nested" in t)
        except ApiError:
            return []

    def _audit(self, action: str, user: User, meta, cluster_id: str, target: str,
               body: SearchBody | None, outcome: str, **extra) -> None:
        ev: dict[str, Any] = {"action": action, "actor": user.username, "outcome": outcome,
                              "requestId": meta.request_id, "sourceIp": meta.source_ip,
                              "clusterId": cluster_id, "configType": "data", "resource": target}
        if body is not None:
            ev["search"] = {
                "query": body.query or None,
                "filters": [f.model_dump(exclude_none=True) for f in body.filters] or None,
                "timeRange": body.timeRange.model_dump(exclude_none=True) if body.timeRange else None,
                "sort": [s.model_dump(exclude_none=True) for s in body.sort] or None,
                "from": body.from_, "size": body.size,
            }
        ev.update(extra)
        self.audit.write(ev)

    def _run(self, es, target: str, body: SearchBody, size: int, frm: int) -> dict:
        req = {"query": build_query(body, self._nested_paths(es, target) if body.filters else ()),
               "from": frm, "size": size, "track_total_hits": True, "timeout": SEARCH_TIMEOUT}
        if body.sort:
            req["sort"] = build_sort(body.sort)
        return _read(es, "POST", f"/{_expr(target)}/_search", body=req, params=_search_params())

    # -- search
    def search(self, user: User, meta, cluster_id: str, target: str, body: SearchBody) -> dict:
        target_in = target
        try:
            require_cluster(user, cluster_id, "view")
            target = check_target(target)
            if body.from_ + body.size > MAX_WINDOW:
                raise bad_request("RESULT_WINDOW_EXCEEDED",
                                  f"Only the first {MAX_WINDOW:,} results can be paged through; "
                                  "narrow the search or change the sort")
            es = self.registry.client(cluster_id)
            r = self._run(es, target, body, body.size, body.from_)
        except ApiError as e:
            self._audit("DATA_SEARCH", user, meta, cluster_id, target_in, body,
                        "REJECTED" if e.status < 500 and e.code != "ES_REJECTED" else "FAILED",
                        error={"status": e.status, "code": e.code, "message": e.message})
            raise
        total = r.get("hits", {}).get("total", {}) or {}
        hits = [_hit(h) for h in r.get("hits", {}).get("hits", []) if not str(h.get("_index", "")).startswith(".")]
        shards = r.get("_shards", {}) or {}
        out = {
            "clusterId": cluster_id, "index": target,
            "total": total.get("value", 0), "totalRelation": total.get("relation", "eq"),
            "took": r.get("took"), "timedOut": bool(r.get("timed_out")),
            "from": body.from_, "size": body.size, "hits": hits,
            "shards": {"total": shards.get("total"), "failed": shards.get("failed", 0)},
            "maxWindow": MAX_WINDOW,
        }
        if shards.get("failed"):
            out["shardFailures"] = [
                (f.get("reason") or {}).get("reason") for f in shards.get("failures", [])[:5]]
        self._audit("DATA_SEARCH", user, meta, cluster_id, target, body, "SUCCESS",
                    hits=out["total"], took=out["took"])
        return out

    # -- one document
    def document(self, user: User, meta, cluster_id: str, index: str, doc_id: str) -> dict:
        try:
            require_cluster(user, cluster_id, "view")
            index = check_target(index)
            if "*" in index:
                raise bad_request("INVALID_INDEX_NAME", "Use the document's concrete _index")
            es = self.registry.client(cluster_id)
            try:
                r = _read(es, "GET", f"/{index}/_doc/{_quote(doc_id)}")
            except ApiError as e:
                if e.code == "ES_NOT_FOUND":
                    raise not_found("DOCUMENT_NOT_FOUND", f"No document '{doc_id}' in '{index}'") from e
                raise
            if str(r.get("_index", "")).startswith("."):
                raise forbidden("SYSTEM_INDEX", "System indices can't be browsed")
        except ApiError as e:
            self._audit("DATA_DOCUMENT", user, meta, cluster_id, index, None,
                        "REJECTED" if e.status < 500 else "FAILED", documentId=doc_id,
                        error={"status": e.status, "code": e.code, "message": e.message})
            raise
        self._audit("DATA_DOCUMENT", user, meta, cluster_id, r.get("_index", index), None,
                    "SUCCESS", documentId=doc_id)
        return {"_index": r.get("_index"), "_id": r.get("_id"), "_version": r.get("_version"),
                "_seq_no": r.get("_seq_no"), "_primary_term": r.get("_primary_term"),
                "_source": r.get("_source") or {}}

    # -- export
    def export(self, user: User, meta, cluster_id: str, target: str, body: ExportBody) -> tuple[bytes, str, str, int]:
        target_in = target
        try:
            require_cluster(user, cluster_id, "view")
            target = check_target(target)
            es = self.registry.client(cluster_id)
            limit = min(body.limit, MAX_EXPORT)
            r = self._run(es, target, body, limit, 0)
        except ApiError as e:
            self._audit("DATA_EXPORT", user, meta, cluster_id, target_in, body,
                        "REJECTED" if e.status < 500 and e.code != "ES_REJECTED" else "FAILED",
                        format=body.format,
                        error={"status": e.status, "code": e.code, "message": e.message})
            raise
        hits = [_hit(h) for h in r.get("hits", {}).get("hits", []) if not str(h.get("_index", "")).startswith(".")]
        total = (r.get("hits", {}).get("total") or {}).get("value", len(hits))
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", target).strip("_") or "export"
        stamp = date.today().isoformat()
        if body.format == "csv":
            cols = body.columns
            if not cols:
                seen: dict[str, None] = {}
                for h in hits:
                    for p in leaf_paths(h["_source"]):
                        seen.setdefault(p, None)
                cols = list(seen)
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["_index", "_id", *cols])
            for h in hits:
                w.writerow([h["_index"], h["_id"], *(csv_cell(get_path(h["_source"], c)) for c in cols)])
            data = ("﻿" + buf.getvalue()).encode("utf-8")  # BOM so Excel reads UTF-8
            media, name = "text/csv; charset=utf-8", f"{safe}-{stamp}.csv"
        elif body.format == "ndjson":
            data = "".join(json.dumps({"_index": h["_index"], "_id": h["_id"], "_source": h["_source"]},
                                      ensure_ascii=False, default=str) + "\n" for h in hits).encode("utf-8")
            media, name = "application/x-ndjson", f"{safe}-{stamp}.ndjson"
        else:
            data = json.dumps([{"_index": h["_index"], "_id": h["_id"], "_source": h["_source"]} for h in hits],
                              ensure_ascii=False, default=str, indent=1).encode("utf-8")
            media, name = "application/json", f"{safe}-{stamp}.json"
        self._audit("DATA_EXPORT", user, meta, cluster_id, target, body, "SUCCESS",
                    format=body.format, rows=len(hits), hits=total, columns=body.columns or None)
        return data, media, name, len(hits)


def _quote(doc_id: str) -> str:
    from urllib.parse import quote
    return quote(doc_id, safe="")
