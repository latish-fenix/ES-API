"""Index-level permissions: a cluster default plus index-pattern rules (most specific wins)."""
from __future__ import annotations

from app.identity import User, index_level_from, normalize_access

from conftest import ROOT, as_user

API = "/api/v1/clusters/test"


def test_most_specific_rule_wins():
    access = normalize_access({"default": "view", "indices": [
        {"pattern": "shop*", "level": "edit"},
        {"pattern": "shop-payments-*", "level": "none"},
        {"pattern": "shop-orders-2024.09", "level": "delete"},
    ]})
    assert index_level_from(access, "logs-1") == "view"                   # default
    assert index_level_from(access, "shop-orders-2024.08") == "edit"      # shop*
    assert index_level_from(access, "shop-orders-2024.09") == "delete"    # exact beats pattern
    assert index_level_from(access, "shop-payments-2024.09") is None      # none excludes
    u = User("u", clusters={"test": {"default": None, "indices": [{"pattern": "a-*", "level": "view"}]}})
    assert u.level("test") is None and u.has_any_access("test") and u.can_index("test", "a-1", "view")
    assert not u.can_index("test", "b-1", "view") and not u.has_any_access("other")


def _mk(es, name, docs=2):
    es("PUT", f"/{name}", json={"settings": {"number_of_replicas": 0},
                                "mappings": {"properties": {"k": {"type": "keyword"}}}})
    for i in range(docs):
        es("POST", f"/{name}/_doc", json={"k": f"{name}-{i}"}, params={"refresh": "true"})


def test_rules_apply_everywhere(client, es, prefix, open_allowlist):
    orders, pay, logs = f"{prefix}-orders", f"{prefix}-payments", f"{prefix}-logs"
    for n in (orders, pay, logs):
        _mk(es, n)
    perms = {"test": {"default": None, "indices": [
        {"pattern": f"{prefix}-*", "level": "view"},
        {"pattern": orders, "level": "edit"},
        {"pattern": pay, "level": "none"},
    ]}}
    r = client.put("/api/v1/admin/users/ina", json={"clusters": perms}, headers=ROOT)
    assert r.status_code == 200, r.text
    ina = as_user("ina")

    me = client.get("/api/v1/me", headers=ina).json()
    assert me["clusters"] == {} and me["access"]["test"]["indices"][1] == {"pattern": orders, "level": "edit"}
    cl = client.get("/api/v1/clusters", headers=ina).json()["items"]
    assert cl[0]["id"] == "test" and cl[0]["permission"] is None and cl[0]["indexRules"]

    # the index list only has what she may see, with her level on each
    rows = {r["index"]: r["permission"] for r in client.get(f"{API}/indices", headers=ina).json()["items"]}
    assert rows == {orders: "edit", logs: "view"}

    # index settings follow the rules
    assert client.get(f"{API}/indices/{logs}/settings", headers=ina).status_code == 200
    assert client.get(f"{API}/indices/{pay}/settings", headers=ina).status_code == 403
    body = {"config": {"index.refresh_interval": "7s"}}
    assert client.put(f"{API}/indices/{orders}/settings?dryRun=true", json=body, headers=ina).status_code == 200
    r = client.put(f"{API}/indices/{logs}/settings?dryRun=true", json=body, headers=ina)
    assert r.status_code == 403 and r.json()["error"]["details"]["index"] == logs
    # no cluster default: templates etc. are off, health still works
    assert client.get(f"{API}/index-templates", headers=ina).status_code == 403
    assert client.get(f"{API}/health", headers=ina).status_code == 200

    # the data browser narrows a pattern to what she may see
    d = client.post(f"/api/v1/clusters/test/data/{prefix}-*/_search", json={"size": 50}, headers=ina).json()
    assert d["total"] == 4 and {h["_index"] for h in d["hits"]} == {orders, logs} and d["hiddenIndices"] == 1
    f = client.get(f"/api/v1/clusters/test/data/{prefix}-*/_fields", headers=ina).json()
    assert f["indices"] == sorted([orders, logs]) and f["editableIndices"] == [orders]
    r = client.post(f"/api/v1/clusters/test/data/{pay}/_search", json={}, headers=ina)
    assert r.status_code == 403
    # the root admin sees all three
    d = client.post(f"/api/v1/clusters/test/data/{prefix}-*/_search", json={"size": 50}, headers=ROOT).json()
    assert d["total"] == 6


def test_permission_validation(client):
    bad = [
        {"test": {"default": "owner"}},
        {"test": {"default": "view", "indices": [{"pattern": ".security*", "level": "view"}]}},
        {"test": {"default": "view", "indices": [{"pattern": "a,b", "level": "view"}]}},
        {"test": {"default": "view", "indices": [{"pattern": "a*", "level": "admin"}]}},
        {"test": {"default": "view", "indices": [{"pattern": "a*", "level": "view"}, {"pattern": "a*", "level": "edit"}]}},
        {"test": {"default": "view", "extra": 1}},
    ]
    for b in bad:
        r = client.put("/api/v1/admin/users/val", json={"clusters": b}, headers=ROOT)
        assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_PERMISSIONS", b
    # a cluster with no default and no rules is simply dropped
    r = client.put("/api/v1/admin/users/val", json={"clusters": {"test": {"default": None, "indices": []}}}, headers=ROOT)
    assert r.status_code in (200, 201)
    assert client.get("/api/v1/admin/users/val", headers=ROOT).json()["clusters"] == {}
    # plain levels still work, and a rules-only entry keeps its shape
    r = client.put("/api/v1/admin/users/val", json={"clusters": {"test": {"default": "view", "indices": []}}}, headers=ROOT)
    assert r.json()["clusters"] == {"test": "view"}
