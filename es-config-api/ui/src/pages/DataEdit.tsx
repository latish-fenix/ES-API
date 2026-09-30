// Document writes on the Data page: edit / create / delete one document, its saved versions,
// and bulk update / delete with a mandatory dry run. Every write needs a reason and keeps the
// previous state in S3 so it can be undone.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { ApiError, enc, get, request, type BulkChange, type BulkPreview, type DataHit, type DataSearchBody, type Diff, type DocVersion } from "../api";
import { DiffTable } from "../components/DiffTable";
import { Icon } from "../components/icons";
import { JsonEditor, parseJson } from "../components/JsonEditor";
import { Badge, Callout, Dialog, Empty, ErrorCallout, Loading, ReasonField, Spinner, useToast } from "../components/ui";
import { num, when } from "../format";

const pretty = (v: unknown) => JSON.stringify(v, null, 2);
const emptyDiff = (d: Diff) => !d.added.length && !d.removed.length && !d.changed.length;

function useInvalidateData(clusterId: string) {
  const qc = useQueryClient();
  return () => {
    qc.invalidateQueries({ queryKey: ["data-search", clusterId] });
    qc.invalidateQueries({ queryKey: ["data-fields", clusterId] });
    qc.invalidateQueries({ queryKey: ["indices", clusterId] });
    qc.invalidateQueries({ queryKey: ["doc-history", clusterId] });
    qc.invalidateQueries({ queryKey: ["bulk-changes", clusterId] });
    qc.invalidateQueries({ queryKey: ["data-recent", clusterId] });
  };
}

// ------------------------------------------------------------------ edit one document

export function EditDoc({ clusterId, hit, onDone, onCancel }: { clusterId: string; hit: DataHit; onDone: () => void; onCancel: () => void }) {
  const toast = useToast();
  const invalidate = useInvalidateData(clusterId);
  const [text, setText] = useState(pretty(hit._source));
  const [preview, setPreview] = useState<Diff | null>(null);
  const [reason, setReason] = useState("");
  const [doc, jsonError] = parseJson(text);
  const url = `/clusters/${enc(clusterId)}/data/${enc(hit._index)}/_doc/${enc(hit._id)}`;
  const body = (dry: boolean) => ({ document: doc, reason: dry ? undefined : reason, ifSeqNo: hit._seq_no, ifPrimaryTerm: hit._primary_term });
  const dry = useMutation({
    mutationFn: async () => (await request<{ diff: Diff; noChange: boolean }>(url, { method: "PUT", query: { dryRun: true }, body: body(true) })).data,
    onSuccess: (r) => setPreview(r.diff),
  });
  const save = useMutation({
    mutationFn: async () => (await request<{ applied: boolean }>(url, { method: "PUT", body: body(false) })).data,
    onSuccess: () => { invalidate(); toast(`Saved ${hit._id}. The previous version is in its history.`); onDone(); },
  });
  const isObj = doc !== null && typeof doc === "object" && !Array.isArray(doc);
  return (
    <div className="stack">
      <JsonEditor label={`Document ${hit._id}`} value={text} onChange={(v) => { setText(v); setPreview(null); }} height={preview ? "20vh" : "42vh"} />
      {jsonError ? <span className="hint" style={{ color: "var(--danger)" }}>{jsonError}</span> : !isObj ? <span className="hint" style={{ color: "var(--danger)" }}>A document must be a JSON object</span> : null}
      {dry.error && <ErrorCallout error={dry.error} />}
      {preview && (emptyDiff(preview) ? <Callout icon="info">No changes compared with the saved document.</Callout> : (
        <div className="stack-sm">
          <span className="label">What will change</span>
          <DiffTable diff={preview} absent="not set" keyLabel="Field" />
          <ReasonField value={reason} onChange={setReason} placeholder="e.g. Fix wrong carrier for order SP0286376220" autoFocus />
          {save.error && <ErrorCallout error={save.error} />}
        </div>
      ))}
      <div className="row" style={{ gap: 8, justifyContent: "flex-end" }}>
        <button type="button" className="btn" onClick={onCancel} disabled={save.isPending}>Cancel</button>
        {!preview || emptyDiff(preview) ? (
          <button type="button" className="btn btn-primary" disabled={!isObj || dry.isPending} onClick={() => dry.mutate()}>{dry.isPending ? <Spinner label="Checking" /> : <Icon name="eye" />} Preview changes</button>
        ) : (
          <button type="button" className="btn btn-primary" disabled={!reason.trim() || save.isPending} onClick={() => save.mutate()}>{save.isPending ? "Saving…" : "Save document"}</button>
        )}
      </div>
    </div>
  );
}

export function DeleteDoc({ clusterId, hit, onDone, onCancel }: { clusterId: string; hit: DataHit; onDone: () => void; onCancel: () => void }) {
  const toast = useToast();
  const invalidate = useInvalidateData(clusterId);
  const [confirm, setConfirm] = useState("");
  const [reason, setReason] = useState("");
  const del = useMutation({
    mutationFn: () => request(`/clusters/${enc(clusterId)}/data/${enc(hit._index)}/_doc/${enc(hit._id)}`, { method: "DELETE", query: { confirm, reason } }),
    onSuccess: () => { invalidate(); toast(`Deleted ${hit._id}. It can be restored from its history.`); onDone(); },
  });
  return (
    <div className="stack">
      <Callout tone="danger" icon="trash" title="Delete this document?">
        A copy is saved first, so an editor can put it back from the document's history. Type the id to confirm.
      </Callout>
      <div className="field">
        <label className="label" htmlFor="del-confirm">Document id <span className="hint">· type <code>{hit._id}</code></span></label>
        <input id="del-confirm" className="input mono" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="off" spellCheck={false} />
      </div>
      <ReasonField value={reason} onChange={setReason} placeholder="Why is it being deleted?" />
      {del.error && <ErrorCallout error={del.error} />}
      <div className="row" style={{ gap: 8, justifyContent: "flex-end" }}>
        <button type="button" className="btn" onClick={onCancel} disabled={del.isPending}>Keep</button>
        <button type="button" className="btn btn-danger" disabled={confirm !== hit._id || !reason.trim() || del.isPending} onClick={() => del.mutate()}>{del.isPending ? "Deleting…" : "Delete document"}</button>
      </div>
    </div>
  );
}

export function DocHistory({ clusterId, hit, canEdit, onRestored }: { clusterId: string; hit: DataHit; canEdit: boolean; onRestored: () => void }) {
  const toast = useToast();
  const invalidate = useInvalidateData(clusterId);
  const base = `/clusters/${enc(clusterId)}/data/${enc(hit._index)}/_doc/${enc(hit._id)}`;
  const q = useQuery({ queryKey: ["doc-history", clusterId, hit._index, hit._id], queryFn: () => get<{ items: DocVersion[] }>(`${base}/_history`).then((r) => r.items) });
  const [pick, setPick] = useState<DocVersion | null>(null);
  const [plan, setPlan] = useState<{ plan: string; diff: Diff } | null>(null);
  const [reason, setReason] = useState("");
  const dry = useMutation({
    mutationFn: async (v: DocVersion) => (await request<{ plan: string; diff: Diff }>(`${base}/_restore`, { method: "POST", query: { dryRun: true }, body: { versionKey: v.key } })).data,
    onSuccess: setPlan,
  });
  const run = useMutation({
    mutationFn: () => request(`${base}/_restore`, { method: "POST", body: { versionKey: pick!.key, reason } }),
    onSuccess: () => { invalidate(); toast("Restored. The version it replaced is in the history too."); setPick(null); setPlan(null); setReason(""); onRestored(); },
  });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorCallout error={q.error} />;
  const items = q.data ?? [];
  if (!items.length) return <Empty title="No saved versions">Versions are saved each time this document is edited, created, deleted or restored here.</Empty>;
  const label = { UPDATE: "Edited", CREATE: "Created", DELETE: "Deleted", RESTORE: "Restored" } as const;
  return (
    <div className="stack">
      <table className="table compact" style={{ tableLayout: "fixed" }}>
        <thead><tr><th style={{ width: 150 }}>When</th><th style={{ width: "22%" }}>By</th><th>Change</th><th>Reason</th><th style={{ width: 200 }} /></tr></thead>
        <tbody>
          {items.map((v) => (
            <tr key={v.key} className={pick?.key === v.key ? "selected" : ""}>
              <td className="nowrap">{when(v.at)}</td>
              <td style={{ overflowWrap: "anywhere" }}>{v.by}</td>
              <td><Badge tone={v.action === "DELETE" ? "red" : undefined}>{label[v.action]}</Badge>{v.changedFields.length > 0 && <div className="hint" style={{ overflowWrap: "anywhere" }}>{v.changedFields.slice(0, 6).join(", ")}{v.changedFields.length > 6 ? "…" : ""}</div>}</td>
              <td>{v.reason ?? "—"}</td>
              <td style={{ textAlign: "right" }}>
                {canEdit && <button type="button" className="btn btn-ghost btn-sm" onClick={() => { setPick(v); setPlan(null); dry.mutate(v); }}>
                  {v.before.exists ? "Restore the version before" : "Undo the create"}</button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {pick && (
        <div className="stack-sm">
          {dry.isPending ? <Loading what="Checking…" /> : dry.error ? <ErrorCallout error={dry.error} /> : plan && (
            <>
              <span className="label">{plan.plan === "delete" ? "The document will be deleted (it did not exist before)" : plan.plan === "recreate" ? "The document will be recreated as it was" : plan.plan === "nothing" ? "Nothing to restore" : "What will change"}</span>
              {plan.plan !== "nothing" && !emptyDiff(plan.diff) && <DiffTable diff={plan.diff} absent="not set" keyLabel="Field" beforeLabel="Now" afterLabel="Restored" />}
              {plan.plan !== "nothing" && <ReasonField value={reason} onChange={setReason} placeholder="Why restore it?" />}
              {run.error && <ErrorCallout error={run.error} />}
              <div className="row" style={{ gap: 8, justifyContent: "flex-end" }}>
                <button type="button" className="btn" onClick={() => { setPick(null); setPlan(null); }}>Cancel</button>
                {plan.plan !== "nothing" && <button type="button" className="btn btn-primary" disabled={!reason.trim() || run.isPending} onClick={() => run.mutate()}>{run.isPending ? "Restoring…" : "Restore"}</button>}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ new document

export function NewDocDialog({ clusterId, indices, initialIndex, sample, onClose }: {
  clusterId: string; indices: string[]; initialIndex: string; sample?: Record<string, unknown>; onClose: () => void;
}) {
  const toast = useToast();
  const invalidate = useInvalidateData(clusterId);
  const [index, setIndex] = useState(indices.includes(initialIndex) ? initialIndex : indices[0] ?? "");
  const [id, setId] = useState("");
  const [text, setText] = useState("{\n  \n}");
  const [reason, setReason] = useState("");
  const [doc, jsonError] = parseJson(text);
  const isObj = doc !== null && typeof doc === "object" && !Array.isArray(doc) && Object.keys(doc as object).length > 0;
  const create = useMutation({
    mutationFn: async () => (await request<{ id: string; index: string }>(`/clusters/${enc(clusterId)}/data/${enc(index)}/_doc`, {
      method: "POST", body: { document: doc, id: id.trim() || undefined, reason } })).data,
    onSuccess: (r) => { invalidate(); toast(`Created ${r.id} in ${r.index}`); onClose(); },
  });
  return (
    <Dialog wide title="New document" subtitle="Saved with a reason in the audit log; it can be undone from its history." onClose={onClose} busy={create.isPending}
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={create.isPending}>Cancel</button>
        <button type="button" className="btn btn-primary" disabled={!index || !isObj || !reason.trim() || create.isPending} onClick={() => create.mutate()}>{create.isPending ? "Creating…" : "Create document"}</button>
      </>}>
      <div className="grid-2" style={{ gap: 14 }}>
        <div className="field">
          <label className="label" htmlFor="nd-index">Index</label>
          <select id="nd-index" className="select mono" value={index} onChange={(e) => setIndex(e.target.value)}>
            {indices.map((i) => <option key={i} value={i}>{i}</option>)}
          </select>
          <span className="hint">Indices you may edit</span>
        </div>
        <div className="field">
          <label className="label" htmlFor="nd-id">Id <span className="hint">· optional; Elasticsearch picks one if empty</span></label>
          <input id="nd-id" className="input mono" value={id} onChange={(e) => setId(e.target.value)} spellCheck={false} />
        </div>
      </div>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span className="label">Document (JSON)</span>
        {sample && <button type="button" className="btn btn-ghost btn-sm" onClick={() => setText(pretty(sample))}>Start from a copy of the first row</button>}
      </div>
      <JsonEditor label="New document" value={text} onChange={setText} height="34vh" />
      {jsonError && <span className="hint" style={{ color: "var(--danger)" }}>{jsonError}</span>}
      <ReasonField value={reason} onChange={setReason} placeholder="e.g. Recreate order missing after the import" />
      {create.error && <ErrorCallout error={create.error} />}
    </Dialog>
  );
}

// ------------------------------------------------------------------ bulk

interface SetRow { field: string; value: string }

/** A value typed in a bulk "set" row: valid JSON (numbers, true, null, "text", {...}) or plain text. */
function parseValue(v: string): unknown {
  const t = v.trim();
  if (t === "") return "";
  try {
    return JSON.parse(t);
  } catch {
    return v;
  }
}

export function BulkDialog({ op, clusterId, index, search, describe, onClose }: {
  op: "update" | "delete"; clusterId: string; index: string; search: DataSearchBody; describe: string; onClose: () => void;
}) {
  const toast = useToast();
  const invalidate = useInvalidateData(clusterId);
  const [rows, setRows] = useState<SetRow[]>([{ field: "", value: "" }]);
  const [remove, setRemove] = useState("");
  const [preview, setPreview] = useState<BulkPreview | null>(null);
  const [reason, setReason] = useState("");
  const [typed, setTyped] = useState("");
  const [result, setResult] = useState<BulkPreview | null>(null);
  const spec = useMemo(() => {
    const set = Object.fromEntries(rows.filter((r) => r.field.trim()).map((r) => [r.field.trim(), parseValue(r.value)]));
    const rm = remove.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
    return { query: search.query, filters: search.filters, timeRange: search.timeRange, ...(op === "update" ? { set, remove: rm } : {}) };
  }, [rows, remove, search, op]);
  const url = `/clusters/${enc(clusterId)}/data/${enc(index)}/_bulk_${op}`;
  const dry = useMutation({
    mutationFn: async () => (await request<BulkPreview>(url, { method: "POST", query: { dryRun: true }, body: spec })).data,
    onSuccess: (r) => { setPreview(r); setTyped(""); },
  });
  const run = useMutation({
    mutationFn: async () => (await request<BulkPreview>(url, { method: "POST", body: { ...spec, reason, dryRunToken: preview!.dryRunToken, expectedCount: preview!.count } })).data,
    onSuccess: (r) => { invalidate(); setResult(r); toast(`${op === "update" ? "Updated" : "Deleted"} ${num(r.succeeded ?? 0)} documents`); },
  });
  const stale = run.error instanceof ApiError && ["COUNT_CHANGED", "DRY_RUN_EXPIRED", "DRY_RUN_MISMATCH"].includes(run.error.code);
  const changed = () => { setPreview(null); setResult(null); run.reset(); };
  const hasSpec = op === "delete" || rows.some((r) => r.field.trim()) || remove.trim();
  const n = preview ? (op === "update" ? preview.willChange : preview.count) : 0;
  const title = op === "update" ? "Update matching documents" : "Delete matching documents";

  if (result) {
    return (
      <Dialog title={title} onClose={onClose} footer={<button type="button" className="btn btn-primary" onClick={onClose}>Done</button>}>
        <Callout tone={result.failed ? "warn" : "success"} icon={result.failed ? "warn" : "check"}
          title={`${num(result.succeeded ?? 0)} ${op === "update" ? "updated" : "deleted"}${result.conflicts ? ` · ${num(result.conflicts)} skipped (changed meanwhile)` : ""}${result.failed ? ` · ${num(result.failed)} failed` : ""}`}>
          A backup of every document was saved first. Undo it under <strong>Bulk changes</strong> (change {result.changeId?.slice(0, 8)}).
        </Callout>
        {result.errors && result.errors.length > 0 && (
          <table className="table compact"><thead><tr><th>Document</th><th>Problem</th></tr></thead>
            <tbody>{result.errors.map((e) => <tr key={`${e._index}/${e._id}`}><td className="cell-mono">{e._id}</td><td>{e.error}</td></tr>)}</tbody></table>
        )}
      </Dialog>
    );
  }
  return (
    <Dialog wide title={title} busy={run.isPending} onClose={onClose}
      subtitle={<>Applies to every document the current search matches in <span className="mono">{index}</span>. A dry run is required first; a backup is kept for undo.</>}
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={run.isPending}>Cancel</button>
        {!preview ? (
          <button type="button" className="btn btn-primary" disabled={!hasSpec || dry.isPending} onClick={() => dry.mutate()}>{dry.isPending ? <Spinner label="Counting" /> : <Icon name="eye" />} Dry run</button>
        ) : n > 0 ? (
          <button type="button" className={`btn ${op === "delete" ? "btn-danger" : "btn-primary"}`} disabled={typed !== String(n) || !reason.trim() || run.isPending}
            onClick={() => run.mutate()}>{run.isPending ? "Working…" : `${op === "update" ? "Update" : "Delete"} ${num(n)} document${n === 1 ? "" : "s"}`}</button>
        ) : null}
      </>}>
      <div className="callout" style={{ flexDirection: "column", gap: 4 }}>
        <span className="title" style={{ fontSize: 13 }}>Matching</span>
        <span className="mono" style={{ fontSize: 12.5, overflowWrap: "anywhere" }}>{describe}</span>
      </div>
      {op === "update" && (
        <div className="stack-sm">
          <span className="label">Set fields <span className="hint">· value as JSON (123, true, null, "text", {"{…}"}) or plain text</span></span>
          {rows.map((r, i) => (
            <div key={i} className="row" style={{ gap: 6, flexWrap: "nowrap" }}>
              <input className="input mono" style={{ flex: "0 1 40%", minHeight: 36 }} aria-label={`Field ${i + 1}`} placeholder="status" value={r.field}
                onChange={(e) => { setRows(rows.map((x, j) => (j === i ? { ...x, field: e.target.value } : x))); changed(); }} spellCheck={false} />
              <input className="input mono" style={{ flex: 1, minHeight: 36 }} aria-label={`Value ${i + 1}`} placeholder="delivered" value={r.value}
                onChange={(e) => { setRows(rows.map((x, j) => (j === i ? { ...x, value: e.target.value } : x))); changed(); }} spellCheck={false} />
              <button type="button" className="btn btn-ghost mini-btn" aria-label={`Remove row ${i + 1}`} onClick={() => { setRows(rows.filter((_, j) => j !== i)); changed(); }}><Icon name="x" size={14} /></button>
            </div>
          ))}
          <button type="button" className="btn btn-ghost btn-sm" style={{ alignSelf: "flex-start" }} onClick={() => setRows([...rows, { field: "", value: "" }])}><Icon name="plus" size={14} /> Add field</button>
          <div className="field">
            <label className="label" htmlFor="bulk-remove">Remove fields <span className="hint">· optional, comma separated</span></label>
            <input id="bulk-remove" className="input mono" value={remove} onChange={(e) => { setRemove(e.target.value); changed(); }} placeholder="notes, order_info.discount" spellCheck={false} />
          </div>
        </div>
      )}
      {dry.error && <ErrorCallout error={dry.error} />}
      {preview && (
        <div className="stack">
          <Callout tone={n === 0 ? "neutral" : op === "delete" ? "danger" : "warn"} icon={n === 0 ? "info" : "warn"}
            title={n === 0 ? "Nothing would change" : `${num(n)} document${n === 1 ? "" : "s"} will be ${op === "update" ? "changed" : "deleted"}`}>
            {num(preview.count)} match{op === "update" && preview.unchanged ? ` · ${num(preview.unchanged)} already have these values` : ""}
            {preview.skippedCount ? ` · ${num(preview.skippedCount)} skipped (${preview.skipped[0]?.reason})` : ""}
            {preview.indices.length > 1 ? ` · across ${preview.indices.length} indices` : ""}
          </Callout>
          {preview.warnings.map((w) => <Callout key={w} tone="warn" icon="warn">{w}</Callout>)}
          {preview.sample && preview.sample.length > 0 && (
            <div className="stack-sm">
              <span className="label">Examples (first {preview.sample.length})</span>
              {preview.sample.map((s) => (
                <details key={`${s._index}/${s._id}`} className="sample">
                  <summary className="mono">{s._id} <span className="hint">· {s._index}</span></summary>
                  {s.diff ? <DiffTable diff={s.diff} absent="not set" keyLabel="Field" /> : <pre className="code-block" style={{ maxHeight: 220 }}>{pretty(s.source)}</pre>}
                </details>
              ))}
            </div>
          )}
          {n > 0 && (
            <>
              <ReasonField value={reason} onChange={setReason} placeholder={op === "update" ? "Why change these documents?" : "Why delete these documents?"} />
              <div className="field" style={{ maxWidth: 340 }}>
                <label className="label" htmlFor="bulk-typed">To confirm, type the number of documents: <code>{n}</code></label>
                <input id="bulk-typed" className="input mono" inputMode="numeric" value={typed} onChange={(e) => setTyped(e.target.value.trim())} autoComplete="off" />
              </div>
              <span className="hint">The dry run is valid for 15 minutes. If the matching documents change before you confirm, you'll be asked to run it again.</span>
            </>
          )}
          {run.error && (stale ? (
            <Callout tone="warn" icon="refresh" title="Run the dry run again">{(run.error as ApiError).message}
              <div style={{ marginTop: 8 }}><button type="button" className="btn btn-sm" onClick={() => { run.reset(); dry.mutate(); }}>Dry run again</button></div>
            </Callout>
          ) : <ErrorCallout error={run.error} />)}
        </div>
      )}
    </Dialog>
  );
}

export function ChangesDialog({ clusterId, onClose }: { clusterId: string; onClose: () => void }) {
  const toast = useToast();
  const invalidate = useInvalidateData(clusterId);
  const q = useQuery({ queryKey: ["bulk-changes", clusterId], queryFn: () => get<{ items: BulkChange[] }>(`/clusters/${enc(clusterId)}/data/_changes`).then((r) => r.items) });
  const [pick, setPick] = useState<BulkChange | null>(null);
  const [plan, setPlan] = useState<{ count: number; dryRunToken: string; note: string } | null>(null);
  const [reason, setReason] = useState("");
  const [typed, setTyped] = useState("");
  const url = (id: string) => `/clusters/${enc(clusterId)}/data/_changes/${enc(id)}/_restore`;
  const dry = useMutation({
    mutationFn: async (c: BulkChange) => (await request<{ count: number; dryRunToken: string; note: string }>(url(c.changeId), { method: "POST", query: { dryRun: true }, body: {} })).data,
    onSuccess: (r) => { setPlan(r); setTyped(""); },
  });
  const run = useMutation({
    mutationFn: async () => (await request<{ succeeded: number }>(url(pick!.changeId), { method: "POST", body: { reason, dryRunToken: plan!.dryRunToken, expectedCount: plan!.count } })).data,
    onSuccess: (r) => { invalidate(); toast(`Restored ${num(r.succeeded)} documents`); setPick(null); setPlan(null); setReason(""); },
  });
  const opLabel = { update: "Bulk update", delete: "Bulk delete", restore: "Restore" } as const;
  return (
    <Dialog wide title="Bulk changes" subtitle="Every bulk update or delete on this cluster, with a backup of the documents as they were." onClose={onClose} busy={run.isPending}
      footer={<button type="button" className="btn btn-primary" onClick={onClose}>Close</button>}>
      {q.isLoading ? <Loading /> : q.error ? <ErrorCallout error={q.error} /> : !q.data?.length ? <Empty title="No bulk changes yet" /> : (
        <div className="table-scroll" style={{ maxHeight: "46vh", border: "1px solid var(--border)", borderRadius: "var(--radius)" }}>
        <table className="table compact" style={{ tableLayout: "fixed" }}>
          <thead><tr><th style={{ width: "46%" }}>Change</th><th>Reason</th><th className="num" style={{ width: 70 }}>Docs</th><th style={{ width: 96 }} /></tr></thead>
          <tbody>
            {q.data.map((c) => (
              <tr key={c.changeId} className={pick?.changeId === c.changeId ? "selected" : ""}>
                <td style={{ overflowWrap: "anywhere" }}>
                  <Badge tone={c.op === "delete" ? "red" : undefined}>{opLabel[c.op]}</Badge> <span className="hint">{when(c.at)} · {c.by}</span>
                  <div className="mono hint">{c.index}</div>
                  {c.fields && <div className="hint mono">{[...c.fields.set.map((f) => `set ${f}`), ...c.fields.remove.map((f) => `remove ${f}`)].join(", ")}</div>}
                  {c.restoredAt && <div className="hint">Restored by {c.restoredBy}, {when(c.restoredAt)}</div>}
                </td>
                <td style={{ overflowWrap: "anywhere" }}>{c.reason}</td>
                <td className="num">{num(c.result?.succeeded ?? c.count)}</td>
                <td style={{ textAlign: "right" }}><button type="button" className="btn btn-ghost btn-sm" onClick={() => { setPick(c); setPlan(null); dry.mutate(c); }}>{c.op === "restore" ? "Undo…" : "Restore…"}</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
      {pick && (
        <div className="stack-sm">
          {dry.isPending ? <Loading what="Checking…" /> : dry.error ? <ErrorCallout error={dry.error} /> : plan && (
            <>
              <Callout tone="warn" icon="undo" title={`Put ${num(plan.count)} document${plan.count === 1 ? "" : "s"} back as they were before this change`}>{plan.note}</Callout>
              <ReasonField value={reason} onChange={setReason} placeholder="Why undo it?" />
              <div className="field" style={{ maxWidth: 340 }}>
                <label className="label" htmlFor="rs-typed">Type the number of documents: <code>{plan.count}</code></label>
                <input id="rs-typed" className="input mono" value={typed} onChange={(e) => setTyped(e.target.value.trim())} autoComplete="off" />
              </div>
              {run.error && <ErrorCallout error={run.error} />}
              <div className="row" style={{ gap: 8, justifyContent: "flex-end" }}>
                <button type="button" className="btn" onClick={() => { setPick(null); setPlan(null); }}>Cancel</button>
                <button type="button" className="btn btn-primary" disabled={typed !== String(plan.count) || !reason.trim() || run.isPending} onClick={() => run.mutate()}>{run.isPending ? "Restoring…" : "Restore"}</button>
              </div>
            </>
          )}
        </div>
      )}
    </Dialog>
  );
}
