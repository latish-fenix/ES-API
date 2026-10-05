"""Create an index from the console (Edit on the cluster, dry run, audit) and undo it while empty."""
from __future__ import annotations

from datetime import datetime, timezone

from conftest import ROOT, as_user

API = "/api/v1/clusters/test"


def _audit(client, action):
    day = datetime.now(timezone.utc).date().isoformat()
    return client.get("/api/v1/admin/audit", params={"date": day, "action": action}, headers=ROOT).json()["items"]


def test_create_index_with_template_and_undo(client, es, prefix):
    name = f"{prefix}-orders-2024.10"
    es("PUT", f"/_index_template/{prefix}-tpl", json={
        "index_patterns": [f"{prefix}-orders-*"], "priority": 50,
        "template": {"settings": {"number_of_replicas": 0, "refresh_interval": "9s"},
                     "mappings": {"properties": {"vendor": {"type": "keyword"}, "total": {"type": "long"}}}}})
    try:
        # preview: what the template gives the name
        p = client.get(f"{API}/indices/{name}/_create-preview", headers=ROOT)
        assert p.status_code == 200, p.text
        p = p.json()
        assert p["exists"] is None and p["template"]["name"] == f"{prefix}-tpl"
        assert p["fromTemplates"]["settings"]["index.refresh_interval"] == "9s"

        body = {"settings": {"number_of_shards": 1, "refresh_interval": "2s"},
                "mappings": {"properties": {"total": {"type": "double"}, "created": {"type": "date"}}},
                "aliases": {f"{prefix}-orders-current": {}}}
        r = client.post(f"{API}/indices/{name}", params={"dryRun": "true"}, json=body, headers=ROOT)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["applied"] is False and es("GET", f"/{name}", ok=False).get("status") == 404
        s, m = d["result"]["settings"], d["result"]["mappings"]["properties"]
        assert s["index.refresh_interval"] == "2s" and str(s["index.number_of_replicas"]) == "0"   # ours win, template kept
        assert m["vendor"] == {"type": "keyword"} and m["total"] == {"type": "double"} and m["created"]["type"] == "date"
        assert any("No replicas" in w for w in d["warnings"]) and any("also applies" in w for w in d["warnings"])

        r = client.post(f"{API}/indices/{name}", json=body, headers=ROOT)
        assert r.json()["error"]["code"] == "REASON_REQUIRED"
        r = client.post(f"{API}/indices/{name}", json={**body, "reason": "October orders"}, headers=ROOT)
        assert r.status_code == 200 and r.json()["applied"], r.text
        got = es("GET", f"/{name}")[name]
        assert got["settings"]["index"]["refresh_interval"] == "2s" and got["mappings"]["properties"]["total"]["type"] == "double"
        assert f"{prefix}-orders-current" in got["aliases"]
        ev = [e for e in _audit(client, "INDEX_CREATE") if e["resource"] == name]
        assert ev and ev[0]["outcome"] == "SUCCESS" and ev[0]["settingKeys"] == ["index.number_of_shards", "index.refresh_interval"]
        assert "created" in ev[0]["mappingFields"] and "2s" not in str(ev[0])
        create_id = r.json()["changeId"]

        # exists now
        r = client.post(f"{API}/indices/{name}", params={"dryRun": "true"}, json={}, headers=ROOT)
        assert r.status_code == 409 and r.json()["error"]["code"] == "INDEX_EXISTS"

        # undo while empty: dry run, then delete (definition kept, so it can be recreated)
        c = client.get(f"{API}/indices/{name}/_created", headers=ROOT).json()
        assert c["changeId"] == create_id and c["canUndo"] is True and c["docs"] == 0
        r = client.post(f"{API}/indices/{name}/_undo-create", params={"dryRun": "true"}, json={}, headers=ROOT)
        assert r.status_code == 200 and r.json()["restoreOf"] == create_id, r.text
        es("POST", f"/{name}/_doc", json={"vendor": "v1"})          # not refreshed yet: still counted
        r = client.post(f"{API}/indices/{name}/_undo-create", json={"reason": "x"}, headers=ROOT)
        assert r.status_code == 409 and r.json()["error"]["code"] == "INDEX_NOT_EMPTY", r.text
        assert client.get(f"{API}/indices/{name}/_created", headers=ROOT).json()["canUndo"] is False
        es("POST", f"/{name}/_delete_by_query", json={"query": {"match_all": {}}}, params={"refresh": "true"})
        r = client.post(f"{API}/indices/{name}/_undo-create", json={"reason": "created by mistake"}, headers=ROOT)
        assert r.status_code == 200 and r.json()["applied"], r.text
        assert es("GET", f"/{name}", ok=False).get("status") == 404
        ev = [e for e in _audit(client, "INDEX_DELETE") if e["resource"] == name and e["outcome"] == "SUCCESS"]
        assert ev and ev[0]["restoreOf"] == create_id and ev[0]["tombstoneKey"]
        tomb = client.get(f"{API}/deleted-indices", headers=ROOT).json()["items"]
        assert any(t["index"] == name for t in tomb)
    finally:
        es("DELETE", f"/_index_template/{prefix}-tpl", ok=False)
        es("DELETE", f"/{name}", ok=False)


def test_create_index_guards(client, es, prefix):
    # access: Edit on the cluster; an index rule that lowers it blocks the name
    client.put("/api/v1/admin/users/viewer", json={"clusters": {"test": "view"}}, headers=ROOT)
    client.put("/api/v1/admin/users/ruled", json={"clusters": {"test": {"default": None, "indices": [
        {"pattern": f"{prefix}-*", "level": "edit"}]}}}, headers=ROOT)
    client.put("/api/v1/admin/users/writer", json={"clusters": {"test": {"default": "edit", "indices": [
        {"pattern": f"{prefix}-pay*", "level": "view"}]}}}, headers=ROOT)
    url = f"{API}/indices/{prefix}-new"
    assert client.post(url, params={"dryRun": "true"}, json={}, headers=as_user("viewer")).status_code == 403
    assert client.post(url, params={"dryRun": "true"}, json={}, headers=as_user("ruled")).status_code == 403
    assert client.post(url, params={"dryRun": "true"}, json={}, headers=as_user("writer")).status_code == 200
    r = client.post(f"{API}/indices/{prefix}-payments", params={"dryRun": "true"}, json={}, headers=as_user("writer"))
    assert r.status_code == 403
    # names
    for bad in (".secret", "Upper", "a*b", "-x", "_all"):
        r = client.post(f"{API}/indices/{bad}", params={"dryRun": "true"}, json={}, headers=ROOT)
        assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_INDEX_NAME", (bad, r.text)
    # Elasticsearch checks the body in the dry run
    r = client.post(url, params={"dryRun": "true"}, json={"mappings": {"properties": {"x": {"type": "nope"}}}}, headers=ROOT)
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_INDEX_BODY" and "nope" in r.text
    # a name a data-stream template claims
    es("PUT", f"/_index_template/{prefix}-ds", json={"index_patterns": [f"{prefix}-logs-*"], "priority": 60, "data_stream": {}})
    try:
        r = client.post(f"{API}/indices/{prefix}-logs-a", params={"dryRun": "true"}, json={}, headers=ROOT)
        assert r.status_code == 422 and r.json()["error"]["code"] == "DATA_STREAM_TEMPLATE"
    finally:
        es("DELETE", f"/_index_template/{prefix}-ds", ok=False)
    # an index that wasn't created here can't be "undone"
    es("PUT", f"/{prefix}-manual", json={"settings": {"number_of_replicas": 0}})
    r = client.post(f"{API}/indices/{prefix}-manual/_undo-create", json={"reason": "x"}, headers=ROOT)
    assert r.status_code == 404 and r.json()["error"]["code"] == "CHANGE_NOT_FOUND"
    assert client.get(f"{API}/indices/{prefix}-manual/_created", headers=ROOT).json() == {
        "index": f"{prefix}-manual", "created": False, "canUndo": False}
