"""Document writes: single edits with undo, bulk update / delete with a mandatory dry run."""
from __future__ import annotations

from conftest import ROOT, as_user

API = "/api/v1/clusters/test/data"


def _seed(es, prefix, n=12):
    idx = f"{prefix}-orders"
    es("PUT", f"/{idx}", json={"settings": {"number_of_replicas": 0}, "mappings": {"properties": {
        "status": {"type": "keyword"}, "vendor": {"type": "keyword"}, "total": {"type": "double"},
        "info": {"properties": {"carrier": {"type": "keyword"}}}}}})
    for i in range(n):
        es("PUT", f"/{idx}/_doc/o{i}", json={"status": "new" if i % 2 else "paid", "vendor": "v1" if i < 8 else "v2",
                                             "total": i * 10.0, "info": {"carrier": "UPS"}},
           params={"refresh": "true"})
    return idx


def test_edit_create_delete_and_undo(client, env, es, prefix):
    idx = _seed(es, prefix)
    doc = client.get(f"{API}/{idx}/_doc/o1", headers=ROOT).json()
    new = {**doc["_source"], "status": "shipped", "info": {"carrier": "DHL"}}
    body = {"document": new, "ifSeqNo": doc["_seq_no"], "ifPrimaryTerm": doc["_primary_term"]}

    # dry run: diff only
    r = client.put(f"{API}/{idx}/_doc/o1", params={"dryRun": "true"}, json=body, headers=ROOT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert not d.get("applied") and {c["path"] for c in d["diff"]["changed"]} == {"status", "info.carrier"}
    assert es("GET", f"/{idx}/_doc/o1")["_source"]["status"] == "new"
    # real: reason needed; saved version; applied
    r = client.put(f"{API}/{idx}/_doc/o1", json=body, headers=ROOT)
    assert r.json()["error"]["code"] == "REASON_REQUIRED"
    r = client.put(f"{API}/{idx}/_doc/o1", json={**body, "reason": "carrier switch"}, headers=ROOT)
    assert r.status_code == 200 and r.json()["applied"], r.text
    assert es("GET", f"/{idx}/_doc/o1")["_source"]["info"]["carrier"] == "DHL"
    # the old seq_no no longer works: someone (we) changed it since
    r = client.put(f"{API}/{idx}/_doc/o1", json={**body, "reason": "again"}, headers=ROOT)
    assert r.status_code == 409 and r.json()["error"]["code"] == "DOCUMENT_CHANGED"

    # history + restore (dry run, then real)
    hist = client.get(f"{API}/{idx}/_doc/o1/_history", headers=ROOT).json()["items"]
    assert hist[0]["action"] == "UPDATE" and hist[0]["before"]["source"]["status"] == "new"
    rb = {"versionKey": hist[0]["key"], "reason": "undo"}
    r = client.post(f"{API}/{idx}/_doc/o1/_restore", params={"dryRun": "true"}, json=rb, headers=ROOT)
    assert r.json()["plan"] == "overwrite" and r.json()["diff"]["changed"]
    r = client.post(f"{API}/{idx}/_doc/o1/_restore", json=rb, headers=ROOT)
    assert r.status_code == 200 and r.json()["applied"], r.text
    assert es("GET", f"/{idx}/_doc/o1")["_source"]["status"] == "new"

    # create with an id, then delete (typed confirmation), then undo the delete
    r = client.post(f"{API}/{idx}/_doc", json={"id": "n1", "document": {"status": "new"}, "reason": "manual order"},
                    headers=ROOT)
    assert r.status_code == 201 and r.json()["id"] == "n1", r.text
    r = client.post(f"{API}/{idx}/_doc", json={"id": "n1", "document": {"status": "x"}, "reason": "dup"}, headers=ROOT)
    assert r.status_code == 409 and r.json()["error"]["code"] == "DOCUMENT_EXISTS"
    r = client.delete(f"{API}/{idx}/_doc/n1", params={"reason": "test", "confirm": "nope"}, headers=ROOT)
    assert r.json()["error"]["code"] == "CONFIRMATION_MISMATCH"
    r = client.delete(f"{API}/{idx}/_doc/n1", params={"reason": "test", "confirm": "n1"}, headers=ROOT)
    assert r.status_code == 200 and r.json()["deleted"]
    assert es("GET", f"/{idx}/_doc/n1", ok=False)["found"] is False
    key = client.get(f"{API}/{idx}/_doc/n1/_history", headers=ROOT).json()["items"][0]["key"]
    r = client.post(f"{API}/{idx}/_doc/n1/_restore", json={"versionKey": key, "reason": "oops"}, headers=ROOT)
    assert r.json()["plan"] == "recreate" and es("GET", f"/{idx}/_doc/n1")["_source"] == {"status": "new"}

    # audit keeps field names, never values
    ev = client.get("/api/v1/admin/audit", params={"date": _today(), "action": "DATA_DOC_UPDATE"}, headers=ROOT).json()["items"]
    ok = [e for e in ev if e["outcome"] == "SUCCESS" and e.get("reason") == "carrier switch"]
    assert ok[0]["changedFields"] == ["info.carrier", "status"] and ok[0]["versionKey"] and "DHL" not in str(ev)


def test_permissions_for_writes(client, es, prefix):
    idx = _seed(es, prefix, 2)
    client.put("/api/v1/admin/users/vi", json={"clusters": {"test": "view"}}, headers=ROOT)
    client.put("/api/v1/admin/users/ed", json={"clusters": {"test": {"default": "view", "indices": [
        {"pattern": idx, "level": "edit"}]}}}, headers=ROOT)
    doc = {"document": {"status": "x"}, "reason": "r"}
    r = client.put(f"{API}/{idx}/_doc/o0", json=doc, headers=as_user("vi"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "PERMISSION_DENIED"
    assert client.put(f"{API}/{idx}/_doc/o0", json=doc, headers=as_user("ed")).status_code == 200
    # bulk delete needs delete, bulk update needs edit
    r = client.post(f"{API}/{idx}/_bulk_delete", params={"dryRun": "true"}, json={"query": "*"}, headers=as_user("ed"))
    assert r.status_code == 403
    r = client.post(f"{API}/{idx}/_bulk_update", params={"dryRun": "true"}, json={"set": {"status": "y"}},
                    headers=as_user("ed"))
    assert r.status_code == 200, r.text
    # system indices are never writable
    r = client.post(f"{API}/.security-7/_doc", json=doc, headers=ROOT)
    assert r.status_code == 403 and r.json()["error"]["code"] == "SYSTEM_INDEX"


def test_bulk_update_requires_dry_run_and_can_be_restored(client, es, prefix):
    idx = _seed(es, prefix)
    body = {"query": "vendor:v1", "filters": [{"field": "status", "op": "is", "value": "new"}],
            "set": {"status": "cancelled", "info.reason": "vendor stopped"}, "remove": ["total"]}
    # no dry run -> refused
    r = client.post(f"{API}/{idx}/_bulk_update", json={**body, "reason": "x", "expectedCount": 4}, headers=ROOT)
    assert r.status_code == 400 and r.json()["error"]["code"] == "DRY_RUN_REQUIRED"
    d = client.post(f"{API}/{idx}/_bulk_update", params={"dryRun": "true"}, json=body, headers=ROOT).json()
    assert d["count"] == 4 and d["willChange"] == 4 and len(d["sample"]) == 4 and d["dryRunToken"]
    s0 = d["sample"][0]["diff"]
    assert {c["path"] for c in s0["changed"]} == {"status"} and s0["added"][0]["path"] == "info.reason"
    assert s0["removed"][0]["path"] == "total"
    tok = d["dryRunToken"]
    # changed request / wrong count / other user -> refused
    r = client.post(f"{API}/{idx}/_bulk_update", json={**body, "set": {"status": "other"}, "reason": "x",
                                                        "dryRunToken": tok, "expectedCount": 4}, headers=ROOT)
    assert r.json()["error"]["code"] == "DRY_RUN_MISMATCH"
    r = client.post(f"{API}/{idx}/_bulk_update", json={**body, "reason": "x", "dryRunToken": tok, "expectedCount": 5},
                    headers=ROOT)
    assert r.json()["error"]["code"] == "COUNT_MISMATCH"
    # data changed after the dry run -> refused
    es("PUT", f"/{idx}/_doc/extra", json={"status": "new", "vendor": "v1"}, params={"refresh": "true"})
    r = client.post(f"{API}/{idx}/_bulk_update", json={**body, "reason": "x", "dryRunToken": tok, "expectedCount": 4},
                    headers=ROOT)
    assert r.status_code == 409 and r.json()["error"]["code"] == "COUNT_CHANGED"
    es("DELETE", f"/{idx}/_doc/extra", params={"refresh": "true"})
    r = client.post(f"{API}/{idx}/_bulk_update", json={**body, "reason": "vendor v1 stopped", "dryRunToken": tok,
                                                        "expectedCount": 4}, headers=ROOT)
    assert r.status_code == 200 and r.json()["succeeded"] == 4 and r.json()["conflicts"] == 0, r.text
    src = es("GET", f"/{idx}/_doc/o1")["_source"]
    assert src["status"] == "cancelled" and src["info"] == {"carrier": "UPS", "reason": "vendor stopped"}
    assert "total" not in src
    change = r.json()["changeId"]

    # listed, then restored (dry run mandatory again)
    items = client.get(f"{API}/_changes", headers=ROOT).json()["items"]
    assert items[0]["changeId"] == change and items[0]["op"] == "update" and items[0]["count"] == 4
    r = client.post(f"{API}/_changes/{change}/_restore", json={"reason": "undo"}, headers=ROOT)
    assert r.json()["error"]["code"] == "DRY_RUN_REQUIRED"
    d = client.post(f"{API}/_changes/{change}/_restore", params={"dryRun": "true"}, json={}, headers=ROOT).json()
    r = client.post(f"{API}/_changes/{change}/_restore", headers=ROOT,
                    json={"reason": "undo", "dryRunToken": d["dryRunToken"], "expectedCount": d["count"]})
    assert r.status_code == 200 and r.json()["succeeded"] == 4, r.text
    src = es("GET", f"/{idx}/_doc/o1")["_source"]
    assert src["status"] == "new" and src["total"] == 10.0


def test_bulk_delete_and_limits(client, es, prefix):
    idx = _seed(es, prefix)
    body = {"filters": [{"field": "vendor", "op": "is", "value": "v2"}]}
    d = client.post(f"{API}/{idx}/_bulk_delete", params={"dryRun": "true"}, json=body, headers=ROOT).json()
    assert d["count"] == 4 and d["sample"][0]["source"]["vendor"] == "v2"
    r = client.post(f"{API}/{idx}/_bulk_delete", headers=ROOT,
                    json={**body, "reason": "v2 test data", "dryRunToken": d["dryRunToken"], "expectedCount": 4})
    assert r.status_code == 200 and r.json()["succeeded"] == 4, r.text
    assert es("GET", f"/{idx}/_count")["count"] == 8
    # restore brings them back
    change = r.json()["changeId"]
    d = client.post(f"{API}/_changes/{change}/_restore", params={"dryRun": "true"}, json={}, headers=ROOT).json()
    client.post(f"{API}/_changes/{change}/_restore", headers=ROOT,
                json={"reason": "undo", "dryRunToken": d["dryRunToken"], "expectedCount": d["count"]})
    es("POST", f"/{idx}/_refresh")
    assert es("GET", f"/{idx}/_count")["count"] == 12
    # empty match: nothing to do, still a clear answer; everything-matching gets a warning
    d = client.post(f"{API}/{idx}/_bulk_delete", params={"dryRun": "true"}, json={}, headers=ROOT).json()
    assert d["count"] == 12 and d["warnings"]
    r = client.post(f"{API}/{idx}/_bulk_update", params={"dryRun": "true"}, json={"set": {"_id": "x"}}, headers=ROOT)
    assert r.json()["error"]["code"] == "INVALID_FIELD"
    r = client.post(f"{API}/{idx}/_bulk_update", params={"dryRun": "true"}, json={}, headers=ROOT)
    assert r.json()["error"]["code"] == "NOTHING_TO_CHANGE"


def _today() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).date().isoformat()
