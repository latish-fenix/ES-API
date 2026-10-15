"""Dashboard: per-cluster summary, trend samples from the alert check, access rules."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.errors import ApiError
from conftest import ROOT, as_user

T = "/api/v1/clusters/test"


def test_summary_and_access(env, client, es, prefix):
    es("PUT", f"/{prefix}-a", json={"settings": {"number_of_replicas": 0}})
    s = client.get(f"{T}/summary", headers=ROOT).json()
    assert s["reachable"] is True and s["status"] in ("green", "yellow") and s["nodes"] >= 1
    for m in ("disk", "ram", "heap", "cpu"):
        assert set(s[m]) >= {"avg", "max", "maxNode"} and 0 <= s[m]["max"] <= 100
    assert s["shards"]["perNode"] == 1000 and s["shards"]["total"] >= 1 and s["shards"]["percent"] is not None
    assert s["size"]["indices"] >= 1 and s["size"]["storeBytes"] > 0
    assert s["alerts"] == {"critical": 0, "warning": 0, "snoozed": 0} and s["thresholds"]["disk"]["crit"] == 80
    assert s["growth"] is None and s["versions"]
    # who may see it
    client.put("/api/v1/admin/users/viewer", json={"clusters": {"test": "view"}}, headers=ROOT)
    assert client.get(f"{T}/summary", headers=as_user("viewer")).status_code == 200
    assert client.get("/api/v1/clusters/other/summary", headers=as_user("viewer")).status_code == 403
    assert client.get(f"{T}/trends", headers=as_user("viewer")).status_code == 200
    assert client.get(f"{T}/trends", params={"hours": 5}, headers=ROOT).status_code == 400


def test_unreachable_cluster_is_a_row_not_an_error(env, client, monkeypatch):
    reg = env["app"].state.registry

    def boom(cid):
        raise ApiError(502, "CLUSTER_UNREACHABLE", "Could not reach cluster: timed out")
    monkeypatch.setattr(reg, "client", boom)
    s = client.get(f"{T}/summary", headers=ROOT).json()
    assert s["reachable"] is False and s["errorCode"] == "CLUSTER_UNREACHABLE" and "alerts" in s


def test_samples_from_check_and_trends(env, client):
    app = env["app"]
    svc = app.state.alerts
    now = datetime.now(timezone.utc).replace(microsecond=0)
    svc.run_once(now - timedelta(hours=30))
    svc.run_once(now - timedelta(minutes=10))
    svc.run_once(now - timedelta(minutes=5))
    t = client.get(f"{T}/trends", headers=ROOT).json()
    assert len(t["points"]) == 2                      # the 30-hour-old one is outside 24 h
    p = t["points"][-1]
    assert set(p) == {"t", "disk", "ram", "heap", "cpu", "shards"} and p["disk"] is not None
    week = client.get(f"{T}/trends", params={"hours": 168}, headers=ROOT).json()
    assert len(week["points"]) in (2, 3) and all(x["t"][14:19] in ("00:00", "30:00") for x in week["points"])
    # old files are pruned once a day
    env["store"].put_json(f"alerts/metrics/test/{(now - timedelta(days=9)).date()}.json", {"points": []})
    assert app.state.dashboard.prune(now) == 1
    # alerts turned off: the check still reads clusters for the trends
    client.put("/api/v1/admin/alerts/settings", json={"enabled": False}, headers=ROOT)
    svc.run_once(now)
    assert len(client.get(f"{T}/trends", headers=ROOT).json()["points"]) == 3


def test_growth_from_daily_sizes(env, client):
    store = env["store"]
    today = datetime.now(timezone.utc).date()
    store.put_json(f"alerts/sizes/test/{today - timedelta(days=8)}.json", {"totalBytes": 1000, "docs": 10, "indices": {}})
    store.put_json(f"alerts/sizes/test/{today - timedelta(days=1)}.json", {"totalBytes": 1500, "docs": 15, "indices": {}})
    store.put_json(f"alerts/sizes/test/{today}.json", {"totalBytes": 4000, "docs": 40, "indices": {}})
    g = client.get(f"{T}/summary", headers=ROOT).json()["growth"]
    assert g == {"bytes": 3000, "docs": 30, "from": str(today - timedelta(days=8)), "to": str(today), "days": 8}
