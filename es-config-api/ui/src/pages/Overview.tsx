import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { enc, get, NAMED_TYPES, type AuditEvent, type IndexRow, type NodeRow, type NodeStats, type Rules } from "../api";
import { Icon } from "../components/icons";
import { Page } from "../components/Shell";
import { Badge, ErrorCallout, HealthDot, Loading } from "../components/ui";
import { bytes, CONFIG_TYPE_LABEL, LEVEL_LABEL, NAMED_META, nf, num, todayUtc, when } from "../format";
import { useAdminClusters, useCluster, useClusters, useHealth } from "../session";
import { AuditOutcome, actionLabel } from "./admin/Audit";

function allowSummary(type: string, rules: Rules): string {
  const r = rules[type];
  if (!r) return "Locked";
  const extra = r.indices?.length ? ` · ${r.indices.join(", ")}` : r.indexPatterns?.length ? ` · ${r.indexPatterns.join(", ")}` : "";
  if (type === "cluster-settings" || type === "index-settings") return `${r.allow.length} key${r.allow.length === 1 ? "" : "s"}${extra}`;
  return r.allow.length ? `${r.allow.slice(0, 3).join(", ")}${r.allow.length > 3 ? ` +${r.allow.length - 3}` : ""}` : "Locked";
}

function Meter({ pct, warn = 75, danger = 90, label, note }: { pct: number | null | undefined; warn?: number | null; danger?: number | null; label: string; note?: string }) {
  if (pct === null || pct === undefined) return <span className="hint">—</span>;
  const tone = danger != null && pct >= danger ? "danger" : warn != null && pct >= warn ? "warn" : "ok";
  return (
    <div className="meter" title={note ? `${label}: ${pct}% · ${note}` : `${label}: ${pct}%`}>
      <div className="meter-top"><span className="mono">{Math.round(pct)}%</span>{note && <span className="hint">{note}</span>}</div>
      <div className="meter-track" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)}>
        <div className={`meter-fill ${tone}`} style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
      </div>
    </div>
  );
}

const ROLE_SHORT: Record<string, string> = { master: "master", data: "data", data_hot: "hot", data_warm: "warm", data_cold: "cold", data_frozen: "frozen", data_content: "content", ingest: "ingest", ml: "ml", transform: "transform", remote_cluster_client: "remote" };

function roleText(n: NodeRow): string {
  const r = n.roles;
  const main = ["master", "data", "ingest", "ml"].filter((x) => r.includes(x));
  const tiers = r.filter((x) => x.startsWith("data_")).map((x) => ROLE_SHORT[x] ?? x);
  return [...main, ...(r.includes("data") ? [] : tiers)].join(" · ") || r.map((x) => ROLE_SHORT[x] ?? x).join(" · ") || "coordinating";
}

function Nodes({ clusterId }: { clusterId: string }) {
  const q = useQuery({
    queryKey: ["nodes", clusterId],
    queryFn: () => get<NodeStats>(`/clusters/${enc(clusterId)}/nodes`),
    refetchInterval: 30_000,
  });
  const d = q.data;
  const wm = d?.watermarks;
  return (
    <section className="card">
      <div className="card-head">
        <div className="grow">
          <h2>Nodes{d ? ` · ${d.summary.nodes}` : ""}</h2>
          <span className="hint">CPU, memory, JVM heap and disk per node · refreshes every 30 s{wm?.high ? ` · disk watermarks ${wm.low}% / ${wm.high}% / ${wm.flood}%` : ""}</span>
        </div>
      </div>
      {q.isLoading ? <Loading what="Reading node stats…" /> : q.error ? <div className="card-body"><ErrorCallout error={q.error} clusterId={clusterId} /></div> : d && (
        <>
          <div className="stats" style={{ padding: "0 18px 16px", gridTemplateColumns: "repeat(4, minmax(0, 1fr))" }}>
            <div className="stat">
              <span className="stat-label">Nodes</span>
              <span className="stat-value">{d.summary.nodes}</span>
              <span className="stat-note">{d.summary.dataNodes} data node{d.summary.dataNodes === 1 ? "" : "s"}</span>
            </div>
            <div className="stat">
              <span className="stat-label">Disk free</span>
              <span className="stat-value">{bytes(d.summary.diskAvailableBytes)}</span>
              <span className="stat-note">of {bytes(d.summary.diskTotalBytes)} · {d.summary.diskUsedPercent ?? "—"}% used</span>
            </div>
            <div className="stat">
              <span className="stat-label">CPU</span>
              <span className="stat-value">{d.summary.cpuPercentAvg ?? "—"}%</span>
              <span className="stat-note">average · busiest node {d.summary.cpuPercentMax ?? "—"}%</span>
            </div>
            <div className="stat">
              <span className="stat-label">JVM heap</span>
              <span className="stat-value">{d.summary.heapUsedPercentAvg ?? "—"}%</span>
              <span className="stat-note">average · highest {d.summary.heapUsedPercentMax ?? "—"}% · RAM {d.summary.memUsedPercentAvg ?? "—"}%</span>
            </div>
          </div>
          <div className="table-scroll" style={{ maxHeight: 520 }}>
            <table className="table compact nodes-table">
              <thead><tr><th>Node</th><th>CPU</th><th>RAM</th><th>JVM heap</th><th>Disk used</th><th className="num">Shards</th></tr></thead>
              <tbody>
                {d.nodes.map((n) => (
                  <tr key={n.id}>
                    <td>
                      <div className="row" style={{ gap: 6, flexWrap: "nowrap" }}>
                        <span className="mono" style={{ fontWeight: 600 }}>{n.name}</span>
                        {n.master && <Badge tone="blue" title="Elected master">master</Badge>}
                      </div>
                      <div className="hint">{[n.ip, roleText(n), n.version && `v${n.version}`].filter(Boolean).join(" · ")}</div>
                    </td>
                    <td><Meter pct={n.cpuPercent} label={`CPU on ${n.name}`} note={n.load1m != null ? `load ${n.load1m}${n.cpus ? ` / ${n.cpus} cpu` : ""}` : undefined} /></td>
                    <td><Meter pct={n.memUsedPercent} warn={90} danger={97} label={`RAM on ${n.name}`} note={`${bytes(n.memUsedBytes)} of ${bytes(n.memTotalBytes)}`} /></td>
                    <td><Meter pct={n.heapUsedPercent} warn={75} danger={85} label={`Heap on ${n.name}`} note={`${bytes(n.heapUsedBytes)} of ${bytes(n.heapMaxBytes)}`} /></td>
                    <td><Meter pct={n.diskUsedPercent} warn={wm?.low ?? 85} danger={wm?.high ?? 90} label={`Disk on ${n.name}`} note={n.diskTotalBytes ? `${bytes(n.diskAvailableBytes)} free of ${bytes(n.diskTotalBytes)}` : undefined} /></td>
                    <td className="num">{n.shards ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

export function Overview() {
  const { clusterId, level, admin, hasAccess, access } = useCluster();
  const c = `/c/${enc(clusterId)}`;
  const cluster = useClusters().data?.find((x) => x.id === clusterId);
  const adminInfo = useAdminClusters(admin).data?.find((x) => x.id === clusterId);
  const health = useHealth(clusterId);
  const indices = useQuery({ queryKey: ["indices", clusterId], queryFn: () => get<{ items: IndexRow[] }>(`/clusters/${enc(clusterId)}/indices`).then((r) => r.items) });
  const allow = useQuery({
    queryKey: ["allowlist-effective", clusterId],
    queryFn: () => get<{ rules: Rules; effectiveSource: string }>(`/admin/allowlist/${enc(clusterId)}`),
    enabled: admin,
  });
  const activity = useQuery({
    queryKey: ["audit", todayUtc(), clusterId, "recent"],
    queryFn: () => get<{ items: AuditEvent[] }>("/admin/audit", { date: todayUtc(), clusterId, limit: 6 }).then((r) => r.items),
    enabled: admin,
  });

  const docs = indices.data?.reduce((s, r) => s + (Number(r["docs.count"]) || 0), 0);
  const title = cluster?.name || clusterId;
  const links = [
    ...(admin ? [{ to: `${c}/cluster-settings`, icon: "sliders" as const, t: "Cluster settings", d: "Persistent cluster-wide settings (admins)" }] : []),
    { to: `${c}/indices`, icon: "table" as const, t: "Indices", d: "Settings, add-only mappings, guarded delete" },
    { to: `${c}/data`, icon: "database" as const, t: "Data", d: "Search, read, edit and export documents" },
    ...(level ? NAMED_TYPES.map((t) => ({ to: `${c}/${t}`, icon: NAMED_META[t].icon, t: NAMED_META[t].label, d: NAMED_META[t].blurb })) : []),
  ];

  if (!hasAccess) {
    return (
      <Page crumbs={[{ label: clusterId }]} title={clusterId}>
        <ErrorCallout error={new Error(`You don't have access to cluster '${clusterId}'.`)} />
      </Page>
    );
  }

  return (
    <Page crumbs={[{ label: clusterId, to: c }, { label: "Overview" }]} title={`${clusterId} overview`} health={health.data?.status ?? null}
      actions={<a href="/docs" target="_blank" rel="noreferrer" className="btn btn-sm">API docs <Icon name="external" size={14} /></a>}>
      <div className="page-head">
        <div className="grow">
          <h1>{title}</h1>
          <span className="sub">
            {[adminInfo?.clusterName ?? health.data?.clusterName, adminInfo?.version && `Elasticsearch ${adminInfo.version}`].filter(Boolean).join(" · ")}
            {adminInfo?.hosts?.[0] && <> · <span className="mono">{adminInfo.hosts[0].replace(/^https?:\/\//, "")}</span></>}
          </span>
          {cluster?.description && <span className="sub">{cluster.description}</span>}
        </div>
        {admin && <Link to={`${c}/cluster-settings`} className="btn btn-primary">Change a setting</Link>}
      </div>

      {health.error && <ErrorCallout error={health.error} clusterId={clusterId} admin={admin} />}

      <div className="stats">
        <div className="stat">
          <span className="stat-label">Cluster health</span>
          <span className="stat-value row" style={{ gap: 8, textTransform: "capitalize" }}>
            {health.data ? <><HealthDot status={health.data.status} />{health.data.status}</> : "—"}
          </span>
          <span className="stat-note">{health.data ? `${health.data.numberOfNodes} nodes · ${health.data.unassignedShards} unassigned shards` : "Checked before every change"}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Indices</span>
          <span className="stat-value">{indices.data ? nf.format(indices.data.length) : "—"}</span>
          <span className="stat-note">System indices hidden</span>
        </div>
        <div className="stat">
          <span className="stat-label">Documents</span>
          <span className="stat-value">{docs !== undefined ? num(docs) : "—"}</span>
          <span className="stat-note">Across listed indices</span>
        </div>
        <div className="stat">
          <span className="stat-label">Your access</span>
          <span className="stat-value">{admin ? "Admin" : level ? LEVEL_LABEL[level] : "Per index"}</span>
          <span className="stat-note">{admin ? "Everything, incl. cluster settings and users" : access?.indices.length ? `${level ? "Default · " : ""}${access.indices.length} index rule${access.indices.length === 1 ? "" : "s"}` : level === "view" ? "Read only" : level === "edit" ? "View, dry run, apply, roll back, edit documents" : "Also delete indices and bulk-delete documents"}</span>
        </div>
      </div>

      <Nodes clusterId={clusterId} />

      <div className={admin ? "grid-side" : ""}>
        <section className="card">
          <div className="card-head">
            <div className="grow"><h2>Configuration</h2><span className="hint">Every change: dry run → snapshot → apply</span></div>
          </div>
          {links.map((l) => (
            <Link key={l.to} to={l.to} className="link-card">
              <Icon name={l.icon} style={{ color: "var(--accent)" }} />
              <span className="stack-sm grow" style={{ gap: 0 }}><span className="t">{l.t}</span><span className="d">{l.d}</span></span>
              <Icon name="chevron" size={16} style={{ color: "var(--faint)" }} />
            </Link>
          ))}
        </section>
        {admin && (
          <section className="card">
            <div className="card-head">
              <div className="grow"><h2>What can be changed</h2><span className="hint">{allow.data?.effectiveSource === "global" ? "Global allowlist" : allow.data ? "Cluster override" : ""}</span></div>
              <Link to="/admin/allowlist" className="btn btn-sm">Manage</Link>
            </div>
            {allow.isLoading ? <Loading /> : (
              <table className="table">
                <tbody>
                  {["cluster-settings", "index-settings", "index-mappings", "index-delete", ...NAMED_TYPES].map((t) => {
                    const s = allowSummary(t, allow.data?.rules ?? {});
                    return (
                      <tr key={t}>
                        <td>{CONFIG_TYPE_LABEL[t]}</td>
                        <td style={{ textAlign: "right" }}>{s === "Locked" ? <Badge><Icon name="lock" size={12} /> Locked</Badge> : <span className="cell-mono" style={{ fontSize: 12 }}>{s}</span>}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </section>
        )}
      </div>

      {admin && (
        <section className="card">
          <div className="card-head">
            <div className="grow"><h2>Recent activity</h2><span className="hint">Today, UTC</span></div>
            <Link to={`/admin/audit?clusterId=${enc(clusterId)}`} className="btn btn-sm">Audit log</Link>
          </div>
          {activity.isLoading ? <Loading /> : activity.error ? <div className="card-body"><ErrorCallout error={activity.error} /></div> : !activity.data?.length ? (
            <div className="empty">No activity on this cluster today.</div>
          ) : (
            <table className="table">
              <thead><tr><th>Time</th><th>User</th><th>Action</th><th>Target</th><th>Outcome</th></tr></thead>
              <tbody>
                {activity.data.map((e) => (
                  <tr key={e.eventId}>
                    <td className="mono">{when(e.timestamp, false)}</td>
                    <td>{e.actor}</td>
                    <td>{actionLabel(e)}</td>
                    <td className="cell-mono">{e.resource === "_cluster" ? "cluster" : e.resource ?? e.targetUser ?? e.allowlist ?? "—"}</td>
                    <td><AuditOutcome outcome={e.outcome} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}
    </Page>
  );
}
