from __future__ import annotations

from datetime import datetime, timezone

from conftest import ROOT, as_user

API = "/api/v1/clusters/test"


def _allow(client, patterns):
    r = client.put("/api/v1/admin/allowlist", headers=ROOT,
                   json={"index-delete": {"allow": patterns}})
    assert r.status_code == 200, r.text


def _mk(es, name, docs=0, **extra):
    es("PUT", f"/{name}", json={"settings": {"number_of_replicas": 0},
                                "mappings": {"properties": {"sku": {"type": "keyword"}}}, **extra})
    for i in range(docs):
        es("POST", f"/{name}/_doc", json={"sku": f"s{i}"}, params={"refresh": "true"})


def test_delete_index_full_flow(client, env, es, prefix):
    idx = f"{prefix}-old"
    _mk(es, idx, docs=3, aliases={f"{prefix}-alias": {}})
    _allow(client, [f"{prefix}-*"])
    url = f"{API}/indices/{idx}"

    # dry run: shows what would go, deletes nothing, needs no confirm/reason
    r = client.delete(url, params={"dryRun": "true"}, headers=ROOT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["applied"] is False and d["docsCount"] == 3 and d["aliases"] == [f"{prefix}-alias"]
    assert d["permanent"] and d["confirmRequired"] == idx
    assert any("3 documents" in w for w in d["warnings"])
    assert es("HEAD", f"/{idx}", ok=False) is None or True
    assert es("GET", f"/{idx}", ok=False).get(idx)

    # guards
    r = client.delete(url, params={"confirm": idx}, headers=ROOT)
    assert r.status_code == 400 and r.json()["error"]["code"] == "REASON_REQUIRED"
    r = client.delete(url, params={"confirm": "wrong", "reason": "x"}, headers=ROOT)
    assert r.status_code == 400 and r.json()["error"]["code"] == "CONFIRMATION_MISMATCH"
    r = client.delete(f"{API}/indices/{prefix}-alias", params={"dryRun": "true"}, headers=ROOT)
    assert r.status_code == 422 and r.json()["error"]["code"] == "NOT_A_CONCRETE_INDEX"
    r = client.delete(f"{API}/indices/{prefix}-*", params={"dryRun": "true"}, headers=ROOT)
    assert r.status_code == 400

    # real delete
    r = client.delete(url, params={"confirm": idx, "reason": "retired"}, headers=ROOT)
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True
    assert es("GET", f"/{idx}", ok=False).get("status") == 404

    # tombstone: settings + mappings saved in S3, listed by the API
    tomb, _ = env["store"].get_json(r.json()["tombstoneKey"])
    assert tomb["definition"]["mappings"]["properties"]["sku"] == {"type": "keyword"}
    assert tomb["summary"]["docsCount"] == 3 and tomb["deletedBy"] == "root"
    lst = client.get(f"{API}/deleted-indices", headers=ROOT).json()["items"]
    assert lst[0]["index"] == idx and lst[0]["reason"] == "retired"

    # audit
    today = datetime.now(timezone.utc).date().isoformat()
    ev = client.get(f"/api/v1/admin/audit?date={today}&action=INDEX_DELETE", headers=ROOT).json()["items"]
    assert any(e["outcome"] == "SUCCESS" and e["resource"] == idx for e in ev)

    # gone now
    r = client.delete(url, params={"dryRun": "true"}, headers=ROOT)
    assert r.status_code == 404


def test_delete_needs_delete_level_and_allowlist(client, es, prefix):
    idx = f"{prefix}-perm"
    _mk(es, idx)
    url = f"{API}/indices/{idx}"
    q = {"confirm": idx, "reason": "x"}
    client.put("/api/v1/admin/users/ed", json={"clusters": {"test": "edit"}}, headers=ROOT)
    client.put("/api/v1/admin/users/del", json={"clusters": {"test": "delete"}}, headers=ROOT)

    _allow(client, [f"{prefix}-*"])
    r = client.delete(url, params=q, headers=as_user("ed"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "PERMISSION_DENIED"

    # delete level also includes view and edit
    assert client.get(f"{API}/indices/{idx}/settings", headers=as_user("del")).status_code == 200

    # no index-delete entry -> everything blocked
    client.put("/api/v1/admin/allowlist", headers=ROOT, json={"cluster-settings": {"allow": ["*"]}})
    r = client.delete(url, params=q, headers=as_user("del"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "NOT_ALLOWLISTED"

    _allow(client, ["logs-*"])
    assert client.delete(url, params=q, headers=as_user("del")).status_code == 403

    _allow(client, [f"{prefix}-*"])
    r = client.delete(url, params=q, headers=as_user("del"))
    assert r.status_code == 200, r.text

    r = client.put("/api/v1/admin/users/bad", json={"clusters": {"test": "owner"}}, headers=ROOT)
    assert r.status_code == 400


def test_dot_index_needs_dot_pattern(client, es, prefix):
    idx = f".{prefix}-sys"
    _mk(es, idx)
    try:
        _allow(client, ["*"])
        r = client.delete(f"{API}/indices/{idx}", params={"dryRun": "true"}, headers=ROOT)
        assert r.status_code == 403
        _allow(client, ["*", ".cfgtest-*"])
        r = client.delete(f"{API}/indices/{idx}", params={"dryRun": "true"}, headers=ROOT)
        assert r.status_code == 200, r.text
    finally:
        es("DELETE", f"/{idx}", ok=False)


def test_data_stream_write_index_refused(client, es, prefix):
    ds = f"{prefix}-ds"
    es("PUT", f"/_index_template/{prefix}-dstpl", json={
        "index_patterns": [f"{ds}*"], "data_stream": {}, "priority": 900,
        "template": {"settings": {"number_of_replicas": 0}}})
    es("PUT", f"/_data_stream/{ds}")
    first = es("GET", f"/_data_stream/{ds}")["data_streams"][0]["indices"][0]["index_name"]
    _allow(client, [".ds-cfgtest-*", "cfgtest-*"])

    r = client.delete(f"{API}/indices/{ds}", params={"dryRun": "true"}, headers=ROOT)
    assert r.status_code == 422 and r.json()["error"]["code"] == "NOT_A_CONCRETE_INDEX"

    r = client.delete(f"{API}/indices/{first}", params={"dryRun": "true"}, headers=ROOT)
    assert r.status_code == 422 and r.json()["error"]["code"] == "DATA_STREAM_WRITE_INDEX"

    es("POST", f"/{ds}/_rollover")
    r = client.delete(f"{API}/indices/{first}", params={"dryRun": "true"}, headers=ROOT)
    assert r.status_code == 200, r.text
    assert any("backing index" in w for w in r.json()["warnings"])
    r = client.delete(f"{API}/indices/{first}", params={"confirm": first, "reason": "old"}, headers=ROOT)
    assert r.status_code == 200, r.text


def test_closed_index_can_be_deleted(client, es, prefix):
    idx = f"{prefix}-closed"
    _mk(es, idx)
    es("POST", f"/{idx}/_close")
    _allow(client, [f"{prefix}-*"])
    r = client.delete(f"{API}/indices/{idx}", params={"dryRun": "true"}, headers=ROOT)
    assert r.status_code == 200 and r.json()["status"] == "close", r.text
    r = client.delete(f"{API}/indices/{idx}", params={"confirm": idx, "reason": "x"}, headers=ROOT)
    assert r.status_code == 200, r.text
