"""Per-node resource usage for the Overview: CPU, RAM, JVM heap, disk, shards."""
from __future__ import annotations

from typing import Any

from .clusters import es_call
from .errors import ApiError


def _pct(v: Any) -> float | None:
    """'85%' -> 85.0; absolute values ('500mb') -> None."""
    if isinstance(v, str) and v.strip().endswith("%"):
        try:
            return float(v.strip()[:-1])
        except ValueError:
            return None
    return None


def _watermarks(es) -> dict:
    try:
        s = es_call(es, "GET", "/_cluster/settings",
                    params={"include_defaults": "true", "filter_path": "**.disk.watermark*"})
    except ApiError:
        return {}
    merged: dict = {}
    for layer in ("defaults", "persistent", "transient"):  # later layers win
        wm = (((((s.get(layer) or {}).get("cluster") or {}).get("routing") or {})
               .get("allocation") or {}).get("disk") or {}).get("watermark") or {}
        merged.update({k: v for k, v in wm.items() if isinstance(v, str)})
    return {"low": _pct(merged.get("low")), "high": _pct(merged.get("high")),
            "flood": _pct(merged.get("flood_stage"))}


def es_memory_bytes(jvm: dict) -> int | None:
    """RAM Elasticsearch holds: heap + non-heap committed + direct buffers (not the file cache)."""
    m = jvm.get("mem") or {}
    heap, non_heap = m.get("heap_committed_in_bytes"), m.get("non_heap_committed_in_bytes")
    if heap is None:
        return None
    direct = ((jvm.get("buffer_pools") or {}).get("direct") or {}).get("used_in_bytes") or 0
    return int(heap + (non_heap or 0) + direct)


def node_stats(es, cluster_id: str) -> dict:
    stats = es_call(es, "GET", "/_nodes/stats/os,fs,jvm")
    info = es_call(es, "GET", "/_nodes/_all/os", params={
        "filter_path": "nodes.*.name,nodes.*.ip,nodes.*.host,nodes.*.roles,nodes.*.version,"
                       "nodes.*.os.available_processors"})
    try:
        master = (es_call(es, "GET", "/_cat/master", params={"format": "json"}) or [{}])[0].get("id")
    except ApiError:
        master = None
    try:
        alloc = {r.get("node"): r for r in es_call(es, "GET", "/_cat/allocation",
                                                   params={"format": "json", "bytes": "b"})}
    except ApiError:
        alloc = {}
    wm = _watermarks(es)

    nodes = []
    for nid, n in (stats.get("nodes") or {}).items():
        i = (info.get("nodes") or {}).get(nid, {})
        os_ = n.get("os") or {}
        cpu = os_.get("cpu") or {}
        mem = os_.get("mem") or {}
        heap = (n.get("jvm") or {}).get("mem") or {}
        jvm = n.get("jvm") or {}
        mem_total = mem.get("adjusted_total_in_bytes") or mem.get("total_in_bytes")
        # used_in_bytes is measured against total_in_bytes; keep the % against the same total
        raw_total = mem.get("total_in_bytes")
        mem_used_pct = (round(100 * mem["used_in_bytes"] / raw_total, 1)
                        if raw_total and mem.get("used_in_bytes") is not None else mem.get("used_percent"))
        ram_used = es_memory_bytes(jvm)
        ram_pct = round(100 * ram_used / mem_total, 1) if mem_total and ram_used is not None else None
        fs = (n.get("fs") or {}).get("total") or {}
        total, avail = fs.get("total_in_bytes"), fs.get("available_in_bytes")
        disk_used = round(100 * (1 - avail / total), 1) if total and avail is not None else None
        a = alloc.get(n.get("name"), {})
        roles = i.get("roles") or n.get("roles") or []
        nodes.append({
            "id": nid, "name": n.get("name"), "ip": i.get("ip") or n.get("ip"),
            "host": i.get("host") or n.get("host"), "version": i.get("version"),
            "roles": roles, "master": nid == master,
            "cpuPercent": cpu.get("percent"),
            "load1m": (cpu.get("load_average") or {}).get("1m"),
            "cpus": (i.get("os") or {}).get("available_processors"),
            "memTotalBytes": mem_total,
            # Elasticsearch's own figure: counts the OS file cache as used (often 90-99%).
            "memUsedBytes": mem.get("used_in_bytes"), "memUsedPercent": mem_used_pct,
            # RAM without cache: what Elasticsearch itself holds. Elasticsearch doesn't report
            # the OS "available" figure (what `free`/CloudWatch show), so this is an estimate
            # that leaves out other processes on the host.
            "ramUsedBytes": ram_used, "ramUsedPercent": ram_pct,
            "ramFreeBytes": (mem_total - ram_used) if mem_total and ram_used is not None else None,
            "heapUsedBytes": heap.get("heap_used_in_bytes"), "heapMaxBytes": heap.get("heap_max_in_bytes"),
            "heapUsedPercent": heap.get("heap_used_percent"),
            "diskTotalBytes": total, "diskAvailableBytes": avail, "diskUsedPercent": disk_used,
            "shards": int(a["shards"]) if str(a.get("shards", "")).isdigit() else None,
            "uptimeMillis": (n.get("jvm") or {}).get("uptime_in_millis"),
            "data": any(r.startswith("data") for r in roles),
        })
    nodes.sort(key=lambda x: (not x["master"], x["name"] or ""))
    data = [x for x in nodes if x["data"] and x["diskTotalBytes"]]
    tot = sum(x["diskTotalBytes"] for x in data)
    free = sum(x["diskAvailableBytes"] or 0 for x in data)
    # Averages over data nodes (dedicated master / coordinating nodes would skew them)
    base = [x for x in nodes if x["data"]] or nodes
    cpus = [x["cpuPercent"] for x in base if x["cpuPercent"] is not None]
    mems = [x["memUsedPercent"] for x in base if x["memUsedPercent"] is not None]
    rams = [x["ramUsedPercent"] for x in base if x["ramUsedPercent"] is not None]
    heaps = [x["heapUsedPercent"] for x in base if x["heapUsedPercent"] is not None]
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None  # noqa: E731
    return {
        "clusterId": cluster_id, "nodes": nodes,
        "summary": {"nodes": len(nodes), "dataNodes": len([x for x in nodes if x["data"]]),
                    "diskTotalBytes": tot, "diskAvailableBytes": free,
                    "diskUsedPercent": round(100 * (1 - free / tot), 1) if tot else None,
                    "cpuPercentAvg": avg(cpus), "cpuPercentMax": max(cpus) if cpus else None,
                    "memUsedPercentAvg": avg(mems), "ramUsedPercentAvg": avg(rams),
                    "ramUsedPercentMax": max(rams) if rams else None, "heapUsedPercentAvg": avg(heaps),
                    "heapUsedPercentMax": max(heaps) if heaps else None},
        "watermarks": wm,
    }
