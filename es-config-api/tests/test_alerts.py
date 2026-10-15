"""Alerts: Slack webhook in Secrets Manager, threshold checks with repeats and resolve messages,
snoozes (console only), request notices, weekly digest, RAM excluding cache."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from app.alerts import IST, AlertService, alert_key
from app.errors import ApiError
from app.slack import RecordingTransport
from conftest import ROOT, as_user

A = "/api/v1/admin/alerts"
HOOK = "https://hooks.slack.com/services/T000/B000/abcdefghijklmnop"
T0 = datetime(2026, 10, 14, 6, 0, tzinfo=timezone.utc)      # a Wednesday


@pytest.fixture()
def slack(env, client):
    app = env["app"]
    app.state.settings = dataclasses.replace(app.state.settings, public_url="http://console.test")
    rec = RecordingTransport()
    app.state.slack.transport = rec
    app.state.slack.async_send = False
    assert client.put(f"{A}/slack", json={"webhookUrl": HOOK}, headers=ROOT).status_code == 200
    return rec


def _settings(client, **defaults):
    body = {"defaults": {m: v for m, v in defaults.items()}}
    r = client.put(f"{A}/settings", json=body, headers=ROOT)
    assert r.status_code == 200, r.text
    return r.json()


def _quiet(**on):
    """Every metric off except the ones given."""
    out = {m: {"on": False} for m in ("disk", "ram", "heap", "cpu", "shards", "health", "nodes")}
    out.update(on)
    return out


def test_webhook_is_a_secret(env, client, slack):
    r = client.put(f"{A}/slack", json={"webhookUrl": "https://example.com/hook"}, headers=ROOT)
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_WEBHOOK"
    assert client.put(f"{A}/slack", json={"webhookUrl": HOOK}, headers=as_user("bob")).status_code == 403
    view = client.get(A, headers=ROOT).json()
    assert view["slack"]["configured"] is True and HOOK not in str(view)
    assert env["app"].state.secrets.get("slack") == {"webhookUrl": HOOK}
    r = client.post(f"{A}/slack/_test", headers=ROOT)
    assert r.json()["ok"] and "Test message" in slack.sent[-1]["text"]
    day = datetime.now(timezone.utc).date().isoformat()
    audit = client.get("/api/v1/admin/audit", params={"date": day}, headers=ROOT).json()["items"]
    assert any(e["action"] == "ALERT_WEBHOOK_UPDATE" for e in audit) and HOOK not in str(audit)
    # Slack refusing is reported, not raised
    slack.status = 404
    r = client.post(f"{A}/slack/_test", headers=ROOT).json()
    assert r["ok"] is False and "no longer exists" in r["error"]
    assert client.get(A, headers=ROOT).json()["slack"]["lastError"]
    assert client.delete(f"{A}/slack", headers=ROOT).json() == {"configured": False}
    assert env["app"].state.alerts.slack.post("x") == {"ok": False, "error": "Slack is not set up (Admin → Alerts)",
                                                       "skipped": True}


def test_settings_validation(client):
    bad = [{"defaults": {"disk": {"warn": 90, "crit": 80}}}, {"defaults": {"nope": {"on": True}}},
           {"clusters": {"ghost": {"off": True}}}, {"repeatMinutes": {"critical": 1}},
           {"defaults": {"health": {"warn": 3}}}, {"clusters": {"test": {"metrics": {"disk": {"warn": 95}}}}}]
    for b in bad:
        r = client.put(f"{A}/settings", json=b, headers=ROOT)
        assert r.status_code == 400, b
    s = client.put(f"{A}/settings", json={"clusters": {"test": {"metrics": {"disk": {"warn": 60}}}}},
                   headers=ROOT).json()
    assert s["defaults"]["disk"] == {"on": True, "warn": 70, "crit": 80} and s["repeatMinutes"] == {"warning": 60, "critical": 5}
    assert s["clusters"]["test"]["metrics"]["disk"] == {"warn": 60} and s["updatedBy"] == "root"
    th = client.get("/api/v1/clusters/test/alerts", headers=ROOT).json()["thresholds"]
    assert th["disk"] == {"on": True, "warn": 60, "crit": 80}


def test_fire_repeat_resolve(env, client, slack):
    svc: AlertService = env["app"].state.alerts
    # disk critical on every data node (1/2%), RAM warning (1/100%)
    _settings(client, **_quiet(disk={"warn": 1, "crit": 2}, ram={"warn": 1, "crit": 100}))
    r = svc.run_once(T0)
    assert r["messages"] == 2 and r["slack"] is None          # one per cluster (test, other)
    msg = next(p for p in slack.sent if "· test" in p["text"])
    assert msg["text"].startswith(":red_circle: *Critical · test (Test cluster)*")
    body = "\n".join(a["text"] for a in msg["attachments"])
    assert "Disk on " in body and "(alert at 2%)" in body and "RAM (excl. cache) on" in body
    assert "<http://console.test/ui/c/test|Open in the console>" in body
    active = client.get(A, headers=ROOT).json()["active"]
    assert {a["metric"] for a in active} == {"disk", "ram"} and all(a["lastSentAt"] for a in active)
    # 2 min later: nothing due
    slack.sent.clear()
    assert svc.run_once(T0 + timedelta(minutes=2))["messages"] == 0
    # 6 min: critical repeats, the warning doesn't (hourly)
    svc.run_once(T0 + timedelta(minutes=6))
    body = "\n".join(a["text"] for p in slack.sent for a in p["attachments"])
    assert "Disk on" in body and "RAM" not in body
    # 61 min: both
    slack.sent.clear()
    svc.run_once(T0 + timedelta(minutes=61))
    body = "\n".join(a["text"] for p in slack.sent for a in p["attachments"])
    assert "Disk on" in body and "RAM" in body
    # thresholds back up: everything clears, one OK message per cluster
    slack.sent.clear()
    _settings(client, **_quiet(disk={"warn": 99, "crit": 100}, ram={"warn": 99, "crit": 100}))
    svc.run_once(T0 + timedelta(minutes=62))
    assert len(slack.sent) == 2 and all(p["text"].startswith(":large_green_circle: *OK again") for p in slack.sent)
    assert "is back to normal" in slack.sent[0]["attachments"][0]["text"]
    assert client.get(A, headers=ROOT).json()["active"] == []
    events = [e["event"] for e in client.get(f"{A}/history", headers=ROOT).json()["items"]]
    assert events.count("FIRING") == 4 and events.count("RESOLVED") == 4


def test_heap_three_of_five_and_snooze(env, client, slack):
    svc: AlertService = env["app"].state.alerts
    _settings(client, **_quiet(heap={"warn": 1, "crit": 2}))
    svc.sample_heap(T0)
    svc.sample_heap(T0 + timedelta(minutes=2))
    assert svc.run_once(T0 + timedelta(minutes=2))["messages"] == 0      # 2 readings over the line: not yet
    assert svc.sample_heap(T0 + timedelta(minutes=3), only_if_due=True) is None   # less than 2 min later: skipped
    svc.sample_heap(T0 + timedelta(minutes=4))
    assert svc.run_once(T0 + timedelta(minutes=5))["messages"] == 2      # 3 of the last 5: critical on both
    a = next(a for a in client.get(A, headers=ROOT).json()["active"] if a["metric"] == "heap")
    assert a["severity"] == "critical" and "3 of the last 3 readings (every 2 min) at or above 2%" in a["detail"]
    # snooze everything on 'test' for 1 hour: 'other' still posts, 'test' doesn't, Slack hears nothing about it
    r = client.post(f"{A}/snoozes", json={"clusterId": "test", "duration": "1h", "reason": "resizing heap"},
                    headers=ROOT)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert client.post(f"{A}/snoozes", json={"clusterId": "test", "duration": "1h", "reason": ""},
                       headers=ROOT).status_code == 400
    assert client.post(f"{A}/snoozes", json={"clusterId": "test", "node": "x", "duration": "1h", "reason": "r"},
                       headers=ROOT).status_code == 400
    slack.sent.clear()
    svc.run_once(T0 + timedelta(minutes=11))
    assert [p["text"] for p in slack.sent] and all("· test" not in p["text"] for p in slack.sent)
    assert all("snooze" not in str(p).lower() for p in slack.sent)
    active = client.get(A, headers=ROOT).json()["active"]
    assert all(a["snoozedBy"] and a["snoozedBy"]["by"] == "root" for a in active if a["clusterId"] == "test")
    # ending the snooze: 'test' is due again at once
    assert client.delete(f"{A}/snoozes/{sid}", headers=ROOT).status_code == 200
    slack.sent.clear()
    svc.run_once(T0 + timedelta(minutes=12))
    assert any("· test" in p["text"] for p in slack.sent)


class FakeCluster:
    """Readings without a real cluster, to drive health / node / reachability alerts."""

    def __init__(self, svc: AlertService, base: dict):
        self.svc, self.base = svc, base
        self.nodes = ["n1", "n2", "n3"]
        self.status = "green"
        self.down = False

    def __call__(self, cid):
        if cid != "test":
            return self.base
        if self.down:
            raise ApiError(502, "CLUSTER_UNREACHABLE", "Could not reach cluster: timed out")
        return {**self.base, "health": {"status": self.status, "unassigned_shards": 3 if self.status != "green" else 0},
                "nodes": {**self.base["nodes"], "nodes": [{"name": n, "data": True} for n in self.nodes]}}


def test_health_nodes_and_unreachable(env, client, slack):
    svc: AlertService = env["app"].state.alerts
    _settings(client, **_quiet(health={"on": True}, nodes={"on": True}))
    fake = FakeCluster(svc, svc.collect("other"))
    svc.collect = fake
    svc.run_once(T0)
    assert client.get(A, headers=ROOT).json()["active"] == [] or all(
        a["clusterId"] != "test" for a in client.get(A, headers=ROOT).json()["active"])
    slack.sent.clear()
    fake.status, fake.nodes = "red", ["n1", "n3"]
    svc.run_once(T0 + timedelta(minutes=5))
    test_msgs = [p for p in slack.sent if "· test" in p["text"]]
    body = "\n".join(a["text"] for a in test_msgs[0]["attachments"])
    assert "Cluster is RED · 3 unassigned shards" in body and "Node n2 left the cluster (2 nodes now)" in body
    # cluster stops answering: one critical 'not answering', other alerts kept as they are
    fake.down = True
    svc.run_once(T0 + timedelta(minutes=10))
    keys = {a["key"] for a in client.get(A, headers=ROOT).json()["active"] if a["clusterId"] == "test"}
    assert keys == {alert_key("test", "nodes"), alert_key("test", "health"), alert_key("test", "nodes", "n2")}
    # back, green, n2 still gone; an admin clears n2 (it was retired)
    fake.down, fake.status = False, "green"
    svc.run_once(T0 + timedelta(minutes=15))
    keys = {a["key"] for a in client.get(A, headers=ROOT).json()["active"] if a["clusterId"] == "test"}
    assert keys == {alert_key("test", "nodes", "n2")}
    k = alert_key("test", "nodes", "n2")
    r = client.post(f"{A}/active/{k}/_clear", headers=ROOT)
    assert r.status_code == 200, r.text
    assert client.post(f"{A}/active/{alert_key('test', 'disk', 'n1')}/_clear", headers=ROOT).status_code == 404
    svc.run_once(T0 + timedelta(minutes=20))
    assert not [a for a in client.get(A, headers=ROOT).json()["active"] if a["clusterId"] == "test"]


def test_hysteresis():
    eff = {"warn": 70, "crit": 80}
    sev = AlertService._severity
    assert sev(69.9, eff, None) is None and sev(70, eff, None) == "warning" and sev(80, eff, None) == "critical"
    assert sev(78.5, eff, {"severity": "critical"}) == "critical"      # within 2 points: stays
    assert sev(77.9, eff, {"severity": "critical"}) == "warning"
    assert sev(68.5, eff, {"severity": "warning"}) == "warning"
    assert sev(67.9, eff, {"severity": "warning"}) is None


on = pytest.mark.parametrize("env", [True], indirect=True)


@on
def test_requests_go_to_slack(env, client, es, prefix, slack, open_allowlist):
    dev = "dev@fenixcommerce.com"
    client.put(f"/api/v1/admin/users/{dev}", json={"clusters": {"test": {"default": "view", "indices": [
        {"pattern": f"{prefix}-*", "level": "edit"}]}}}, headers=ROOT)
    idx = f"{prefix}-idx"
    es("PUT", f"/{idx}", json={"settings": {"number_of_replicas": 0}})
    r = client.put(f"/api/v1/clusters/test/indices/{idx}/settings",
                   json={"config": {"refresh_interval": "31s"}, "reason": "import"}, headers=as_user(dev))
    assert r.status_code == 202
    rid = r.json()["approval"]["id"]
    assert slack.sent[-1]["text"].startswith(":inbox_tray: *Approval needed* · dev@fenixcommerce.com asked to")
    assert "Reason: import" in slack.sent[-1]["attachments"][0]["text"] and "31s" not in str(slack.sent[-1])
    assert f"http://console.test/ui/requests/{rid}" in str(slack.sent[-1])
    client.post(f"/api/v1/approvals/{rid}/_approve", json={"comment": "ok"}, headers=ROOT)
    assert slack.sent[-1]["text"].startswith(":white_check_mark: *Approved and applied* by root")


def test_digest(env, client, slack, es, prefix):
    svc: AlertService = env["app"].state.alerts
    idx = f"{prefix}-big"
    es("PUT", f"/{idx}", json={"settings": {"number_of_replicas": 0}})
    es("POST", f"/{idx}/_doc", params={"refresh": "true"}, json={"a": "x" * 1000})
    # a snapshot from 8 days ago without this index: it shows as new growth
    now = datetime.now(timezone.utc)
    old = svc.sizes("test")
    old["indices"].pop(idx, None)
    old["totalBytes"] = sum(v["bytes"] for v in old["indices"].values())
    old["at"] = (now - timedelta(days=8)).isoformat()
    env["store"].put_json(f"alerts/sizes/test/{(now - timedelta(days=8)).date()}.json", old)
    d = client.get(f"{A}/digest/_preview", headers=ROOT).json()
    t = next(c for c in d["digest"]["clusters"] if c["clusterId"] == "test")
    assert t["growthBytes"] > 0 and any(g["index"] == idx and g["new"] for g in t["topGrowth"])
    o = next(c for c in d["digest"]["clusters"] if c["clusterId"] == "other")
    assert "growthBytes" not in o
    assert "growth from next week" in str(d["slack"]) and "Weekly Elasticsearch digest" in d["slack"]["text"]
    assert set(d["digest"]["requests"]) >= {"submitted", "applied", "rejected", "expired", "pending"}
    r = client.post(f"{A}/digest/_send", headers=ROOT).json()
    assert r["ok"] and slack.sent[-1]["text"].startswith(":bar_chart: *Weekly Elasticsearch digest*")
    # schedule: Fridays from 15:00 IST, once a week
    fri = datetime(2026, 10, 16, 15, 5, tzinfo=IST)
    assert svc._digest_due({}, fri.astimezone(timezone.utc)) == "2026-W42"
    assert svc._digest_due({}, (fri - timedelta(minutes=10)).astimezone(timezone.utc)) is None
    assert svc._digest_due({}, (fri - timedelta(days=1)).astimezone(timezone.utc)) is None
    assert svc._digest_due({"lastDigest": "2026-W42"}, fri.astimezone(timezone.utc)) is None


def test_cluster_alerts_and_ram(env, client):
    client.put("/api/v1/admin/users/viewer", json={"clusters": {"test": "view"}}, headers=ROOT)
    assert client.get("/api/v1/clusters/test/alerts", headers=as_user("viewer")).status_code == 200
    assert client.get("/api/v1/clusters/other/alerts", headers=as_user("viewer")).status_code == 403
    assert client.get(A, headers=as_user("viewer")).status_code == 403
    n = client.get("/api/v1/clusters/test/nodes", headers=ROOT).json()
    node = n["nodes"][0]
    assert 0 < node["ramUsedPercent"] < 100 and node["ramFreeBytes"] > 0
    assert node["ramUsedBytes"] >= node["heapMaxBytes"] * 0.5
    assert n["summary"]["ramUsedPercentAvg"] is not None


def test_shard_limit(env, client, slack, es, prefix):
    svc: AlertService = env["app"].state.alerts
    es("PUT", f"/{prefix}-shards", json={"settings": {"number_of_shards": 1, "number_of_replicas": 0}})
    es("PUT", "/_cluster/settings", json={"persistent": {"cluster.max_shards_per_node": 2}})
    try:
        d = svc.collect("test")
        assert d["shardsPerNodeLimit"] == 2 and d["shardLimit"] == 2 * d["health"]["number_of_data_nodes"]
        _settings(client, **_quiet(shards={"warn": 1, "crit": 2}))
        svc.run_once(T0)
        a = next(a for a in client.get(A, headers=ROOT).json()["active"] if a["metric"] == "shards")
        assert a["severity"] == "critical" and "(2 per data node)" in a["detail"]
    finally:
        es("PUT", "/_cluster/settings", json={"persistent": {"cluster.max_shards_per_node": None}})
    assert svc.collect("test")["shardsPerNodeLimit"] == 1000


class FakeNodes:
    """'test' with nodes whose values a test sets directly; 'other' is unchanged."""

    def __init__(self, svc: AlertService):
        self.base = svc.collect("other")
        self.values = {"n1": 50.0, "n2": 50.0}
        self.heap = 10.0
        self.down = False

    def __call__(self, cid):
        if cid != "test":
            return self.base
        if self.down:
            raise ApiError(502, "CLUSTER_UNREACHABLE", "Could not reach cluster: timed out")
        nodes = [{"name": n, "data": True, "diskUsedPercent": v, "heapUsedPercent": self.heap} for n, v in self.values.items()]
        return {**self.base, "health": {"status": "green", "number_of_data_nodes": len(nodes)},
                "nodes": {**self.base["nodes"], "nodes": nodes}}


def _texts(slack):
    return ["\n".join([p["text"]] + [a["text"] for a in p.get("attachments", [])]) for p in slack.sent]


def test_review_regressions(env, client, slack):
    svc: AlertService = env["app"].state.alerts
    fake = FakeNodes(svc)
    svc.collect = fake
    _settings(client, **_quiet(disk={"warn": 70, "crit": 80}, nodes={"on": True}))
    svc.run_once(T0)
    fake.values = {"n1": 90.0, "n2": 90.0}
    svc.run_once(T0 + timedelta(minutes=5))
    keys = lambda: {a["key"] for a in client.get(A, headers=ROOT).json()["active"] if a["clusterId"] == "test"}  # noqa: E731
    assert keys() == {alert_key("test", "disk", "n1"), alert_key("test", "disk", "n2")}
    # n2 leaves: its disk alert is dropped quietly, the node-left alert fires; n1 recovers ("OK again")
    slack.sent.clear()
    fake.values = {"n1": 60.0}
    svc.run_once(T0 + timedelta(minutes=10))
    assert keys() == {alert_key("test", "nodes", "n2")}
    t = "\n".join(_texts(slack))
    assert "Node n2 left the cluster" in t and "Disk on n1 is back to normal" in t and "Disk on n2" not in t
    # the nodes check is turned off: the node-left alert goes away without a false "back to normal"
    slack.sent.clear()
    _settings(client, **_quiet(disk={"warn": 70, "crit": 80}, nodes={"on": False}))
    svc.run_once(T0 + timedelta(minutes=15))
    assert keys() == set() and not any("· test" in p["text"] for p in slack.sent)
    events = client.get(f"{A}/history", headers=ROOT).json()["items"]
    assert any(e["event"] == "DROPPED" and e["node"] == "n2" for e in events)

    # Slack down: nothing is marked posted and the "OK again" waits for the next run
    fake.values = {"n1": 90.0}
    slack.status = 500
    svc.run_once(T0 + timedelta(minutes=20))
    a = next(a for a in client.get(A, headers=ROOT).json()["active"] if a["clusterId"] == "test")
    assert "lastSentAt" not in a and client.get(A, headers=ROOT).json()["lastRun"]["slack"]
    slack.status = 200
    slack.sent.clear()
    svc.run_once(T0 + timedelta(minutes=21))
    assert any("Disk on n1" in t for t in _texts(slack))
    slack.status = 500
    fake.values = {"n1": 10.0}
    svc.run_once(T0 + timedelta(minutes=26))             # cleared, but Slack is down
    slack.status = 200
    slack.sent.clear()
    svc.run_once(T0 + timedelta(minutes=31))
    assert any("Disk on n1 is back to normal" in t for t in _texts(slack))

    # critical easing to warning is posted at once
    fake.values = {"n1": 90.0}
    svc.run_once(T0 + timedelta(minutes=36))
    slack.sent.clear()
    fake.values = {"n1": 75.0}
    svc.run_once(T0 + timedelta(minutes=41))
    assert any("*Warning*" in t and "Disk on n1: 75%" in t for t in _texts(slack))

    # heap: 3 of the last 5 two-minute readings over the line
    _settings(client, **_quiet(heap={"warn": 50, "crit": 90}))
    reads = iter([60.0, 40.0, 60.0, 40.0, 60.0, 60.0, 30.0, 30.0, 30.0])
    svc._read_heap = lambda cid: {"n1": next(reads)} if cid == "test" else {}
    heap = lambda: [a for a in client.get(A, headers=ROOT).json()["active"] if a["metric"] == "heap" and a["clusterId"] == "test"]  # noqa: E731
    base = T0 + timedelta(minutes=50)
    for i in range(4):                                   # 60, 40, 60, 40: only 2 over the line
        svc.sample_heap(base + timedelta(minutes=2 * i))
    svc.run_once(base + timedelta(minutes=6))
    assert not heap()
    svc.sample_heap(base + timedelta(minutes=8))         # 60, 40, 60, 40, 60: 3 of 5
    svc.run_once(base + timedelta(minutes=8))
    assert [a["severity"] for a in heap()] == ["warning"]
    assert client.post(f"{A}/snoozes", json={"clusterId": "test", "metric": "heap", "duration": "resolved",
                                             "reason": "x"}, headers=ROOT).status_code == 201
    for i in range(4):                                   # 60, 30, 30, 30: back under
        svc.sample_heap(base + timedelta(minutes=10 + 2 * i))
    svc.run_once(base + timedelta(minutes=16))
    assert not heap()

    # 'until it clears' needs something active to wait for
    r = client.post(f"{A}/snoozes", json={"clusterId": "test", "metric": "disk", "duration": "resolved", "reason": "x"},
                    headers=ROOT)
    assert r.status_code == 400 and r.json()["error"]["code"] == "NOTHING_TO_SNOOZE"
    # the 'until it clears' heap snooze ended when the heap alert cleared
    assert not [x for x in client.get(A, headers=ROOT).json()["snoozes"] if x["metric"] == "heap"]


def test_digest_once_and_counts(env, client, slack):
    svc: AlertService = env["app"].state.alerts
    store = env["store"]
    now = datetime.now(timezone.utc)
    rid = (now - timedelta(days=10)).strftime("%Y%m%dT%H%M%S") + "-aaaa"
    store.put_json(f"approvals/{rid}.json", {
        "id": rid, "status": "EXPIRED", "op": "config.update", "requestedBy": "dev@x.com",
        "requestedAt": (now - timedelta(days=10)).isoformat(), "decidedAt": (now - timedelta(days=3)).isoformat(),
        "expiresAt": (now - timedelta(days=3)).isoformat()})
    c = svc._request_counts(now - timedelta(days=7))
    assert c["expired"] == 1 and c["submitted"] == 0
    fri = datetime(2026, 10, 16, 15, 5, tzinfo=IST).astimezone(timezone.utc)
    st: dict = {}
    assert svc._maybe_digest(st, fri) is True and st["lastDigest"] == "2026-W42"
    n = len(slack.sent)
    assert svc._maybe_digest({}, fri) is True and len(slack.sent) == n     # another worker: already claimed
