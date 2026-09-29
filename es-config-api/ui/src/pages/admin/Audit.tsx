import { useQuery } from "@tanstack/react-query";
import { Fragment, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { get, type AuditEvent } from "../../api";
import { DiffTable } from "../../components/DiffTable";
import { Icon } from "../../components/icons";
import { Page } from "../../components/Shell";
import { Badge, Empty, ErrorCallout, Loading, downloadFile } from "../../components/ui";
import { CONFIG_TYPE_LABEL, pretty, todayUtc, when } from "../../format";
import { useClusters } from "../../session";

const ACTIONS = ["UPDATE", "ROLLBACK", "DRY_RUN", "INDEX_DELETE", "ADMIN_*", "AUTH_*"];
const OUTCOMES = ["SUCCESS", "REJECTED", "FAILED", "NO_CHANGE"];

const HUMAN: Record<string, string> = {
  UPDATE: "Update",
  ROLLBACK: "Rollback",
  INDEX_DELETE: "Index delete",
  AUTH_LOGIN: "Sign in",
  AUTH_LOGOUT: "Sign out",
  AUTH_LOGOUT_ALL: "Sign out everywhere",
  AUTH_PASSWORD_CHANGE: "Password change",
  ADMIN_USER_CREATE: "User added",
  ADMIN_USER_UPDATE: "User updated",
  ADMIN_USER_DELETE: "User removed",
  ADMIN_PERMISSIONS_UPDATE: "Permissions updated",
  ADMIN_PASSWORD_RESET: "Password reset",
  ADMIN_ALLOWLIST_UPDATE: "Allowlist update",
  ADMIN_ALLOWLIST_DELETE: "Allowlist override removed",
};

export function actionLabel(e: AuditEvent): string {
  const type = e.configType ? CONFIG_TYPE_LABEL[e.configType]?.toLowerCase() ?? e.configType : "";
  if (e.action === "DRY_RUN") return `Dry run · ${e.requestedAction === "ROLLBACK" ? "rollback · " : ""}${type}`;
  if (e.action === "UPDATE" || e.action === "ROLLBACK") return `${HUMAN[e.action]} · ${type}`;
  return HUMAN[e.action] ?? e.action;
}

export function AuditOutcome({ outcome }: { outcome: AuditEvent["outcome"] }) {
  const map = { SUCCESS: ["green", "Success"], REJECTED: ["amber", "Rejected"], FAILED: ["red", "Failed"], NO_CHANGE: [undefined, "No change"] } as const;
  const [tone, label] = map[outcome] ?? [undefined, outcome];
  return <Badge tone={tone}>{label}</Badge>;
}

function target(e: AuditEvent): string {
  if (e.resource === "_cluster") return "cluster";
  return e.resource ?? e.targetUser ?? e.allowlist ?? "—";
}

const SKIP = new Set(["eventId", "timestamp", "action", "requestedAction", "actor", "outcome", "clusterId", "configType", "resource", "reason", "requestId", "sourceIp", "diff", "error", "auditKey", "changeId"]);

function Details({ e }: { e: AuditEvent }) {
  const blocked = (e.error as { details?: { blocked?: string[] } } | undefined)?.details?.blocked;
  const rest = Object.fromEntries(Object.entries(e).filter(([k, v]) => !SKIP.has(k) && v !== null && v !== undefined && v !== false));
  const rows: [string, string | undefined | null][] = [
    ["Error", e.error ? `${e.error.code} (${e.error.status})` : null],
    ["Message", e.error?.message],
    ["Blocked", blocked?.join(", ")],
    ["Reason", e.reason ?? null],
    ["Cluster", e.clusterId],
    ["Change id", e.changeId as string | undefined],
    ["Request id", e.requestId],
    ["Source IP", e.sourceIp],
  ];
  return (
    <div className="stack" style={{ padding: "4px 0 10px" }}>
      <dl style={{ display: "grid", gridTemplateColumns: "140px 1fr", gap: "6px 16px", margin: 0, fontSize: 13 }}>
        {rows.filter(([, v]) => v).map(([k, v]) => (
          <Fragment key={k}><dt style={{ color: "var(--muted)" }}>{k}</dt><dd style={{ margin: 0 }} className={k.endsWith("id") || k === "Source IP" ? "mono" : ""}>{v}</dd></Fragment>
        ))}
      </dl>
      {e.diff && (e.diff.added.length + e.diff.removed.length + e.diff.changed.length > 0) && (
        <div className="stack-sm"><span className="label">Change</span><DiffTable diff={e.diff} absent="not set" keyLabel="Path" /></div>
      )}
      {Object.keys(rest).length > 0 && (
        <details>
          <summary className="label">More fields</summary>
          <pre className="code-block" style={{ marginTop: 6 }}>{pretty(rest)}</pre>
        </details>
      )}
    </div>
  );
}

export function Audit() {
  const clusters = useClusters().data ?? [];
  const [params, setParams] = useSearchParams();
  const date = params.get("date") || todayUtc();
  const clusterId = params.get("clusterId") ?? "";
  const user = params.get("user") ?? "";
  const action = params.get("action") ?? "";
  const outcome = params.get("outcome") ?? "";
  const [userDraft, setUserDraft] = useState(user);
  const [open, setOpen] = useState<string | null>(null);
  const exactAction = action && !action.endsWith("*") ? action : undefined;

  const set = (k: string, v: string) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v); else next.delete(k);
    setParams(next, { replace: true });
  };

  const q = useQuery({
    queryKey: ["audit", date, clusterId, user, exactAction],
    queryFn: () => get<{ items: AuditEvent[]; count: number }>("/admin/audit", { date, clusterId, user, action: exactAction, limit: 1000 }),
  });
  const items = useMemo(() => (q.data?.items ?? []).filter((e) =>
    (!action || !action.endsWith("*") || e.action.startsWith(action.slice(0, -1))) && (!outcome || e.outcome === outcome)), [q.data, action, outcome]);

  return (
    <Page crumbs={[{ label: "Administration" }, { label: "Audit log" }]} title="Audit log">
      <div className="page-head" style={{ alignItems: "center" }}>
        <div className="grow">
          <h1>Audit log</h1>
          <span className="sub">Every change, dry run, sign-in and rejected attempt, newest first. Times in UTC.</span>
        </div>
        <button type="button" className="btn" disabled={!items.length} onClick={() => downloadFile(`audit-${date}${clusterId ? `-${clusterId}` : ""}.json`, pretty(items), "application/json")}>
          <Icon name="download" /> Export JSON
        </button>
      </div>
      <form className="card" onSubmit={(e) => { e.preventDefault(); set("user", userDraft.trim().toLowerCase()); }}>
        <div className="card-body" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 12 }}>
          <div className="field"><label className="label" htmlFor="f-date">Date (UTC)</label>
            <input id="f-date" className="input" type="date" value={date} max={todayUtc()} onChange={(e) => set("date", e.target.value)} /></div>
          <div className="field"><label className="label" htmlFor="f-cluster">Cluster</label>
            <select id="f-cluster" className="select" value={clusterId} onChange={(e) => set("clusterId", e.target.value)}>
              <option value="">All clusters</option>
              {clusters.map((c) => <option key={c.id} value={c.id}>{c.id}</option>)}
            </select></div>
          <div className="field"><label className="label" htmlFor="f-user">User</label>
            <input id="f-user" className="input" type="search" placeholder="Any user (press Enter)" value={userDraft} onChange={(e) => setUserDraft(e.target.value)}
              onBlur={() => userDraft.trim().toLowerCase() !== user && set("user", userDraft.trim().toLowerCase())} /></div>
          <div className="field"><label className="label" htmlFor="f-action">Action</label>
            <select id="f-action" className="select" value={action} onChange={(e) => set("action", e.target.value)}>
              <option value="">All actions</option>
              {ACTIONS.map((a) => <option key={a} value={a}>{a}</option>)}
            </select></div>
          <div className="field"><label className="label" htmlFor="f-out">Outcome</label>
            <select id="f-out" className="select" value={outcome} onChange={(e) => set("outcome", e.target.value)}>
              <option value="">Any</option>
              {OUTCOMES.map((o) => <option key={o} value={o}>{o.replace("_", " ").toLowerCase().replace(/^./, (c) => c.toUpperCase())}</option>)}
            </select></div>
        </div>
      </form>
      <section className="card" style={{ overflow: "hidden" }}>
        {q.isLoading ? <Loading what="Reading the audit log…" /> : q.error ? <div className="card-body"><ErrorCallout error={q.error} /></div> : items.length === 0 ? (
          <Empty title="No events">Nothing matches these filters on {date}.</Empty>
        ) : (
          <div className="table-scroll" style={{ maxHeight: "calc(100vh - 360px)" }}>
            <table className="table">
              <thead><tr><th style={{ width: 28 }} /><th>Time</th><th>User</th><th>Action</th><th>Type</th><th>Target</th><th>Outcome</th></tr></thead>
              <tbody>
                {items.map((e) => {
                  const isOpen = open === e.eventId;
                  return (
                    <Fragment key={e.eventId}>
                      <tr className={`clickable ${isOpen ? "selected" : ""}`} onClick={() => setOpen(isOpen ? null : e.eventId)}>
                        <td>
                          <button type="button" className="btn btn-ghost icon-btn btn-sm" style={{ width: 26, height: 26, minHeight: 0 }} aria-expanded={isOpen}
                            aria-label={isOpen ? "Hide details" : "Show details"} onClick={(ev) => { ev.stopPropagation(); setOpen(isOpen ? null : e.eventId); }}>
                            <Icon name={isOpen ? "caret" : "chevron"} size={14} />
                          </button>
                        </td>
                        <td className="mono">{when(e.timestamp, false)}</td>
                        <td>{e.actor}</td>
                        <td className="mono" style={{ fontSize: 12 }}>{e.action}</td>
                        <td>{e.configType ?? (e.action.startsWith("ADMIN_ALLOWLIST") ? "allowlist" : e.action.startsWith("ADMIN_") ? "users" : e.action.startsWith("AUTH_") ? "auth" : "—")}</td>
                        <td className="cell-mono">{target(e)}</td>
                        <td><AuditOutcome outcome={e.outcome} /></td>
                      </tr>
                      {isOpen && (
                        <tr className="selected"><td /><td colSpan={6}><Details e={e} /></td></tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
      {q.data && q.data.count >= 500 && <span className="hint">Showing the newest {q.data.count} events (the server limit). Narrow the filters to see the rest.</span>}
    </Page>
  );
}
