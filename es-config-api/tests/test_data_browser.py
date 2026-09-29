from __future__ import annotations

import csv
import io
import json

from conftest import ROOT, as_user

API = "/api/v1/clusters/test/data"


def _seed(es, prefix):
    idx = f"{prefix}-shipment_summary-2024.09"
    es("PUT", f"/{idx}", json={"settings": {"number_of_replicas": 0}, "mappings": {"properties": {
        "vendor": {"type": "keyword"},
        "created_date": {"type": "date"},
        "amount": {"type": "double"},
        "note": {"type": "text"},
        "order_info": {"properties": {"order_number": {"type": "keyword"}, "order_id": {"type": "keyword"}}},
        "items": {"type": "nested", "properties": {"sku": {"type": "keyword"}, "qty": {"type": "integer"}}},
    }}})
    docs = []
    for i in range(30):
        docs.append({"vendor": "2593" if i % 3 else "2156", "created_date": f"2024-09-{i % 28 + 1:02d}T10:00:00Z",
                     "amount": i * 1.5, "note": "fast shipping" if i % 2 else "=HYPERLINK(\"x\")",
                     "order_info": {"order_number": f"SP0286{i:04d}38", "order_id": str(5487429550140 + i)},
                     "items": [{"sku": f"sku-{i}", "qty": 1}, {"sku": "common", "qty": i}]})
    body = "".join(json.dumps({"index": {"_id": str(5138553929788 + i)}}) + "\n" + json.dumps(d) + "\n"
                   for i, d in enumerate(docs))
    import requests
    from conftest import ES_PASSWORD, ES_URL, ES_USER
    r = requests.post(f"{ES_URL}/{idx}/_bulk", params={"refresh": "true"}, data=body,
                      headers={"Content-Type": "application/x-ndjson"}, auth=(ES_USER, ES_PASSWORD))
    assert r.ok and not r.json()["errors"], r.text
    # a dot index the pattern must never reach
    es("PUT", f"/.{prefix}-secret", json={"settings": {"number_of_replicas": 0}})
    es("POST", f"/.{prefix}-secret/_doc", json={"vendor": "2593", "secret": True}, params={"refresh": "true"})
    return idx


def test_fields_search_filters_sort(client, es, prefix):
    idx = _seed(es, prefix)
    r = client.get(f"{API}/{idx}/_fields", headers=ROOT)
    assert r.status_code == 200, r.text
    f = r.json()
    names = {x["name"]: x for x in f["fields"]}
    assert names["order_info.order_number"]["type"] == "keyword" and names["order_info"]["object"]
    assert f["nestedPaths"] == ["items"] and f["dateFields"] == ["created_date"]
    assert r.headers["cache-control"] == "no-store"

    s = lambda body: client.post(f"{API}/{idx}/_search", json=body, headers=ROOT)  # noqa: E731
    r = s({"size": 10})
    d = r.json()
    assert r.status_code == 200 and d["total"] == 30 and len(d["hits"]) == 10
    assert set(d["hits"][0]) >= {"_index", "_id", "_source"}

    # query string (AND by default) + nested filter + range + sort
    d = s({"query": "vendor:2593 AND order_info.order_number:SP0286*", "size": 100}).json()
    assert d["total"] == 20
    d = s({"filters": [{"field": "items.sku", "op": "is", "value": "sku-4"}]}).json()
    assert d["total"] == 1 and d["hits"][0]["_source"]["items"][0]["sku"] == "sku-4"
    d = s({"filters": [{"field": "amount", "op": "between", "gte": 3, "lte": 6}]}).json()
    assert d["total"] == 3
    d = s({"filters": [{"field": "vendor", "op": "is_not", "value": "2593"}]}).json()
    assert d["total"] == 10
    d = s({"filters": [{"field": "order_info.order_number", "op": "contains", "value": "sp0286001"}]}).json()
    assert d["total"] == 10  # SP0286 0010..0019 38, case-insensitive
    d = s({"filters": [{"field": "vendor", "op": "one_of", "values": ["2156"]}],
           "sort": [{"field": "amount", "order": "desc"}], "size": 2}).json()
    assert [h["_source"]["amount"] for h in d["hits"]] == [40.5, 36.0]
    d = s({"timeRange": {"field": "created_date", "gte": "2024-09-01T00:00:00Z", "lte": "2024-09-02T23:59:59Z"}}).json()
    assert d["total"] == 4
    d = s({"filters": [{"field": "missing_field", "op": "not_exists"}]}).json()
    assert d["total"] == 30

    # paging window and bad input
    r = s({"from": 9990, "size": 20})
    assert r.status_code == 400 and r.json()["error"]["code"] == "RESULT_WINDOW_EXCEEDED"
    r = s({"size": 101})
    assert r.status_code == 400
    r = s({"filters": [{"field": "vendor", "op": "is"}]})
    assert r.json()["error"]["code"] == "INVALID_FILTER"
    r = s({"sort": [{"field": "note"}]})  # text field can't be sorted
    assert r.status_code == 400 and r.json()["error"]["code"] == "ES_REJECTED"


def test_system_indices_never_reachable(client, es, prefix):
    _seed(es, prefix)
    for target in (f".{prefix}-secret", ".*", "_all", ".security-7"):
        r = client.post(f"{API}/{target}/_search", json={}, headers=ROOT)
        assert r.status_code in (400, 403), (target, r.text)
    r = client.get(f"{API}/.{prefix}-secret/_fields", headers=ROOT)
    assert r.json()["error"]["code"] == "SYSTEM_INDEX"
    # a wildcard that would also match the dot index returns only the normal one
    d = client.post(f"{API}/*{prefix}*/_search", json={"size": 100}, headers=ROOT).json()
    assert d["total"] == 30 and all(not h["_index"].startswith(".") for h in d["hits"])
    r = client.get(f"{API}/.{prefix}-secret/_doc/1", headers=ROOT)
    assert r.status_code == 403


def test_document_export_permissions_audit(client, env, es, prefix):
    idx = _seed(es, prefix)
    r = client.get(f"{API}/{idx}/_doc/5138553929788", headers=ROOT)
    assert r.status_code == 200 and r.json()["_source"]["vendor"] == "2156"
    r = client.get(f"{API}/{idx}/_doc/nope", headers=ROOT)
    assert r.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"

    # CSV export with chosen columns; formula cells are neutralised
    r = client.post(f"{API}/{idx}/_export", headers=ROOT, json={
        "format": "csv", "columns": ["vendor", "order_info.order_number", "note", "items.sku"],
        "sort": [{"field": "amount", "order": "asc"}]})
    assert r.status_code == 200, r.text
    assert "attachment" in r.headers["content-disposition"] and r.headers["x-export-rows"] == "30"
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0] == ["_index", "_id", "vendor", "order_info.order_number", "note", "items.sku"]
    assert rows[1][3] == "SP0286000038" and rows[1][4].startswith("'=") and rows[1][5] == '["sku-0", "common"]'
    r = client.post(f"{API}/{idx}/_export", headers=ROOT, json={"format": "json", "limit": 5})
    assert len(r.json()) == 5 and "_source" in r.json()[0]
    r = client.post(f"{API}/{idx}/_export", headers=ROOT, json={"format": "ndjson", "limit": 3})
    assert len(r.content.decode().strip().splitlines()) == 3

    # view is enough; no access = refused
    assert client.put("/api/v1/admin/users/viewer", json={"clusters": {"test": "view"}}, headers=ROOT).status_code == 200
    assert client.post(f"{API}/{idx}/_search", json={}, headers=as_user("viewer")).status_code == 200
    r = client.post("/api/v1/clusters/other/data/x/_search", json={}, headers=as_user("viewer"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "PERMISSION_DENIED"

    # audit: query recorded, documents never
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).date().isoformat()
    ev = client.get("/api/v1/admin/audit", params={"date": day, "action": "DATA_SEARCH", "user": "viewer"},
                    headers=ROOT).json()["items"]
    ok = [e for e in ev if e["outcome"] == "SUCCESS"]
    assert ok and ok[0]["hits"] == 30 and ok[0]["resource"] == idx and ok[0]["search"]["size"] == 25
    exp = client.get("/api/v1/admin/audit", params={"date": day, "action": "DATA_EXPORT"}, headers=ROOT).json()["items"]
    assert {e["format"] for e in exp} == {"csv", "json", "ndjson"}
    blob = json.dumps(ev + exp)
    assert "SP0286000038" not in blob and "HYPERLINK" not in blob
    denied = client.get("/api/v1/admin/audit", params={"date": day, "action": "DATA_SEARCH", "clusterId": "other"},
                        headers=ROOT).json()["items"]
    assert denied and denied[0]["outcome"] == "REJECTED"
