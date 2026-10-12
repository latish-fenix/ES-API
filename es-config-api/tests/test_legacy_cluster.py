"""Clusters older than 7.14 (no X-Elastic-Product header, no 8.x media types) still connect."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.clusters import NDJSON_HEADERS, build_client, es_call, is_legacy, parse_cluster
from app.errors import ApiError
from app.routes_clusters import test_connection as run_test


class OldEs(BaseHTTPRequestHandler):
    """Behaves like Elasticsearch 7.9 ("old") or 7.12 ("mid") for a handful of endpoints."""
    mode = "old"
    seen: list = []

    def log_message(self, *a):  # quiet
        pass

    def _send(self, status: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=UTF-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n).decode() if n else ""
        ctype, accept = self.headers.get("content-type", ""), self.headers.get("accept", "")
        if not (self.headers.get("authorization") or "").startswith("Basic "):
            return self._send(401, {"error": {"type": "security_exception",
                                              "reason": "missing authentication credentials"},
                                    "status": 401})
        if self.mode == "mid" and "compatible-with=8" in accept + ctype:
            return self._send(400, {"error": {"type": "media_type_header_exception",
                                              "reason": "Invalid media-type value on headers [Accept]"},
                                    "status": 400})
        if raw and "vnd.elasticsearch" in ctype:
            return self._send(406, {"error": f"Content-Type header [{ctype}] is not supported",
                                    "status": 406})
        OldEs.seen.append((self.command, self.path, ctype, raw))
        path = self.path.split("?")[0]
        if path == "/":
            return self._send(200, {"cluster_name": "elkop3", "cluster_uuid": "u1",
                                    "version": {"number": "7.9.1" if self.mode == "old" else "7.12.1"}})
        if path == "/_cluster/health":
            return self._send(200, {"status": "green", "number_of_nodes": 3})
        if path == "/orders" and self.command == "PUT":
            return self._send(200, {"acknowledged": True, "index": "orders"})
        if path == "/_bulk":
            return self._send(200, {"errors": False, "items": [{} for _ in raw.strip().splitlines()[::2]]})
        return self._send(404, {"error": {"type": "index_not_found_exception",
                                          "root_cause": [{"reason": "no such index [nope]"}]},
                                "status": 404})

    do_GET = do_PUT = do_POST = do_DELETE = _handle


@pytest.fixture(params=["old", "mid"])
def old_es(request):
    OldEs.mode, OldEs.seen = request.param, []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), OldEs)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", request.param
    srv.shutdown()


def _cfg(url):
    return parse_cluster({"id": "elkop3", "hosts": [url], "auth": {"type": "basic", "username": "u",
                                                              "password": "p"}}, "managed")


def test_connection_falls_back_to_plain_json(old_es):
    url, mode = old_es
    res = run_test(_cfg(url))
    assert res["reachable"] is True, res
    assert res["compatibilityMode"] == "legacy"
    assert res["health"] == "green" and res["numberOfNodes"] == 3
    assert res["version"] == ("7.9.1" if mode == "old" else "7.12.1")
    assert any("compatibility mode" in w for w in res["warnings"])


def test_writes_and_errors_after_fallback(old_es):
    url, _ = old_es
    es = build_client(_cfg(url))
    assert es_call(es, "GET", "/")["cluster_name"] == "elkop3"
    assert is_legacy(es) and is_legacy(es.options(request_timeout=5))
    # JSON body
    assert es_call(es, "PUT", "/orders", body={"settings": {"number_of_replicas": 1}})["acknowledged"]
    # NDJSON body
    out = es_call(es, "POST", "/_bulk", body=[{"index": {"_index": "orders"}}, {"a": 1}],
                  headers=NDJSON_HEADERS)
    assert out["errors"] is False and len(out["items"]) == 1
    put = [s for s in OldEs.seen if s[0] == "PUT"][0]
    bulk = [s for s in OldEs.seen if s[1].startswith("/_bulk")][0]
    assert put[2] == "application/json" and json.loads(put[3])["settings"]
    assert bulk[2] == "application/x-ndjson"
    # Errors still map to clean codes
    with pytest.raises(ApiError) as e:
        es_call(es, "GET", "/nope/_search")
    assert e.value.code == "ES_NOT_FOUND" and "no such index" in e.value.message


def test_first_call_being_a_write_runs_once(old_es):
    """A write as the very first request must not be sent twice."""
    url, _ = old_es
    es = build_client(_cfg(url))
    assert es_call(es, "PUT", "/orders", body={"settings": {}})["acknowledged"]
    assert len([s for s in OldEs.seen if s[0] == "PUT"]) == 1
