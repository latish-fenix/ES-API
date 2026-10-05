import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { ApiError, PendingApproval, get, request, type ChangeResult, type Snapshot } from "../api";
import { pretty, when } from "../format";
import { DiffTable, diffCount } from "./DiffTable";
import { Icon } from "./icons";
import { JsonEditor, parseJson } from "./JsonEditor";
import { applyLabel, Badge, Callout, Dialog, ErrorCallout, Loading, ReasonField, useToast } from "./ui";
import { useNeedsApproval } from "../session";

type Stage = "edit" | "checked" | "applied";

function Steps({ stage }: { stage: Stage }) {
  const idx = stage === "edit" ? 0 : stage === "checked" ? 1 : 3;
  const items = ["Edit", "Dry run", "Apply"];
  return (
    <ol className="steps" aria-label="Progress">
      {items.map((s, i) => (
        <li key={s} className={i < idx ? "done" : i === idx ? "current" : ""} aria-current={i === idx ? "step" : undefined}>
          <span className="n">{i < idx ? <Icon name="check" size={12} strokeWidth={3} /> : i + 1}</span>
          {s}
        </li>
      ))}
    </ol>
  );
}

function Simulation({ sim }: { sim: Record<string, unknown> }) {
  if (sim.kind === "ingest_pipeline_simulate") {
    const docs = (sim.docs as { doc?: { _source?: unknown }; error?: unknown }[]) ?? [];
    return (
      <div className="stack-sm">
        <span className="label">Result for each sample document</span>
        {sim.note ? <span className="hint">{String(sim.note)}</span> : null}
        {docs.map((d, i) => (
          <pre key={i} className="code-block" style={{ maxHeight: 200 }}>{pretty(d.error ?? d.doc?._source ?? d)}</pre>
        ))}
      </div>
    );
  }
  if (sim.kind === "index_template_simulate") {
    const overlapping = (sim.overlapping as { name: string; index_patterns: string[] }[]) ?? [];
    return (
      <details>
        <summary className="label">What a new matching index would get{overlapping.length ? ` · overlaps ${overlapping.length} template(s)` : ""}</summary>
        {overlapping.length > 0 && (
          <Callout tone="warn" title="Overlapping templates">
            {overlapping.map((o) => `${o.name} (${o.index_patterns.join(", ")})`).join("; ")}
          </Callout>
        )}
        <pre className="code-block" style={{ marginTop: 8 }}>{pretty(sim.resolvedTemplate)}</pre>
      </details>
    );
  }
  return (
    <details>
      <summary className="label">Elasticsearch simulation</summary>
      <pre className="code-block" style={{ marginTop: 8 }}>{pretty(sim)}</pre>
    </details>
  );
}

export function DryRunSummary({ r, absent, inRollback }: { r: ChangeResult; absent: string; inRollback?: boolean }) {
  const n = diffCount(r.diff);
  const facts = [
    `Health ${r.clusterHealth}`,
    inRollback ? null : r.driftDetected ? "drift detected" : "no drift",
    r.permanent ? "permanent: no rollback" : inRollback ? "rolling back again re-applies the change" : r.rollbackAvailableAfter ?? r.rollbackAvailable ? "rollback available after apply" : null,
  ].filter(Boolean).join(" · ");
  return (
    <div className="stack">
      {r.noChange ? (
        <Callout tone="info" title="No change">The live config already matches. There is nothing to apply.</Callout>
      ) : r.valid === false ? (
        <Callout tone="danger" title="Dry run found a problem · nothing changed">
          {r.errors?.map((e) => <div key={e.code}>{e.message} <span className="meta">{e.code}</span></div>)}
        </Callout>
      ) : (
        <Callout tone="success" title={`Dry run passed · ${n} change${n === 1 ? "" : "s"} · nothing changed yet`}>{facts}</Callout>
      )}
      {r.createsResource && <Callout tone="info" title="Creates a new resource">Rolling back afterwards deletes it again.</Callout>}
      {r.deletesResource && <Callout tone="warn" title="This deletes the resource">It didn't exist before the last change.</Callout>}
      {r.warnings.length > 0 && (
        <Callout tone="warn" title={r.warnings.length === 1 ? "Warning" : "Warnings"}>
          <ul>{r.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
        </Callout>
      )}
      {!r.noChange && <DiffTable diff={r.diff} absent={absent} keyLabel={absent === "default" ? "Setting" : "Path"} />}
      {r.simulation ? <Simulation sim={r.simulation} /> : null}
    </div>
  );
}

export interface ChangeFlowProps {
  /** e.g. /clusters/prod/cluster-settings */
  path: string;
  text: string;
  setText: (v: string) => void;
  /** version from the GET (for If-Match); undefined for new resources */
  version?: string;
  title?: string;
  editorLabel: ReactNode;
  absent: string;
  toConfig?: (parsed: unknown) => unknown;
  sampleDocs?: boolean;
  permanent?: boolean;
  applyLabel?: string;
  editorHeight?: string;
  clusterId: string;
  admin: boolean;
  onApplied: (r: ChangeResult) => void;
  headerExtra?: ReactNode;
}

export function ChangeFlow(p: ChangeFlowProps) {
  const needsApproval = useNeedsApproval();
  const toast = useToast();
  const [stage, setStage] = useState<Stage>("edit");
  const [result, setResult] = useState<ChangeResult | null>(null);
  const [checkedText, setCheckedText] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [ack, setAck] = useState(false);
  const [force, setForce] = useState(false);
  const [samples, setSamples] = useState('[\n  { "message": "hello" }\n]');
  const [localErr, setLocalErr] = useState<string | null>(null);

  // Editing after a dry run invalidates it.
  useEffect(() => {
    if (stage === "checked" && checkedText !== null && p.text !== checkedText) {
      setStage("edit");
      setResult(null);
    }
  }, [p.text, stage, checkedText]);

  const body = () => {
    const [parsed, err] = parseJson(p.text);
    if (err) throw new Error(err);
    const out: Record<string, unknown> = { config: p.toConfig ? p.toConfig(parsed) : parsed };
    if (p.sampleDocs) {
      const [docs, e2] = parseJson(samples);
      if (e2) throw new Error(`Sample documents: ${e2}`);
      if (!Array.isArray(docs)) throw new Error("Sample documents must be a JSON array of objects");
      out.sampleDocs = docs;
    }
    return out;
  };

  const dryRun = useMutation({
    mutationFn: async () => {
      setLocalErr(null);
      const b = body();
      return (await request<ChangeResult>(p.path, { method: "PUT", query: { dryRun: true, force }, body: b })).data;
    },
    onSuccess: (r) => {
      setResult(r);
      setCheckedText(p.text);
      setStage("checked");
      setAck(false);
    },
    onError: (e) => {
      if (!(e instanceof ApiError)) setLocalErr((e as Error).message);
    },
  });

  const apply = useMutation({
    mutationFn: async () => {
      const b = { ...body(), reason: reason.trim() };
      const headers: Record<string, string> = p.version ? { "If-Match": `"${p.version}"` } : {};
      return (await request<ChangeResult>(p.path, { method: "PUT", query: { force }, body: b, headers })).data;
    },
    onSuccess: (r) => {
      if (r.noChange) toast("No change: the live config already matched");
      else toast(r.permanent ? "Change applied (permanent)" : "Change applied. The previous config is saved for rollback.");
      setStage("edit");
      setResult(null);
      setReason("");
      setForce(false);
      p.onApplied(r);
    },
  });

  // A fresh edit clears the last dry-run error.
  const resetDry = dryRun.reset;
  const resetApply = apply.reset;
  useEffect(() => {
    resetDry();
    resetApply();
    setLocalErr(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.text]);

  const err = dryRun.error instanceof ApiError ? dryRun.error : apply.error;
  const needsForce = err instanceof ApiError && err.code === "CLUSTER_UNHEALTHY";
  const canApply = stage === "checked" && !!result && !result.noChange && result.valid !== false
    && reason.trim().length > 0 && (!p.permanent || ack) && !apply.isPending && !(apply.error instanceof PendingApproval);

  return (
    <section className="card" aria-label={p.title ?? "Change"}>
      <div className="card-head">
        <div className="grow"><h2>{p.title ?? "Change"}</h2></div>
        {p.headerExtra}
        <Steps stage={stage} />
      </div>
      <div className="card-body">
        <div className="field">
          <span className="label">{p.editorLabel}</span>
          <JsonEditor label="Config JSON" value={p.text} onChange={p.setText} height={p.editorHeight ?? "240px"} />
        </div>
        {p.sampleDocs && (
          <div className="field">
            <span className="label">Sample documents <span className="hint">· a JSON array; the dry run shows each one after processing</span></span>
            <JsonEditor label="Sample documents JSON" value={samples} onChange={setSamples} height="120px" />
          </div>
        )}
        {localErr && <Callout tone="danger" title="Can't send this">{localErr}</Callout>}
        {err && <ErrorCallout error={err} clusterId={p.clusterId} admin={p.admin} />}
        {needsForce && (
          <label className="check"><input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} /> Change anyway while the cluster is red (force)</label>
        )}
        {err instanceof ApiError && err.code === "VERSION_MISMATCH" && (
          <Callout tone="info">Your edits are kept. Reload the page section to get the latest version, then run the dry run again.</Callout>
        )}
        {stage === "checked" && result && (
          <>
            <DryRunSummary r={result} absent={p.absent} />
            {!result.noChange && result.valid !== false && (
              <>
                <ReasonField value={reason} onChange={setReason} />
                {needsApproval && <span className="hint">This goes to the admins for approval: it is applied when one of them approves it, and you get an email.</span>}
                {p.permanent && (
                  <label className="check"><input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} /> I understand this can't be removed or rolled back later</label>
                )}
              </>
            )}
          </>
        )}
      </div>
      <div className="card-foot">
        <span className="hint grow" style={{ minWidth: 200 }}>
          {stage === "checked"
            ? p.permanent
              ? "Applies only if the config hasn't changed since you loaded it."
              : "Applies only if the config hasn't changed since you loaded it. The current config is saved to S3 first."
            : "Nothing changes until you apply. The dry run shows exactly what would."}
        </span>
        {stage === "checked" ? (
          <>
            <button type="button" className="btn" onClick={() => { setStage("edit"); setResult(null); }}>Back to edit</button>
            <button type="button" className="btn btn-primary" disabled={!canApply} onClick={() => apply.mutate()}>
              {apply.isPending ? (needsApproval ? "Sending…" : "Applying…") : applyLabel(needsApproval, p.applyLabel ?? "Apply change")}
            </button>
          </>
        ) : (
          <button type="button" className="btn btn-primary" onClick={() => dryRun.mutate()} disabled={dryRun.isPending}>
            <Icon name="eye" />
            {dryRun.isPending ? "Checking…" : "Dry run"}
          </button>
        )}
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ rollback

export function RollbackDialog({ path, label, absent, clusterId, admin, onClose, onDone }: {
  path: string;
  label: string;
  absent: string;
  clusterId: string;
  admin: boolean;
  onClose: () => void;
  onDone: () => void;
}) {
  const needsApproval = useNeedsApproval();
  const toast = useToast();
  const [reason, setReason] = useState("");
  const [force, setForce] = useState(false);
  const snap = useQuery({ queryKey: ["previous", path], queryFn: () => get<Snapshot>(`${path}/previous`), retry: false });
  const preview = useQuery({
    queryKey: ["rollback-preview", path, force],
    queryFn: async () => (await request<ChangeResult>(`${path}/rollback`, { method: "POST", query: { dryRun: true, force }, body: {} })).data,
    retry: false,
    gcTime: 0,
  });
  const run = useMutation({
    mutationFn: async () => (await request<ChangeResult>(`${path}/rollback`, { method: "POST", query: { force }, body: { reason: reason.trim() } })).data,
    onSuccess: (r) => {
      toast(r.noChange ? "Nothing to roll back: already at the snapshot" : "Rolled back. Rolling back again re-applies the change.");
      onDone();
      onClose();
    },
  });

  const pe = preview.error;
  const drift = pe instanceof ApiError && pe.code === "DRIFT_DETECTED";
  const r = preview.data;
  return (
    <Dialog
      title={`Roll back ${label}`}
      subtitle="Restores the snapshot taken before the last change. The config you replace becomes the new snapshot, so rolling back again re-applies the change."
      onClose={onClose}
      busy={run.isPending}
      wide
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={run.isPending}>Cancel</button>
          <button type="button" className="btn btn-primary" disabled={!r || r.noChange || !reason.trim() || run.isPending || run.error instanceof PendingApproval} onClick={() => run.mutate()}>
            <Icon name="undo" />
            {run.isPending ? "Working…" : applyLabel(needsApproval, "Roll back")}
          </button>
        </>
      }
    >
      {snap.data && (
        <div className="kv">
          <div><span className="k">Snapshot taken by</span><span className="v" title={snap.data.capturedBy}>{snap.data.capturedBy}</span></div>
          <div><span className="k">Taken</span><span className="v">{when(snap.data.capturedAt)}</span></div>
          <div><span className="k">Before change</span><span className="v">{snap.data.replacedBy}</span></div>
          <div><span className="k">Restores version</span><span className="v mono">{snap.data.version}</span></div>
        </div>
      )}
      {snap.error && <ErrorCallout error={snap.error} />}
      {preview.isLoading && <Loading what="Working out what a rollback would change…" />}
      {drift && (
        <>
          <ErrorCallout error={pe} />
          <label className="check"><input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} /> Roll back anyway and overwrite the outside change (force)</label>
        </>
      )}
      {pe && !drift && <ErrorCallout error={pe} clusterId={clusterId} admin={admin} />}
      {r && (
        <>
          {!r.driftDetected && !force && <Callout tone="success">No drift: the live config still matches what the API last applied.</Callout>}
          <DryRunSummary r={r} absent={absent} inRollback />
          {!r.noChange && <ReasonField value={reason} onChange={setReason} autoFocus />}
        </>
      )}
      {run.error && <ErrorCallout error={run.error} clusterId={clusterId} admin={admin} />}
    </Dialog>
  );
}

export function SnapshotDialog({ path, label, onClose }: { path: string; label: string; onClose: () => void }) {
  const snap = useQuery({ queryKey: ["previous", path], queryFn: () => get<Snapshot>(`${path}/previous`), retry: false });
  return (
    <Dialog title={`Saved snapshot · ${label}`} subtitle="The config as it was before the last change made through the API." onClose={onClose} wide
      footer={<button type="button" className="btn" onClick={onClose}>Close</button>}>
      {snap.isLoading && <Loading />}
      {snap.error && <ErrorCallout error={snap.error} />}
      {snap.data && (
        <>
          <div className="row">
            <Badge mono>version {snap.data.version}</Badge>
            <Badge>{snap.data.replacedBy} by {snap.data.capturedBy}</Badge>
            <Badge>{when(snap.data.capturedAt)}</Badge>
          </div>
          {snap.data.state.exists ? (
            <pre className="code-block">{pretty(snap.data.state.config)}</pre>
          ) : (
            <Callout tone="info">The resource did not exist before the last change. Rolling back deletes it.</Callout>
          )}
        </>
      )}
    </Dialog>
  );
}

export function StatusBadges({ version, drift, rollbackAvailable, lastApplied }: { version: string; drift: boolean; rollbackAvailable: boolean; lastApplied?: { by: string; at: string } | null }) {
  return (
    <div className="row" style={{ gap: 8 }}>
      <Badge mono title="Content hash of the live config">version {version}</Badge>
      {drift ? <Badge tone="amber" title="Changed directly in Elasticsearch since the API last applied it">Drift detected</Badge> : <Badge tone="green">No drift</Badge>}
      {rollbackAvailable ? <Badge tone="blue">Snapshot saved</Badge> : <Badge>No snapshot yet</Badge>}
      {lastApplied && <span className="hint">Last applied by {lastApplied.by}, {when(lastApplied.at)}</span>}
    </div>
  );
}
