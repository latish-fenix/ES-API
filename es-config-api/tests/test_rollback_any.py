"""Roll back any change: config changes from the audit log (not only the latest), deleted
indices recreated empty, and the Data page's recent-changes feed."""
from __future__ import annotations

from datetime import datetime, timezone

from conftest import ROOT, as_user

API = "/api/v1/clusters/test"
REASON = {"reason": "test"}


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _audit(client, action):
    return client.get("/api/v1/admin/audit", params={"date": _today(), "action": action}, headers=ROOT).json()["items"]


def _age(es, pol):
    return es("GET", f"/_ilm/policy/{pol}")[pol]["policy"]["phases"]["hot"]["actions"]["rollover"]["max_age"]


def _policy(age):
    return {"policy": {"phases": {"hot": {"actions": {"rollover": {"max_age": age}}}}}}


def test_restore_any_config_change(client, es, prefix, open_allowlist):
    pol = f"{prefix}-pol"
    ids = []
    for age in ("7d", "5d", "3d", "1d"):      # create, then three changes
        r = client.put(f"{API}/ilm-policies/{pol}", json={"config": _policy(age), **REASON}, headers=ROOT)
        assert r.status_code == 200 and r.json()["applied"], r.text
        ids.append(r.json()["changeId"])
    assert _age(es, pol) == "1d"

    # the history lists every applied change, newest first
    h = client.get(f"{API}/config-history", params={"configType": "ilm-policies", "resource": pol}, headers=ROOT)
    assert h.status_code == 200, h.text
    assert [i["changeId"] for i in h.json()["items"]] == ids[::-1]
    assert h.json()["items"][-1]["createdResource"] is True

    # undo the second change (7d -> 5d): back to 7d, although two changes came after it
    url = f"{API}/config-history/{ids[1]}/_restore"
    body = {"configType": "ilm-policies", "resource": pol}
    r = client.post(url, params={"dryRun": "true"}, json=body, headers=ROOT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["dryRun"] and not d["applied"] and _age(es, pol) == "1d"
    assert any(c["path"].endswith("max_age") and c["before"] == "1d" and c["after"] == "7d" for c in d["diff"]["changed"])
    assert any("changed again after that change" in w for w in d["warnings"])
    r = client.post(url, json=body, headers=ROOT)
    assert r.json()["error"]["code"] == "REASON_REQUIRED"
    r = client.post(url, json={**body, "reason": "undo Monday's change"}, headers=ROOT)
    assert r.status_code == 200 and r.json()["applied"], r.text
    assert _age(es, pol) == "7d"
    ev = [e for e in _audit(client, "RESTORE") if e.get("resource") == pol]
    assert ev and ev[0]["restoreOf"] == ids[1] and ev[0]["outcome"] == "SUCCESS"

    # the restore is itself a change in the history, so it can be undone too
    restore_id = r.json()["changeId"]
    r = client.post(f"{API}/config-history/{restore_id}/_restore", json={**body, "reason": "redo"}, headers=ROOT)
    assert r.status_code == 200 and _age(es, pol) == "1d"
    # and the ordinary one-step rollback still works after a restore
    r = client.post(f"{API}/ilm-policies/{pol}/rollback", json=REASON, headers=ROOT)
    assert r.status_code == 200 and _age(es, pol) == "7d"

    # undoing the creation deletes the policy
    r = client.post(f"{API}/config-history/{ids[0]}/_restore", params={"dryRun": "true"}, json=body, headers=ROOT)
    assert r.json()["deletesResource"] is True

    # unknown change, wrong resource, edit level needed
    r = client.post(f"{API}/config-history/{'0' * 32}/_restore", json={**body, **REASON}, headers=ROOT)
    assert r.status_code == 404 and r.json()["error"]["code"] == "CHANGE_NOT_FOUND"
    r = client.post(url, json={"configType": "ilm-policies", "resource": f"{prefix}-other", **REASON}, headers=ROOT)
    assert r.status_code == 404
    client.put("/api/v1/admin/users/viewer", json={"clusters": {"test": "view"}}, headers=ROOT)
    r = client.post(url, json={**body, **REASON}, headers=as_user("viewer"))
    assert r.status_code == 403


def test_restore_cluster_setting_and_mapping_refused(client, es, prefix, open_allowlist):
    key = "indices.recovery.max_bytes_per_sec"
    es("PUT", "/_cluster/settings", json={"persistent": {key: None}})
    r1 = client.put(f"{API}/cluster-settings", json={"config": {key: "41mb"}, **REASON}, headers=ROOT)
    r2 = client.put(f"{API}/cluster-settings", json={"config": {key: "42mb"}, **REASON}, headers=ROOT)
    assert r1.status_code == r2.status_code == 200
    body = {"configType": "cluster-settings", "resource": "_cluster", **REASON}
    r = client.post(f"{API}/config-history/{r1.json()['changeId']}/_restore", json=body, headers=ROOT)
    assert r.status_code == 200, r.text
    got = es("GET", "/_cluster/settings")["persistent"]
    assert key not in str(got)   # the state before the first change: not set
    es("PUT", "/_cluster/settings", json={"persistent": {key: None}})

    idx = f"{prefix}-m"
    es("PUT", f"/{idx}", json={"settings": {"number_of_replicas": 0}})
    r = client.put(f"{API}/indices/{idx}/mapping", json={"config": {"properties": {"a": {"type": "keyword"}}}, **REASON},
                   headers=ROOT)
    assert r.status_code == 200, r.text
    r = client.post(f"{API}/config-history/{r.json()['changeId']}/_restore",
                    json={"configType": "index-mappings", "resource": idx, **REASON}, headers=ROOT)
    assert r.status_code == 422 and r.json()["error"]["code"] == "ROLLBACK_NOT_SUPPORTED"


def test_recreate_deleted_index(client, env, es, prefix):
    idx = f"{prefix}-gone"
    es("PUT", f"/{idx}", json={"settings": {"number_of_replicas": 0, "refresh_interval": "7s"},
                               "mappings": {"properties": {"sku": {"type": "keyword"}}},
                               "aliases": {f"{prefix}-al": {}}})
    es("POST", f"/{idx}/_doc", json={"sku": "a"}, params={"refresh": "true"})
    client.put("/api/v1/admin/allowlist", json={"index-delete": {"allow": [f"{prefix}-*"]}}, headers=ROOT)
    r = client.delete(f"{API}/indices/{idx}", params={"confirm": idx, "reason": "cleanup"}, headers=ROOT)
    assert r.status_code == 200, r.text
    key = r.json()["tombstoneKey"]

    r = client.post(f"{API}/deleted-indices/_recreate", params={"dryRun": "true"}, json={"key": key}, headers=ROOT)
    assert r.status_code == 200, r.text
    d = r.json()
    s = d["body"]["settings"]["index"]
    assert s["refresh_interval"] == "7s" and "uuid" not in s and "creation_date" not in s and "provided_name" not in s
    assert d["docsLost"] == 1 and any("EMPTY" in w for w in d["warnings"])
    assert es("GET", f"/{idx}", ok=False).get("status") == 404

    r = client.post(f"{API}/deleted-indices/_recreate", json={"key": key}, headers=ROOT)
    assert r.json()["error"]["code"] == "REASON_REQUIRED"
    r = client.post(f"{API}/deleted-indices/_recreate", json={"key": key, "reason": "deleted by mistake"}, headers=ROOT)
    assert r.status_code == 200 and r.json()["applied"], r.text
    got = es("GET", f"/{idx}")[idx]
    assert got["mappings"]["properties"]["sku"] == {"type": "keyword"} and f"{prefix}-al" in got["aliases"]
    assert got["settings"]["index"]["refresh_interval"] == "7s"
    assert es("GET", f"/{idx}/_count")["count"] == 0
    lst = client.get(f"{API}/deleted-indices", headers=ROOT).json()["items"]
    assert next(t for t in lst if t["key"] == key)["recreatedBy"] == "root"
    assert any(e["outcome"] == "SUCCESS" and e["resource"] == idx for e in _audit(client, "INDEX_RECREATE"))

    # exists again -> refused; bad key; view-only user refused
    r = client.post(f"{API}/deleted-indices/_recreate", json={"key": key, **REASON}, headers=ROOT)
    assert r.status_code == 409 and r.json()["error"]["code"] == "INDEX_EXISTS"
    r = client.post(f"{API}/deleted-indices/_recreate", json={"key": "state/users.json", **REASON}, headers=ROOT)
    assert r.status_code == 400
    client.put("/api/v1/admin/users/viewer2", json={"clusters": {"test": "view"}}, headers=ROOT)
    es("DELETE", f"/{idx}")
    r = client.post(f"{API}/deleted-indices/_recreate", json={"key": key, **REASON}, headers=as_user("viewer2"))
    assert r.status_code == 403


def _seed(es, idx, n=6):
    es("PUT", f"/{idx}", json={"settings": {"number_of_replicas": 0},
                               "mappings": {"properties": {"status": {"type": "keyword"}}}})
    for i in range(n):
        es("PUT", f"/{idx}/_doc/d{i}", json={"status": "new", "n": i}, params={"refresh": "true"})


def test_recent_changes_feed_and_rollback(client, es, prefix):
    idx, other = f"{prefix}-feed", f"{prefix}-other"
    _seed(es, idx)
    _seed(es, other, 2)
    D = f"{API}/data"
    doc = client.get(f"{D}/{idx}/_doc/d1", headers=ROOT).json()
    r = client.put(f"{D}/{idx}/_doc/d1", headers=ROOT, json={
        "document": {**doc["_source"], "status": "paid"}, "reason": "paid", "ifSeqNo": doc["_seq_no"],
        "ifPrimaryTerm": doc["_primary_term"]})
    assert r.status_code == 200, r.text
    r = client.delete(f"{D}/{idx}/_doc/d2", params={"confirm": "d2", "reason": "dup"}, headers=ROOT)
    assert r.status_code == 200
    spec = {"query": "status:new", "set": {"status": "held"}}
    dry = client.post(f"{D}/{idx}/_bulk_update", params={"dryRun": "true"}, json=spec, headers=ROOT).json()
    r = client.post(f"{D}/{idx}/_bulk_update", headers=ROOT, json={**spec, "reason": "hold", "dryRunToken": dry["dryRunToken"],
                                                                  "expectedCount": dry["count"]})
    assert r.status_code == 200 and r.json()["succeeded"] == 4, r.text
    bulk_id = r.json()["changeId"]
    client.put(f"{D}/{other}/_doc/d0", headers=ROOT, json={"document": {"status": "x"}, "reason": "other"})

    # newest first, only this index, each with what's needed to roll it back
    r = client.get(f"{D}/{idx}/_recent", headers=ROOT)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["action"] for i in items] == ["BULK_UPDATE", "DELETE", "UPDATE"]
    assert items[0]["changeId"] == bulk_id and items[0]["count"] == 4 and items[0]["canRollBack"]
    assert items[2]["id"] == "d1" and items[2]["versionKey"] and items[2]["fields"] == ["status"]
    assert all(i["index"] == idx for i in items)
    # a pattern covers both indices
    assert {i["index"] for i in client.get(f"{D}/{prefix}-*/_recent", headers=ROOT).json()["items"]} == {idx, other}

    # roll back the delete from the feed (the document comes back) -> flagged as rolled back
    dele = items[1]
    r = client.post(f"{D}/{idx}/_doc/d2/_restore", json={"versionKey": dele["versionKey"], "reason": "undo"}, headers=ROOT)
    assert r.status_code == 200 and r.json()["plan"] == "recreate", r.text
    items = client.get(f"{D}/{idx}/_recent", headers=ROOT).json()["items"]
    assert items[0]["action"] == "RESTORE" and items[0]["restoreOf"] == dele["changeId"]
    assert next(i for i in items if i["changeId"] == dele["changeId"])["rolledBack"] is True

    # roll back the bulk change, then roll back that restore (restore of a restore)
    dry = client.post(f"{D}/_changes/{bulk_id}/_restore", params={"dryRun": "true"}, json={}, headers=ROOT).json()
    r = client.post(f"{D}/_changes/{bulk_id}/_restore", headers=ROOT,
                    json={"reason": "undo hold", "dryRunToken": dry["dryRunToken"], "expectedCount": dry["count"]})
    assert r.status_code == 200 and r.json()["succeeded"] == 4, r.text
    assert es("GET", f"/{idx}/_doc/d0")["_source"]["status"] == "new"
    restore_id = r.json()["changeId"]
    dry = client.post(f"{D}/_changes/{restore_id}/_restore", params={"dryRun": "true"}, json={}, headers=ROOT).json()
    r = client.post(f"{D}/_changes/{restore_id}/_restore", headers=ROOT,
                    json={"reason": "redo hold", "dryRunToken": dry["dryRunToken"], "expectedCount": dry["count"]})
    assert r.status_code == 200, r.text
    assert es("GET", f"/{idx}/_doc/d0")["_source"]["status"] == "held"

    # a viewer sees the feed but may not roll back
    client.put("/api/v1/admin/users/vw", json={"clusters": {"test": "view"}}, headers=ROOT)
    items = client.get(f"{D}/{idx}/_recent", headers=as_user("vw")).json()["items"]
    assert items and not any(i["canRollBack"] for i in items)
    # someone without access to the other index doesn't see its changes
    client.put("/api/v1/admin/users/only", json={"clusters": {"test": {"default": None, "indices": [
        {"pattern": idx, "level": "edit"}]}}}, headers=ROOT)
    r = client.get(f"{D}/{prefix}-*/_recent", headers=as_user("only"))
    assert r.status_code == 200 and {i["index"] for i in r.json()["items"]} == {idx}
