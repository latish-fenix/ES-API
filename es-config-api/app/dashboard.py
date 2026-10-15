"""All-clusters dashboard: one summary per cluster, and trend samples.

The console's home page asks for each cluster's summary on its own (so a slow cluster only
delays its own row) every 2 minutes. Trend samples are written by the 5-minute alert check
(``alerts/metrics/<cluster>/<date>.json``, worst node per metric) and kept for 8 days.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from .alerts import PERCENT_METRICS, SIZES, effective, shard_limit_per_node
from .clusters import es_call, is_legacy
from .errors import ApiError, bad_request
from .identity import User, require_any_access
from .nodes import node_stats
from .repos import _update_with_retry
from .util import iso, utcnow

log = logging.getLogger("es_config_api.dashboard")

METRICS_PREFIX = "alerts/metrics/"
KEEP_DAYS = 8
SAMPLE_FIELDS = ("disk", "ram", "heap", "cpu", "shards")
SUMMARY_TIMEOUT = 15


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _stat(nodes: list[dict], field: str, data_only: bool = False) -> dict:
    vals = [n[field] for n in nodes if n.get(field) is not None and (n.get("data") or not data_only)]
    if not vals:
        return {"avg": None, "max": None, "maxNode": None}
    worst = max((n for n in nodes if n.get(field) is not None and (n.get("data") or not data_only)),
                key=lambda n: n[field])
    return {"avg": round(sum(vals) / len(vals), 1), "max": round(float(worst[field]), 1), "maxNode": worst["name"]}


def sample_from(data: dict, now: datetime) -> dict:
    """One trend point from an alert check's readings: the worst node per metric."""
    nodes = data["nodes"]["nodes"]
    data_nodes = [n for n in nodes if n.get("data")] or nodes

    def worst(field: str, pool: list[dict]) -> float | None:
        vals = [n[field] for n in pool if n.get(field) is not None]
        return round(float(max(vals)), 1) if vals else None
    return {"t": iso(now), "disk": worst("diskUsedPercent", data_nodes), "ram": worst("ramUsedPercent", nodes),
            "heap": worst("heapUsedPercent", nodes), "cpu": worst("cpuPercent", nodes),
            "shards": round(100 * data["shards"] / data["shardLimit"], 1) if data.get("shardLimit") else None}


class Dashboard:
    def __init__(self, app, store):
        self.app = app
        self.store = store

    # ---- samples (written by the alert check)
    def record(self, samples: dict[str, dict], now: datetime) -> None:
        day = now.date().isoformat()
        for cid, point in samples.items():
            def m(doc, point=point):
                doc["points"] = doc.get("points", []) + [point]
            try:
                _update_with_retry(self.store, f"{METRICS_PREFIX}{cid}/{day}.json", m, {"points": []})
            except Exception:
                log.exception("could not save the trend sample of %s", cid)

    def prune(self, now: datetime) -> int:
        """Drop trend files older than KEEP_DAYS (run once a day)."""
        cutoff = (now - timedelta(days=KEEP_DAYS)).date().isoformat()
        gone = 0
        for key in self.store.list_keys(METRICS_PREFIX):
            day = key.rsplit("/", 1)[-1][:-5]
            if day < cutoff:
                try:
                    self.store.delete(key)
                    gone += 1
                except Exception:
                    log.exception("could not delete %s", key)
        return gone

    # ---- reads
    def trends(self, user: User, cluster_id: str, hours: int) -> dict:
        require_any_access(user, cluster_id)
        self.app.state.registry.get(cluster_id)
        if hours not in (24, 168):
            raise bad_request("INVALID_PERIOD", "hours must be 24 or 168 (7 days)")
        now = utcnow()
        since = now - timedelta(hours=hours)
        points: list[dict] = []
        d = since.date()
        while d <= now.date():
            doc, _ = self.store.get_json(f"{METRICS_PREFIX}{cluster_id}/{d.isoformat()}.json")
            points.extend(p for p in (doc or {}).get("points", []) if _parse(p["t"]) >= since)
            d += timedelta(days=1)
        points.sort(key=lambda p: p["t"])
        if hours > 24:
            points = _bucket(points, 30)          # 7 days: 30-minute buckets, peak per bucket
        return {"clusterId": cluster_id, "hours": hours, "points": points,
                "since": iso(since), "until": iso(now)}

    def summary(self, user: User, cluster_id: str) -> dict:
        require_any_access(user, cluster_id)
        reg = self.app.state.registry
        cfg = reg.get(cluster_id)
        alerts = self.app.state.alerts
        settings = alerts.settings()
        st = alerts.state()
        snoozes = alerts.snoozes()
        mine = [a for a in st.get("alerts", {}).values() if a["clusterId"] == cluster_id]
        live = [a for a in mine if not alerts.snoozed_by(a, snoozes)]
        out: dict[str, Any] = {
            "clusterId": cluster_id, "name": cfg.name, "checkedAt": iso(),
            "alerts": {"critical": sum(1 for a in live if a["severity"] == "critical"),
                       "warning": sum(1 for a in live if a["severity"] == "warning"),
                       "snoozed": len(mine) - len(live)},
            "thresholds": {m: effective(settings, cluster_id, m) for m in PERCENT_METRICS},
        }
        out["growth"] = self._growth(cluster_id)
        try:
            es = reg.client(cluster_id).options(request_timeout=SUMMARY_TIMEOUT, max_retries=0)
            health = es_call(es, "GET", "/_cluster/health")
            ns = node_stats(es, cluster_id)
            stats = es_call(es, "GET", "/_cluster/stats", params={"filter_path": (
                "indices.count,indices.docs.count,indices.store.size_in_bytes,indices.shards.total,"
                "nodes.versions")})
            per_node = shard_limit_per_node(es) or 1000
        except ApiError as e:
            if e.status in (401, 403) and e.code in ("PERMISSION_DENIED", "ADMIN_REQUIRED"):
                raise
            out.update(reachable=False, error=e.message, errorCode=e.code)
            return out
        nodes = ns["nodes"]
        data_nodes = health.get("number_of_data_nodes") or ns["summary"]["dataNodes"] or 0
        shards = sum(health.get(k) or 0 for k in ("active_shards", "initializing_shards", "unassigned_shards"))
        limit = per_node * data_nodes if data_nodes else None
        idx = stats.get("indices") or {}
        versions = sorted(set((stats.get("nodes") or {}).get("versions") or [n.get("version") for n in nodes if n.get("version")]))
        out.update(
            reachable=True, status=health.get("status"), clusterName=health.get("cluster_name"),
            versions=versions, legacy=is_legacy(es),
            nodes=health.get("number_of_nodes"), dataNodes=data_nodes,
            unassignedShards=health.get("unassigned_shards") or 0,
            disk={**_stat(nodes, "diskUsedPercent", data_only=True),
                  "freeBytes": ns["summary"]["diskAvailableBytes"], "totalBytes": ns["summary"]["diskTotalBytes"]},
            ram=_stat(nodes, "ramUsedPercent"), heap=_stat(nodes, "heapUsedPercent"), cpu=_stat(nodes, "cpuPercent"),
            shards={"total": shards, "limit": limit, "perNode": per_node,
                    "percent": round(100 * shards / limit, 1) if limit else None},
            size={"storeBytes": (idx.get("store") or {}).get("size_in_bytes"),
                  "docs": (idx.get("docs") or {}).get("count"), "indices": idx.get("count")},
        )
        return out

    def _growth(self, cluster_id: str) -> dict | None:
        """Size change over about 7 days, from the daily index-size snapshots (open indices)."""
        try:
            days = sorted(k.rsplit("/", 1)[-1][:-5] for k in self.store.list_keys(f"{SIZES}{cluster_id}/"))
        except Exception:
            return None
        if len(days) < 2:
            return None
        latest = days[-1]
        target = (datetime.fromisoformat(latest) - timedelta(days=7)).date().isoformat()
        older = [d for d in days[:-1] if d <= target] or days[:1]
        base = older[-1]
        new, _ = self.store.get_json(f"{SIZES}{cluster_id}/{latest}.json")
        old, _ = self.store.get_json(f"{SIZES}{cluster_id}/{base}.json")
        if not new or not old:
            return None
        return {"bytes": new["totalBytes"] - old["totalBytes"], "docs": new.get("docs", 0) - old.get("docs", 0),
                "from": base, "to": latest,
                "days": (datetime.fromisoformat(latest) - datetime.fromisoformat(base)).days}


def _bucket(points: list[dict], minutes: int) -> list[dict]:
    """Peak per bucket, so a short spike still shows on the 7-day chart."""
    out: dict[str, dict] = {}
    for p in points:
        t = _parse(p["t"])
        start = t.replace(minute=(t.minute // minutes) * minutes, second=0, microsecond=0)
        k = iso(start)
        b = out.setdefault(k, {"t": k})
        for f in SAMPLE_FIELDS:
            v = p.get(f)
            if v is not None and (b.get(f) is None or v > b[f]):
                b[f] = v
    return [out[k] for k in sorted(out)]
