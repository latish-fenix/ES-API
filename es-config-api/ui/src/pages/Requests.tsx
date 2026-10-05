import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { enc, get, request, type ApprovalRequest, type ApprovalStatus, type Diff } from "../api";
import { DiffTable } from "../components/DiffTable";
import { Icon } from "../components/icons";
import { Page } from "../components/Shell";
import { Badge, Callout, Dialog, Empty, ErrorCallout, Loading, Spinner, useToast } from "../components/ui";
import { ago, bytes, num, pretty, when } from "../format";
import { useClusters, useMe } from "../session";

const STATUS: Record<ApprovalStatus, { label: string; tone?: "blue" | "green" | "amber" | "red" }> = {
  PENDING: { label: "Waiting for approval", tone: "amber" },
  APPLYING: { label: "Applying", tone: "blue" },
  APPLIED: { label: "Approved · applied", tone: "green" },
  FAILED: { label: "Approved · failed", tone: "red" },
  REJECTED: { label: "Rejected", tone: "red" },
  CANCELLED: { label: "Cancelled" },
  EXPIRED: { label: "Expired" },
  OUTDATED: { label: "Outdated · not applied", tone: "amber" },
};

export function StatusBadge({ status }: { status: ApprovalStatus }) {
  const s = STATUS[status] ?? { label: status };
  return <Badge tone={s.tone}>{s.label}</Badge>;
}

function details(r: ApprovalRequest): string {
  const s = r.summary ?? {};
  const bits: string[] = [];
  if (s.count !== undefined && s.count !== null) bits.push(`${num(s.count)} document${s.count === 1 ? "" : "s"}`);
  if (s.docs) bits.push(`${num(s.docs)} documents deleted`);
  if (s.fields?.length) bits.push(s.fields.slice(0, 4).join(", ") + (s.fields.length > 4 ? ` +${s.fields.length - 4}` : ""));
  return bits.join(" · ");
}

const EVENT: Record<string, string> = {
  REQUESTED: "Requested", APPLYING: "Approved", APPLIED: "Applied", FAILED: "Failed", REJECTED: "Rejected",
  CANCELLED: "Cancelled", EXPIRED: "Expired", OUTDATED: "Outdated (not applied)",
};

type Tab = "pending" | "all" | "mine";
const STATUSES: ApprovalStatus[] = ["PENDING", "APPLIED", "REJECTED", "FAILED", "OUTDATED", "CANCELLED", "EXPIRED"];

export function Requests() {
  const me = useMe().data!;
  const clusters = useClusters().data ?? [];
  const [params, setParams] = useSearchParams();
  const tab: Tab = me.admin ? ((params.get("tab") as Tab) || "pending") : "mine";
  const status = params.get("status") ?? "";
  const clusterId = params.get("clusterId") ?? "";
  const requestedBy = params.get("requestedBy") ?? "";
  const set = (k: string, v: string) => {
    const n = new URLSearchParams(params);
    if (v) n.set(k, v);
    else n.delete(k);
    setParams(n, { replace: true });
  };
  const list = useQuery({
    queryKey: ["approvals", "list", tab, status, clusterId, requestedBy],
    queryFn: () => get<{ items: ApprovalRequest[] }>("/approvals", {
      scope: tab, status: tab === "pending" ? undefined : status, clusterId, requestedBy: tab === "all" ? requestedBy : undefined,
    }).then((r) => r.items),
    refetchInterval: 30_000,
  });
  const items = list.data ?? [];

  return (
    <Page crumbs={[{ label: "Requests" }]} title="Requests">
      <div className="page-head" style={{ alignItems: "center" }}>
        <div className="stack-sm grow">
          <h1>Requests</h1>
          <span className="sub">
            {me.admin
              ? "Changes made by other users wait here until an admin approves them. Approving runs the change as the requester, after checking it is still the same change."
              : me.approvalsRequired
                ? "Your changes are sent to the admins and applied when one of them approves. You get an email when that happens."
                : "Requests you have sent for approval."}
          </span>
        </div>
        {me.admin && (
          <div className="tabs" role="tablist" aria-label="Request lists">
            <button type="button" role="tab" className="tab" aria-selected={tab === "pending"} onClick={() => setParams({ tab: "pending" })}>Waiting for approval</button>
            <button type="button" role="tab" className="tab" aria-selected={tab === "all"} onClick={() => setParams({ tab: "all" })}>All requests</button>
          </div>
        )}
      </div>

      {tab !== "pending" && (
        <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
          <label className="field" style={{ minWidth: 200 }}>
            <span className="label">Status</span>
            <select className="select" value={status} onChange={(e) => set("status", e.target.value)}>
              <option value="">Any status</option>
              {STATUSES.map((s) => <option key={s} value={s}>{STATUS[s].label}</option>)}
            </select>
          </label>
          <label className="field" style={{ minWidth: 180 }}>
            <span className="label">Cluster</span>
            <select className="select" value={clusterId} onChange={(e) => set("clusterId", e.target.value)}>
              <option value="">All clusters</option>
              {clusters.map((c) => <option key={c.id} value={c.id}>{c.id}</option>)}
            </select>
          </label>
          {tab === "all" && (
            <label className="field" style={{ minWidth: 260 }}>
              <span className="label">Requested by</span>
              <input className="input" value={requestedBy} placeholder="name@fenixcommerce.com" onChange={(e) => set("requestedBy", e.target.value.trim())} />
            </label>
          )}
        </div>
      )}

      <section className="card" style={{ overflow: "hidden" }}>
        {list.isLoading ? <Loading what="Loading requests…" /> : list.error ? <div className="card-body"><ErrorCallout error={list.error} /></div> : items.length === 0 ? (
          <Empty title={tab === "pending" ? "Nothing is waiting for approval" : "No requests"}>
            {tab === "mine" && me.approvalsRequired ? "When you apply a change, it shows up here until an admin approves it." : null}
          </Empty>
        ) : (
          <div className="table-scroll" style={{ maxHeight: "calc(100vh - 280px)" }}>
            <table className="table">
              <thead>
                <tr>
                  <th>Change</th>
                  <th>{tab === "mine" ? "Requested" : "Requested by"}</th>
                  <th>Details</th>
                  <th>Status</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {items.map((r) => (
                  <tr key={r.id}>
                    <td style={{ maxWidth: 420 }}>
                      <div>{r.label}</div>
                      <span className="req-res" title={`${r.clusterId} · ${r.resource}`}>{r.clusterId} · {r.resource}</span>
                    </td>
                    <td>
                      {tab !== "mine" && <span className="req-by" title={r.requestedBy}>{r.requestedBy}</span>}
                      <span className={tab !== "mine" ? "hint" : ""} title={when(r.requestedAt)}>{ago(r.requestedAt)}</span>
                    </td>
                    <td className="hint" style={{ maxWidth: 220 }}>{details(r)}</td>
                    <td className="nowrap">
                      <StatusBadge status={r.status} />
                      {r.status === "PENDING" && <div className="hint">expires {when(r.expiresAt).replace(" UTC", "")}</div>}
                    </td>
                    <td style={{ textAlign: "right" }}>
                      <Link className={`btn btn-sm ${r.canApprove ? "btn-primary" : "btn-ghost"}`} to={`/requests/${enc(r.id)}`}>{r.canApprove ? "Review" : "Open"}</Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </Page>
  );
}

// ------------------------------------------------------------------ one request

function isDiff(d: unknown): d is Diff {
  return !!d && typeof d === "object" && Array.isArray((d as Diff).added) && Array.isArray((d as Diff).changed);
}

function Json({ value, max = 360 }: { value: unknown; max?: number }) {
  return <pre className="code-block" style={{ maxHeight: max, overflow: "auto", margin: 0 }}>{pretty(value)}</pre>;
}

/** What the change does, from its dry run (shown to the requester and the admins only). */
export function ChangePreview({ r, preview }: { r: Pick<ApprovalRequest, "op" | "kind" | "body">; preview: Record<string, unknown> }) {
  const p = preview as Record<string, any>;
  const warnings: string[] = Array.isArray(p.warnings) ? p.warnings : [];
  const body = r.body ?? {};
  let main: ReactNode = null;
  if (r.op.startsWith("bulk.") && r.op !== "bulk.restore") {
    const sample = (p.sample ?? []) as { _index: string; _id: string; diff?: Diff; source?: unknown }[];
    main = (
      <div className="stack">
        <div className="kv">
          <div><span className="k">Matching documents</span><span className="v">{num(p.count)}</span></div>
          <div><span className="k">Will change</span><span className="v">{num(p.willChange)}</span></div>
          {p.unchanged !== undefined && <div><span className="k">Already as asked</span><span className="v">{num(p.unchanged)}</span></div>}
          <div><span className="k">Indices</span><span className="v" title={(p.indices ?? []).join(", ")}>{(p.indices ?? []).length}</span></div>
        </div>
        <div className="kv">
          <div><span className="k">Query</span><span className="v mono">{typeof body.query === "object" && body.query ? JSON.stringify(body.query) : String(body.query || (body.dsl ? JSON.stringify(body.dsl) : "(all documents)"))}</span></div>
          {r.op === "bulk.update" && <div><span className="k">Set</span><span className="v mono">{Object.keys((body.set as object) ?? {}).join(", ") || "—"}</span></div>}
          {r.op === "bulk.update" && <div><span className="k">Remove</span><span className="v mono">{((body.remove as string[]) ?? []).join(", ") || "—"}</span></div>}
        </div>
        {sample.length > 0 && (
          <div className="stack-sm">
            <span className="label">Sample ({sample.length} of {num(p.willChange)})</span>
            {sample.map((s) => (
              <div key={`${s._index}/${s._id}`} className="stack-sm">
                <span className="hint mono">{s._index} / {s._id}</span>
                {s.diff ? <DiffTable diff={s.diff} keyLabel="Field" absent="missing" /> : <Json value={s.source} max={160} />}
              </div>
            ))}
          </div>
        )}
      </div>
    );
  } else if (r.op === "bulk.restore") {
    main = <div className="kv">
      <div><span className="k">Documents put back</span><span className="v">{num(p.count)}</span></div>
      <div><span className="k">Undoes</span><span className="v">bulk {String(p.op)} by {String(p.by)}</span></div>
      <div><span className="k">Made</span><span className="v">{when(p.at)}</span></div>
    </div>;
  } else if (r.op === "index.delete") {
    main = <div className="kv">
      <div><span className="k">Documents deleted</span><span className="v">{num(p.docsCount)}</span></div>
      <div><span className="k">Size</span><span className="v">{bytes(p.storeSizeBytes)}</span></div>
      <div><span className="k">Shards</span><span className="v">{num(p.primaryShards)} × {num((p.replicas ?? 0) + 1)}</span></div>
      <div><span className="k">Aliases</span><span className="v">{(p.aliases ?? []).join(", ") || "none"}</span></div>
    </div>;
  } else if (r.op === "index.create") {
    main = <div className="stack-sm">
      {p.template && <span className="hint">Index template <b className="mono">{p.template.name}</b> also applies; its settings and mappings are included.</span>}
      <Json value={p.result} />
    </div>;
  } else if (r.op === "index.recreate") {
    main = <div className="stack-sm"><span className="hint">Recreated empty ({num(p.docsLost)} documents were lost when it was deleted).</span><Json value={p.body} /></div>;
  } else if (r.op === "doc.delete") {
    main = <div className="stack-sm"><span className="hint">This document is deleted (a copy is kept, so it can be undone):</span><Json value={p.source} /></div>;
  } else if (isDiff(p.diff)) {
    main = <DiffTable diff={p.diff} keyLabel={r.kind === "doc" ? "Field" : "Setting"} absent={r.kind === "doc" ? "missing" : "not set"} />;
  } else if (r.op === "index.undo_create") {
    main = <span>The empty index <b className="mono">{String(p.index)}</b> is deleted; its definition is kept so it can be recreated.</span>;
  } else {
    main = <Json value={p} />;
  }
  return (
    <div className="stack">
      {warnings.map((w) => <Callout key={w} tone="warn">{w}</Callout>)}
      {main}
    </div>
  );
}

function DecideDialog({ r, mode, onClose }: { r: ApprovalRequest; mode: "approve" | "reject"; onClose: () => void }) {
  const [comment, setComment] = useState("");
  const qc = useQueryClient();
  const toast = useToast();
  const run = useMutation({
    mutationFn: async () => (await request<ApprovalRequest>(`/approvals/${enc(r.id)}/_${mode}`, { method: "POST", body: { comment: comment.trim() || undefined } })).data,
    onSuccess: (out) => {
      toast(mode === "approve" ? (out.status === "APPLIED" ? "Approved and applied" : `Approved: ${out.status.toLowerCase()}`) : "Rejected; the requester is emailed");
      qc.invalidateQueries({ queryKey: ["approvals"] });
      onClose();
    },
    onError: () => qc.invalidateQueries({ queryKey: ["approvals"] }),
  });
  const ok = comment.trim().length > 0;
  return (
    <Dialog title={mode === "approve" ? "Approve and apply" : "Reject request"} subtitle={`${r.label} · ${r.resource}`} onClose={onClose} busy={run.isPending}
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={run.isPending}>Cancel</button>
        <button type="button" className={`btn ${mode === "approve" ? "btn-primary" : "btn-danger"}`} disabled={!ok || run.isPending} onClick={() => run.mutate()}>
          {run.isPending ? <Spinner label="Working" /> : null} {mode === "approve" ? "Approve and apply" : "Reject"}
        </button>
      </>}>
      <div className="stack">
        {mode === "approve" ? (
          <span>The dry run is run again first, as <b>{r.requestedBy}</b>. If the result differs from what was requested (someone changed it meanwhile), nothing is applied and the request is closed as outdated. Otherwise the change runs now, with the usual snapshot, backup and audit.</span>
        ) : (
          <span>Nothing is changed. <b>{r.requestedBy}</b> gets an email with your comment.</span>
        )}
        <label className="field">
          <span className="label">Comment (required)</span>
          <textarea className="textarea" rows={3} value={comment} onChange={(e) => setComment(e.target.value)} placeholder={mode === "reject" ? "Why, and what to do instead" : "Why it is approved, e.g. OK for today's import"} autoFocus />
        </label>
        <ErrorCallout error={run.error} />
      </div>
    </Dialog>
  );
}

export function RequestDetail() {
  const { requestId = "" } = useParams();
  const me = useMe().data!;
  const qc = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const [deciding, setDeciding] = useState<"approve" | "reject" | null>(null);
  const q = useQuery({ queryKey: ["approvals", "one", requestId], queryFn: () => get<ApprovalRequest>(`/approvals/${enc(requestId)}`) });
  const recheck = useMutation({
    mutationFn: async () => (await request<{ upToDate: boolean; preview?: Record<string, unknown>; error?: { code: string; message: string } }>(`/approvals/${enc(requestId)}/_recheck`, { method: "POST" })).data,
  });
  const cancel = useMutation({
    mutationFn: () => request(`/approvals/${enc(requestId)}/_cancel`, { method: "POST" }),
    onSuccess: () => {
      toast("Request cancelled");
      qc.invalidateQueries({ queryKey: ["approvals"] });
    },
  });
  const r = q.data;
  return (
    <Page crumbs={[{ label: "Requests", to: "/requests" }, { label: r ? r.label : "Request" }]} title={r ? `${r.label} · request` : "Request"}>
      {q.isLoading ? <Loading /> : q.error ? <ErrorCallout error={q.error} /> : r && (
        <>
          <div className="page-head" style={{ alignItems: "center" }}>
            <div className="stack-sm grow">
              <h1>{r.label} <span className="mono" style={{ fontWeight: 500 }}>{r.resource}</span></h1>
              <span className="sub">Requested by {r.requestedBy} on {r.clusterId}, {when(r.requestedAt)} ({ago(r.requestedAt)})</span>
            </div>
            <StatusBadge status={r.status} />
          </div>

          {r.status === "PENDING" && !me.admin && (
            <Callout tone="info" icon="inbox" title="Waiting for an admin">Nothing has changed yet. The admins were emailed; you get an email when it is approved or rejected. It expires {when(r.expiresAt)}.</Callout>
          )}
          {r.status === "OUTDATED" && (
            <Callout tone="warn" title="Not applied: it changed since the request">{r.error?.message} Run the dry run again and send a new request if it is still needed.</Callout>
          )}
          {r.status === "FAILED" && <Callout tone="danger" title="Approved, but applying it failed">{r.error?.message} <span className="meta">{r.error?.code}</span></Callout>}
          {r.status === "REJECTED" && <Callout tone="danger" title={`Rejected by ${r.decidedBy}`}>{r.comment}</Callout>}
          {r.status === "APPLIED" && (
            <Callout tone="success" title={`Approved by ${r.decidedBy} and applied`}>
              {when(r.finishedAt ?? r.decidedAt)}{r.comment ? ` · “${r.comment}”` : ""}
              {r.result?.changeId && <> · change <span className="mono">{r.result.changeId.slice(0, 12)}</span></>}
              {r.result?.succeeded !== undefined && <> · {num(r.result.succeeded)} changed{r.result.conflicts ? `, ${num(r.result.conflicts)} conflicts` : ""}{r.result.failed ? `, ${num(r.result.failed)} failed` : ""}</>}
            </Callout>
          )}

          <div className="grid-side" style={{ alignItems: "start" }}>
            <section className="card">
              <div className="card-head"><div className="grow"><h2>What changes</h2><span className="sub">From the dry run when it was requested</span></div>
                {me.admin && r.status === "PENDING" && (
                  <button type="button" className="btn btn-sm" onClick={() => recheck.mutate()} disabled={recheck.isPending}>
                    {recheck.isPending ? <Spinner label="Checking" /> : <Icon name="refresh" size={15} />} Check again now
                  </button>
                )}
              </div>
              <div className="card-body stack">
                {r.status === "PENDING" && recheck.data && (recheck.data.error
                  ? <Callout tone="danger" title="The dry run now fails">{recheck.data.error.message}</Callout>
                  : recheck.data.upToDate
                    ? <Callout tone="success" title="Still the same change">The dry run, run now as {r.requestedBy}, gives the same result.</Callout>
                    : <Callout tone="warn" title="It changed since the request">Approving will not apply it. The dry run now gives a different result; reject it and ask for a new request.</Callout>)}
                <ErrorCallout error={recheck.error} />
                {r.preview && <ChangePreview r={r} preview={r.preview} />}
                {r.previewNow && (
                  <div className="stack-sm">
                    <span className="label">The dry run when it was approved</span>
                    <ChangePreview r={r} preview={r.previewNow} />
                  </div>
                )}
              </div>
            </section>

            <div className="stack">
              <section className="card">
                <div className="card-head"><h2>Request</h2></div>
                <div className="card-body">
                  <dl className="detail-list">
                    <dt>Reason</dt><dd>{r.reason}</dd>
                    <dt>Cluster</dt><dd className="mono">{r.clusterId}</dd>
                    <dt>Resource</dt><dd className="mono" style={{ wordBreak: "break-all" }}>{r.resource}</dd>
                    <dt>Requested</dt><dd>{when(r.requestedAt)}</dd>
                    {r.status === "PENDING" && <><dt>Expires</dt><dd>{when(r.expiresAt)}</dd></>}
                    {r.decidedBy && r.status !== "PENDING" && <><dt>{r.status === "CANCELLED" ? "Cancelled by" : r.status === "EXPIRED" ? "Closed" : "Decided by"}</dt><dd>{r.decidedBy} · {when(r.decidedAt)}</dd></>}
                    <dt>Request id</dt><dd className="mono">{r.id}</dd>
                  </dl>
                </div>
                {(r.canApprove || r.canCancel) && (
                  <div className="card-foot row" style={{ gap: 8, justifyContent: "flex-end" }}>
                    {r.canCancel && <button type="button" className="btn" disabled={cancel.isPending} onClick={() => cancel.mutate()}>Cancel request</button>}
                    {r.canApprove && <button type="button" className="btn btn-danger" onClick={() => setDeciding("reject")}>Reject</button>}
                    {r.canApprove && <button type="button" className="btn btn-primary" onClick={() => setDeciding("approve")}><Icon name="check" /> Approve and apply</button>}
                  </div>
                )}
                {me.admin && r.requestedBy === me.username && r.status === "PENDING" && (
                  <div className="card-foot hint">Another admin has to approve your own request.</div>
                )}
                <ErrorCallout error={cancel.error} />
              </section>

              <section className="card">
                <div className="card-head"><h2>History</h2></div>
                <div className="card-body">
                  <ol className="timeline">
                    {(r.events ?? []).map((e, i) => (
                      <li key={i}><b>{EVENT[e.event] ?? e.event.toLowerCase()}</b> · {e.by} · <span className="hint">{when(e.at)}</span>{e.comment ? <div className="hint">“{e.comment}”</div> : null}</li>
                    ))}
                  </ol>
                  {me.admin && r.mail && (
                    <div className="hint" style={{ marginTop: 10 }}>
                      {Object.entries(r.mail).map(([who, m]) => (
                        <div key={who}>
                          Email to {who}: {m.skipped ? m.skipped : `${m.sent.length} sent`}
                          {m.failed.length ? <span style={{ color: "var(--danger)" }}>, {m.failed.length} failed ({m.failed.map((f) => f.to).join(", ")}: {m.failed[0].error})</span> : null}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </section>
            </div>
          </div>
          <div><button type="button" className="btn btn-ghost" onClick={() => navigate("/requests")}>← All requests</button></div>
          {deciding && <DecideDialog r={r} mode={deciding} onClose={() => setDeciding(null)} />}
        </>
      )}
    </Page>
  );
}
