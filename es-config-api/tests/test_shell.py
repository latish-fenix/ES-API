"""Shell: reads inside the access rules, writes through the console's change flows."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from conftest import ROOT, as_user

SH = "/api/v1/clusters/test/shell"


def _seed(es, prefix):
    for name in (f"{prefix}-shop-a", f"{prefix}-shop-b", f"{prefix}-pay"):
        es("PUT", f"/{name}", json={"settings": {"number_of_replicas": 0}, "mappings": {"properties": {
            "status": {"type": "keyword"}, "total": {"type": "double"}, "vendor": {"type": "keyword"}}}})
        for i in range(4):
            es("PUT", f"/{name}/_doc/d{i}", json={"status": "new" if i % 2 else "paid", "total": i * 10.0,
                                                 "vendor": "secret-vendor-42"}, params={"refresh": "true"})


def _users(client, prefix):
    client.put("/api/v1/admin/users/viewer", json={"clusters": {"test": {"default": None, "indices": [
        {"pattern": f"{prefix}-shop-*", "level": "view"}]}}}, headers=ROOT)
    client.put("/api/v1/admin/users/wide", json={"clusters": {"test": "view"}}, headers=ROOT)
    client.put("/api/v1/admin/users/editor", json={"clusters": {"test": {"default": "view", "indices": [
        {"pattern": f"{prefix}-shop-*", "level": "edit"}]}}}, headers=ROOT)


def _audit(client, **f):
    day = datetime.now(timezone.utc).date().isoformat()
    items = client.get("/api/v1/admin/audit", params={"date": day, "action": "SHELL_QUERY"}, headers=ROOT).json()["items"]
    return [e for e in items if all(e.get(k) == v for k, v in f.items())]


def test_reads_follow_access_rules(client, es, prefix):
    _seed(es, prefix)
    _users(client, prefix)
    v = as_user("viewer")
    agg = {"size": 0, "query": {"term": {"vendor": "secret-vendor-42"}},
           "aggs": {"by_status": {"terms": {"field": "status"}, "aggs": {"spent": {"sum": {"field": "total"}}}}}}
    r = client.post(SH, json={"method": "POST", "path": f"{prefix}-shop-*/_search", "body": agg}, headers=v)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["kind"] == "read" and out["status"] == 200 and out["indices"] == [f"{prefix}-shop-a", f"{prefix}-shop-b"]
    buckets = {b["key"]: b["doc_count"] for b in out["response"]["aggregations"]["by_status"]["buckets"]}
    assert buckets == {"new": 4, "paid": 4}
    # audit: names, never values
    ev = _audit(client, actor="viewer", path=f"/{prefix}-shop-*/_search")
    assert ev and ev[0]["fields"] == ["status", "total", "vendor"] and ev[0]["aggs"] == ["by_status:terms", "spent:sum"]
    assert "secret-vendor-42" not in str(ev[0])
    # a pattern that reaches an index they can't see: refused, not narrowed
    r = client.post(SH, json={"method": "POST", "path": f"{prefix}-*/_count"}, headers=v)
    assert r.status_code == 403 and f"{prefix}-pay" in str(r.json()["error"]["details"]["indices"])
    assert client.post(SH, json={"method": "GET", "path": f"{prefix}-pay/_mapping"}, headers=v).status_code == 403
    # scripts: admins only
    r = client.post(SH, json={"method": "POST", "path": f"{prefix}-shop-a/_search",
                              "body": {"script_fields": {"x": {"script": "1"}}}}, headers=v)
    assert r.status_code == 403 and r.json()["error"]["code"] == "SCRIPT_NOT_ALLOWED"
    # _cat/indices shows only what they can see
    rows = client.post(SH, json={"method": "GET", "path": "_cat/indices"}, headers=v).json()["response"]
    names = {x["index"] for x in rows}
    assert {f"{prefix}-shop-a", f"{prefix}-shop-b"} <= names and f"{prefix}-pay" not in names
    # SQL needs view on everything
    sql = {"method": "POST", "path": "_sql?format=json", "body": {"query": f'SELECT status, COUNT(*) FROM "{prefix}-shop-a" GROUP BY status'}}
    assert client.post(SH, json=sql, headers=v).status_code == 403
    r = client.post(SH, json=sql, headers=as_user("wide"))
    assert r.status_code == 200 and r.json()["response"]["rows"], r.text
    # msearch: every header is checked
    nd = f'{{"index": "{prefix}-shop-a"}}\n{{"size": 0}}\n{{"index": "{prefix}-pay"}}\n{{"size": 0}}\n'
    assert client.post(SH, json={"method": "POST", "path": "_msearch", "body": nd}, headers=v).status_code == 403
    nd = f'{{"index": "{prefix}-shop-a"}}\n{{"size": 0}}\n{{"index": "{prefix}-shop-b"}}\n{{"size": 1}}\n'
    r = client.post(SH, json={"method": "POST", "path": "_msearch", "body": nd}, headers=v)
    assert r.status_code == 200 and len(r.json()["response"]["responses"]) == 2
    # Elasticsearch errors come back as the response, like Dev Tools
    r = client.post(SH, json={"method": "POST", "path": f"{prefix}-shop-a/_search", "body": {"query": {"nope": {}}}}, headers=v)
    assert r.status_code == 200 and r.json()["status"] == 400 and "error" in r.json()["response"]
    # things the shell doesn't do
    for m, path in (("POST", "_reindex"), ("POST", f"{prefix}-shop-a/_forcemerge"), ("POST", f"{prefix}-shop-a/_close"),
                    ("GET", "_security/user"), ("GET", "_nodes/stats"), ("POST", "_search")):
        r = client.post(SH, json={"method": m, "path": path, "body": {}}, headers=v)
        assert r.status_code in (400, 403), (m, path, r.text)
    # writes disguised as reads, and other ways around the checks
    a = f"{prefix}-shop-a"
    r = client.post(SH, json={"method": "POST", "path": f"{a}/_alias/{prefix}-sneaky"}, headers=v)
    assert r.status_code == 403, r.text
    assert es("GET", f"/_alias/{prefix}-sneaky", ok=False).get("status") == 404
    r = client.post(SH, json={"method": "POST", "path": f"{a}/_search",
                              "body": '{"script_fields": {"x": {"script": "1"}}}'}, headers=v)
    assert r.status_code == 403 and r.json()["error"]["code"] == "SCRIPT_NOT_ALLOWED"
    lookup = {"query": {"terms": {"vendor": {"index": f"{prefix}-pay", "id": "d1", "path": "vendor"}}}}
    r = client.post(SH, json={"method": "POST", "path": f"{a}/_search", "body": lookup}, headers=v)
    assert r.status_code == 403 and r.json()["error"]["code"] == "LOOKUP_NOT_ALLOWED"
    nd = f'{{"index": "{a}", "indices": ["{prefix}-pay"]}}\n{{"size": 0}}\n'
    assert client.post(SH, json={"method": "POST", "path": "_msearch", "body": nd}, headers=v).status_code == 400
    assert client.post(SH, json={"method": "POST", "path": "_msearch", "body": '[1]\n{}\n'}, headers=v).status_code == 400
    r = client.post(SH, json={"method": "GET", "path": "_cat/indices?h=health,docs.count"}, headers=v)
    assert all(row.get("index", "").startswith(f"{prefix}-shop-") for row in r.json()["response"]
               if row.get("index", "").startswith(prefix)), r.text
    assert not any(row.get("index") == f"{prefix}-pay" for row in r.json()["response"])
    assert client.post(SH, json={"method": "GET", "path": "_cat/count"}, headers=v).status_code == 403
    for p_ in (f"{a}/_mtermvectors", f"{a}/_termvectors/d1", f"{a}/_search/template"):
        assert client.post(SH, json={"method": "POST", "path": p_, "body": {}}, headers=v).status_code == 403, p_
    r = client.post(SH, json={"method": "PUT", "path": f"{prefix}-pay/_doc/d1", "body": {"x": 1}, "dryRun": True},
                    headers=as_user("editor"))
    assert r.status_code == 403      # no peek at whether the document exists
    # history: newest first, only theirs
    h = client.get(f"{SH}/history", headers=v).json()["items"]
    assert h[0]["path"] == "_cat/indices?h=health,docs.count" and len(h) <= 50
    assert any(x["path"] == f"{prefix}-shop-a/_search" for x in h)
    assert client.get(f"{SH}/history", headers=as_user("wide")).json()["items"][0]["path"].startswith("_sql")
    assert client.post(f"{SH}/history/_clear", headers=v).json() == {"items": []}


def test_writes_go_through_the_change_flows(client, es, prefix, open_allowlist):
    _seed(es, prefix)
    _users(client, prefix)
    idx = f"{prefix}-shop-a"
    # settings: dry run shows the diff; the real one is a normal audited, roll-back-able change
    req = {"method": "PUT", "path": f"{idx}/_settings", "body": {"index": {"refresh_interval": "17s"}}}
    r = client.post(SH, json={**req, "dryRun": True}, headers=ROOT)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["kind"] == "write" and out["op"] == "config.update" and out["result"]["dryRun"]
    assert out["result"]["diff"]["added"] == [{"path": "index.refresh_interval", "after": "17s"}]
    assert client.post(SH, json=req, headers=ROOT).json()["error"]["code"] == "REASON_REQUIRED"
    r = client.post(SH, json={**req, "reason": "from the shell"}, headers=ROOT)
    assert r.status_code == 200 and r.json()["result"]["applied"], r.text
    assert es("GET", f"/{idx}/_settings", params={"flat_settings": "true"})[idx]["settings"]["index.refresh_interval"] == "17s"
    r = client.post(f"/api/v1/clusters/test/indices/{idx}/settings/rollback", json={"reason": "undo"}, headers=ROOT)
    assert r.status_code == 200, r.text
    # viewers can't write; scripts never run
    assert client.post(SH, json={**req, "reason": "x"}, headers=as_user("viewer")).status_code == 403
    r = client.post(SH, json={"method": "POST", "path": f"{idx}/_update_by_query", "reason": "x",
                              "body": {"script": {"source": "ctx._source.x = 1"}}}, headers=ROOT)
    assert r.status_code == 403 and r.json()["error"]["code"] == "SCRIPT_NOT_ALLOWED"

    # update by query with set/remove: dry run (count + token), then the real run with a backup
    ubq = {"method": "POST", "path": f"{idx}/_update_by_query",
           "body": {"query": {"term": {"status": "new"}}, "set": {"status": "archived"}}}
    dry = client.post(SH, json={**ubq, "dryRun": True}, headers=ROOT).json()
    assert dry["result"]["count"] == 2 and dry["needs"]["count"] == 2 and dry["result"]["dryRunToken"]
    r = client.post(SH, json={**ubq, "reason": "archive", "dryRunToken": dry["result"]["dryRunToken"],
                              "expectedCount": 2}, headers=ROOT)
    assert r.status_code == 200 and r.json()["result"]["succeeded"] == 2, r.text
    es("POST", f"/{idx}/_refresh")
    assert es("POST", f"/{idx}/_count", json={"query": {"term": {"status": "archived"}}})["count"] == 2
    changes = client.get("/api/v1/clusters/test/data/_changes", headers=ROOT).json()["items"]
    assert changes[0]["changeId"] == r.json()["result"]["changeId"]          # restorable like any bulk change

    # documents: partial _update merges; delete needs the id typed
    r = client.post(SH, json={"method": "POST", "path": f"{idx}/_update/d1", "body": {"doc": {"total": 99}},
                              "reason": "fix total"}, headers=ROOT)
    assert r.status_code == 200 and r.json()["op"] == "doc.update", r.text
    src = es("GET", f"/{idx}/_doc/d1")["_source"]
    assert src["total"] == 99 and src["vendor"] == "secret-vendor-42"
    r = client.post(SH, json={"method": "DELETE", "path": f"{idx}/_doc/d2", "reason": "dup"}, headers=ROOT)
    assert r.json()["error"]["code"] == "CONFIRMATION_MISMATCH"
    r = client.post(SH, json={"method": "DELETE", "path": f"{idx}/_doc/d2", "reason": "dup", "confirm": "d2"}, headers=ROOT)
    assert r.status_code == 200 and r.json()["result"]["deleted"]

    # create and delete an index
    new = f"{prefix}-shop-new"
    r = client.post(SH, json={"method": "PUT", "path": new, "body": {"settings": {"number_of_replicas": 0}},
                              "reason": "new"}, headers=ROOT)
    assert r.status_code == 200 and r.json()["op"] == "index.create", r.text
    client.put("/api/v1/admin/allowlist", json={**open_allowlist, "index-delete": {"allow": [f"{prefix}-*"]}}, headers=ROOT)
    r = client.post(SH, json={"method": "DELETE", "path": new, "reason": "test", "confirm": new}, headers=ROOT)
    assert r.status_code == 200 and r.json()["result"]["applied"], r.text


@pytest.mark.parametrize("env", [True], indirect=True)
def test_shell_writes_wait_for_approval(env, client, es, prefix, open_allowlist):
    _seed(es, prefix)
    _users(client, prefix)
    idx = f"{prefix}-shop-b"
    ed = as_user("editor")
    req = {"method": "PUT", "path": f"{idx}/_settings", "body": {"refresh_interval": "23s"}, "reason": "slower"}
    assert client.post(SH, json={**req, "dryRun": True}, headers=ed).json()["approvalRequired"] is True
    r = client.post(SH, json=req, headers=ed)
    assert r.status_code == 202 and r.json()["pendingApproval"], r.text
    assert "index.refresh_interval" not in es("GET", f"/{idx}/_settings", params={"flat_settings": "true"})[idx]["settings"]
    rid = r.json()["approval"]["id"]
    assert client.post(f"/api/v1/approvals/{rid}/_approve", json={"comment": "ok"}, headers=ROOT).json()["status"] == "APPLIED"
    assert es("GET", f"/{idx}/_settings", params={"flat_settings": "true"})[idx]["settings"]["index.refresh_interval"] == "23s"
    # reads are never held
    r = client.post(SH, json={"method": "POST", "path": f"{idx}/_count"}, headers=ed)
    assert r.status_code == 200 and r.json()["response"]["count"] == 4
