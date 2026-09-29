from __future__ import annotations

import time
from datetime import datetime, timezone

from conftest import ROOT, as_user

API = "/api/v1/clusters/test"
REASON = {"reason": "test change"}


# ------------------------------------------------------------------ identity
def test_missing_and_unknown_user(client):
    assert client.get("/api/v1/clusters").status_code == 401
    r = client.get("/api/v1/clusters", headers=as_user("nobody"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "UNKNOWN_USER"


def test_permissions_view_vs_edit(client, open_allowlist):
    r = client.put("/api/v1/admin/users/alice", json={"clusters": {"test": "view"}}, headers=ROOT)
    assert r.status_code == 200, r.text
    alice = as_user("alice")
    clusters = client.get("/api/v1/clusters", headers=alice).json()["items"]
    assert [c["id"] for c in clusters] == ["test"] and clusters[0]["permission"] == "view"
    assert client.get(f"{API}/cluster-settings", headers=alice).status_code == 200
    r = client.put(f"{API}/cluster-settings", json={"config": {"x": "y"}, **REASON}, headers=alice)
    assert r.status_code == 403 and r.json()["error"]["code"] == "PERMISSION_DENIED"
    assert client.get("/api/v1/clusters/other/cluster-settings", headers=alice).status_code == 403
    # admin endpoints are admin-only
    assert client.get("/api/v1/admin/users", headers=alice).status_code == 403
    # upgrade to edit via the permissions endpoint
    r = client.put("/api/v1/admin/users/alice/permissions", json={"test": "edit"}, headers=ROOT)
    assert r.json()["clusters"] == {"test": "edit"}
    r = client.put(f"{API}/cluster-settings?dryRun=true",
                   json={"config": {"indices.recovery.max_bytes_per_sec": "41mb"}}, headers=alice)
    assert r.status_code == 200, r.text


def test_permissions_validation(client):
    r = client.put("/api/v1/admin/users/bob", json={"clusters": {"nope": "view"}}, headers=ROOT)
    assert r.status_code == 400 and r.json()["error"]["code"] == "UNKNOWN_CLUSTER"
    r = client.put("/api/v1/admin/users/bob", json={"clusters": {"test": "owner"}}, headers=ROOT)
    assert r.status_code == 400


# ----------------------------------------------------------------- allowlist
def test_empty_allowlist_blocks_everything(client, prefix):
    r = client.put(f"{API}/cluster-settings",
                   json={"config": {"indices.recovery.max_bytes_per_sec": "41mb"}, **REASON},
                   headers=ROOT)
    assert r.status_code == 403 and r.json()["error"]["code"] == "NOT_ALLOWLISTED"


def test_deny_wins_and_cluster_override(client, prefix):
    client.put("/api/v1/admin/allowlist", headers=ROOT, json={
        "cluster-settings": {"allow": ["cluster.routing.allocation.*", "indices.recovery.*"],
                             "deny": ["cluster.routing.allocation.disk.watermark.high"]}})
    ok = {"config": {"cluster.routing.allocation.disk.watermark.low": "86%"}}
    blocked = {"config": {"cluster.routing.allocation.disk.watermark.high": "91%"}}
    assert client.put(f"{API}/cluster-settings?dryRun=true", json=ok, headers=ROOT).status_code == 200
    r = client.put(f"{API}/cluster-settings?dryRun=true", json=blocked, headers=ROOT)
    assert r.status_code == 403
    assert r.json()["error"]["details"]["blocked"] == ["cluster.routing.allocation.disk.watermark.high"]
    # per-cluster override replaces global
    client.put("/api/v1/admin/allowlist/test", headers=ROOT,
               json={"cluster-settings": {"allow": ["indices.recovery.*"]}})
    assert client.put(f"{API}/cluster-settings?dryRun=true", json=ok, headers=ROOT).status_code == 403
    client.delete("/api/v1/admin/allowlist/test", headers=ROOT)
    assert client.put(f"{API}/cluster-settings?dryRun=true", json=ok, headers=ROOT).status_code == 200
    r = client.put("/api/v1/admin/allowlist", headers=ROOT, json={"bogus-type": {"allow": ["*"]}})
    assert r.status_code == 400


# ---------------------------------------------------------- cluster settings
def test_cluster_settings_full_cycle(client, es, env, prefix, open_allowlist):
    key = "indices.recovery.max_bytes_per_sec"
    body = {"config": {key: "42mb"}, **REASON}

    # dry run: diff, nothing applied
    r = client.put(f"{API}/cluster-settings?dryRun=true", json=body, headers=ROOT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["dryRun"] and not d["applied"]
    assert d["diff"]["added"] == [{"path": key, "after": "42mb"}]
    assert key not in es("GET", "/_cluster/settings", params={"flat_settings": "true"})["persistent"]

    # reason required for real change
    r = client.put(f"{API}/cluster-settings", json={"config": {key: "42mb"}}, headers=ROOT)
    assert r.status_code == 400 and r.json()["error"]["code"] == "REASON_REQUIRED"

    # apply
    r = client.put(f"{API}/cluster-settings", json=body, headers=ROOT)
    assert r.status_code == 200, r.text
    assert r.json()["applied"] and r.json()["rollbackAvailable"]
    assert es("GET", "/_cluster/settings", params={"flat_settings": "true"})["persistent"][key] == "42mb"

    # GET returns version + ETag, no drift
    g = client.get(f"{API}/cluster-settings", headers=ROOT)
    assert g.json()["config"][key] == "42mb" and not g.json()["driftDetected"]
    assert g.headers["ETag"] == f'"{g.json()["version"]}"'

    # snapshot holds the state before the change
    prev = client.get(f"{API}/cluster-settings/previous", headers=ROOT).json()
    assert key not in prev["state"]["config"]

    # same value again = no change
    r = client.put(f"{API}/cluster-settings", json=body, headers=ROOT)
    assert r.json()["noChange"] is True

    # rollback dry run then real rollback: key is reset (null)
    r = client.post(f"{API}/cluster-settings/rollback?dryRun=true", json=REASON, headers=ROOT)
    assert r.json()["diff"]["removed"] == [{"path": key, "before": "42mb"}]
    r = client.post(f"{API}/cluster-settings/rollback", json=REASON, headers=ROOT)
    assert r.status_code == 200, r.text
    assert key not in es("GET", "/_cluster/settings", params={"flat_settings": "true"})["persistent"]

    # rollback swaps: rolling back again re-applies 42mb
    r = client.post(f"{API}/cluster-settings/rollback", json=REASON, headers=ROOT)
    assert r.status_code == 200, r.text
    assert es("GET", "/_cluster/settings", params={"flat_settings": "true"})["persistent"][key] == "42mb"

    # null resets a key
    r = client.put(f"{API}/cluster-settings", json={"config": {key: None}, **REASON}, headers=ROOT)
    assert r.json()["diff"]["removed"][0]["path"] == key

    # audit trail
    today = datetime.now(timezone.utc).date().isoformat()
    events = client.get(f"/api/v1/admin/audit?date={today}&clusterId=test", headers=ROOT).json()
    actions = [e["action"] for e in events["items"]]
    assert "UPDATE" in actions and "ROLLBACK" in actions and "DRY_RUN" in actions
    upd = next(e for e in events["items"] if e["action"] == "UPDATE" and e["outcome"] == "SUCCESS")
    assert upd["actor"] == "root" and upd["reason"] == "test change" and upd["diff"]


def test_if_match_mismatch(client, prefix, open_allowlist):
    r = client.put(f"{API}/cluster-settings",
                   json={"config": {"indices.recovery.max_bytes_per_sec": "43mb"}, **REASON},
                   headers={**ROOT, "If-Match": '"deadbeefdeadbeef"'})
    assert r.status_code == 412 and r.json()["error"]["code"] == "VERSION_MISMATCH"
    v = client.get(f"{API}/cluster-settings", headers=ROOT).headers["ETag"]
    r = client.put(f"{API}/cluster-settings",
                   json={"config": {"indices.recovery.max_bytes_per_sec": "43mb"}, **REASON},
                   headers={**ROOT, "If-Match": v})
    assert r.status_code == 200, r.text


def test_drift_detection(client, es, prefix, open_allowlist):
    key = "indices.recovery.max_bytes_per_sec"
    client.put(f"{API}/cluster-settings", json={"config": {key: "44mb"}, **REASON}, headers=ROOT)
    es("PUT", "/_cluster/settings", json={"persistent": {key: "99mb"}})  # outside the API
    assert client.get(f"{API}/cluster-settings", headers=ROOT).json()["driftDetected"] is True
    r = client.post(f"{API}/cluster-settings/rollback", json=REASON, headers=ROOT)
    assert r.status_code == 409 and r.json()["error"]["code"] == "DRIFT_DETECTED"
    r = client.post(f"{API}/cluster-settings/rollback?force=true", json=REASON, headers=ROOT)
    assert r.status_code == 200, r.text
    # update with drift proceeds but warns
    client.put(f"{API}/cluster-settings", json={"config": {key: "45mb"}, **REASON}, headers=ROOT)
    es("PUT", "/_cluster/settings", json={"persistent": {key: "98mb"}})
    r = client.put(f"{API}/cluster-settings", json={"config": {key: "46mb"}, **REASON}, headers=ROOT)
    assert r.status_code == 200 and any("Drift" in w for w in r.json()["warnings"])
    # the snapshot captured the real live value (98mb), not the stale one
    prev = client.get(f"{API}/cluster-settings/previous", headers=ROOT).json()
    assert prev["state"]["config"][key] == "98mb"


# ------------------------------------------------------------ index settings
def test_index_settings(client, es, prefix, open_allowlist):
    idx = f"{prefix}-idx"
    es("PUT", f"/{idx}", json={"settings": {"number_of_replicas": 0}})
    base = f"{API}/indices/{idx}/settings"
    g = client.get(base, headers=ROOT).json()
    assert "index.uuid" not in g["config"] and g["config"]["index.number_of_replicas"] == "0"

    r = client.put(base, json={"config": {"number_of_shards": 3}, **REASON}, headers=ROOT)
    assert r.status_code == 422 and r.json()["error"]["code"] == "STATIC_SETTING"
    r = client.put(base, json={"config": {"index.uuid": "x"}, **REASON}, headers=ROOT)
    assert r.status_code == 422

    r = client.put(base, json={"config": {"refresh_interval": "30s"}, **REASON}, headers=ROOT)
    assert r.status_code == 200, r.text
    assert r.json()["diff"]["added"] == [{"path": "index.refresh_interval", "after": "30s"}]
    s = es("GET", f"/{idx}/_settings", params={"flat_settings": "true"})[idx]["settings"]
    assert s["index.refresh_interval"] == "30s"

    r = client.post(f"{base}/rollback", json=REASON, headers=ROOT)
    assert r.status_code == 200, r.text
    s = es("GET", f"/{idx}/_settings", params={"flat_settings": "true"})[idx]["settings"]
    assert "index.refresh_interval" not in s

    assert client.get(f"{API}/indices/{prefix}-*/settings", headers=ROOT).status_code == 400
    assert client.get(f"{API}/indices/{prefix}-missing/settings", headers=ROOT).status_code == 404


def test_index_allowlist_by_key(client, es, prefix):
    idx = f"{prefix}-idx2"
    es("PUT", f"/{idx}", json={"settings": {"number_of_replicas": 0}})
    client.put("/api/v1/admin/allowlist", headers=ROOT,
               json={"index-settings": {"allow": ["index.refresh_interval"]}})
    base = f"{API}/indices/{idx}/settings?dryRun=true"
    assert client.put(base, json={"config": {"refresh_interval": "5s"}}, headers=ROOT).status_code == 200
    r = client.put(base, json={"config": {"number_of_replicas": 1}}, headers=ROOT)
    assert r.status_code == 403


# ------------------------------------------------------------ index mappings
def test_mapping_add_only(client, es, prefix, open_allowlist):
    idx = f"{prefix}-map"
    es("PUT", f"/{idx}", json={"settings": {"number_of_replicas": 0},
                               "mappings": {"properties": {"a": {"type": "keyword"}}}})
    base = f"{API}/indices/{idx}/mapping"
    r = client.put(f"{base}?dryRun=true", json={"config": {"properties": {"b": {"type": "long"}}}},
                   headers=ROOT)
    assert r.status_code == 200 and r.json()["permanent"] is True
    assert r.json()["rollbackAvailableAfter"] is False
    r = client.put(base, json={"config": {"properties": {"a": {"type": "long"}}}, **REASON}, headers=ROOT)
    assert r.status_code == 422 and r.json()["error"]["code"] == "MAPPING_CONFLICT"
    r = client.put(base, json={"config": {"dynamic": "strict"}, **REASON}, headers=ROOT)
    assert r.status_code == 422
    r = client.put(base, json={"config": {"properties": {"b": {"type": "long"}}}, **REASON}, headers=ROOT)
    assert r.status_code == 200, r.text
    assert es("GET", f"/{idx}/_mapping")[idx]["mappings"]["properties"]["b"] == {"type": "long"}
    r = client.post(f"{base}/rollback", json=REASON, headers=ROOT)
    assert r.status_code == 422 and r.json()["error"]["code"] == "ROLLBACK_NOT_SUPPORTED"


# ------------------------------------------------------------ index templates
def test_index_template_create_and_rollback_deletes(client, es, prefix, open_allowlist):
    name = f"{prefix}-tpl"
    base = f"{API}/index-templates/{name}"
    tpl = {"index_patterns": [f"{prefix}-logs-*"], "priority": 500,
           "template": {"settings": {"number_of_replicas": 0}}}
    assert client.get(base, headers=ROOT).status_code == 404

    r = client.put(f"{base}?dryRun=true", json={"config": tpl}, headers=ROOT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["createsResource"] and d["simulation"]["resolvedTemplate"]["settings"]["index"]["number_of_replicas"] == "0"

    assert client.put(base, json={"config": tpl, **REASON}, headers=ROOT).status_code == 200
    assert name in client.get(f"{API}/index-templates", headers=ROOT).json()["items"]

    tpl2 = {**tpl, "template": {"settings": {"number_of_replicas": 0, "refresh_interval": "10s"}}}
    r = client.put(base, json={"config": tpl2, **REASON}, headers=ROOT)
    assert r.status_code == 200
    r = client.post(f"{base}/rollback", json=REASON, headers=ROOT)
    assert r.status_code == 200
    got = es("GET", f"/_index_template/{name}")["index_templates"][0]["index_template"]
    assert "refresh_interval" not in str(got)

    # roll back again (swap) -> tpl2, again -> tpl2's previous is tpl ... create/delete case:
    name2 = f"{prefix}-tpl-new"
    client.put(f"{API}/index-templates/{name2}", headers=ROOT,
               json={"config": {**tpl, "index_patterns": [f"{prefix}-other-*"]}, **REASON})
    r = client.post(f"{API}/index-templates/{name2}/rollback", json=REASON, headers=ROOT)
    assert r.status_code == 200 and r.json()["deletesResource"] is True
    assert es("GET", f"/_index_template/{name2}", ok=False).get("status") == 404

    r = client.put(f"{API}/index-templates/{prefix}-bad", json={"config": {"priority": 1}, **REASON},
                   headers=ROOT)
    assert r.status_code == 400


# ------------------------------------------------ component templates + ILM
def test_component_template_and_ilm(client, es, prefix, open_allowlist):
    comp = f"{prefix}-comp"
    r = client.put(f"{API}/component-templates/{comp}", headers=ROOT,
                   json={"config": {"template": {"settings": {"number_of_replicas": 0}}}, **REASON})
    assert r.status_code == 200, r.text
    assert client.get(f"{API}/component-templates/{comp}", headers=ROOT).json()["config"]["template"]

    pol = f"{prefix}-pol"
    p1 = {"policy": {"phases": {"hot": {"actions": {"rollover": {"max_age": "7d"}}}}}}
    p2 = {"policy": {"phases": {"hot": {"actions": {"rollover": {"max_age": "3d"}}}}}}
    assert client.put(f"{API}/ilm-policies/{pol}", json={"config": p1, **REASON}, headers=ROOT).status_code == 200
    r = client.put(f"{API}/ilm-policies/{pol}", json={"config": p2, **REASON}, headers=ROOT)
    assert r.status_code == 200
    assert any(c["path"].endswith("max_age") for c in r.json()["diff"]["changed"])
    r = client.post(f"{API}/ilm-policies/{pol}/rollback", json=REASON, headers=ROOT)
    assert r.status_code == 200
    got = es("GET", f"/_ilm/policy/{pol}")[pol]["policy"]["phases"]["hot"]["actions"]["rollover"]
    assert got["max_age"] == "7d"
    r = client.put(f"{API}/ilm-policies/{pol}", json={"config": {"phases": {}}, **REASON}, headers=ROOT)
    assert r.status_code == 400


# ---------------------------------------------------------- ingest pipelines
def test_ingest_pipeline_dry_run(client, es, prefix, open_allowlist):
    pid = f"{prefix}-pipe"
    base = f"{API}/ingest-pipelines/{pid}"
    pipe = {"description": "t", "processors": [{"set": {"field": "env", "value": "prod"}}]}
    r = client.put(f"{base}?dryRun=true", json={"config": pipe, "sampleDocs": [{"msg": "hi"}]},
                   headers=ROOT)
    assert r.status_code == 200, r.text
    assert r.json()["simulation"]["docs"][0]["doc"]["_source"] == {"msg": "hi", "env": "prod"}

    r = client.put(f"{base}?dryRun=true", json={"config": {"processors": [{"bogus": {}}]}}, headers=ROOT)
    assert r.status_code == 200 and r.json()["valid"] is False

    assert client.put(base, json={"config": pipe, **REASON}, headers=ROOT).status_code == 200
    assert pid in client.get(f"{API}/ingest-pipelines", headers=ROOT).json()["items"]
    r = client.post(f"{base}/rollback", json=REASON, headers=ROOT)
    assert r.status_code == 200 and r.json()["deletesResource"]


# --------------------------------------------------- locks + crash recovery
def test_lock_blocks_concurrent_change(client, env, prefix, open_allowlist):
    store = env["store"]
    key = "locks/test/cluster-settings/_cluster.lock"
    store.put_json(key, {"owner": "bob", "token": "x", "acquiredAt": "now",
                         "expiresAtEpoch": time.time() + 60})
    body = {"config": {"indices.recovery.max_bytes_per_sec": "47mb"}, **REASON}
    r = client.put(f"{API}/cluster-settings", json=body, headers=ROOT)
    assert r.status_code == 409 and r.json()["error"]["details"]["lockedBy"] == "bob"
    # dry runs don't need the lock
    assert client.put(f"{API}/cluster-settings?dryRun=true", json=body, headers=ROOT).status_code == 200
    # an expired lock is taken over
    store.put_json(key, {"owner": "bob", "token": "x", "expiresAtEpoch": time.time() - 1})
    assert client.put(f"{API}/cluster-settings", json=body, headers=ROOT).status_code == 200
    assert store.get_json(key)[0] is None  # released


def test_pending_snapshot_recovery(client, env, es, prefix, open_allowlist):
    """Simulate a crash after ES was changed but before the snapshot was promoted."""
    key = "indices.recovery.max_bytes_per_sec"
    before = client.get(f"{API}/cluster-settings", headers=ROOT).json()
    es("PUT", "/_cluster/settings", json={"persistent": {key: "48mb"}})  # the 'applied' change
    store = env["store"]
    store.put_json("snapshots/test/cluster-settings/_cluster.json", {"pending": {
        "changeId": "c1", "action": "UPDATE", "by": "root", "at": "t",
        "beforeVersion": before["version"],
        "state": {"exists": True, "config": before["config"]}}})
    r = client.put(f"{API}/cluster-settings", json={"config": {key: "49mb"}, **REASON}, headers=ROOT)
    assert r.status_code == 200, r.text
    # rollback now goes to 48mb (the recovered change), and once more to the original
    client.post(f"{API}/cluster-settings/rollback", json=REASON, headers=ROOT)
    assert es("GET", "/_cluster/settings", params={"flat_settings": "true"})["persistent"][key] == "48mb"


# ---------------------------------------------------------------- admin misc
def test_admin_clusters_and_users(client):
    items = client.get("/api/v1/admin/clusters", headers=ROOT).json()["items"]
    assert {i["id"] for i in items} == {"test", "other"} and all(i["reachable"] for i in items)
    assert items[0]["version"] == "8.17.1"
    client.put("/api/v1/admin/users/carol", json={"clusters": {"*": "view"}}, headers=ROOT)
    assert len(client.get("/api/v1/clusters", headers=as_user("carol")).json()["items"]) == 2
    assert client.delete("/api/v1/admin/users/carol", headers=ROOT).status_code == 200
    assert client.delete("/api/v1/admin/users/root", headers=ROOT).status_code == 400
    names = [u["username"] for u in client.get("/api/v1/admin/users", headers=ROOT).json()["items"]]
    assert names == ["root"]


def test_health_and_unknown_cluster(client):
    assert client.get(f"{API}/health", headers=ROOT).json()["status"] in ("green", "yellow")
    r = client.get("/api/v1/clusters/nope/cluster-settings", headers=ROOT)
    assert r.status_code == 404 and r.json()["error"]["code"] == "CLUSTER_NOT_FOUND"


# ------------------------------------------------ health gate + ES rejection
def test_red_cluster_blocks_unless_forced(client, es, prefix, open_allowlist):
    # an index whose primary can never be allocated turns the cluster red
    es("PUT", f"/{prefix}-red", json={"settings": {
        "number_of_replicas": 0, "index.routing.allocation.require._name": "no-such-node"}},
       params={"wait_for_active_shards": "0"})
    try:
        body = {"config": {"indices.recovery.max_bytes_per_sec": "51mb"}, **REASON}
        r = client.put(f"{API}/cluster-settings", json=body, headers=ROOT)
        assert r.status_code == 409 and r.json()["error"]["code"] == "CLUSTER_UNHEALTHY"
        r = client.put(f"{API}/cluster-settings?force=true", json=body, headers=ROOT)
        assert r.status_code == 200 and "Cluster health is red" in r.json()["warnings"]
    finally:
        es("DELETE", f"/{prefix}-red", ok=False)


def test_es_rejection_keeps_old_snapshot(client, env, es, prefix, open_allowlist):
    key = "indices.recovery.max_bytes_per_sec"
    client.put(f"{API}/cluster-settings", json={"config": {key: "52mb"}, **REASON}, headers=ROOT)
    prev_before = client.get(f"{API}/cluster-settings/previous", headers=ROOT).json()
    r = client.put(f"{API}/cluster-settings", json={"config": {"not.a.real.setting": "1"}, **REASON},
                   headers=ROOT)
    assert r.status_code == 400 and r.json()["error"]["code"] == "ES_REJECTED"
    doc, _ = env["store"].get_json("snapshots/test/cluster-settings/_cluster.json")
    assert "pending" not in doc
    assert client.get(f"{API}/cluster-settings/previous", headers=ROOT).json() == prev_before
    today = datetime.now(timezone.utc).date().isoformat()
    ev = client.get(f"/api/v1/admin/audit?date={today}&action=UPDATE", headers=ROOT).json()["items"][0]
    assert ev["outcome"] == "FAILED" and ev["error"]["code"] == "ES_REJECTED"


# --------------------------------------------- regressions from code review
def test_apply_error_after_es_changed_keeps_correct_rollback_target(client, env, es, prefix,
                                                                     open_allowlist, monkeypatch):
    from app.errors import ApiError
    from app.handlers import HANDLERS
    key = "indices.recovery.max_bytes_per_sec"
    client.put(f"{API}/cluster-settings", json={"config": {key: "61mb"}, **REASON}, headers=ROOT)
    h = HANDLERS["cluster-settings"]
    real_apply = h.apply

    def flaky_apply(es_, resource, current, target, plan=None):
        real_apply(es_, resource, current, target, plan)       # ES changes...
        raise ApiError(502, "ES_UNAVAILABLE", "timeout")        # ...but the call "fails"
    monkeypatch.setattr(h, "apply", flaky_apply)
    r = client.put(f"{API}/cluster-settings", json={"config": {key: "63mb"}, **REASON}, headers=ROOT)
    monkeypatch.setattr(h, "apply", real_apply)
    assert r.status_code == 200 and r.json()["applied"] and any("did change" in w for w in r.json()["warnings"])
    prev = client.get(f"{API}/cluster-settings/previous", headers=ROOT).json()
    assert prev["state"]["config"][key] == "61mb"
    client.post(f"{API}/cluster-settings/rollback", json=REASON, headers=ROOT)
    assert es("GET", "/_cluster/settings", params={"flat_settings": "true"})["persistent"][key] == "61mb"


def test_reapplying_same_body_keeps_previous(client, prefix, open_allowlist):
    name = f"{prefix}-same"
    base = f"{API}/index-templates/{name}"
    t1 = {"index_patterns": [f"{prefix}-s-*"], "priority": 1}
    t2 = {"index_patterns": [f"{prefix}-s-*"], "priority": 2}
    client.put(base, json={"config": t1, **REASON}, headers=ROOT)
    client.put(base, json={"config": t2, **REASON}, headers=ROOT)
    r = client.put(base, json={"config": t2, **REASON}, headers=ROOT)   # same body again
    assert r.status_code == 200 and r.json().get("noChange") is True
    assert client.get(f"{base}/previous", headers=ROOT).json()["state"]["config"]["priority"] == 1


def test_allowlist_rejects_non_list_patterns(client):
    for bad in ({"index-templates": {"allow": "logs-*"}}, {"index-templates": {"allow": None}},
                {"index-templates": {"allow": ["*"], "indices": ["x"]}}):
        r = client.put("/api/v1/admin/allowlist", json=bad, headers=ROOT)
        assert r.status_code == 400, bad


def test_mapping_cannot_touch_runtime_or_null(client, es, prefix, open_allowlist):
    idx = f"{prefix}-rt"
    es("PUT", f"/{idx}", json={"settings": {"number_of_replicas": 0}, "mappings": {
        "runtime": {"rt": {"type": "keyword"}}, "properties": {"o": {"properties": {"x": {"type": "long"}}}}}})
    base = f"{API}/indices/{idx}/mapping"
    for cfg in ({"runtime": {"rt": None}}, {"properties": {"new": None}},
                {"properties": {"o": {"type": "keyword"}}}):
        r = client.put(base, json={"config": cfg, **REASON}, headers=ROOT)
        assert r.status_code == 422, (cfg, r.text)
    assert "rt" in es("GET", f"/{idx}/_mapping")[idx]["mappings"]["runtime"]


def test_young_pending_blocks_other_requests(client, env, prefix, open_allowlist):
    before = client.get(f"{API}/cluster-settings", headers=ROOT).json()
    env["store"].put_json("snapshots/test/cluster-settings/_cluster.json", {"pending": {
        "changeId": "c2", "action": "UPDATE", "by": "bob", "at": "t", "atEpoch": time.time(),
        "beforeVersion": before["version"], "state": {"exists": True, "config": before["config"]}}})
    r = client.put(f"{API}/cluster-settings",
                   json={"config": {"indices.recovery.max_bytes_per_sec": "64mb"}, **REASON}, headers=ROOT)
    assert r.status_code == 409 and r.json()["error"]["code"] == "CHANGE_IN_PROGRESS"


def test_dot_indices_need_explicit_allowlist(client, es, prefix):
    idx = f".{prefix}-sys"
    es("PUT", f"/{idx}", json={"settings": {"number_of_replicas": 0, "index.hidden": True}})
    try:
        body = {"config": {"refresh_interval": "7s"}}
        client.put("/api/v1/admin/allowlist", headers=ROOT, json={
            "index-settings": {"allow": ["index.refresh_interval"]},
            "index-mappings": {"allow": ["*"]}})
        r = client.put(f"{API}/indices/{idx}/settings?dryRun=true", json=body, headers=ROOT)
        assert r.status_code == 403
        r = client.put(f"{API}/indices/{idx}/mapping?dryRun=true",
                       json={"config": {"properties": {"z": {"type": "long"}}}}, headers=ROOT)
        assert r.status_code == 403
        client.put("/api/v1/admin/allowlist", headers=ROOT, json={
            "index-settings": {"allow": ["index.refresh_interval"], "indices": [".cfgtest-*", "*"]}})
        r = client.put(f"{API}/indices/{idx}/settings?dryRun=true", json=body, headers=ROOT)
        assert r.status_code == 200, r.text
        client.put("/api/v1/admin/allowlist", headers=ROOT, json={
            "index-settings": {"allow": ["index.refresh_interval"], "indices": ["logs-*"]}})
        r = client.put(f"{API}/indices/{idx}/settings?dryRun=true", json=body, headers=ROOT)
        assert r.status_code == 403
    finally:
        es("DELETE", f"/{idx}", ok=False)


def test_template_index_patterns_are_checked(client, prefix):
    base = f"{API}/index-templates/{prefix}-wide"
    client.put("/api/v1/admin/allowlist", headers=ROOT, json={"index-templates": {"allow": [f"{prefix}-*"]}})
    r = client.put(f"{base}?dryRun=true", json={"config": {"index_patterns": ["*"], "priority": 900}},
                   headers=ROOT)
    assert r.status_code == 403
    client.put("/api/v1/admin/allowlist", headers=ROOT, json={
        "index-templates": {"allow": [f"{prefix}-*"], "indexPatterns": ["logs-*"]}})
    r = client.put(f"{base}?dryRun=true", json={"config": {"index_patterns": ["metrics-*"]}}, headers=ROOT)
    assert r.status_code == 403 and r.json()["error"]["details"]["blocked"] == ["metrics-*"]
    r = client.put(f"{base}?dryRun=true", json={"config": {"index_patterns": ["logs-app-*"]}}, headers=ROOT)
    assert r.status_code == 200, r.text
