"""Cluster alerts to Slack, request notices and the weekly digest.

Every ALERT_INTERVAL_SECONDS (default 300) one API worker checks each cluster:

  disk    each data node's disk used                       warning 70% · critical 80%
  ram     RAM used excluding cache (see nodes.py)          70 / 80
  heap    JVM heap used, above the line 2 checks running   70 / 80
  cpu     CPU, above the line 2 checks running             70 / 80
  shards  shards in the cluster vs its limit                70 / 80
          (cluster.max_shards_per_node × data nodes)
  health  cluster yellow / red (critical)
  nodes   a node left the cluster, or the cluster stopped answering (critical)

Thresholds are editable, with per-cluster overrides (Admin → Alerts). An alert posts when it
starts, then again every hour while it is a warning and every 5 minutes while it is critical,
and once more when it clears. It clears 2 points below the line it fired at, so a value
hovering on the line doesn't flap. Snoozes are set in the console only and never shown in
Slack: a snoozed alert is still tracked and listed, it just doesn't post.

Workers coordinate through S3: ``alerts/state.json`` records the last run, and a short lock
makes sure only one worker runs a check at a time. Nothing here changes any cluster.

S3 keys (alerts/): settings.json, state.json, snoozes.json, history/<date>.json,
sizes/<cluster>/<date>.json (index sizes, one per day, for the weekly digest).
"""
from __future__ import annotations

import copy
import logging
import os
import secrets as pysecrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .clusters import es_call
from .errors import ApiError, bad_request, conflict, not_found
from .identity import User
from .nodes import node_stats
from .repos import LockRepo, _update_with_retry
from .slack import COLORS, SlackNotifier, esc, link
from .storage import PreconditionFailed
from .util import iso, utcnow

log = logging.getLogger("es_config_api.alerts")

SETTINGS_KEY = "alerts/settings.json"
STATE_KEY = "alerts/state.json"
SNOOZES_KEY = "alerts/snoozes.json"
HISTORY = "alerts/history/"
SIZES = "alerts/sizes/"
DIGESTS = "alerts/digests/"
RUN_LOCK = ("_alerts", "run", "check")

PERCENT_METRICS = ("disk", "ram", "heap", "cpu", "shards")
STATE_METRICS = ("health", "nodes")
METRICS = PERCENT_METRICS + STATE_METRICS
SUSTAINED = {"heap", "cpu"}                   # must stay above the line for 2 checks (one interval)
COLLECT_TIMEOUT = 15                          # seconds per Elasticsearch call during a check
HYSTERESIS = 2.0                              # clears this many points below the line
METRIC_LABEL = {"disk": "Disk", "ram": "RAM (excl. cache)", "heap": "JVM heap", "cpu": "CPU",
                "shards": "Shards", "health": "Cluster health", "nodes": "Nodes"}
SNOOZE_DURATIONS = {"1h": timedelta(hours=1), "4h": timedelta(hours=4), "24h": timedelta(hours=24),
                    "7d": timedelta(days=7), "resolved": None}
IST = timezone(timedelta(hours=5, minutes=30))

DEFAULT_SETTINGS: dict = {
    "enabled": True,
    "repeatMinutes": {"warning": 60, "critical": 5},
    "defaults": {**{m: {"on": True, "warn": 70, "crit": 80} for m in PERCENT_METRICS},
                 **{m: {"on": True} for m in STATE_METRICS}},
    "clusters": {},
    "digest": {"enabled": True},
}
DIGEST_WEEKDAY, DIGEST_HOUR = 4, 15          # Fridays 15:00 IST (the digest covers the 7 days before)
DIGEST_WHEN = "Fridays 15:00 IST"


def human_bytes(n: float | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:g}%"


def alert_key(cluster_id: str, metric: str, node: str | None = None) -> str:
    return f"{cluster_id}|{metric}|{node or ''}"


# ------------------------------------------------------------------ settings
def validate_settings(doc: Any, cluster_ids: set[str] | None = None) -> dict:
    """Merge onto the defaults and check every value. Returns the full settings document."""
    if not isinstance(doc, dict):
        raise bad_request("INVALID_ALERT_SETTINGS", "Settings must be a JSON object")
    out = copy.deepcopy(DEFAULT_SETTINGS)
    out["enabled"] = bool(doc.get("enabled", True))
    rep = doc.get("repeatMinutes") or {}
    for sev, lo, hi in (("warning", 5, 1440), ("critical", 5, 1440)):
        v = rep.get(sev, out["repeatMinutes"][sev])
        if not isinstance(v, int) or not lo <= v <= hi:
            raise bad_request("INVALID_ALERT_SETTINGS", f"repeatMinutes.{sev} must be {lo}–{hi} minutes")
        out["repeatMinutes"][sev] = v

    def metric_block(where: str, m: str, v: Any, partial: bool) -> dict:
        if not isinstance(v, dict):
            raise bad_request("INVALID_ALERT_SETTINGS", f"{where}.{m} must be an object")
        res: dict = {}
        if "on" in v:
            res["on"] = bool(v["on"])
        if m in PERCENT_METRICS:
            for f in ("warn", "crit"):
                if f in v:
                    x = v[f]
                    if not isinstance(x, (int, float)) or not 1 <= x <= 100:
                        raise bad_request("INVALID_ALERT_SETTINGS", f"{where}.{m}.{f} must be 1–100")
                    res[f] = x
        elif set(v) - {"on"}:
            raise bad_request("INVALID_ALERT_SETTINGS", f"{where}.{m} only has 'on'")
        if not partial:
            res = {**DEFAULT_SETTINGS["defaults"][m], **res}
        return res

    for m, v in (doc.get("defaults") or {}).items():
        if m not in METRICS:
            raise bad_request("INVALID_ALERT_SETTINGS", f"Unknown metric '{m}' (one of {', '.join(METRICS)})")
        out["defaults"][m] = metric_block("defaults", m, v, False)
    for cid, c in (doc.get("clusters") or {}).items():
        if cluster_ids is not None and cid not in cluster_ids:
            raise bad_request("INVALID_ALERT_SETTINGS", f"Unknown cluster '{cid}'")
        if not isinstance(c, dict):
            raise bad_request("INVALID_ALERT_SETTINGS", f"clusters.{cid} must be an object")
        entry: dict = {"off": bool(c.get("off", False)), "metrics": {}}
        for m, v in (c.get("metrics") or {}).items():
            if m not in METRICS:
                raise bad_request("INVALID_ALERT_SETTINGS", f"Unknown metric '{m}'")
            block = metric_block(f"clusters.{cid}", m, v, True)
            if block:
                entry["metrics"][m] = block
        if entry["off"] or entry["metrics"]:
            out["clusters"][cid] = entry
    for m in PERCENT_METRICS:
        blocks = [("defaults", out["defaults"][m])] + [
            (f"clusters.{cid}", {**out["defaults"][m], **c["metrics"].get(m, {})}) for cid, c in out["clusters"].items()]
        for where, b in blocks:
            if b["warn"] > b["crit"]:
                raise bad_request("INVALID_ALERT_SETTINGS", f"{where}.{m}: warning ({b['warn']}%) must not be "
                                                            f"above critical ({b['crit']}%)")
    dg = doc.get("digest") or {}
    out["digest"]["enabled"] = bool(dg.get("enabled", True))
    for f in ("updatedBy", "updatedAt"):
        if doc.get(f):
            out[f] = doc[f]
    return out


def effective(settings: dict, cluster_id: str, metric: str) -> dict:
    c = (settings.get("clusters") or {}).get(cluster_id) or {}
    eff = {**settings["defaults"].get(metric, {}), **(c.get("metrics") or {}).get(metric, {})}
    if c.get("off"):
        eff["on"] = False
    return eff


def shard_limit_per_node(es) -> int | None:
    """cluster.max_shards_per_node (transient > persistent > default). Elasticsearch nests it
    with max_shards_per_node.frozen, so the value is either "1000" or {"frozen": ...}."""
    try:
        s = es_call(es, "GET", "/_cluster/settings", params={
            "include_defaults": "true", "filter_path": "*.cluster.max_shards_per_node*"})
    except ApiError:
        return None
    out = None
    for layer in ("defaults", "persistent", "transient"):        # later layers win
        c = (s.get(layer) or {}).get("cluster") or {}
        v = c.get("max_shards_per_node")
        if isinstance(v, dict):
            v = v.get("") if "" in v else None
        try:
            if v is not None:
                out = int(v)
        except (TypeError, ValueError):
            pass
    return out


# ------------------------------------------------------------------ service
class AlertService:
    def __init__(self, app, store, slack: SlackNotifier, interval_seconds: int = 300):
        self.app = app
        self.store = store
        self.slack = slack
        self.interval = max(60, int(interval_seconds))
        self.locks = LockRepo(store, 900)        # longer than the slowest possible check
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.worker_id = f"{os.uname().nodename if hasattr(os, 'uname') else 'host'}:{os.getpid()}:{uuid.uuid4().hex[:6]}"

    # ---- documents
    def settings(self) -> dict:
        doc, _ = self.store.get_json(SETTINGS_KEY)
        try:
            return validate_settings(doc or {})
        except ApiError:
            log.exception("alerts/settings.json is invalid; using defaults")
            return copy.deepcopy(DEFAULT_SETTINGS)

    def save_settings(self, doc: dict, by: str) -> dict:
        ids = self.app.state.registry.ids()
        out = validate_settings(doc, ids)
        out.update(updatedBy=by, updatedAt=iso())
        self.store.put_json(SETTINGS_KEY, out)
        return out

    def state(self) -> dict:
        doc, _ = self.store.get_json(STATE_KEY)
        return doc or {"alerts": {}, "pending": {}, "nodes": {}}

    def snoozes(self, now: datetime | None = None) -> list[dict]:
        now = now or utcnow()
        doc, _ = self.store.get_json(SNOOZES_KEY)
        items = list(((doc or {}).get("items") or {}).values())
        return sorted([s for s in items if not s.get("until") or _parse(s["until"]) > now],
                      key=lambda s: s["createdAt"], reverse=True)

    # ---- snoozes (console only)
    def snooze(self, by: str, cluster_id: str, metric: str | None, node: str | None, duration: str,
               reason: str) -> dict:
        if cluster_id not in self.app.state.registry.ids():
            raise not_found("CLUSTER_NOT_FOUND", f"Unknown cluster '{cluster_id}'")
        if metric and metric not in METRICS:
            raise bad_request("INVALID_SNOOZE", f"Unknown metric '{metric}'")
        if node and not metric:
            raise bad_request("INVALID_SNOOZE", "Pick the alert type when snoozing one node")
        if duration not in SNOOZE_DURATIONS:
            raise bad_request("INVALID_SNOOZE", f"duration must be one of {', '.join(SNOOZE_DURATIONS)}")
        if not (reason or "").strip():
            raise bad_request("REASON_REQUIRED", "Say why this alert is snoozed")
        now = utcnow()
        delta = SNOOZE_DURATIONS[duration]
        probe = {"clusterId": cluster_id, "metric": metric or None, "node": node or None}
        if delta is None and not any(self._matches(probe, a) for a in self.state().get("alerts", {}).values()):
            raise bad_request("NOTHING_TO_SNOOZE", "'Until it clears' needs an active alert to wait for; nothing "
                                                   "it covers is active now. Pick a duration instead.")
        item = {"id": now.strftime("%Y%m%dT%H%M%S") + "-" + pysecrets.token_hex(3), "clusterId": cluster_id,
                "metric": metric or None, "node": node or None, "duration": duration,
                "until": iso(now + delta) if delta else None, "untilResolved": delta is None,
                "by": by, "reason": reason.strip()[:300], "createdAt": iso(now)}

        def m(doc):
            doc["items"] = {k: v for k, v in doc.get("items", {}).items()
                            if not v.get("until") or _parse(v["until"]) > now}
            doc["items"][item["id"]] = item
        _update_with_retry(self.store, SNOOZES_KEY, m, {"items": {}})
        return item

    def unsnooze(self, snooze_id: str) -> dict:
        found: dict = {}

        def m(doc):
            found.update(doc.get("items", {}).pop(snooze_id, None) or {})
        _update_with_retry(self.store, SNOOZES_KEY, m, {"items": {}})
        if not found:
            raise not_found("SNOOZE_NOT_FOUND", f"No snooze '{snooze_id}'")
        return found

    @staticmethod
    def _matches(s: dict, a: dict) -> bool:
        return (s["clusterId"] == a["clusterId"] and (not s.get("metric") or s["metric"] == a["metric"])
                and (not s.get("node") or s["node"] == (a.get("node") or "")))

    def snoozed_by(self, a: dict, snoozes: list[dict]) -> dict | None:
        return next((s for s in snoozes if self._matches(s, a)), None)

    # ---- views
    def overview(self) -> dict:
        st = self.state()
        sn = self.snoozes()
        active = []
        for a in sorted(st.get("alerts", {}).values(),
                        key=lambda a: (a["severity"] != "critical", a["clusterId"], a["metric"], a.get("node") or "")):
            s = self.snoozed_by(a, sn)
            active.append({**a, "snoozedBy": s and {k: s[k] for k in ("id", "by", "until", "untilResolved", "reason")}})
        return {"active": active, "snoozes": sn, "lastRun": st.get("lastRun"),
                "lastDigest": st.get("lastDigest"), "digestWhen": DIGEST_WHEN, "intervalSeconds": self.interval}

    def for_cluster(self, cluster_id: str) -> dict:
        st = self.state()
        s = self.settings()
        return {"active": [a for a in st.get("alerts", {}).values() if a["clusterId"] == cluster_id],
                "thresholds": {m: effective(s, cluster_id, m) for m in PERCENT_METRICS},
                "lastRunAt": (st.get("lastRun") or {}).get("at")}

    def history(self, days: int = 7) -> list[dict]:
        out: list[dict] = []
        today = utcnow().date()
        for i in range(max(1, min(days, 31))):
            d, _ = self.store.get_json(f"{HISTORY}{today - timedelta(days=i)}.json")
            out.extend((d or {}).get("items", []))
        return sorted(out, key=lambda e: e["at"], reverse=True)

    def _history_add(self, events: list[dict]) -> None:
        if not events:
            return
        by_day: dict[str, list[dict]] = {}
        for e in events:
            by_day.setdefault(e["at"][:10], []).append(e)
        for day, evs in by_day.items():
            def m(doc, evs=evs):
                doc["items"] = (doc.get("items", []) + evs)[-2000:]
            try:
                _update_with_retry(self.store, f"{HISTORY}{day}.json", m, {"items": []})
            except Exception:
                log.exception("could not write alert history")

    def clear(self, key: str, by: str) -> dict:
        """Forget a node that left for good (only 'node left' alerts can be cleared by hand; the
        others clear themselves, and a gone node's own disk/RAM/... alerts are dropped with it)."""
        token = self._lock_run(wait=True)
        try:
            st, etag = self.store.get_json(STATE_KEY)
            a = (st or {}).get("alerts", {}).get(key)
            if not a:
                raise not_found("ALERT_NOT_FOUND", "No such active alert")
            if a["metric"] != "nodes" or not a.get("node"):
                raise bad_request("NOT_CLEARABLE", "Only 'node left' alerts can be cleared; the others clear "
                                                   "themselves when the value drops")
            st["alerts"].pop(key)
            known = st.setdefault("nodes", {}).get(a["clusterId"]) or []
            st["nodes"][a["clusterId"]] = [n for n in known if n != a["node"]]
            self.store.put_json(STATE_KEY, st, if_match=etag)
            self._history_add([{"at": iso(), "event": "CLEARED", "key": key, "clusterId": a["clusterId"],
                                "metric": a["metric"], "node": a.get("node"), "by": by}])
            return a
        finally:
            self.locks.release(*RUN_LOCK, token)

    # ---- collection
    def _client(self, cluster_id: str):
        """The cluster's client with short timeouts and no retries, so one slow cluster can't
        hold a check for long (8 clusters are read in parallel)."""
        return self.app.state.registry.client(cluster_id).options(request_timeout=COLLECT_TIMEOUT, max_retries=0)

    def collect(self, cluster_id: str) -> dict:
        """Current readings for one cluster (raises ApiError when it can't be reached)."""
        es = self._client(cluster_id)
        ns = node_stats(es, cluster_id)
        health = es_call(es, "GET", "/_cluster/health")
        per_node = shard_limit_per_node(es)
        data_nodes = health.get("number_of_data_nodes") or ns["summary"]["dataNodes"] or 0
        # the limit counts every shard of every open index, assigned or not
        shards = sum(health.get(k) or 0 for k in ("active_shards", "initializing_shards", "unassigned_shards"))
        limit = (per_node or 1000) * data_nodes if data_nodes else None
        return {"nodes": ns, "health": health, "shards": shards, "shardLimit": limit,
                "shardsPerNodeLimit": per_node or 1000}

    def readings(self, cluster_id: str, data: dict, settings: dict) -> list[dict]:
        """Every current value of the enabled percent metrics (below the line too)."""
        rows: list[dict] = []
        ns = data["nodes"]
        for n in ns["nodes"]:
            for metric, field_, data_only in (("disk", "diskUsedPercent", True), ("ram", "ramUsedPercent", False),
                                              ("heap", "heapUsedPercent", False), ("cpu", "cpuPercent", False)):
                if data_only and not n.get("data"):
                    continue
                v = n.get(field_)
                if v is not None and effective(settings, cluster_id, metric).get("on"):
                    rows.append({"metric": metric, "node": n["name"], "value": float(v)})
        if data["shardLimit"] and effective(settings, cluster_id, "shards").get("on"):
            rows.append({"metric": "shards", "node": None,
                         "value": round(100 * data["shards"] / data["shardLimit"], 1),
                         "detail": f"{data['shards']:,} of {data['shardLimit']:,} shards "
                                   f"({data['shardsPerNodeLimit']:,} per data node)"})
        return rows

    # ---- one check of every cluster
    def due(self, now: datetime | None = None, st: dict | None = None) -> bool:
        now = now or utcnow()
        last = ((st if st is not None else self.state()).get("lastRun") or {}).get("at")
        return not last or (now - _parse(last)).total_seconds() >= self.interval - 15

    def _lock_run(self, wait: bool = False) -> str:
        deadline = time.monotonic() + (60 if wait else 0)
        while True:
            try:
                return self.locks.acquire(*RUN_LOCK, f"alerts {self.worker_id}")
            except ApiError as e:
                if e.code != "CHANGE_IN_PROGRESS" or time.monotonic() > deadline:
                    raise conflict("CHECK_IN_PROGRESS", "An alert check is running right now; try again "
                                                        "in a minute") from e
                time.sleep(1)

    def run_once(self, now: datetime | None = None, send: bool = True, only_if_due: bool = False) -> dict | None:
        token = self._lock_run()
        try:
            return self._run(now or utcnow(), send, token, only_if_due)
        finally:
            self.locks.release(*RUN_LOCK, token)

    def _run(self, now: datetime, send: bool, token: str, only_if_due: bool) -> dict | None:
        started = time.monotonic()
        settings = self.settings()
        st, etag = self.store.get_json(STATE_KEY)
        st = st or {}
        if only_if_due and not self.due(now, st):   # the other worker just ran it
            return None
        for k in ("alerts", "pending", "nodes", "unannounced"):
            st.setdefault(k, {})
        snoozes = self.snoozes(now)
        clusters = sorted(self.app.state.registry.ids())
        run: dict = {"at": iso(now), "worker": self.worker_id, "clusters": {}, "slack": None}
        events: list[dict] = []
        firing: dict[str, list[dict]] = {}

        # read every cluster (also with alerts off: the dashboard's trend charts use the readings)
        with ThreadPoolExecutor(min(8, max(1, len(clusters)))) as pool:
            results = dict(zip(clusters, pool.map(self._collect_safe, clusters)))
        samples = {cid: self._sample(data, now) for cid, (data, err) in results.items() if data is not None}
        samples = {k: v for k, v in samples.items() if v}
        if not settings["enabled"]:
            run["disabled"] = True
            st["alerts"], st["pending"], st["unannounced"] = {}, {}, {}
            for cid in clusters:
                run["clusters"][cid] = {"ok": results[cid][1] is None,
                                        **({"error": results[cid][1]} if results[cid][1] else {})}
        else:
            for bucket in ("alerts", "pending", "unannounced"):   # clusters that were removed
                for k in [k for k in st[bucket] if k.split("|")[0] not in clusters]:
                    st[bucket].pop(k)
            for cid in clusters:
                data, err = results[cid]
                run["clusters"][cid] = {"ok": err is None, **({"error": err} if err else {})}
                try:
                    firing[cid] = self._evaluate(cid, data, err, settings, st, snoozes, now, events)
                except Exception:
                    log.exception("evaluating alerts of %s failed", cid)
                    run["clusters"][cid] = {"ok": False, "error": "internal error while evaluating"}
            self._end_resolved_snoozes(st, snoozes)

        # one message per cluster; an alert counts as posted only when its message went out
        sent = 0
        for cid in clusters:
            fire = firing.get(cid) or []
            gone = [a for a in st["unannounced"].values() if a["clusterId"] == cid]
            if not fire and not gone:
                continue
            if not send:
                continue
            self.locks.verify(*RUN_LOCK, token)                  # never post from a run that lost its lock
            res = self.slack.post(*self._message(cid, fire, gone, now))
            if not res.get("ok"):
                run["slack"] = res.get("error")
                if res.get("skipped"):
                    for a in gone:                               # nobody to tell
                        st["unannounced"].pop(a["key"], None)
                continue
            sent += 1
            for a in fire:
                a["lastSentAt"] = iso(now)
                a["lastSentSeverity"] = a["severity"]
            for a in gone:
                st["unannounced"].pop(a["key"], None)
        # resolved notices older than a day are dropped
        for k in [k for k, a in st["unannounced"].items() if (now - _parse(a["resolvedAt"])).total_seconds() > 86400]:
            st["unannounced"].pop(k)
        run["messages"] = sent
        run["durationMs"] = int((time.monotonic() - started) * 1000)
        st["lastRun"] = run

        self.locks.verify(*RUN_LOCK, token)
        etag = self._save_state(st, etag)
        self._history_add(events)

        # best effort extras: they never undo or block the check above
        changed = False
        for extra in (lambda: self._record_samples(samples, st, now),
                      lambda: self._daily_sizes(st, now),
                      lambda: self._maybe_digest(st, now) if settings["digest"]["enabled"] and send else False,
                      lambda: self._sweep_approvals() and False):
            try:
                changed = bool(extra()) or changed
            except Exception:
                log.exception("alert extras failed")
        if changed and etag:
            self._save_state(st, etag)
        return {**run, "active": len(st["alerts"])}

    @staticmethod
    def _sample(data: dict, now: datetime) -> dict | None:
        from .dashboard import sample_from
        try:
            return sample_from(data, now)
        except Exception:
            log.exception("could not build a trend sample")
            return None

    def _record_samples(self, samples: dict, st: dict, now: datetime) -> bool:
        dash = getattr(self.app.state, "dashboard", None)
        if not dash or not samples:
            return False
        dash.record(samples, now)
        if st.get("metricsPrunedDate") != now.date().isoformat():
            dash.prune(now)
            st["metricsPrunedDate"] = now.date().isoformat()
            return True
        return False

    def _save_state(self, st: dict, etag: str | None) -> str | None:
        try:
            if etag:
                return self.store.put_json(STATE_KEY, st, if_match=etag)
            return self.store.put_json(STATE_KEY, st, if_none_match=True)
        except PreconditionFailed:  # only an admin's "forget node" can race us; next run reconciles
            log.warning("alerts/state.json changed during the check; not saved this time")
            return None

    def _collect_safe(self, cid: str) -> tuple[dict | None, str | None]:
        try:
            return self.collect(cid), None
        except ApiError as e:
            return None, e.message
        except Exception as e:  # never let one cluster stop the others
            log.exception("alert check of %s failed", cid)
            return None, str(e)[:300]

    def _evaluate(self, cid: str, data: dict | None, err: str | None, settings: dict, st: dict,
                  snoozes: list[dict], now: datetime, events: list[dict]) -> list[dict]:
        """Update st for one cluster. Returns the alerts to post now; alerts that cleared go to
        st["unannounced"] (posted as "OK again"). An alert that disappears because its check was
        turned off, or its node left, is dropped without an "OK again"."""
        alerts, pending = st["alerts"], st["pending"]
        mine = {k: a for k, a in alerts.items() if a["clusterId"] == cid}
        on = {m: bool(effective(settings, cid, m).get("on")) for m in METRICS}
        conds: dict[str, dict] = {}        # key -> condition that is on now
        recovered: set[str] = set()        # keys we know are fine now ("OK again")
        down_key = alert_key(cid, "nodes", None)

        if err is not None:
            for k in [k for k in pending if k.startswith(f"{cid}|")]:
                pending.pop(k)             # a gap in readings restarts the "2 checks" count
            if on["nodes"]:
                conds[down_key] = {"metric": "nodes", "node": None, "severity": "critical", "value": None,
                                   "text": f"Cluster not answering: {err[:200]}"}
            keep = {k for k in mine if k != down_key}   # no fresh readings: the rest stays as it is
        else:
            recovered.add(down_key)
            over: set[str] = set()
            for r in self.readings(cid, data, settings):
                key = alert_key(cid, r["metric"], r["node"])
                eff = effective(settings, cid, r["metric"])
                prev = alerts.get(key)
                sev = self._severity(r["value"], eff, prev)
                if sev and r["metric"] in SUSTAINED and not prev:
                    over.add(key)
                    first = pending.setdefault(key, iso(now))
                    if (now - _parse(first)).total_seconds() < self.interval * 0.8:
                        sev = None          # above the line, but not for 2 checks yet
                if sev:
                    conds[key] = {**r, "severity": sev, "threshold": eff["crit" if sev == "critical" else "warn"]}
                else:
                    recovered.add(key)
            for k in [k for k in pending if k.startswith(f"{cid}|") and k not in over]:
                pending.pop(k)
            hkey = alert_key(cid, "health", None)
            h = data["health"]
            if on["health"] and h.get("status") in ("yellow", "red"):
                un = h.get("unassigned_shards") or 0
                conds[hkey] = {"metric": "health", "node": None, "severity": "critical", "value": None,
                               "text": f"Cluster is {h['status'].upper()}"
                                       + (f" · {un:,} unassigned shard{'s' if un != 1 else ''}" if un else "")}
            elif on["health"]:
                recovered.add(hkey)
            # nodes that left: remembered until they come back or an admin clears the alert
            names = {n["name"] for n in data["nodes"]["nodes"] if n.get("name")}
            if on["nodes"]:
                known = set(st["nodes"].get(cid) or names)
                for g in sorted(known - names):
                    conds[alert_key(cid, "nodes", g)] = {
                        "metric": "nodes", "node": g, "severity": "critical", "value": None,
                        "text": f"Node {g} left the cluster ({len(names)} node{'s' if len(names) != 1 else ''} now)"}
                recovered.update(alert_key(cid, "nodes", n) for n in names)
                st["nodes"][cid] = sorted(known | names)
            else:
                st["nodes"][cid] = sorted(names)
            keep = set()

        to_post: list[dict] = []
        for key, c in conds.items():
            prev = alerts.get(key)
            a = {**(prev or {}), "key": key, "clusterId": cid, "metric": c["metric"], "node": c.get("node") or "",
                 "severity": c["severity"], "value": c.get("value"), "threshold": c.get("threshold"),
                 "detail": c.get("detail"), "text": c.get("text"), "updatedAt": iso(now)}
            a = {k: v for k, v in a.items() if v is not None or k == "value"}
            st["unannounced"].pop(key, None)            # firing again: no "OK again" for it
            if not prev:
                a["since"] = iso(now)
                events.append({"at": iso(now), "event": "FIRING", **self._ev(a)})
                due = True
            else:
                if prev["severity"] != c["severity"]:
                    events.append({"at": iso(now), "event": "ESCALATED" if c["severity"] == "critical" else "EASED",
                                   **self._ev(a)})
                due = (c["severity"] != prev.get("lastSentSeverity")
                       or self._repeat_due(prev, settings, now, c["severity"]))
            alerts[key] = a
            if due and not self.snoozed_by(a, snoozes):
                to_post.append(a)
        for key, a in mine.items():
            if key in conds or key in keep:
                continue
            alerts.pop(key, None)
            pending.pop(key, None)
            if key in recovered:
                events.append({"at": iso(now), "event": "RESOLVED", **self._ev(a)})
                if a.get("lastSentAt") and not self.snoozed_by(a, snoozes):
                    st["unannounced"][key] = {**self._ev(a), "resolvedAt": iso(now)}
            else:                                         # check turned off, or its node is gone
                events.append({"at": iso(now), "event": "DROPPED", **self._ev(a)})
        return to_post

    @staticmethod
    def _severity(value: float, eff: dict, prev: dict | None) -> str | None:
        warn, crit = float(eff["warn"]), float(eff["crit"])
        if prev:  # hysteresis: stay at the current level until 2 points below its line
            if prev["severity"] == "critical" and value >= crit - HYSTERESIS:
                return "critical"
            if value >= crit:
                return "critical"
            if value >= warn - HYSTERESIS:
                return "warning"
            return None
        if value >= crit:
            return "critical"
        if value >= warn:
            return "warning"
        return None

    @staticmethod
    def _repeat_due(prev: dict, settings: dict, now: datetime, severity: str) -> bool:
        last = prev.get("lastSentAt")
        if not last:
            return True
        return (now - _parse(last)).total_seconds() >= settings["repeatMinutes"][severity] * 60 - 30

    @staticmethod
    def _ev(a: dict) -> dict:
        return {k: a.get(k) for k in ("key", "clusterId", "metric", "node", "severity", "value", "threshold", "text")}

    def _end_resolved_snoozes(self, st: dict, snoozes: list[dict]) -> None:
        """'Until it clears' snoozes end once nothing they cover is active. They can only be
        created while something they cover is active (see snooze())."""
        ending = [s["id"] for s in snoozes if s.get("untilResolved")
                  and not any(self._matches(s, a) for a in st["alerts"].values())]
        if not ending:
            return

        def m(doc):
            for sid in ending:
                doc.get("items", {}).pop(sid, None)
        try:
            _update_with_retry(self.store, SNOOZES_KEY, m, {"items": {}})
        except Exception:
            log.exception("could not end snoozes")

    # ---- Slack text
    def _console(self, path: str) -> str | None:
        base = (getattr(self.app.state.settings, "public_url", "") or "").rstrip("/")
        return f"{base}/ui{path}" if base else None

    def _cluster_label(self, cid: str) -> str:
        try:
            name = self.app.state.registry.get(cid).name
        except ApiError:
            name = None
        return f"{cid}" + (f" ({name})" if name and name != cid else "")

    @staticmethod
    def describe(a: dict) -> str:
        if a.get("text"):
            return a["text"]
        what = METRIC_LABEL.get(a["metric"], a["metric"])
        where = f" on {a['node']}" if a.get("node") else ""
        line = f"{what}{where}: {_pct(a.get('value'))}"
        if a.get("threshold") is not None:
            line += f" (alert at {_pct(a['threshold'])})"
        if a.get("detail"):
            line += f" · {a['detail']}"
        return line

    def _message(self, cid: str, firing: list[dict], resolved: list[dict], now: datetime) -> tuple[str, list[dict]]:
        firing = sorted(firing, key=lambda a: (a["metric"], a.get("node") or ""))
        label = self._cluster_label(cid)
        crit = [a for a in firing if a["severity"] == "critical"]
        warn = [a for a in firing if a["severity"] == "warning"]
        top = "critical" if crit else "warning" if warn else "ok"
        icon = {"critical": ":red_circle:", "warning": ":large_orange_circle:", "ok": ":large_green_circle:"}[top]
        title = {"critical": "Critical", "warning": "Warning", "ok": "OK again"}[top]
        text = f"{icon} *{title} · {esc(label)}*"
        atts: list[dict] = []
        for group, sev in ((crit, "critical"), (warn, "warning")):
            if not group:
                continue
            lines = [f"• {esc(self.describe(a))} · since {_parse(a['since']).strftime('%d %b %H:%M')} UTC" for a in group]
            atts.append({"color": COLORS[sev], "mrkdwn_in": ["text"],
                         "text": f"*{'Critical' if sev == 'critical' else 'Warning'}*\n" + "\n".join(lines)})
        if resolved:
            lines = [f"• {esc(METRIC_LABEL.get(a['metric'], a['metric']))}"
                     + (f" on {esc(a['node'])}" if a.get("node") else "") + " is back to normal"
                     for a in resolved]
            atts.append({"color": COLORS["ok"], "mrkdwn_in": ["text"], "text": "*OK again*\n" + "\n".join(lines)})
        url = self._console(f"/c/{cid}")
        if url and atts:
            atts[-1]["text"] += f"\n{link(url, 'Open in the console')}"
        return text, atts

    # ---- requests (approvals) → Slack
    def request_event(self, rec: dict) -> None:
        try:
            if not self.settings()["enabled"]:
                return
        except Exception:
            pass
        st = rec["status"]
        link_url = self._console(f"/requests/{rec['id']}")
        where = f"{esc(rec['label'])} `{esc(rec['resource'])}` on *{esc(rec.get('clusterId') or '')}*"
        if st == "PENDING":
            text = f":inbox_tray: *Approval needed* · {esc(rec['requestedBy'])} asked to {where}"
            body = f"Reason: {esc(rec.get('reason') or '')}"
            color = COLORS["info"]
        else:
            head = {"APPLIED": (":white_check_mark:", "Approved and applied"),
                    "REJECTED": (":x:", "Rejected"), "EXPIRED": (":hourglass:", "Expired"),
                    "FAILED": (":warning:", "Approved, but it failed"),
                    "OUTDATED": (":warning:", "Not applied: it changed since it was requested")}.get(st)
            if not head:
                return
            by = f" by {esc(rec.get('decidedBy'))}" if rec.get("decidedBy") and rec.get("decidedBy") != "system" else ""
            text = f"{head[0]} *{head[1]}*{by} · {where} (asked by {esc(rec['requestedBy'])})"
            body = f"Comment: {esc(rec['comment'])}" if rec.get("comment") else ""
            color = COLORS["ok"] if st == "APPLIED" else COLORS["critical"] if st in ("REJECTED", "FAILED") else COLORS["warning"]
        if link_url:
            body = (body + "\n" if body else "") + link(link_url, "Open the request")
        self.slack.post_async(text, [{"color": color, "mrkdwn_in": ["text"], "text": body}] if body else None)

    def _sweep_approvals(self) -> None:
        """Pending requests expire when read; reading them here sends the 'expired' notices."""
        svc = getattr(self.app.state, "approvals", None)
        if not svc:
            return
        try:
            for rid in list(svc._index().keys()):
                try:
                    svc.get(rid)
                except ApiError:
                    pass
        except Exception:
            log.exception("approval sweep failed")

    # ---- weekly digest
    def _daily_sizes(self, st: dict, now: datetime) -> bool:
        today = now.date().isoformat()
        if st.get("sizesDate") == today:
            return False
        for cid in sorted(self.app.state.registry.ids()):
            try:
                snap = self.sizes(cid)
                snap["at"] = iso(now)
                self.store.put_json(f"{SIZES}{cid}/{today}.json", snap)
            except Exception as e:
                log.warning("index sizes of %s not saved: %s", cid, getattr(e, "message", e))
        st["sizesDate"] = today
        return True

    def sizes(self, cid: str) -> dict:
        es = self.app.state.registry.client(cid).options(request_timeout=60, max_retries=0)
        rows = es_call(es, "GET", "/_cat/indices", params={
            "format": "json", "bytes": "b", "h": "index,store.size,docs.count,pri,rep", "expand_wildcards": "open"})
        idx = {}
        for r in rows or []:
            try:
                idx[r["index"]] = {"bytes": int(r.get("store.size") or 0), "docs": int(r.get("docs.count") or 0),
                                   "shards": int(r.get("pri") or 0) * (1 + int(r.get("rep") or 0))}
            except (TypeError, ValueError):
                continue
        return {"clusterId": cid, "indices": idx, "totalBytes": sum(v["bytes"] for v in idx.values()),
                "docs": sum(v["docs"] for v in idx.values())}

    def _old_sizes(self, cid: str, before: date) -> dict | None:
        """The snapshot nearest to (on or before) `before`; else the oldest one there is."""
        keys = self.store.list_keys(f"{SIZES}{cid}/")
        days = sorted(k.rsplit("/", 1)[-1][:-5] for k in keys)
        if not days:
            return None
        older = [d for d in days if d <= before.isoformat()]
        pick = older[-1] if older else days[0]
        if pick >= utcnow().date().isoformat():
            return None
        doc, _ = self.store.get_json(f"{SIZES}{cid}/{pick}.json")
        return doc

    def _digest_due(self, st: dict, now: datetime) -> str | None:
        local = now.astimezone(IST)
        week = f"{local.isocalendar()[0]}-W{local.isocalendar()[1]:02d}"
        if local.weekday() != DIGEST_WEEKDAY or local.hour < DIGEST_HOUR or st.get("lastDigest") == week:
            return None
        return week

    def _maybe_digest(self, st: dict, now: datetime) -> bool:
        week = self._digest_due(st, now)
        if not week:
            return False
        claim = f"{DIGESTS}{week}.json"     # created once per week: two workers can't both send it
        try:
            self.store.put_json(claim, {"week": week, "worker": self.worker_id, "at": iso(now)}, if_none_match=True)
        except PreconditionFailed:
            st["lastDigest"] = week
            return True
        res = self.send_digest(now)
        if res.get("ok") or res.get("skipped"):
            st["lastDigest"] = week
            return True
        try:                                 # not sent: let the next check try again
            self.store.delete(claim)
        except Exception:
            log.exception("could not release the digest claim")
        return False

    def digest(self, now: datetime | None = None) -> dict:
        now = now or utcnow()
        since = now - timedelta(days=7)
        clusters = []
        hist = [e for e in self.history(8) if _parse(e["at"]) >= since]
        for cid in sorted(self.app.state.registry.ids()):
            item: dict = {"clusterId": cid, "label": self._cluster_label(cid)}
            try:
                cur = self.sizes(cid)
                data = self.collect(cid)
            except ApiError as e:
                item["error"] = e.message
                clusters.append(item)
                continue
            old = self._old_sizes(cid, (now - timedelta(days=7)).date())
            item.update(totalBytes=cur["totalBytes"], docs=cur["docs"], indices=len(cur["indices"]),
                        shards=data["shards"], shardLimit=data["shardLimit"],
                        health=data["health"].get("status"),
                        diskUsedPercent=data["nodes"]["summary"].get("diskUsedPercent"))
            if old:
                item.update(baselineDate=old.get("at", "")[:10], growthBytes=cur["totalBytes"] - old["totalBytes"],
                            growthDocs=cur["docs"] - old.get("docs", 0))
                grow = []
                for name, v in cur["indices"].items():
                    before = (old["indices"].get(name) or {}).get("bytes", 0)
                    if v["bytes"] - before:
                        grow.append({"index": name, "growthBytes": v["bytes"] - before, "bytes": v["bytes"],
                                     "new": name not in old["indices"]})
                item["topGrowth"] = sorted(grow, key=lambda g: g["growthBytes"], reverse=True)[:10]
            top = sorted(cur["indices"].items(), key=lambda kv: kv[1]["bytes"], reverse=True)[:10]
            item["topSize"] = [{"index": n, "bytes": v["bytes"], "docs": v["docs"]} for n, v in top]
            fired = [e for e in hist if e["clusterId"] == cid and e["event"] == "FIRING"]
            item["alertsFired"] = len(fired)
            item["alertsByMetric"] = {m: sum(1 for e in fired if e["metric"] == m) for m in METRICS
                                      if any(e["metric"] == m for e in fired)}
            clusters.append(item)
        return {"generatedAt": iso(now), "since": iso(since), "clusters": clusters,
                "requests": self._request_counts(since)}

    def _request_counts(self, since: datetime) -> dict:
        """Submitted this week (by request time); approved / rejected / expired this week (by when
        that happened, so a request from last week decided this week counts); pending now."""
        svc = getattr(self.app.state, "approvals", None)
        counts = {"submitted": 0, "applied": 0, "rejected": 0, "expired": 0, "failed": 0, "pending": 0}
        if not svc:
            return counts
        expiry = int(getattr(self.app.state.settings, "approval_expiry_days", 7))
        cutoff = (since - timedelta(days=expiry + 1)).strftime("%Y%m%dT%H%M%S")
        for key in self.store.list_keys("approvals/"):
            rid = key[len("approvals/"):-5]
            if rid < cutoff:
                continue
            try:
                rec, _ = svc.get(rid)
            except ApiError:
                continue
            if _parse(rec["requestedAt"]) >= since:
                counts["submitted"] += 1
            if rec["status"] in ("PENDING", "APPLYING"):
                counts["pending"] += 1
                continue
            at = rec.get("finishedAt") or rec.get("decidedAt")
            k = {"APPLIED": "applied", "REJECTED": "rejected", "EXPIRED": "expired",
                 "FAILED": "failed", "OUTDATED": "failed"}.get(rec["status"])
            if k and at and _parse(at) >= since:
                counts[k] += 1
        return counts

    def digest_message(self, d: dict) -> tuple[str, list[dict]]:
        hb = human_bytes
        start = _parse(d["since"]).astimezone(IST).strftime("%d %b")
        end = _parse(d["generatedAt"]).astimezone(IST).strftime("%d %b %Y")
        r = d["requests"]
        text = (f":bar_chart: *Weekly Elasticsearch digest* · {start} – {end}\n"
                f"Requests: {r['submitted']} submitted · {r['applied']} approved · {r['rejected']} rejected · "
                f"{r['expired']} expired"
                + (f" · {r['failed']} not applied" if r.get("failed") else "") + f" · {r['pending']} pending now")
        atts = []
        for c in d["clusters"]:
            if c.get("error"):
                atts.append({"color": COLORS["critical"], "mrkdwn_in": ["text"],
                             "text": f"*{esc(c['label'])}*\nCould not read the cluster: {esc(c['error'])}"})
                continue
            lines = [f"*{esc(c['label'])}* · health {esc(c.get('health') or '?')}"]
            size = f"Size {hb(c['totalBytes'])} · {c['docs']:,} docs · {c['indices']:,} indices"
            if "growthBytes" in c:
                sign = "+" if c["growthBytes"] >= 0 else "−"
                size += f" · {sign}{hb(abs(c['growthBytes']))} since {c['baselineDate']}"
            else:
                size += " · growth from next week (first size snapshot)"
            lines.append(size)
            if c.get("shardLimit"):
                lines.append(f"Shards {c['shards']:,} of {c['shardLimit']:,} ({round(100 * c['shards'] / c['shardLimit'])}%)"
                             f" · disk {_pct(c.get('diskUsedPercent'))} used")
            fired = c.get("alertsFired", 0)
            lines.append("Alerts this week: " + (", ".join(f"{METRIC_LABEL[m]} {n}" for m, n in c["alertsByMetric"].items())
                                                 if fired else "none"))
            if c.get("topSize"):
                lines.append("_Largest:_ " + ", ".join(f"`{esc(t['index'])}` {hb(t['bytes'])}" for t in c["topSize"]))
            if c.get("topGrowth"):
                lines.append("_Grew most:_ " + ", ".join(
                    f"`{esc(t['index'])}` +{hb(t['growthBytes'])}{' (new)' if t['new'] else ''}"
                    for t in c["topGrowth"] if t["growthBytes"] > 0))
            pct = 100 * c["shards"] / c["shardLimit"] if c.get("shardLimit") else 0
            color = COLORS["critical"] if c.get("health") == "red" or pct >= 80 else \
                COLORS["warning"] if c.get("health") == "yellow" or pct >= 70 else COLORS["ok"]
            atts.append({"color": color, "mrkdwn_in": ["text"], "text": "\n".join(lines)})
        url = self._console("/admin/alerts")
        if url and atts:
            atts[-1]["text"] += f"\n{link(url, 'Alerts in the console')}"
        return text, atts

    def send_digest(self, now: datetime | None = None) -> dict:
        d = self.digest(now)
        text, atts = self.digest_message(d)
        res = self.slack.post(text, atts)
        self._history_add([{"at": iso(now or utcnow()), "event": "DIGEST", "key": "", "clusterId": "",
                            "metric": "", "node": "", "severity": "info", "ok": bool(res.get("ok")),
                            "text": "Weekly digest" + ("" if res.get("ok") else f" not sent: {res.get('error')}")}])
        return {**res, "digest": d}

    # ---- background loop
    def tick(self) -> None:
        try:
            if self.due():
                self.run_once(only_if_due=True)
        except ApiError as e:
            if e.code != "CHECK_IN_PROGRESS":
                log.warning("alert check: %s", e.message)
        except Exception:
            log.exception("alert check failed")

    def start(self) -> None:
        if self._thread:
            return

        def loop():
            self._stop.wait(20 + (os.getpid() % 10))   # let the app finish starting
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(30)
        self._thread = threading.Thread(target=loop, name="alerts", daemon=True)
        self._thread.start()
        log.info("alert checks every %ss (worker %s)", self.interval, self.worker_id)

    def stop(self) -> None:
        self._stop.set()
