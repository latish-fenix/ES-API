"""Shell: Dev Tools-style requests against a cluster, inside the console's access rules.

Reads (search with aggregations, count, SQL, mappings, _cat...) run at once for anyone who
can view the indices they touch. A pattern that reaches an index the user can't see is
refused (never silently narrowed), and so are scripts in non-admin requests.

Writes never go to Elasticsearch as typed. Each one is matched to the console feature that
already does it (config change, create / delete index, document edit, bulk change) and runs
through it: dry run first, the same permission checks, snapshots / backups for roll back,
and approval for non-admins. Anything else is refused.

Audit: SHELL_QUERY with method, path, indices and the field / aggregation names, never
values or results. Each user's last 50 requests are kept in S3 (shell-history/<user>.json),
visible only to them.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Literal
from urllib.parse import parse_qsl, quote, unquote

from pydantic import BaseModel, Field

from .clusters import NDJSON_HEADERS, es_call
from .data_browser import resolve_scope
from .errors import ApiError, bad_request, forbidden
from .identity import User, require_any_access, require_cluster
from .repos import _update_with_retry
from .util import dsl_names, index_refs, iso, script_paths

HISTORY_MAX = 50
HISTORY_BODY_MAX = 20_000
RESPONSE_MAX = 5 * 1024 * 1024
MAX_SIZE = 10_000


class ShellRequest(BaseModel):
    method: Literal["GET", "POST", "PUT", "DELETE", "HEAD"]
    path: str = Field(..., max_length=4000, description="As in Kibana Dev Tools, e.g. orders-*/_search?size=0")
    body: Any = Field(None, description="JSON body (an object), or NDJSON text for _msearch")
    dryRun: bool = Field(False, description="Writes only: show what would change")
    reason: str | None = Field(None, max_length=1000, description="Writes: required unless dryRun")
    confirm: str | None = Field(None, description="Deletes: the index name or document id")
    dryRunToken: str | None = Field(None, description="Bulk changes: from the dry run")
    expectedCount: int | None = Field(None, description="Bulk changes: the count from the dry run")

    model_config = {"json_schema_extra": {"examples": [{
        "method": "POST", "path": "shoppremiumoutlets.myshopify.com-shipment_summary-*/_search",
        "body": {"size": 0, "aggs": {"by_status": {"terms": {"field": "status"}}}}}]}}


# ------------------------------------------------------------------ parsing
def split_path(raw: str) -> tuple[list[str], dict[str, str]]:
    raw = (raw or "").strip()
    if raw.lower().startswith(("http://", "https://")):
        raise bad_request("INVALID_PATH", "Give the path only (e.g. my-index/_search), not a URL")
    path, _, qs = raw.partition("?")
    parts = [unquote(p) for p in path.strip("/").split("/") if p]
    params = dict(parse_qsl(qs, keep_blank_values=True))
    if any(p in (".", "..") for p in parts):
        raise bad_request("INVALID_PATH", "Invalid path")
    return parts, params


def _targets(t: str) -> list[str]:
    return [x for x in (s.strip() for s in t.split(",")) if x]


# reads on a target (index, alias or pattern): GET or POST, endpoint after the target
# reads on a target, by endpoint: which methods, and how many path segments after the target
# (None = any). Everything else on a target is refused (POST <index>/_alias/x would add an alias).
TARGET_READS: dict[str, tuple[set[str], int | None]] = {
    "_search": ({"GET", "POST"}, 0), "_count": ({"GET", "POST"}, 0), "_field_caps": ({"GET", "POST"}, 0),
    "_validate": ({"GET", "POST"}, 1), "_explain": ({"GET", "POST"}, 1), "_terms_enum": ({"GET", "POST"}, 0),
    "_eql": ({"GET", "POST"}, 1), "_msearch": ({"GET", "POST"}, 0), "_mget": ({"GET", "POST"}, 0),
    "_analyze": ({"GET", "POST"}, 0),
    "_doc": ({"GET"}, 1), "_source": ({"GET"}, 1), "_mapping": ({"GET"}, None), "_settings": ({"GET"}, None),
    "_alias": ({"GET"}, None), "_stats": ({"GET"}, None),
}
MSEARCH_HEADER_KEYS = {"index", "search_type", "preference", "routing", "request_cache", "allow_no_indices",
                       "ignore_unavailable", "expand_wildcards", "ccs_minimize_roundtrips"}
# reads without a target
CLUSTER_ANY = {("_cluster", "health"), ("_cat", "health"), ("_cat", "nodes")}
CAT_WITH_INDEX = {"indices", "count", "shards", "aliases", "segments", "recovery"}
CONFIG_READS = {"_index_template": "index-templates", "_component_template": "component-templates",
                "_ilm": "ilm-policies", "_ingest": "ingest-pipelines"}
READ_PARAMS_BLOCKED = {"scroll", "pre_filter_shard_size"}

NAMED_WRITE = {"_index_template": "index-templates", "_component_template": "component-templates"}


class Shell:
    def __init__(self, app):
        self.app = app

    @property
    def registry(self):
        return self.app.state.registry

    # -- entry --------------------------------------------------------------
    def run(self, request, user: User, cluster_id: str, req: ShellRequest):
        from .routes_config import meta
        require_any_access(user, cluster_id)
        self.registry.get(cluster_id)
        parts, params = split_path(req.path)
        started = time.monotonic()
        write = self._write_op(user, cluster_id, req, parts, params)
        if write is not None:
            op, p, b = write
            self._audit(user, request, cluster_id, req, parts, params, op=op)
            self._remember(user, cluster_id, req)
            return self._run_write(request, user, op, p, b, req)
        out = self._read(user, cluster_id, req, parts, params)
        out["tookMs"] = int((time.monotonic() - started) * 1000)
        self._audit(user, request, cluster_id, req, parts, params, indices=out.get("indices"),
                    status=out.get("status"))
        self._remember(user, cluster_id, req)
        return out

    # -- reads --------------------------------------------------------------
    def _read(self, user: User, cluster_id: str, req: ShellRequest, parts: list[str], params: dict) -> dict:
        if req.method not in ("GET", "POST", "HEAD"):
            raise self._unsupported(req, parts)
        if not parts:
            return self._es(cluster_id, "GET", "/", None, {}, [])
        bad = READ_PARAMS_BLOCKED & set(params)
        if bad:
            raise bad_request("PARAM_NOT_ALLOWED", f"'{sorted(bad)[0]}' isn't supported in the shell")
        body = req.body
        msearch = "_msearch" in parts[:2]
        if isinstance(body, str) and not msearch:
            if body.strip():
                try:
                    body = json.loads(body)
                except json.JSONDecodeError as e:
                    raise bad_request("INVALID_BODY", f"The body isn't valid JSON: {e}") from e
            else:
                body = None
        if not user.admin and body is not None and not isinstance(body, str):
            found = script_paths(body)
            if found:
                raise forbidden("SCRIPT_NOT_ALLOWED", "Scripts can't be run from the shell (only admins may): "
                                + ", ".join(found[:5]), {"paths": found[:20]})
            if "_mget" not in parts[:2]:
                lookups = index_refs(body)
                if lookups:
                    raise forbidden("LOOKUP_NOT_ALLOWED", "Queries that fetch documents from another index (terms "
                                    "lookup, more_like_this with documents, indexed shapes, percolate by id) are "
                                    "for admins: " + ", ".join(lookups[:5]), {"paths": lookups[:20]})
        if isinstance(body, dict) and isinstance(body.get("size"), int) and body["size"] > MAX_SIZE:
            raise bad_request("SIZE_TOO_LARGE", f"size is limited to {MAX_SIZE:,}; use aggregations or paging")
        head = parts[0]
        es = self.registry.client(cluster_id)

        if user.admin and req.method in ("GET", "HEAD"):
            return self._es(cluster_id, req.method, "/" + "/".join(parts), body, params, [])

        # cluster-level reads anyone with access may run
        if tuple(parts[:2]) in CLUSTER_ANY and len(parts) <= 2:
            if parts[0] == "_cat":
                params = {**params, "format": "json"}
            return self._es(cluster_id, "GET", "/" + "/".join(parts), None, params, [])
        if head == "_cat":
            if len(parts) >= 2 and parts[1] in CAT_WITH_INDEX:
                params = {k: v for k, v in params.items() if k not in ("h", "expand_wildcards")}
                params["format"] = "json"
                indices: list[str] = []
                if len(parts) >= 3:
                    for t in _targets(parts[2]):
                        indices += resolve_scope(es, user, cluster_id, t, "view", require_all=True).permission_names()
                elif parts[1] == "count":
                    self._unrestricted(user, cluster_id, "_cat/count without an index")
                out = self._es(cluster_id, "GET", "/" + "/".join(parts), None, params, indices)
                if isinstance(out.get("response"), list) and parts[1] != "count":
                    out["response"] = [r for r in out["response"] if self._row_visible(user, cluster_id, r)]
                return out
            raise forbidden("NOT_ALLOWED_IN_SHELL", f"_cat/{parts[1] if len(parts) > 1 else ''} is for admins")
        if head in CONFIG_READS:
            require_cluster(user, cluster_id, "view")
            simulate = parts[-1] == "_simulate"
            if req.method == "POST" and not simulate:
                raise self._unsupported(req, parts)
            return self._es(cluster_id, req.method, "/" + "/".join(parts), body, params, [])
        if head == "_sql":
            self._unrestricted(user, cluster_id, "SQL")
            if req.method != "POST":
                raise bad_request("INVALID_REQUEST", "Send SQL as POST _sql with {\"query\": \"SELECT ...\"}")
            return self._es(cluster_id, "POST", "/" + "/".join(parts), body, {**params}, [])
        if head == "_msearch":
            return self._msearch(user, cluster_id, None, body, params)
        if head == "_analyze":
            return self._es(cluster_id, req.method, "/_analyze", body, params, [])
        if head == "_resolve" and len(parts) == 3 and parts[1] == "index":
            out = self._es(cluster_id, "GET", "/" + "/".join(parts), None, {"expand_wildcards": "open"}, [])
            r = out.get("response") or {}
            if isinstance(r, dict):
                ok = lambda n: not n.startswith(".") and user.can_index(cluster_id, n, "view")  # noqa: E731
                r["indices"] = [x for x in r.get("indices", []) if ok(x["name"])]
                r["aliases"] = [x for x in r.get("aliases", []) if not x["name"].startswith(".")
                                and x.get("indices") and all(ok(i) for i in x["indices"])]
                r["data_streams"] = [x for x in r.get("data_streams", []) if ok(x["name"])]
            return out
        if head in TARGET_READS:
            raise bad_request("TARGET_REQUIRED", f"Give an index, alias or pattern first, e.g. my-index-*/{head}")
        if head.startswith("_"):
            if head == "_cluster" and parts[1:2] == ["settings"]:
                raise forbidden("ADMIN_REQUIRED", "Cluster settings are for admins")
            raise forbidden("NOT_ALLOWED_IN_SHELL", f"'{head}' requests are for admins (or not available in the shell)")

        # <target>/<endpoint>
        target = head
        endpoint = parts[1] if len(parts) > 1 else None
        if endpoint is None:
            if req.method == "GET":     # GET my-index: settings + mappings + aliases
                endpoint = ""
            else:
                raise self._unsupported(req, parts)
        else:
            spec = TARGET_READS.get(endpoint)
            extra = len(parts) - 2
            if spec is None or req.method not in spec[0] or (spec[1] is not None and extra > spec[1]):
                raise self._unsupported(req, parts)
            if endpoint in ("_validate", "_eql") and parts[2:] not in ([], ["query"], ["search"]):
                raise self._unsupported(req, parts)
        indices: list[str] = []
        targets = _targets(target)
        for t in targets:
            indices += resolve_scope(es, user, cluster_id, t, "view", require_all=True).permission_names()
        params = dict(params)
        if not user.admin:
            params.pop("expand_wildcards", None)          # never reach hidden / closed indices
        if endpoint == "_msearch":
            return self._msearch(user, cluster_id, target, body, params)
        if endpoint == "_mget" and isinstance(body, dict):
            for d in body.get("docs", []) or []:
                if not isinstance(d, dict):
                    raise bad_request("INVALID_BODY", "_mget docs must be objects")
                if d.get("_index"):
                    resolve_scope(es, user, cluster_id, str(d["_index"]), "view", require_all=True)
                if not user.admin and index_refs({k: v for k, v in d.items() if k != "_index"}):
                    raise forbidden("LOOKUP_NOT_ALLOWED", "_mget docs may only name _index and _id")
        if endpoint in ("_search", "_count", "_field_caps", "_validate", "_terms_enum", "_eql", "_mget") or endpoint == "":
            params.setdefault("expand_wildcards", "open")
        # a wildcard never reaches dot indices (resolve_scope ignores them; Elasticsearch would not)
        expr = ",".join(f"{t},-.*" if "*" in t and not user.admin else t for t in targets)
        path = "/" + "/".join([quote(expr, safe=",*"), *[quote(x, safe="") for x in parts[1:]]])
        return self._es(cluster_id, req.method, path, body, params, sorted(set(indices)))

    def _msearch(self, user, cluster_id, target, body, params):
        es = self.registry.client(cluster_id)
        if isinstance(body, list):
            lines = [json.dumps(x) for x in body]
        elif isinstance(body, str):
            lines = [ln for ln in body.splitlines() if ln.strip()]
        else:
            raise bad_request("INVALID_BODY", "_msearch takes NDJSON: a header line then a body line per search")
        if len(lines) % 2:
            raise bad_request("INVALID_BODY", "_msearch needs pairs of lines: header, then body")
        indices: list[str] = []
        if not user.admin:
            params = {k: v for k, v in params.items() if k != "expand_wildcards"}
        for i in range(0, len(lines), 2):
            try:
                header, q = json.loads(lines[i]), json.loads(lines[i + 1])
            except json.JSONDecodeError as e:
                raise bad_request("INVALID_BODY", f"Line {i + 1}: {e}") from e
            if not isinstance(header, dict) or not isinstance(q, dict):
                raise bad_request("INVALID_BODY", f"Line {i + 1}: each line must be a JSON object")
            unknown = set(header) - MSEARCH_HEADER_KEYS
            if unknown:
                raise bad_request("INVALID_BODY", f"Line {i + 1}: header key(s) {', '.join(sorted(unknown))} "
                                  "aren't supported in the shell")
            idx = header.get("index") or target
            if not idx:
                raise bad_request("INVALID_BODY", f"Line {i + 1}: give \"index\" in the header (or a target in the path)")
            if isinstance(idx, str):
                names = _targets(idx)
            elif isinstance(idx, list) and all(isinstance(x, str) for x in idx):
                names = idx
            else:
                raise bad_request("INVALID_BODY", f"Line {i + 1}: \"index\" must be a name or a list of names")
            for t in names:
                indices += resolve_scope(es, user, cluster_id, t, "view", require_all=True).permission_names()
            if not user.admin:
                header["index"] = [f"{t},-.*" if "*" in t else t for t in names]
            header["expand_wildcards"] = "open"
            lines[i] = json.dumps(header)
            if not user.admin and script_paths(q):
                raise forbidden("SCRIPT_NOT_ALLOWED", "Scripts can't be run from the shell (only admins may)")
            if not user.admin and index_refs(q):
                raise forbidden("LOOKUP_NOT_ALLOWED", "Queries that fetch documents from another index are for admins")
        if target and not user.admin:
            target = ",".join(f"{t},-.*" if "*" in t else t for t in _targets(target))
        path = f"/{quote(target, safe=',*')}/_msearch" if target else "/_msearch"
        return self._es(cluster_id, "POST", path, "\n".join(lines) + "\n", params, sorted(set(indices)),
                        headers=NDJSON_HEADERS)

    def _row_visible(self, user, cluster_id, row) -> bool:
        if not isinstance(row, dict):
            return False
        idx = row.get("index")
        if idx is None:
            return False
        return not str(idx).startswith(".") and user.can_index(cluster_id, idx, "view")

    def _unrestricted(self, user: User, cluster_id: str, what: str) -> None:
        if user.admin:
            return
        acc = user.access(cluster_id) or {}
        if not acc.get("default") or any(r.get("level") in (None, "none") for r in acc.get("indices", [])):
            raise forbidden("PERMISSION_DENIED", f"{what} needs view access on every index of the cluster "
                            "(no index rules that hide indices). Use _search on the indices you can see instead")

    def _es(self, cluster_id, method, path, body, params, indices, headers=None) -> dict:
        es = self.registry.client(cluster_id)
        try:
            resp = es_call(es, method, path, body=body, params=params or None, headers=headers)
            status = 200
        except ApiError as e:
            d = e.details if isinstance(e.details, dict) else {}
            if e.code in ("ES_REJECTED", "ES_NOT_FOUND"):
                resp, status = d.get("esError") or {"error": e.message}, d.get("esStatus", e.status)
            else:
                raise
        size = len(json.dumps(resp, default=str)) if resp is not None else 0
        if size > RESPONSE_MAX:
            raise ApiError(413, "RESPONSE_TOO_LARGE", f"The response is {size / 1048576:.1f} MB; the shell shows at "
                           "most 5 MB. Lower size, add filter_path or use aggregations")
        return {"kind": "read", "status": status, "response": resp, "indices": indices}

    def _unsupported(self, req: ShellRequest, parts: list[str]) -> ApiError:
        return forbidden("NOT_ALLOWED_IN_SHELL",
                         f"{req.method} /{'/'.join(parts)} can't be run from the shell. Reads: _search, _count, "
                         "_msearch, _sql, _field_caps, _mapping, _settings, _cat/indices... Changes: _settings, "
                         "_mapping, templates, ILM, pipelines, create / delete index, _doc, _update, "
                         "_update_by_query (with \"set\"/\"remove\"), _delete_by_query")

    # -- writes -------------------------------------------------------------
    def _write_op(self, user, cluster_id, req: ShellRequest, parts, params) -> tuple[str, dict, dict] | None:
        """(approval op, params, body) for a write, or None for a read."""
        m, body = req.method, req.body if req.body is not None else {}
        c = {"clusterId": cluster_id}
        if m in ("GET", "HEAD") or not parts:
            return None
        head = parts[0]
        reason = req.reason

        def obj() -> dict:
            if not isinstance(body, dict):
                raise bad_request("INVALID_BODY", "This request needs a JSON object body")
            if script_paths(body):
                raise forbidden("SCRIPT_NOT_ALLOWED", "Scripts aren't run from the shell: "
                                + ", ".join(script_paths(body)[:5]))
            return body

        # cluster settings
        if parts == ["_cluster", "settings"] and m == "PUT":
            b = obj()
            if b.get("transient"):
                raise bad_request("TRANSIENT_NOT_SUPPORTED", "Use \"persistent\" settings (transient ones are lost "
                                  "on restart and can't be rolled back)")
            return "config.update", {**c, "configType": "cluster-settings", "resource": "_cluster"}, \
                {"config": b.get("persistent") or {}, "reason": reason}
        # templates, ILM, pipelines
        if head in NAMED_WRITE and len(parts) == 2 and m in ("PUT", "POST"):
            return "config.update", {**c, "configType": NAMED_WRITE[head], "resource": parts[1]}, \
                {"config": obj(), "reason": reason}
        if head == "_ilm" and len(parts) == 3 and parts[1] == "policy" and m == "PUT":
            return "config.update", {**c, "configType": "ilm-policies", "resource": parts[2]}, \
                {"config": obj(), "reason": reason}
        if head == "_ingest" and len(parts) == 3 and parts[1] == "pipeline" and m == "PUT":
            return "config.update", {**c, "configType": "ingest-pipelines", "resource": parts[2]}, \
                {"config": obj(), "reason": reason}
        if head in CONFIG_READS and parts[-1] == "_simulate":
            return None
        if head.startswith("_"):
            if head in ("_msearch", "_sql", "_analyze", "_mget", "_count", "_search", "_field_caps") and m == "POST":
                return None
            raise self._unsupported(req, parts)

        index = head
        if len(parts) == 1:
            if m == "PUT":
                b = obj()
                extra = set(b) - {"settings", "mappings", "aliases"}
                if extra:
                    raise bad_request("INVALID_BODY", f"Creating an index takes settings, mappings and aliases "
                                      f"(not {', '.join(sorted(extra))})")
                return "index.create", {**c, "index": index}, {**b, "reason": reason}
            if m == "DELETE":
                return "index.delete", {**c, "index": index, "confirm": req.confirm, "reason": reason}, {}
            return None
        ep = parts[1]
        if ep == "_settings" and m == "PUT":
            b = obj()
            return "config.update", {**c, "configType": "index-settings", "resource": index}, \
                {"config": b.get("settings", b) if set(b) == {"settings"} else b, "reason": reason}
        if ep == "_mapping" and m in ("PUT", "POST"):
            return "config.update", {**c, "configType": "index-mappings", "resource": index}, \
                {"config": obj(), "reason": reason}
        if ep in ("_doc", "_create") and m in ("PUT", "POST"):
            doc = obj()
            if len(parts) == 2 and ep == "_doc" and m == "POST":
                return "doc.create", {**c, "index": index}, {"document": doc, "reason": reason}
            if len(parts) != 3:
                raise self._unsupported(req, parts)
            doc_id = parts[2]
            if ep == "_create" or params.get("op_type") == "create":
                return "doc.create", {**c, "index": index}, {"document": doc, "id": doc_id, "reason": reason}
            exists = self._doc_exists(cluster_id, index, doc_id, user)
            if not exists:
                return "doc.create", {**c, "index": index}, {"document": doc, "id": doc_id, "reason": reason}
            return "doc.update", {**c, "index": index, "id": doc_id}, {"document": doc, "reason": reason}
        if ep == "_doc" and m == "DELETE" and len(parts) == 3:
            return "doc.delete", {**c, "index": index, "id": parts[2], "confirm": req.confirm, "reason": reason}, {}
        if ep == "_update" and m == "POST" and len(parts) == 3:
            b = obj()
            if set(b) - {"doc"} or not isinstance(b.get("doc"), dict):
                raise bad_request("INVALID_BODY", "From the shell, _update takes {\"doc\": {...}} (a partial "
                                  "document); scripts and upserts aren't run")
            cur = self._doc(cluster_id, index, parts[2], user)
            merged = _merge(cur["_source"], b["doc"])
            return "doc.update", {**c, "index": cur["_index"], "id": parts[2]}, {
                "document": merged, "reason": reason, "ifSeqNo": cur["_seq_no"], "ifPrimaryTerm": cur["_primary_term"]}
        if ep in ("_update_by_query", "_delete_by_query") and m == "POST":
            b = obj() if body else {}
            q = b.get("query") or {"match_all": {}}
            if ep == "_update_by_query":
                extra = set(b) - {"query", "set", "remove"}
                if extra or not (b.get("set") or b.get("remove")):
                    raise bad_request("INVALID_BODY", "From the shell, _update_by_query takes {\"query\": {...}, "
                                      "\"set\": {\"field\": value}, \"remove\": [\"field\"]}; scripts aren't run")
                bb = {"dsl": q, "set": b.get("set") or {}, "remove": b.get("remove") or []}
                op = "bulk.update"
            else:
                if set(b) - {"query"}:
                    raise bad_request("INVALID_BODY", "From the shell, _delete_by_query takes {\"query\": {...}}")
                bb, op = {"dsl": q}, "bulk.delete"
            bb.update(reason=reason, dryRunToken=req.dryRunToken, expectedCount=req.expectedCount, size=0)
            return op, {**c, "index": index}, bb
        if ep in TARGET_READS and m in TARGET_READS[ep][0]:
            return None
        raise self._unsupported(req, parts)

    def _doc(self, cluster_id, index, doc_id, user: User | None = None) -> dict:
        from .data_browser import _quote, check_target
        check_target(index)
        if user is not None and not user.can_index(cluster_id, index, "edit"):
            raise forbidden("PERMISSION_DENIED", f"'{user.username}' needs 'edit' access on '{index}'")
        es = self.registry.client(cluster_id)
        try:
            r = es_call(es, "GET", f"/{index}/_doc/{_quote(doc_id)}")
        except ApiError as e:
            if e.code == "ES_NOT_FOUND":
                from .errors import not_found
                raise not_found("DOCUMENT_NOT_FOUND", f"No document '{doc_id}' in '{index}'") from e
            raise
        if not r.get("found"):
            from .errors import not_found
            raise not_found("DOCUMENT_NOT_FOUND", f"No document '{doc_id}' in '{index}'")
        return r

    def _doc_exists(self, cluster_id, index, doc_id, user: User | None = None) -> bool:
        try:
            self._doc(cluster_id, index, doc_id, user)
            return True
        except ApiError as e:
            if e.code == "DOCUMENT_NOT_FOUND":
                return False
            raise

    def _run_write(self, request, user, op, p, b, req: ShellRequest):
        from fastapi.responses import JSONResponse

        from .approvals import OPS, gate
        out = gate(request, user, op, p, {k: v for k, v in b.items() if v is not None}, req.dryRun)
        if isinstance(out, JSONResponse):
            return out
        label = OPS[op].label(p, b)
        needs: dict[str, Any] = {"reason": True}
        if op in ("index.delete",):
            needs["confirm"] = p["index"]
        if op == "doc.delete":
            needs["confirm"] = p["id"]
        if op.startswith("bulk.") and isinstance(out, dict) and out.get("dryRun"):
            needs["count"] = out.get("count")
        return {"kind": "write", "op": op, "label": label, "dryRun": req.dryRun, "result": out, "needs": needs,
                "approvalRequired": self.app.state.approvals.required(user)}

    # -- audit + history ----------------------------------------------------
    def _audit(self, user, request, cluster_id, req, parts, params, op=None, indices=None, status=None):
        from .routes_config import meta
        m = meta(request)
        # field / aggregation names of a read's query; a write's body is a document or config, so none
        # of it is recorded here (the change's own audit entry lists the field names)
        names = dsl_names(req.body) if op is None and isinstance(req.body, (dict, list)) \
            else {"fields": [], "aggs": []}
        self.app.state.audit.write({
            "action": "SHELL_QUERY", "actor": user.username, "outcome": "SUCCESS" if not status or status < 400 else "REJECTED",
            "requestId": m.request_id, "sourceIp": m.source_ip, "clusterId": cluster_id, "configType": "shell",
            "resource": parts[0] if parts else "/", "method": req.method,
            "path": "/" + "/".join(parts), "params": sorted(params)[:30], "indices": (indices or [])[:100],
            "fields": names["fields"], "aggs": names["aggs"], **({"writeOp": op, "dryRun": req.dryRun} if op else {}),
            **({"esStatus": status} if status else {})})

    def _remember(self, user: User, cluster_id: str, req: ShellRequest) -> None:
        body = req.body
        text = body if isinstance(body, str) else (json.dumps(body, indent=2) if body is not None else "")
        if len(text) > HISTORY_BODY_MAX:
            text = text[:HISTORY_BODY_MAX]
        entry = {"at": iso(), "clusterId": cluster_id, "method": req.method, "path": req.path, "body": text}

        def m(doc):
            items = [e for e in doc.get("items", []) if not (e["method"] == entry["method"] and e["path"] == entry["path"]
                                                             and e.get("body") == entry["body"] and e["clusterId"] == cluster_id)]
            doc["items"] = ([entry] + items)[:HISTORY_MAX]
        try:
            _update_with_retry(self.app.state.store, self.history_key(user), m, {"items": []})
        except Exception:
            pass

    @staticmethod
    def history_key(user: User) -> str:
        return f"shell-history/{re.sub(r'[^A-Za-z0-9@._+-]', '_', user.username)}.json"

    def history(self, user: User, cluster_id: str | None) -> dict:
        doc, _ = self.app.state.store.get_json(self.history_key(user))
        items = (doc or {}).get("items", [])
        if cluster_id:
            items = [e for e in items if e.get("clusterId") == cluster_id]
        return {"items": items}

    def clear_history(self, user: User) -> dict:
        self.app.state.store.put_json(self.history_key(user), {"items": []})
        return {"items": []}


def _merge(base: Any, patch: Any) -> Any:
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for k, v in patch.items():
            out[k] = _merge(base.get(k), v) if isinstance(v, dict) else v
        return out
    return patch
