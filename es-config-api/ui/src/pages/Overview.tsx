import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { enc, get, NAMED_TYPES, type AuditEvent, type IndexRow, type Rules } from "../api";
import { Icon } from "../components/icons";
import { Page } from "../components/Shell";
import { Badge, ErrorCallout, HealthDot, Loading } from "../components/ui";
import { CONFIG_TYPE_LABEL, LEVEL_LABEL, NAMED_META, nf, num, todayUtc, when } from "../format";
import { useAdminClusters, useCluster, useClusters, useHealth } from "../session";
import { AuditOutcome, actionLabel } from "./admin/Audit";

function allowSummary(type: string, rules: Rules): string {
  const r = rules[type];
  if (!r) return "Locked";
  const extra = r.indices?.length ? ` · ${r.indices.join(", ")}` : r.indexPatterns?.length ? ` · ${r.indexPatterns.join(", ")}` : "";
  if (type === "cluster-settings" || type === "index-settings") return `${r.allow.length} key${r.allow.length === 1 ? "" : "s"}${extra}`;
  return r.allow.length ? `${r.allow.slice(0, 3).join(", ")}${r.allow.length > 3 ? ` +${r.allow.length - 3}` : ""}` : "Locked";
}

export function Overview() {
  const { clusterId, level, admin } = useCluster();
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
    { to: `${c}/cluster-settings`, icon: "sliders" as const, t: "Cluster settings", d: "Persistent cluster-wide settings" },
    { to: `${c}/indices`, icon: "table" as const, t: "Indices", d: "Settings, add-only mappings, guarded delete" },
    ...NAMED_TYPES.map((t) => ({ to: `${c}/${t}`, icon: NAMED_META[t].icon, t: NAMED_META[t].label, d: NAMED_META[t].blurb })),
  ];

  if (!level) {
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
        {level !== "view" && <Link to={`${c}/cluster-settings`} className="btn btn-primary">Change a setting</Link>}
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
          <span className="stat-value">{admin ? "Admin" : LEVEL_LABEL[level]}</span>
          <span className="stat-note">{admin ? "View, edit, delete, manage users" : level === "view" ? "Read only" : level === "edit" ? "View, dry run, apply, roll back" : "Also delete indices"}</span>
        </div>
      </div>

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
