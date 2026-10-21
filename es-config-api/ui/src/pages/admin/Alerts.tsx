import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  enc, get, request, type ActiveAlert, type AlertEvent, type AlertMetric, type AlertRun, type AlertSettings,
  type AlertsOverview, type MetricRule, type Snooze,
} from "../../api";
import { Icon } from "../../components/icons";
import { Page } from "../../components/Shell";
import { Badge, Callout, Dialog, Empty, ErrorCallout, Loading, useToast } from "../../components/ui";
import { ago, when } from "../../format";
import { useClusters } from "../../session";

export const METRIC_LABEL: Record<AlertMetric, string> = {
  disk: "Disk", ram: "RAM (excl. cache)", heap: "JVM heap", cpu: "CPU", shards: "Shards",
  health: "Cluster health", nodes: "Nodes",
};
const METRIC_NOTE: Record<AlertMetric, string> = {
  disk: "Disk used on each data node",
  ram: "RAM Elasticsearch holds, without the OS file cache (estimate, within ~5% of CloudWatch)",
  heap: "JVM heap used, read every 2 minutes; heap swings a lot with garbage collection, so it alerts only when enough recent readings are over the line",
  cpu: "CPU; alerts after 2 checks in a row above the line",
  shards: "Shards in the cluster vs its limit (max_shards_per_node × data nodes); new indices fail past 100%",
  health: "Cluster yellow or red (unassigned shards): always critical",
  nodes: "A node left the cluster, or the cluster stopped answering: always critical",
};
const PCT: AlertMetric[] = ["disk", "ram", "heap", "cpu", "shards"];
const ALL: AlertMetric[] = [...PCT, "health", "nodes"];
const DURATIONS: [string, string][] = [["1h", "1 hour"], ["4h", "4 hours"], ["24h", "24 hours"], ["7d", "7 days"], ["resolved", "Until it clears"]];

export function describeAlert(a: Pick<ActiveAlert, "metric" | "node" | "value" | "threshold" | "detail" | "text">): string {
  if (a.text) return a.text;
  let s = `${METRIC_LABEL[a.metric] ?? a.metric}${a.node ? ` on ${a.node}` : ""}: ${a.value ?? "—"}%`;
  if (a.threshold != null) s += ` (alert at ${a.threshold}%)`;
  if (a.detail) s += ` · ${a.detail}`;
  return s;
}

export function SeverityBadge({ s }: { s: string }) {
  return s === "critical" ? <Badge tone="red">Critical</Badge> : s === "warning" ? <Badge tone="amber">Warning</Badge> : <Badge>{s}</Badge>;
}

function snoozeText(s: { until: string | null; untilResolved: boolean }) {
  return s.untilResolved ? "until it clears" : `until ${when(s.until)}`;
}

function RunLine({ run, every }: { run: AlertRun | null; every: number }) {
  if (!run) return <span className="hint">No check has run yet · every {Math.round(every / 60)} min</span>;
  const failed = Object.entries(run.clusters).filter(([, c]) => !c.ok);
  return (
    <span className="hint">
      Last check {ago(run.at)} · every {Math.round(every / 60)} min · {run.messages} Slack message{run.messages === 1 ? "" : "s"}
      {run.disabled && " · alerts are turned off"}
      {failed.length > 0 && <span style={{ color: "var(--danger)" }}> · could not read {failed.map(([c]) => c).join(", ")}</span>}
      {run.slack && <span style={{ color: "var(--danger)" }}> · Slack: {run.slack}</span>}
    </span>
  );
}

export function Alerts() {
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({ queryKey: ["alerts"], queryFn: () => get<AlertsOverview>("/admin/alerts"), refetchInterval: 30_000 });
  const [hookOpen, setHookOpen] = useState(false);
  const [snooze, setSnooze] = useState<ActiveAlert | { clusterId: string } | null>(null);
  const [preview, setPreview] = useState(false);
  const refresh = () => qc.invalidateQueries({ queryKey: ["alerts"] });

  const check = useMutation({
    mutationFn: async () => (await request<AlertRun & { active: number }>("/admin/alerts/_check", { method: "POST" })).data,
    onSuccess: (r) => { refresh(); qc.invalidateQueries({ queryKey: ["alert-history"] }); toast(`Checked: ${r.active} active alert${r.active === 1 ? "" : "s"}, ${r.messages} Slack message${r.messages === 1 ? "" : "s"}`); },
  });
  const test = useMutation({
    mutationFn: async () => (await request<{ ok: boolean; error?: string }>("/admin/alerts/slack/_test", { method: "POST" })).data,
    onSuccess: (r) => { refresh(); toast(r.ok ? "Test message sent: check the Slack channel" : `Slack: ${r.error}`, r.ok ? "ok" : "error"); },
  });
  const unsnooze = useMutation({
    mutationFn: async (id: string) => request(`/admin/alerts/snoozes/${enc(id)}`, { method: "DELETE" }),
    onSuccess: () => { refresh(); toast("Snooze ended"); },
  });
  const clear = useMutation({
    mutationFn: async (key: string) => request(`/admin/alerts/active/${enc(key)}/_clear`, { method: "POST" }),
    onSuccess: () => { refresh(); toast("Node forgotten"); },
  });
  const sendDigest = useMutation({
    mutationFn: async () => (await request<{ ok: boolean; error?: string }>("/admin/alerts/digest/_send", { method: "POST" })).data,
    onSuccess: (r) => toast(r.ok ? "Digest posted to Slack" : `Slack: ${r.error}`, r.ok ? "ok" : "error"),
  });

  const d = q.data;
  const crit = d?.active.filter((a) => a.severity === "critical").length ?? 0;
  return (
    <Page crumbs={[{ label: "Administration" }, { label: "Alerts" }]} title="Alerts">
      <div className="page-head" style={{ alignItems: "center" }}>
        <div className="grow stack-sm" style={{ gap: 4 }}>
          <h1>Alerts</h1>
          <span className="sub">
            Every cluster is checked every 5 minutes and problems are posted to Slack: warnings repeat hourly, critical alerts every 5 minutes, and "OK again" is posted when one clears.
            Snoozing is done here only; nothing about snoozes is posted to Slack.
          </span>
          {d && <RunLine run={d.lastRun} every={d.intervalSeconds} />}
        </div>
        <button type="button" className="btn" onClick={() => check.mutate()} disabled={check.isPending}>
          <Icon name="refresh" /> {check.isPending ? "Checking…" : "Check now"}
        </button>
      </div>
      {check.error && <ErrorCallout error={check.error} />}
      {q.isLoading ? <Loading what="Reading alerts…" /> : q.error ? <ErrorCallout error={q.error} /> : d && (
        <>
          {/* Slack */}
          <section className="card">
            <div className="card-head">
              <div className="grow">
                <h2>Slack</h2>
                <span className="hint">One channel for alerts, approval requests and the weekly digest. The webhook is kept in AWS Secrets Manager (<code>{d.slack.secret}</code>) and never shown again.</span>
              </div>
            </div>
            <div className="card-body stack-sm">
              <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                {d.slack.configured ? (d.slack.lastError ? <Badge tone="red">Error</Badge> : <Badge tone="green">Connected</Badge>) : <Badge tone="amber">Not set up</Badge>}
                <span className="grow" />
                <button type="button" className="btn btn-sm" onClick={() => setHookOpen(true)}>{d.slack.configured ? "Replace webhook" : "Set webhook"}</button>
                <button type="button" className="btn btn-sm" onClick={() => test.mutate()} disabled={!d.slack.configured || test.isPending}>{test.isPending ? "Sending…" : "Send test message"}</button>
              </div>
              {d.slack.lastError && <Callout tone="danger" title="The last Slack message failed">{d.slack.lastError}</Callout>}
              {!d.slack.configured && <span className="hint">Until a webhook is set, alerts are tracked and shown here but not posted.</span>}
            </div>
          </section>

          {/* Active */}
          <section className="card" style={{ overflow: "hidden" }}>
            <div className="card-head">
              <div className="grow">
                <h2>Active alerts{d.active.length ? ` · ${d.active.length}` : ""}</h2>
                <span className="hint">{crit ? `${crit} critical` : "Nothing critical"} · snoozed alerts stay listed but don't post</span>
              </div>
            </div>
            {d.active.length === 0 ? <Empty title="All clear">No cluster is above its thresholds.</Empty> : (
              <div className="table-scroll">
                <table className="table">
                  <thead><tr><th>Level</th><th>Cluster</th><th>Alert</th><th>Since</th><th>Last posted</th><th style={{ textAlign: "right" }}>Actions</th></tr></thead>
                  <tbody>
                    {d.active.map((a) => (
                      <tr key={a.key}>
                        <td><SeverityBadge s={a.severity} /></td>
                        <td className="nowrap"><Link to={`/c/${enc(a.clusterId)}`} className="mono">{a.clusterId}</Link></td>
                        <td>
                          {describeAlert(a)}
                          {a.snoozedBy && <div className="hint"><Icon name="clock" size={12} /> Snoozed by {a.snoozedBy.by} {snoozeText(a.snoozedBy)} · {a.snoozedBy.reason}</div>}
                        </td>
                        <td className="nowrap" title={when(a.since)}>{ago(a.since)}</td>
                        <td className="nowrap">{a.lastSentAt ? ago(a.lastSentAt) : <span className="hint">not yet</span>}</td>
                        <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                          {a.snoozedBy ? (
                            <button type="button" className="btn btn-ghost btn-sm" onClick={() => unsnooze.mutate(a.snoozedBy!.id)}>End snooze</button>
                          ) : (
                            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setSnooze(a)}>Snooze</button>
                          )}
                          {a.metric === "nodes" && a.node && (
                            <button type="button" className="btn btn-ghost btn-sm" title="The node was removed on purpose: stop alerting about it"
                              onClick={() => clear.mutate(a.key)}>Forget node</button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {/* Snoozes */}
          <section className="card" style={{ overflow: "hidden" }}>
            <div className="card-head">
              <div className="grow">
                <h2>Snoozes{d.snoozes.length ? ` · ${d.snoozes.length}` : ""}</h2>
                <span className="hint">While snoozed, matching alerts post nothing. When the snooze ends, alerts that are still on post again.</span>
              </div>
              <button type="button" className="btn btn-sm" onClick={() => setSnooze({ clusterId: "" })}><Icon name="clock" /> Snooze…</button>
            </div>
            {d.snoozes.length === 0 ? <div className="card-body"><span className="hint">Nothing is snoozed.</span></div> : (
              <div className="table-scroll">
                <table className="table">
                  <thead><tr><th>What</th><th>Until</th><th>By</th><th>Reason</th><th /></tr></thead>
                  <tbody>
                    {d.snoozes.map((s: Snooze) => (
                      <tr key={s.id}>
                        <td><span className="mono">{s.clusterId}</span> · {s.metric ? METRIC_LABEL[s.metric] : "every alert"}{s.node ? ` on ${s.node}` : ""}</td>
                        <td className="nowrap">{s.untilResolved ? "Until it clears" : when(s.until)}</td>
                        <td>{s.by}</td>
                        <td>{s.reason}</td>
                        <td style={{ textAlign: "right" }}><button type="button" className="btn btn-ghost btn-sm" onClick={() => unsnooze.mutate(s.id)}>End now</button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <Thresholds settings={d.settings} />

          {/* Digest */}
          <section className="card">
            <div className="card-head">
              <div className="grow">
                <h2>Weekly digest</h2>
                <span className="hint">{d.digestWhen}: per cluster size and growth over the week, shards vs the limit, the 10 largest and fastest-growing indices, alerts of the week, and request counts.{d.lastDigest ? ` Last sent: week ${d.lastDigest.split("-W")[1]}.` : ""}</span>
              </div>
              <button type="button" className="btn btn-sm" onClick={() => setPreview(true)}>Preview</button>
              <button type="button" className="btn btn-sm" onClick={() => sendDigest.mutate()} disabled={!d.slack.configured || sendDigest.isPending}>{sendDigest.isPending ? "Sending…" : "Send now"}</button>
            </div>
          </section>

          <History />
        </>
      )}
      {hookOpen && d && <WebhookDialog configured={d.slack.configured} onClose={() => setHookOpen(false)} />}
      {snooze && <SnoozeDialog target={snooze} onClose={() => setSnooze(null)} />}
      {preview && <DigestPreview onClose={() => setPreview(false)} />}
    </Page>
  );
}

function WebhookDialog({ configured, onClose }: { configured: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const id = useId();
  const [url, setUrl] = useState("");
  const [removing, setRemoving] = useState(false);
  const ok = /^https:\/\/hooks\.slack\.com\/(services|workflows|triggers)\/\S{10,}$/.test(url.trim());
  const save = useMutation({
    mutationFn: async () => request("/admin/alerts/slack", { method: "PUT", body: { webhookUrl: url.trim() } }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["alerts"] }); toast("Webhook saved to Secrets Manager. Send a test message to check it."); onClose(); },
  });
  const remove = useMutation({
    mutationFn: async () => request("/admin/alerts/slack", { method: "DELETE" }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["alerts"] }); toast("Webhook removed: nothing is posted to Slack now"); onClose(); },
  });
  return (
    <Dialog title={configured ? "Replace the Slack webhook" : "Set the Slack webhook"} onClose={onClose} busy={save.isPending || remove.isPending}
      subtitle="In Slack: Apps → Incoming Webhooks → Add to Slack → pick the channel → copy the URL."
      footer={
        <>
          {configured && !removing && <button type="button" className="btn btn-ghost" style={{ marginRight: "auto", color: "var(--danger)" }} onClick={() => setRemoving(true)}>Remove webhook</button>}
          {removing && <button type="button" className="btn btn-danger" style={{ marginRight: "auto" }} onClick={() => remove.mutate()} disabled={remove.isPending}>Yes, stop posting to Slack</button>}
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button type="button" className="btn btn-primary" onClick={() => save.mutate()} disabled={!ok || save.isPending}>{save.isPending ? "Saving…" : "Save"}</button>
        </>
      }>
      <div className="field">
        <label htmlFor={id} className="label">Webhook URL</label>
        <input id={id} className="input mono" type="password" autoComplete="off" value={url} placeholder="https://hooks.slack.com/services/…" onChange={(e) => setUrl(e.target.value)} autoFocus />
        {url && !ok && <span className="hint" style={{ color: "var(--danger)" }}>That isn't a Slack incoming-webhook URL</span>}
        <span className="hint">Stored in AWS Secrets Manager. Anyone with this URL can post to the channel, so don't share it elsewhere.</span>
      </div>
      {(save.error || remove.error) && <ErrorCallout error={save.error || remove.error} />}
    </Dialog>
  );
}

function SnoozeDialog({ target, onClose }: { target: ActiveAlert | { clusterId: string }; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const clusters = useClusters().data ?? [];
  const base = useId();
  const alert = "key" in target ? target : null;
  const [clusterId, setClusterId] = useState(target.clusterId || clusters[0]?.id || "");
  const [scope, setScope] = useState<"alert" | "metric" | "cluster">(alert ? (alert.node ? "alert" : "metric") : "cluster");
  const [metric, setMetric] = useState<AlertMetric>(alert?.metric ?? "disk");
  const [duration, setDuration] = useState("4h");
  const [reason, setReason] = useState("");
  useEffect(() => { if (!clusterId && clusters[0]) setClusterId(clusters[0].id); }, [clusters, clusterId]);
  const body = {
    clusterId, duration, reason: reason.trim(),
    metric: scope === "cluster" ? null : metric,
    node: scope === "alert" && alert?.node ? alert.node : null,
  };
  const save = useMutation({
    mutationFn: async () => request("/admin/alerts/snoozes", { method: "POST", body }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["alerts"] }); toast("Snoozed"); onClose(); },
  });
  const id = (s: string) => `${base}-${s}`;
  return (
    <Dialog title="Snooze alerts" subtitle="Silences Slack for these alerts. They stay listed here, and nothing is posted about the snooze." onClose={onClose} busy={save.isPending}
      footer={<>
        <button type="button" className="btn" onClick={onClose}>Cancel</button>
        <button type="button" className="btn btn-primary" onClick={() => save.mutate()} disabled={!clusterId || !reason.trim() || save.isPending}>{save.isPending ? "Snoozing…" : "Snooze"}</button>
      </>}>
      {alert ? (
        <Callout tone="neutral"><SeverityBadge s={alert.severity} /> <span className="mono">{alert.clusterId}</span> · {describeAlert(alert)}</Callout>
      ) : (
        <div className="field">
          <label htmlFor={id("c")} className="label">Cluster</label>
          <select id={id("c")} className="select" value={clusterId} onChange={(e) => setClusterId(e.target.value)}>
            {clusters.map((c) => <option key={c.id} value={c.id}>{c.id}{c.name && c.name !== c.id ? ` · ${c.name}` : ""}</option>)}
          </select>
        </div>
      )}
      <fieldset className="field" style={{ border: 0, padding: 0, margin: 0 }}>
        <legend className="label">What</legend>
        {alert?.node && <label className="check"><input type="radio" name={id("s")} checked={scope === "alert"} onChange={() => setScope("alert")} /> Only this alert ({METRIC_LABEL[alert.metric]} on {alert.node})</label>}
        <label className="check"><input type="radio" name={id("s")} checked={scope === "metric"} onChange={() => setScope("metric")} />
          Every {alert ? METRIC_LABEL[alert.metric] : ""} alert on {clusterId || "the cluster"}
        </label>
        {!alert && scope === "metric" && (
          <select className="select" aria-label="Alert type" value={metric} onChange={(e) => setMetric(e.target.value as AlertMetric)} style={{ marginLeft: 26, maxWidth: 260 }}>
            {ALL.map((m) => <option key={m} value={m}>{METRIC_LABEL[m]}</option>)}
          </select>
        )}
        <label className="check"><input type="radio" name={id("s")} checked={scope === "cluster"} onChange={() => setScope("cluster")} /> Every alert on {clusterId || "the cluster"}</label>
      </fieldset>
      <div className="field">
        <label htmlFor={id("d")} className="label">For</label>
        <select id={id("d")} className="select" value={duration} onChange={(e) => setDuration(e.target.value)} style={{ maxWidth: 260 }}>
          {DURATIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
      </div>
      <div className="field">
        <label htmlFor={id("r")} className="label">Reason <span className="hint">· required, kept in the audit log</span></label>
        <input id={id("r")} className="input" value={reason} maxLength={300} placeholder="Disk cleanup scheduled tonight" onChange={(e) => setReason(e.target.value)} />
      </div>
      {save.error && <ErrorCallout error={save.error} />}
    </Dialog>
  );
}

function Thresholds({ settings }: { settings: AlertSettings }) {
  const qc = useQueryClient();
  const toast = useToast();
  const clusters = useClusters().data ?? [];
  const [draft, setDraft] = useState<AlertSettings>(settings);
  const [scope, setScope] = useState("");          // "" = defaults
  const loaded = useRef(JSON.stringify(settings));
  useEffect(() => {
    setDraft((cur) => (JSON.stringify(cur) === loaded.current ? settings : cur));   // keep unsaved edits
    loaded.current = JSON.stringify(settings);
  }, [settings]);
  const dirty = useMemo(() => JSON.stringify(draft) !== JSON.stringify(settings), [draft, settings]);
  const save = useMutation({
    mutationFn: async () => (await request<AlertSettings>("/admin/alerts/settings", { method: "PUT", body: draft })).data,
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["alerts"] }); toast("Alert settings saved"); },
  });
  const cl = scope ? draft.clusters[scope] ?? { off: false, metrics: {} } : null;
  const rule = (m: AlertMetric): MetricRule => (cl ? { ...draft.defaults[m], ...cl.metrics[m] } : draft.defaults[m]);
  const overridden = (m: AlertMetric, f: keyof MetricRule) => !!cl && cl.metrics[m]?.[f] !== undefined;
  const setRule = (m: AlertMetric, f: keyof MetricRule, v: number | boolean | undefined) => {
    setDraft((d) => {
      const next = structuredClone(d);
      if (!scope) {
        (next.defaults[m] as Record<string, unknown>)[f] = v;
        return next;
      }
      const c = next.clusters[scope] ?? { off: false, metrics: {} };
      const r = { ...(c.metrics[m] ?? {}) } as Record<string, unknown>;
      if (v === undefined) delete r[f]; else r[f] = v;
      c.metrics[m] = r;
      if (!Object.keys(r).length) delete c.metrics[m];
      next.clusters[scope] = c;
      if (!c.off && !Object.keys(c.metrics).length) delete next.clusters[scope];
      return next;
    });
  };
  const bad = PCT.some((m) => {
    const r = rule(m);
    return r.warn == null || r.crit == null || r.warn < 1 || r.crit > 100 || r.warn > r.crit;
  }) || (() => {
    const h = rule("heap");
    const n = h.need ?? 3, w = h.window ?? 5;
    return !Number.isInteger(n) || !Number.isInteger(w) || n < 1 || w < 1 || w > 15 || n > w;
  })();
  return (
    <section className="card">
      <div className="card-head">
        <div className="grow">
          <h2>Thresholds</h2>
          <span className="hint">Defaults apply to every cluster; pick a cluster to override them for it. An alert clears 2 points below its line.</span>
        </div>
        <select className="select" aria-label="Thresholds for" value={scope} onChange={(e) => setScope(e.target.value)} style={{ maxWidth: 240 }}>
          <option value="">Defaults (all clusters)</option>
          {clusters.map((c) => <option key={c.id} value={c.id}>{c.id}{draft.clusters[c.id] ? " · overridden" : ""}</option>)}
        </select>
      </div>
      <div className="card-body stack-sm">
        {!scope && (
          <div className="row" style={{ gap: 18, flexWrap: "wrap" }}>
            <label className="check"><input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} /> Alerts on</label>
            <label className="check"><input type="checkbox" checked={draft.digest.enabled} onChange={(e) => setDraft({ ...draft, digest: { enabled: e.target.checked } })} /> Weekly digest</label>
            <label className="row" style={{ gap: 6 }}>Repeat warnings every
              <input className="input" type="number" min={5} max={1440} style={{ width: 80 }} value={draft.repeatMinutes.warning}
                onChange={(e) => setDraft({ ...draft, repeatMinutes: { ...draft.repeatMinutes, warning: Number(e.target.value) } })} /> min</label>
            <label className="row" style={{ gap: 6 }}>critical every
              <input className="input" type="number" min={5} max={1440} style={{ width: 80 }} value={draft.repeatMinutes.critical}
                onChange={(e) => setDraft({ ...draft, repeatMinutes: { ...draft.repeatMinutes, critical: Number(e.target.value) } })} /> min</label>
          </div>
        )}
        {scope && (
          <label className="check">
            <input type="checkbox" checked={!!cl?.off} onChange={(e) => setDraft((d) => {
              const next = structuredClone(d);
              const c = next.clusters[scope] ?? { off: false, metrics: {} };
              c.off = e.target.checked;
              next.clusters[scope] = c;
              if (!c.off && !Object.keys(c.metrics).length) delete next.clusters[scope];
              return next;
            })} /> No alerts at all for {scope}
          </label>
        )}
        <div className="table-scroll">
          <table className="table compact">
            <thead><tr><th>Alert</th><th>On</th><th>Warning at</th><th>Critical at</th>{scope && <th />}</tr></thead>
            <tbody>
              {ALL.map((m) => {
                const r = rule(m);
                const pct = PCT.includes(m);
                return (
                  <tr key={m} style={cl?.off ? { opacity: 0.5 } : undefined}>
                    <td>
                      <strong>{METRIC_LABEL[m]}</strong><div className="hint">{METRIC_NOTE[m]}</div>
                      {m === "heap" && (
                        <div className="row" style={{ gap: 6, marginTop: 6, flexWrap: "wrap", fontSize: 13.5 }}>
                          Alert when
                          <input className="input" type="number" min={1} max={15} aria-label="Readings that must be over the line" value={r.need ?? 3}
                            disabled={!!cl?.off} style={{ width: 64, fontWeight: overridden(m, "need") ? 700 : undefined }}
                            onChange={(e) => setRule(m, "need", Number(e.target.value))} />
                          of the last
                          <input className="input" type="number" min={1} max={15} aria-label="Readings looked at" value={r.window ?? 5}
                            disabled={!!cl?.off} style={{ width: 64, fontWeight: overridden(m, "window") ? 700 : undefined }}
                            onChange={(e) => setRule(m, "window", Number(e.target.value))} />
                          readings are over the line
                          <span className="hint">(= the last {((r.window ?? 5) * 2)} minutes)</span>
                        </div>
                      )}
                      {m === "heap" && (r.need ?? 3) > (r.window ?? 5) && <div className="hint" style={{ color: "var(--danger)" }}>The first number can't be bigger than the second.</div>}
                    </td>
                    <td><input type="checkbox" aria-label={`${METRIC_LABEL[m]} on`} checked={r.on !== false} disabled={!!cl?.off} onChange={(e) => setRule(m, "on", e.target.checked)} /></td>
                    <td>{pct ? <PctInput label={`${METRIC_LABEL[m]} warning`} value={r.warn} strong={overridden(m, "warn")} disabled={!!cl?.off} onChange={(v) => setRule(m, "warn", v)} /> : <span className="hint">—</span>}</td>
                    <td>{pct ? <PctInput label={`${METRIC_LABEL[m]} critical`} value={r.crit} strong={overridden(m, "crit")} disabled={!!cl?.off} onChange={(v) => setRule(m, "crit", v)} /> : <span className="hint">critical</span>}</td>
                    {scope && <td>{cl?.metrics[m] && <button type="button" className="btn btn-ghost btn-sm" onClick={() => {
                      setRule(m, "warn", undefined); setRule(m, "crit", undefined); setRule(m, "on", undefined);
                      if (m === "heap") { setRule(m, "need", undefined); setRule(m, "window", undefined); }
                    }}>Use defaults</button>}</td>}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {bad && <span className="hint" style={{ color: "var(--danger)" }}>Each warning must be 1–100% and not above its critical; the heap rule is up to 15 readings, and the first number can't be bigger than the second.</span>}
        {save.error && <ErrorCallout error={save.error} />}
        <div className="row" style={{ gap: 8, justifyContent: "flex-end" }}>
          {settings.updatedBy && <span className="hint grow">Last changed by {settings.updatedBy} {ago(settings.updatedAt)}</span>}
          <button type="button" className="btn" disabled={!dirty || save.isPending} onClick={() => setDraft(settings)}>Discard</button>
          <button type="button" className="btn btn-primary" disabled={!dirty || bad || save.isPending} onClick={() => save.mutate()}>{save.isPending ? "Saving…" : "Save"}</button>
        </div>
      </div>
    </section>
  );
}

function PctInput({ label, value, strong, disabled, onChange }: { label: string; value?: number; strong?: boolean; disabled?: boolean; onChange: (v: number) => void }) {
  return (
    <span className="row nowrap" style={{ gap: 4, flexWrap: "nowrap", minWidth: 96 }}>
      <input className="input" type="number" min={1} max={100} aria-label={label} value={value ?? ""} disabled={disabled}
        style={{ width: 76, fontWeight: strong ? 700 : undefined }} onChange={(e) => onChange(Number(e.target.value))} />%
    </span>
  );
}

const EVENT_LABEL: Record<string, string> = {
  FIRING: "Started", ESCALATED: "Became critical", EASED: "Eased to warning", RESOLVED: "Cleared", CLEARED: "Forgotten", DROPPED: "Dropped (check off or node gone)", DIGEST: "Digest",
};

function History() {
  const [days, setDays] = useState(7);
  const q = useQuery({ queryKey: ["alert-history", days], queryFn: () => get<{ items: AlertEvent[] }>("/admin/alerts/history", { days }).then((r) => r.items) });
  return (
    <section className="card" style={{ overflow: "hidden" }}>
      <div className="card-head">
        <div className="grow"><h2>History</h2><span className="hint">Alerts that started, changed or cleared</span></div>
        <select className="select" aria-label="Period" value={days} onChange={(e) => setDays(Number(e.target.value))} style={{ maxWidth: 160 }}>
          <option value={1}>Today</option><option value={7}>Last 7 days</option><option value={31}>Last 31 days</option>
        </select>
      </div>
      {q.isLoading ? <Loading /> : q.error ? <div className="card-body"><ErrorCallout error={q.error} /></div> : !q.data?.length ? (
        <div className="card-body"><span className="hint">Nothing in this period.</span></div>
      ) : (
        <div className="table-scroll" style={{ maxHeight: 420 }}>
          <table className="table compact">
            <thead><tr><th>When</th><th>Event</th><th>Cluster</th><th>Alert</th></tr></thead>
            <tbody>
              {q.data.map((e, i) => (
                <tr key={`${e.at}-${i}`}>
                  <td className="nowrap">{when(e.at)}</td>
                  <td className="nowrap">{EVENT_LABEL[e.event] ?? e.event}{e.severity && e.event !== "DIGEST" && e.event !== "RESOLVED" && e.event !== "CLEARED" && e.event !== "DROPPED" ? <> · <SeverityBadge s={e.severity} /></> : null}</td>
                  <td className="mono">{e.clusterId}</td>
                  <td>{e.event === "DIGEST" ? e.text : describeAlert({ metric: e.metric as AlertMetric, node: e.node ?? "", value: e.value, threshold: e.threshold ?? undefined, text: e.text ?? undefined })}{e.by ? ` · by ${e.by}` : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function DigestPreview({ onClose }: { onClose: () => void }) {
  const q = useQuery({ queryKey: ["digest-preview"], queryFn: () => get<{ slack: { text: string; attachments: { text: string; color: string }[] } }>("/admin/alerts/digest/_preview"), staleTime: 0 });
  const plain = (t: string) => t.replace(/<([^|>]+)\|([^>]+)>/g, "$2").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
  return (
    <Dialog title="Weekly digest preview" subtitle="What would be posted to Slack right now." onClose={onClose} wide
      footer={<button type="button" className="btn btn-primary" onClick={onClose}>Close</button>}>
      {q.isLoading ? <Loading what="Reading every cluster…" /> : q.error ? <ErrorCallout error={q.error} /> : q.data && (
        <div className="stack-sm">
          <pre className="code-block" style={{ whiteSpace: "pre-wrap" }}>{plain(q.data.slack.text)}</pre>
          {q.data.slack.attachments.map((a, i) => (
            <pre key={i} className="code-block" style={{ whiteSpace: "pre-wrap", borderLeft: `4px solid ${a.color}` }}>{plain(a.text)}</pre>
          ))}
        </div>
      )}
    </Dialog>
  );
}
