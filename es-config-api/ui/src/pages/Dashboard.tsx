import { useQueries, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { enc, get, type Cluster, type ClusterSummary, type MetricRule, type ResourceStat, type TrendMetric, type Trends } from "../api";
import { Icon } from "../components/icons";
import { Page } from "../components/Shell";
import { Badge, ErrorCallout, HealthDot, Spinner, errorMessage } from "../components/ui";
import { bytes, num, when } from "../format";

const REFRESH_MS = 120_000;
const METRIC_LABEL: Record<TrendMetric, string> = { disk: "Disk", ram: "RAM (excl. cache)", heap: "JVM heap", cpu: "CPU" };
const MAX_SERIES = 8;

type SortKey = "attention" | "cluster" | "health" | "alerts" | "nodes" | "disk" | "ram" | "heap" | "cpu" | "shards" | "size" | "growth";

/** Higher = needs attention sooner. Not answering and red first, then critical alerts, yellow, warnings. */
function attention(s: ClusterSummary | undefined, failed: boolean): number {
  if (failed) return 90;
  if (!s) return -1;
  if (s.reachable === false) return 100;
  return (s.status === "red" ? 80 : 0) + s.alerts.critical * 10 + (s.status === "yellow" ? 30 : 0) + s.alerts.warning * 3;
}

function sortValue(k: SortKey, c: Cluster, s: ClusterSummary | undefined, failed: boolean): number | string {
  switch (k) {
    case "attention": return attention(s, failed);
    case "cluster": return c.id;
    case "health": return s?.reachable === false ? 3 : ({ red: 2, yellow: 1, green: 0 } as Record<string, number>)[s?.status ?? ""] ?? -1;
    case "alerts": return s ? s.alerts.critical * 1000 + s.alerts.warning : -1;
    case "nodes": return s?.nodes ?? -1;
    case "disk": case "ram": case "heap": case "cpu": return s?.[k]?.max ?? -1;
    case "shards": return s?.shards?.percent ?? -1;
    case "size": return s?.size?.storeBytes ?? -1;
    case "growth": return s?.growth?.bytes ?? -Infinity;
  }
}

function tone(v: number | null | undefined, t?: MetricRule): "" | "warn" | "danger" {
  if (v == null || !t) return "";
  if (t.crit != null && v >= t.crit) return "danger";
  if (t.warn != null && v >= t.warn) return "warn";
  return "";
}

function MiniMeter({ stat, rule, label, note }: { stat?: ResourceStat; rule?: MetricRule; label: string; note?: string }) {
  if (!stat || stat.max == null) return <span className="hint">—</span>;
  const t = tone(stat.max, rule);
  const title = stat.maxNode
    ? `${label}: worst node ${stat.maxNode} ${stat.max}% · average ${stat.avg}%${rule?.warn ? ` · warning ${rule.warn}%, critical ${rule.crit}%` : ""}`
    : `${label}: ${stat.max}%${note ? ` (${note})` : ""}${rule?.warn ? ` · warning ${rule.warn}%, critical ${rule.crit}%` : ""}`;
  return (
    <div className="mini-meter" title={title}>
      <span className="mono" style={{ fontWeight: 600, fontSize: 12.5 }}>{stat.max < 1 && stat.max > 0 ? "<1" : Math.round(stat.max)}%</span>
      <div className="track" role="meter" aria-label={`${label}, worst node`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(stat.max)}>
        <div className={`fill ${t}`} style={{ width: `${Math.min(100, Math.max(0, stat.max))}%` }} />
      </div>
      <span className="hint nowrap" style={{ fontSize: 11.5 }}>{note ?? (stat.avg != null ? `avg ${Math.round(stat.avg)}%` : "")}</span>
    </div>
  );
}

function Sparkline({ t, metric, rule }: { t?: Trends; metric: TrendMetric; rule?: MetricRule }) {
  const pts = (t?.points ?? []).filter((p) => p[metric] != null);
  if (!t) return <span className="hint">…</span>;
  if (pts.length < 2) return <span className="hint" title="Samples are saved by the 5-minute check">no data yet</span>;
  const W = 120, H = 28, t0 = new Date(t.since).getTime(), t1 = new Date(t.until).getTime();
  const x = (s: string) => ((new Date(s).getTime() - t0) / (t1 - t0)) * W;
  const y = (v: number) => H - (Math.min(100, Math.max(0, v)) / 100) * H;
  const d = pts.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p[metric]!).toFixed(1)}`).join("");
  const last = pts[pts.length - 1];
  const peak = Math.max(...pts.map((p) => p[metric]!));
  const lt = tone(last[metric], rule);
  return (
    <svg className="spark" width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img"
      aria-label={`${METRIC_LABEL[metric]} over 24 hours: now ${last[metric]}%, peak ${peak}%`}>
      <title>{`${METRIC_LABEL[metric]}, worst node, last 24 h: now ${last[metric]}% · peak ${peak}%`}</title>
      {rule?.crit != null && <line x1={0} x2={W} y1={y(rule.crit)} y2={y(rule.crit)} stroke="var(--red)" strokeOpacity={0.35} strokeDasharray="2 3" />}
      <path d={d} fill="none" stroke="var(--accent)" strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={x(last.t)} cy={y(last[metric]!)} r={2.5} fill={lt === "danger" ? "var(--red)" : lt === "warn" ? "var(--yellow)" : "var(--accent)"} />
    </svg>
  );
}

function Th({ k, sort, setSort, children, title }: { k: SortKey; sort: { key: SortKey; desc: boolean }; setSort: (s: { key: SortKey; desc: boolean }) => void; children: ReactNode; title?: string }) {
  const on = sort.key === k;
  return (
    <th aria-sort={on ? (sort.desc ? "descending" : "ascending") : "none"} title={title}>
      <button type="button" className="th-sort" onClick={() => setSort({ key: k, desc: on ? !sort.desc : k !== "cluster" })}>
        {children}{on && <span aria-hidden="true">{sort.desc ? " ↓" : " ↑"}</span>}
      </button>
    </th>
  );
}

function useWidth<T extends HTMLElement>(): [React.RefObject<T>, number] {
  const ref = useRef<T>(null);
  const [w, setW] = useState(800);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(280, Math.floor(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

interface Series { id: string; color: string; points: { t: number; v: number }[] }

function TrendChart({ series, metric, hours, rule }: { series: Series[]; metric: TrendMetric; hours: number; rule?: MetricRule }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const H = 260, L = 40, R = series.length <= 4 ? 96 : 16, T = 12, B = 26;
  const now = Date.now(), t0 = now - hours * 3600_000;
  const pw = width - L - R, ph = H - T - B;
  const x = (t: number) => L + ((t - t0) / (now - t0)) * pw;
  const y = (v: number) => T + ph - (Math.min(100, Math.max(0, v)) / 100) * ph;
  const ticks = useMemo(() => {
    const out: number[] = [];
    const step = hours <= 24 ? 4 * 3600_000 : 24 * 3600_000;
    let t = Math.ceil(t0 / step) * step;
    for (; t <= now; t += step) out.push(t);
    return out;
  }, [t0, now, hours]);
  const fmt = (t: number) => {
    const d = new Date(t);
    return hours <= 24 ? d.toISOString().slice(11, 16) : d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
  };
  const nearest = (s: Series, t: number) => {
    let best: { t: number; v: number } | null = null;
    for (const p of s.points) if (!best || Math.abs(p.t - t) < Math.abs(best.t - t)) best = p;
    const gap = hours <= 24 ? 8 * 60_000 : 40 * 60_000;
    return best && Math.abs(best.t - t) <= gap ? best : null;
  };
  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - box.left;
    if (px < L || px > L + pw) return setHover(null);
    setHover(t0 + ((px - L) / pw) * (now - t0));
  };
  const rows = hover != null ? series.map((s) => ({ s, p: nearest(s, hover) })).filter((r) => r.p).sort((a, b) => b.p!.v - a.p!.v) : [];
  const empty = series.every((s) => s.points.length < 2);
  return (
    <div className="trend-wrap" ref={ref}>
      {empty ? (
        <div className="hint" style={{ padding: "32px 0", textAlign: "center" }}>No samples yet. The 5-minute check saves one per cluster; the chart fills from the first check after the update.</div>
      ) : (
        <svg className="trend-svg" width={width} height={H} role="img" onPointerMove={onMove} onPointerLeave={() => setHover(null)}
          aria-label={`${METRIC_LABEL[metric]} of the worst node per cluster, last ${hours <= 24 ? "24 hours" : "7 days"}`}>
          {[0, 25, 50, 75, 100].map((v) => (
            <g key={v}>
              <line className="grid" x1={L} x2={L + pw} y1={y(v)} y2={y(v)} />
              <text className="axis-text" x={L - 8} y={y(v) + 4} textAnchor="end">{v}%</text>
            </g>
          ))}
          {ticks.map((t) => <text key={t} className="axis-text" x={x(t)} y={H - 6} textAnchor="middle">{fmt(t)}</text>)}
          {rule?.warn != null && <line className="threshold" x1={L} x2={L + pw} y1={y(rule.warn)} y2={y(rule.warn)} stroke="var(--yellow)" />}
          {rule?.crit != null && <line className="threshold" x1={L} x2={L + pw} y1={y(rule.crit)} y2={y(rule.crit)} stroke="var(--red)" />}
          {series.map((s) => {
            if (s.points.length < 2) return null;
            const parts: string[] = [];
            const gap = hours <= 24 ? 12 * 60_000 : 70 * 60_000;
            s.points.forEach((p, i) => parts.push(`${i && p.t - s.points[i - 1].t <= gap ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`));
            return <path key={s.id} className="line" d={parts.join("")} stroke={s.color} />;
          })}
          {series.length <= 4 && series.map((s) => {
            const last = s.points[s.points.length - 1];
            return last ? <text key={s.id} className="axis-text" x={x(last.t) + 6} y={y(last.v) + 4} style={{ fill: "var(--text-2)" }}>{s.id}</text> : null;
          })}
          {hover != null && <line className="cross" x1={x(hover)} x2={x(hover)} y1={T} y2={T + ph} />}
          {rows.map(({ s, p }) => <circle key={s.id} cx={x(p!.t)} cy={y(p!.v)} r={4} fill={s.color} stroke="var(--surface)" strokeWidth={2} />)}
        </svg>
      )}
      {hover != null && rows.length > 0 && (
        <div className="trend-tip" style={{ left: Math.min(x(hover) + 12, width - 200), top: 8 }}>
          <div className="hint" style={{ marginBottom: 4 }}>{when(new Date(hover).toISOString())}</div>
          {rows.map(({ s, p }) => (
            <div key={s.id} className="row-k"><span className="key" style={{ background: s.color }} /><span className="v mono">{p!.v}%</span><span className="n">{s.id}</span></div>
          ))}
        </div>
      )}
    </div>
  );
}

function SummaryCells({ s, failed, err }: { s?: ClusterSummary; failed: boolean; err?: unknown }) {
  if (failed) return <td colSpan={6}><span style={{ color: "var(--danger)" }}>{errorMessage(err)}</span></td>;
  if (!s) return <td colSpan={6}><Spinner label="Reading the cluster" /> <span className="hint">Reading the cluster…</span></td>;
  if (s.reachable === false) {
    return <td colSpan={6}><span className="hint" title={s.error}>{s.errorCode === "ES_AUTH_FAILED" ? "The cluster refused the API's login" : "No readings: the cluster isn't answering"} · {s.error?.slice(0, 120)}</span></td>;
  }
  const g = s.growth;
  return (
    <>
      <td><MiniMeter stat={s.disk} rule={s.thresholds.disk} label="Disk" /></td>
      <td><MiniMeter stat={s.ram} rule={s.thresholds.ram} label="RAM excl. cache" /></td>
      <td><MiniMeter stat={s.heap} rule={s.thresholds.heap} label="JVM heap" /></td>
      <td><MiniMeter stat={s.cpu} rule={s.thresholds.cpu} label="CPU" /></td>
      <td>
        {s.shards?.limit ? (
          <MiniMeter stat={{ avg: null, max: s.shards.percent, maxNode: null }} rule={s.thresholds.shards} label="Shards vs limit"
            note={`${num(s.shards.total)} / ${num(s.shards.limit)}`} />
        ) : <span className="hint">—</span>}
      </td>
      <td className="nowrap" title={`${num(s.size?.docs)} documents · ${num(s.size?.indices)} indices${g ? ` · ${g.from} → ${g.to}: ${g.bytes >= 0 ? "+" : "−"}${bytes(Math.abs(g.bytes))}, ${g.docs >= 0 ? "+" : "−"}${num(Math.abs(g.docs))} docs (open indices)` : ""}`}>
        <span style={{ fontWeight: 600 }}>{bytes(s.size?.storeBytes)}</span>
        <div className="hint">{num(s.size?.indices)} indices</div>
        <div className="hint">{g ? <>{g.bytes >= 0 ? "+" : "−"}{bytes(Math.abs(g.bytes))} in {g.days} d</> : "growth from tomorrow"}</div>
      </td>
    </>
  );
}

export function Dashboard({ clusters }: { clusters: Cluster[] }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: "attention", desc: true });
  const [metric, setMetric] = useState<TrendMetric>("disk");
  const [hours, setHours] = useState(24);
  const summaries = useQueries({
    queries: clusters.map((c) => ({
      queryKey: ["summary", c.id],
      queryFn: () => get<ClusterSummary>(`/clusters/${enc(c.id)}/summary`),
      refetchInterval: REFRESH_MS,
      staleTime: REFRESH_MS / 2,
      retry: false,
    })),
  });
  const trends24 = useQueries({
    queries: clusters.map((c) => ({
      queryKey: ["trends", c.id, 24],
      queryFn: () => get<Trends>(`/clusters/${enc(c.id)}/trends`, { hours: 24 }),
      refetchInterval: REFRESH_MS,
      staleTime: REFRESH_MS / 2,
    })),
  });
  const trendsWeek = useQueries({
    queries: clusters.map((c) => ({
      queryKey: ["trends", c.id, 168],
      queryFn: () => get<Trends>(`/clusters/${enc(c.id)}/trends`, { hours: 168 }),
      refetchInterval: REFRESH_MS,
      staleTime: REFRESH_MS / 2,
      enabled: hours === 168,
    })),
  });
  const updatedAt = Math.max(0, ...summaries.map((q) => q.dataUpdatedAt || 0));
  const fetching = summaries.some((q) => q.isFetching);
  const refresh = () => ["summary", "trends"].forEach((k) => qc.invalidateQueries({ queryKey: [k] }));

  const rows = clusters.map((c, i) => ({ c, s: summaries[i].data, failed: summaries[i].isError && !summaries[i].data, err: summaries[i].error, t: trends24[i].data }));
  const sorted = [...rows].sort((a, b) => {
    const va = sortValue(sort.key, a.c, a.s, a.failed), vb = sortValue(sort.key, b.c, b.s, b.failed);
    const cmp = typeof va === "string" ? va.localeCompare(vb as string) : (va as number) - (vb as number);
    return (sort.desc ? -cmp : cmp) || a.c.id.localeCompare(b.c.id);
  });
  const counts = {
    down: rows.filter((r) => r.s?.reachable === false || r.failed).length,
    red: rows.filter((r) => r.s?.status === "red").length,
    yellow: rows.filter((r) => r.s?.status === "yellow").length,
    crit: rows.reduce((n, r) => n + (r.s?.alerts.critical ?? 0), 0),
    warn: rows.reduce((n, r) => n + (r.s?.alerts.warning ?? 0), 0),
  };

  // trend chart: the clusters with the highest latest value first, at most 8, colour fixed per cluster (by id order)
  const colorOf = useMemo(() => {
    const ids = [...clusters].map((c) => c.id).sort();
    return (id: string) => `var(--series-${(ids.indexOf(id) % MAX_SERIES) + 1})`;
  }, [clusters]);
  const source = hours === 24 ? trends24 : trendsWeek;
  const allSeries: Series[] = clusters.map((c, i) => ({
    id: c.id, color: colorOf(c.id),
    points: (source[i].data?.points ?? []).filter((p) => p[metric] != null).map((p) => ({ t: new Date(p.t).getTime(), v: p[metric]! })),
  }));
  const shown = [...allSeries].sort((a, b) => (b.points.at(-1)?.v ?? -1) - (a.points.at(-1)?.v ?? -1)).slice(0, MAX_SERIES)
    .sort((a, b) => a.id.localeCompare(b.id));
  const rules = rows.map((r) => r.s?.thresholds?.[metric]).filter(Boolean) as MetricRule[];
  const sameRule = rules.length > 0 && rules.every((r) => r.warn === rules[0].warn && r.crit === rules[0].crit) ? rules[0] : undefined;
  const chartLoading = source.some((q) => q.isLoading && q.fetchStatus !== "idle");

  return (
    <Page crumbs={[{ label: "Dashboard" }]} title="Dashboard">
      <div className="page-head" style={{ alignItems: "center" }}>
        <div className="grow stack-sm" style={{ gap: 4 }}>
          <h1>Dashboard</h1>
          <span className="sub">
            {clusters.length} cluster{clusters.length === 1 ? "" : "s"}
            {counts.down ? ` · ${counts.down} not answering` : ""}{counts.red ? ` · ${counts.red} red` : ""}{counts.yellow ? ` · ${counts.yellow} yellow` : ""}
            {` · ${counts.crit} critical and ${counts.warn} warning alert${counts.warn === 1 ? "" : "s"}`}
          </span>
          <span className="hint" role="status">
            <Icon name="clock" size={12} /> This page refreshes every 2 minutes{updatedAt ? ` · last refreshed ${when(new Date(updatedAt).toISOString(), false)} UTC` : ""}{fetching ? " · refreshing…" : ""}
          </span>
        </div>
        <button type="button" className="btn" onClick={refresh} disabled={fetching}><Icon name="refresh" /> {fetching ? "Refreshing…" : "Refresh now"}</button>
      </div>

      <div className="dash-controls">
        <span className="label" id="trend-metric">Trend</span>
        <div className="seg" role="group" aria-labelledby="trend-metric">
          {(Object.keys(METRIC_LABEL) as TrendMetric[]).map((m) => (
            <button key={m} type="button" aria-pressed={metric === m} onClick={() => setMetric(m)}>{m === "ram" ? "RAM" : METRIC_LABEL[m]}</button>
          ))}
        </div>
        <span className="hint">Bars show the worst node, with the average beside it; colours follow each cluster's alert thresholds.</span>
      </div>

      <section className="card" style={{ overflow: "hidden" }}>
        <div className="table-scroll">
          <table className="table compact dash-table">
            <thead>
              <tr>
                <Th k="cluster" sort={sort} setSort={setSort}>Cluster</Th>
                <Th k="attention" sort={sort} setSort={setSort} title="Health, nodes and active alerts; sorts the clusters that need attention first">Status</Th>
                <Th k="disk" sort={sort} setSort={setSort} title="Disk used, worst data node">Disk</Th>
                <Th k="ram" sort={sort} setSort={setSort} title="RAM used excluding the OS file cache (estimate), worst node">RAM</Th>
                <Th k="heap" sort={sort} setSort={setSort}>Heap</Th>
                <Th k="cpu" sort={sort} setSort={setSort}>CPU</Th>
                <Th k="shards" sort={sort} setSort={setSort} title="Shards against the cluster's limit (max_shards_per_node × data nodes)">Shards</Th>
                <Th k="size" sort={sort} setSort={setSort} title="Store size of all indices (replicas included), and the change over about 7 days">Size</Th>
                <th title={`${METRIC_LABEL[metric]}, worst node, last 24 hours`}>{metric === "ram" ? "RAM" : METRIC_LABEL[metric]} · 24 h</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map(({ c, s, failed, err, t }) => {
                const down = s?.reachable === false;
                return (
                  <tr key={c.id} onClick={(e) => { if (!(e.target as HTMLElement).closest("a")) navigate(`/c/${enc(c.id)}`); }}>
                    <td>
                      <Link to={`/c/${enc(c.id)}`} className="mono nowrap" style={{ fontWeight: 600 }}>{c.id}</Link>
                      <div className="hint" style={{ maxWidth: 200 }}>{c.name && c.name !== c.id ? `${c.name} · ` : ""}{s?.versions?.length ? `v${s.versions.join(", ")}` : ""}{s?.legacy ? " · 7.x mode" : ""}</div>
                    </td>
                    <td>
                      {failed ? <span className="hint">—</span> : !s ? <span className="hint">…</span> : down ? (
                        <span className="nowrap" style={{ color: "var(--danger)", fontWeight: 600 }} title={s.error}><Icon name="alert" size={14} /> Not answering</span>
                      ) : (
                        <span className="row" style={{ gap: 6, flexWrap: "nowrap", textTransform: "capitalize" }}><HealthDot status={s.status} />{s.status}
                          <span className="hint nowrap" style={{ textTransform: "none" }}>· {s.nodes} node{s.nodes === 1 ? "" : "s"}</span></span>
                      )}
                      {s && !down && (s.unassignedShards ?? 0) > 0 && <div className="hint">{num(s.unassignedShards)} unassigned shards</div>}
                      {s && (s.alerts.critical > 0 || s.alerts.warning > 0) && (
                        <div className="row" style={{ gap: 4, marginTop: 4 }}>
                          {s.alerts.critical > 0 && <Badge tone="red">{s.alerts.critical} critical</Badge>}
                          {s.alerts.warning > 0 && <Badge tone="amber">{s.alerts.warning} warning</Badge>}
                        </div>
                      )}
                      {s && s.alerts.snoozed > 0 && <div className="hint">{s.alerts.snoozed} snoozed</div>}
                    </td>
                    <SummaryCells s={s} failed={failed} err={err} />
                    <td><Sparkline t={t} metric={metric} rule={s?.thresholds?.[metric]} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card">
        <div className="card-head">
          <div className="grow">
            <h2>{METRIC_LABEL[metric]} · worst node per cluster</h2>
            <span className="hint">
              One sample per cluster every 5 minutes{hours > 24 ? " (7 days: the peak of each half hour)" : ""}; times in UTC.
              {sameRule ? ` Dashed lines: warning ${sameRule.warn}%, critical ${sameRule.crit}%.` : ""}
              {allSeries.length > MAX_SERIES ? ` Showing the ${MAX_SERIES} highest of ${allSeries.length} clusters.` : ""}
            </span>
          </div>
          <div className="seg" role="group" aria-label="Period">
            <button type="button" aria-pressed={hours === 24} onClick={() => setHours(24)}>24 hours</button>
            <button type="button" aria-pressed={hours === 168} onClick={() => setHours(168)}>7 days</button>
          </div>
        </div>
        <div className="card-body stack-sm">
          {shown.length > 1 && (
            <div className="legend" aria-label="Clusters">
              {shown.map((s) => <span key={s.id}><span className="key" style={{ background: s.color }} />{s.id}</span>)}
            </div>
          )}
          {chartLoading ? <div className="hint" style={{ padding: "32px 0", textAlign: "center" }}><Spinner label="Loading" /> Loading the samples…</div>
            : <TrendChart series={shown} metric={metric} hours={hours} rule={sameRule} />}
        </div>
      </section>
      {summaries.every((q) => q.isError) && clusters.length > 0 && <ErrorCallout error={summaries[0].error} />}
    </Page>
  );
}
