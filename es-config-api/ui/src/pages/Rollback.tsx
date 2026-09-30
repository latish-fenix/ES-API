// Roll back any change: from the audit log (config changes, document edits, bulk changes,
// index deletes) and from the Data page's "Recent changes" bar. Every rollback starts with a
// dry run that shows exactly what will change, needs a reason, and is itself a change that
// can be rolled back again.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, enc, get, request, type AuditEvent, type ChangeResult, type Diff, type RecentChange, type RecreatePlan } from "../api";
import { DiffTable } from "../components/DiffTable";
import { Icon } from "../components/icons";
import { Badge, Callout, Dialog, ErrorCallout, Loading, ReasonField, Spinner, useToast } from "../components/ui";
import { CONFIG_TYPE_LABEL, num, when } from "../format";

/** `detail` = who made the change, when and why (shown in the dialog). */
export type RollbackTarget = { detail?: string } & (
  | { kind: "config"; clusterId: string; configType: string; resource: string; changeId: string; label: string }
  | { kind: "doc"; clusterId: string; index: string; id: string; versionKey: string; label: string }
  | { kind: "bulk"; clusterId: string; changeId: string; label: string }
  | { kind: "index"; clusterId: string; key: string; index: string; label: string });

const who = (by: string, at: string, reason?: string | null) => `${by}, ${when(at)}${reason ? ` · “${reason}”` : ""}`;

const CONFIG_ACTIONS = new Set(["UPDATE", "ROLLBACK", "RESTORE"]);
const DOC_ACTIONS: Record<string, string> = {
  DATA_DOC_UPDATE: "edit", DATA_DOC_CREATE: "creation", DATA_DOC_DELETE: "delete", DATA_DOC_RESTORE: "restore",
};
const BULK_ACTIONS: Record<string, string> = {
  DATA_BULK_UPDATE: "bulk update", DATA_BULK_DELETE: "bulk delete", DATA_BULK_RESTORE: "bulk restore",
};

/** What rolling back this audit entry means, or null when it can't be rolled back. */
export function rollbackTargetFromAudit(e: AuditEvent): RollbackTarget | null {
  const t = auditTarget(e);
  return t && { ...t, detail: who(e.actor, e.timestamp, e.reason) };
}

function auditTarget(e: AuditEvent): RollbackTarget | null {
  if (e.outcome !== "SUCCESS" || !e.clusterId) return null;
  const s = (k: string) => (typeof e[k] === "string" ? (e[k] as string) : "");
  if (CONFIG_ACTIONS.has(e.action) && e.configType && e.configType !== "index-mappings" && e.resource && e.changeId) {
    const type = CONFIG_TYPE_LABEL[e.configType] ?? e.configType;
    const what = e.resource === "_cluster" ? type : `${type} · ${e.resource}`;
    return { kind: "config", clusterId: e.clusterId, configType: e.configType, resource: e.resource, changeId: s("changeId"), label: what };
  }
  if (DOC_ACTIONS[e.action] && s("versionKey") && s("documentId") && e.resource) {
    return { kind: "doc", clusterId: e.clusterId, index: e.resource, id: s("documentId"), versionKey: s("versionKey"),
      label: `the ${DOC_ACTIONS[e.action]} of document ${s("documentId")}` };
  }
  if (BULK_ACTIONS[e.action] && s("changeId")) {
    return { kind: "bulk", clusterId: e.clusterId, changeId: s("changeId"), label: `the ${BULK_ACTIONS[e.action]} on ${e.resource ?? ""}` };
  }
  if (e.action === "INDEX_DELETE" && s("tombstoneKey") && e.resource) {
    return { kind: "index", clusterId: e.clusterId, key: s("tombstoneKey"), index: e.resource, label: `the delete of index ${e.resource}` };
  }
  return null;
}

export function rollbackTargetFromRecent(clusterId: string, c: RecentChange): RollbackTarget | null {
  const detail = who(c.by, c.at, c.reason);
  if (c.kind === "doc" && c.versionKey && c.id) {
    const what = { UPDATE: "edit", CREATE: "creation", DELETE: "delete", RESTORE: "restore" }[c.action as "UPDATE"] ?? "change";
    return { kind: "doc", clusterId, index: c.index, id: c.id, versionKey: c.versionKey, label: `the ${what} of document ${c.id}`, detail };
  }
  if (c.kind === "bulk") return { kind: "bulk", clusterId, changeId: c.changeId, label: `the ${c.action.replace("BULK_", "bulk ").toLowerCase()} on ${c.index}`, detail };
  return null;
}

interface DocPlan { plan: "overwrite" | "recreate" | "delete" | "nothing"; diff: Diff; restoresTo: { at: string; by: string; action: string } }
interface BulkPlan { count: number; dryRunToken: string; note: string; op: string; at: string; by: string }

function url(t: RollbackTarget): string {
  const c = `/clusters/${enc(t.clusterId)}`;
  switch (t.kind) {
    case "config": return `${c}/config-history/${enc(t.changeId)}/_restore`;
    case "doc": return `${c}/data/${enc(t.index)}/_doc/${enc(t.id)}/_restore`;
    case "bulk": return `${c}/data/_changes/${enc(t.changeId)}/_restore`;
    case "index": return `${c}/deleted-indices/_recreate`;
  }
}

function body(t: RollbackTarget, reason?: string, extra?: Record<string, unknown>): Record<string, unknown> {
  const r = reason ? { reason } : {};
  switch (t.kind) {
    case "config": return { configType: t.configType, resource: t.resource, ...r };
    case "doc": return { versionKey: t.versionKey, ...r };
    case "bulk": return { ...r, ...extra };
    case "index": return { key: t.key, ...r };
  }
}

const emptyDiff = (d?: Diff) => !d || (!d.added.length && !d.removed.length && !d.changed.length);

export function RollbackDialog({ target, onClose, onDone }: { target: RollbackTarget; onClose: () => void; onDone?: () => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [reason, setReason] = useState("");
  const [typed, setTyped] = useState("");
  const dry = useQuery({
    queryKey: ["rollback-dry", target],
    queryFn: async () => (await request<ChangeResult & DocPlan & BulkPlan & RecreatePlan>(url(target), { method: "POST", query: { dryRun: true }, body: body(target) })).data,
    retry: false,
    gcTime: 0,
  });
  const plan = dry.data;
  const run = useMutation({
    mutationFn: async () => (await request<Record<string, unknown>>(url(target), {
      method: "POST",
      body: body(target, reason, target.kind === "bulk" ? { dryRunToken: plan?.dryRunToken, expectedCount: plan?.count } : undefined),
    })).data,
    onSuccess: () => {
      // config, documents, indices and the audit log may all have changed (not this dry run: it is done)
      qc.invalidateQueries({ predicate: (q) => q.queryKey[0] !== "rollback-dry" });
      toast(target.kind === "index" ? `Recreated ${target.index} (empty)` : "Rolled back. The rollback is in the audit log and can be undone too.");
      onDone?.();
      onClose();
    },
  });

  const nothing = plan && ((target.kind === "config" && (plan.noChange || (emptyDiff(plan.diff) && !plan.createsResource && !plan.deletesResource)))
    || (target.kind === "doc" && plan.plan === "nothing") || (target.kind === "bulk" && plan.count === 0));
  const needCount = target.kind === "bulk" && !!plan && plan.count > 0;
  const ready = !!plan && !nothing && !!reason.trim() && (!needCount || typed === String(plan.count));
  const stale = run.error instanceof ApiError && ["DRY_RUN_EXPIRED", "DRY_RUN_MISMATCH", "DOCUMENT_CHANGED", "VERSION_MISMATCH"].includes(run.error.code);

  return (
    <Dialog wide busy={run.isPending} onClose={onClose}
      title={target.kind === "index" ? <>Recreate <span className="mono">{target.index}</span></> : "Roll back"}
      subtitle={<>Undo {target.label}{target.detail ? <> ({target.detail})</> : null}. Nothing changes until you confirm.</>}
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={run.isPending}>Cancel</button>
        {plan && !nothing && (
          <button type="button" className="btn btn-primary" disabled={!ready || run.isPending} onClick={() => run.mutate()}>
            <Icon name="undo" /> {run.isPending ? "Working…" : target.kind === "index" ? "Recreate empty index" : "Roll back"}
          </button>
        )}
      </>}>
      {dry.isLoading ? <Loading what="Dry run: working out what would change…" /> : dry.error ? <ErrorCallout error={dry.error} clusterId={target.clusterId} /> : plan && (
        <div className="stack">
          {target.kind === "config" && <ConfigPlan r={plan} />}
          {target.kind === "doc" && <DocPlanView r={plan} id={target.id} />}
          {target.kind === "bulk" && (
            <Callout tone={plan.count ? "warn" : "neutral"} icon="undo"
              title={plan.count ? `${num(plan.count)} document${plan.count === 1 ? "" : "s"} will be put back as they were before this change` : "Nothing to put back"}>
              {plan.note}
            </Callout>
          )}
          {target.kind === "index" && <RecreateView r={plan} />}
          {nothing ? (
            <Callout icon="info" title="Nothing to roll back">It is already in the state from before that change.</Callout>
          ) : (
            <>
              <ReasonField value={reason} onChange={setReason} placeholder="Why roll it back?" autoFocus />
              {needCount && (
                <div className="field" style={{ maxWidth: 340 }}>
                  <label className="label" htmlFor="rb-typed">To confirm, type the number of documents: <code>{plan.count}</code></label>
                  <input id="rb-typed" className="input mono" inputMode="numeric" value={typed} onChange={(e) => setTyped(e.target.value.trim())} autoComplete="off" />
                </div>
              )}
            </>
          )}
          {run.error && (stale ? (
            <Callout tone="warn" icon="refresh" title="It changed since the dry run">{(run.error as ApiError).message}
              <div style={{ marginTop: 8 }}><button type="button" className="btn btn-sm" onClick={() => { run.reset(); setTyped(""); dry.refetch(); }}>Dry run again</button></div>
            </Callout>
          ) : <ErrorCallout error={run.error} clusterId={target.clusterId} />)}
        </div>
      )}
    </Dialog>
  );
}

function ConfigPlan({ r }: { r: ChangeResult }) {
  return (
    <>
      {r.deletesResource && <Callout tone="warn" icon="warn" title="This removes it">It didn't exist before that change, so rolling back deletes it.</Callout>}
      {r.createsResource && <Callout tone="warn" icon="info" title="This creates it again">It was deleted after that change.</Callout>}
      {r.warnings.map((w) => <Callout key={w} tone="warn" icon="warn">{w}</Callout>)}
      {!emptyDiff(r.diff) && (
        <div className="stack-sm">
          <span className="label">What will change</span>
          <DiffTable diff={r.diff} absent="not set" keyLabel="Path" />
        </div>
      )}
    </>
  );
}

function DocPlanView({ r, id }: { r: DocPlan; id: string }) {
  const text = { overwrite: `Document ${id} will be put back as it was`, recreate: `Document ${id} will be recreated`,
    delete: `Document ${id} will be deleted (it didn't exist before that change)`, nothing: "" }[r.plan];
  return (
    <>
      {r.plan !== "nothing" && (
        <Callout tone={r.plan === "delete" ? "warn" : "neutral"} icon="undo" title={text}>
          Back to the version from before the {r.restoresTo.action.toLowerCase()} by {r.restoresTo.by}, {when(r.restoresTo.at)}. The current version is kept, so this can be undone too.
        </Callout>
      )}
      {!emptyDiff(r.diff) && (
        <div className="stack-sm">
          <span className="label">What will change</span>
          <DiffTable diff={r.diff} absent="not set" keyLabel="Field" />
        </div>
      )}
    </>
  );
}

function RecreateView({ r }: { r: RecreatePlan }) {
  return (
    <>
      <Callout tone="warn" icon="warn" title="The index comes back empty">
        {r.docsLost ? `Its ${num(r.docsLost)} document${r.docsLost === 1 ? " was" : "s were"} deleted and can't come back. ` : ""}Deleted by {r.deletedBy}, {when(r.deletedAt)}. Reindex or reload the data from its source afterwards.
      </Callout>
      {r.warnings.slice(1).map((w) => <Callout key={w} tone="warn" icon="warn">{w}</Callout>)}
      <details className="sample" open>
        <summary className="label">Settings, mappings and aliases it gets</summary>
        <pre className="code-block" style={{ maxHeight: 260 }}>{JSON.stringify(r.body, null, 2)}</pre>
      </details>
    </>
  );
}

// ------------------------------------------------------------------ Data page: recent changes

const ACTION_TEXT: Record<RecentChange["action"], string> = {
  UPDATE: "Edited", CREATE: "Added", DELETE: "Deleted", RESTORE: "Restored",
  BULK_UPDATE: "Bulk update", BULK_DELETE: "Bulk delete", BULK_RESTORE: "Bulk restore",
};

function fieldsText(f: RecentChange["fields"]): string {
  if (!f) return "";
  if (Array.isArray(f)) return f.slice(0, 4).join(", ") + (f.length > 4 ? ` +${f.length - 4}` : "");
  return [...f.set.map((x) => `set ${x}`), ...f.remove.map((x) => `remove ${x}`)].join(", ");
}

/** The strip at the top of the Data page: newest changes on this index/pattern, each with Roll back. */
export function RecentChanges({ clusterId, index }: { clusterId: string; index: string }) {
  const [open, setOpen] = useState(false);
  const [pick, setPick] = useState<RollbackTarget | null>(null);
  const q = useQuery({
    queryKey: ["data-recent", clusterId, index],
    queryFn: () => get<{ items: RecentChange[] }>(`/clusters/${enc(clusterId)}/data/${enc(index)}/_recent`, { limit: 10 }).then((r) => r.items),
    enabled: !!index,
    retry: false,
  });
  const items = q.data ?? [];
  if (!index || q.isError || (!q.isLoading && items.length === 0)) return null;
  const shown = open ? items : items.slice(0, 3);
  return (
    <section className="card recent-changes" aria-label="Recent changes">
      <div className="recent-head">
        <Icon name="undo" size={16} />
        <strong>Recent changes</strong>
        <span className="hint">on <span className="mono">{index}</span>, newest first. Roll back puts the documents back as they were before that change.</span>
        <div className="grow" />
        {q.isFetching && <Spinner label="Loading" />}
        {items.length > 3 && <button type="button" className="btn btn-ghost btn-sm" onClick={() => setOpen(!open)}>{open ? "Show less" : `Show all ${items.length}`}</button>}
      </div>
      {q.isLoading ? <Loading /> : (
        <ul className="recent-list">
          {shown.map((c) => {
            const t = rollbackTargetFromRecent(clusterId, c);
            return (
              <li key={`${c.changeId}/${c.index}`}>
                <Badge tone={c.action.includes("DELETE") ? "red" : c.action.includes("RESTORE") ? "blue" : undefined}>{ACTION_TEXT[c.action] ?? c.action}</Badge>
                <span className="recent-what">
                  {c.kind === "doc" ? <span className="mono">{c.id}</span> : <>{num(c.count ?? 0)} documents</>}
                  {fieldsText(c.fields) && <span className="hint mono"> · {fieldsText(c.fields)}</span>}
                  {c.reason && <span className="hint"> · “{c.reason}”</span>}
                </span>
                <span className="hint recent-who">{c.by} · {when(c.at)}</span>
                {c.rolledBack ? <Badge>Rolled back</Badge> : c.canRollBack && t ? (
                  <button type="button" className="btn btn-sm" onClick={() => setPick(t)}><Icon name="undo" size={14} /> Roll back</button>
                ) : <span />}
              </li>
            );
          })}
        </ul>
      )}
      {pick && <RollbackDialog target={pick} onClose={() => setPick(null)} />}
    </section>
  );
}
