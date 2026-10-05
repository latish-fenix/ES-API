// Create an index: name, settings / mappings / aliases (pre-filled from the matching index
// template), a required dry run that shows what Elasticsearch will really create, a reason.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { PendingApproval, enc, get, request } from "../api";
import { Icon } from "../components/icons";
import { JsonEditor, parseJson } from "../components/JsonEditor";
import { applyLabel, Callout, Dialog, ErrorCallout, ReasonField, Spinner, useToast } from "../components/ui";
import { useNeedsApproval } from "../session";

const NAME_RE = /^[a-z0-9][a-z0-9._+-]{0,254}$/;
const pretty = (v: unknown) => JSON.stringify(v, null, 2);

interface TemplateInfo { name: string; priority: number; indexPatterns: string[]; composedOf: string[]; dataStream: boolean }
interface Preview {
  index: string;
  exists: string | null;
  template: TemplateInfo | null;
  fromTemplates: { settings: Record<string, unknown>; mappings: Record<string, unknown>; aliases: Record<string, unknown> };
}
interface Result {
  index: string;
  dryRun: boolean;
  applied: boolean;
  template: TemplateInfo | null;
  result: { settings: Record<string, unknown>; mappings: { properties?: Record<string, unknown> }; aliases: Record<string, unknown> };
  warnings: string[];
  changeId?: string;
}

const DEFAULT_BODY = { settings: { number_of_shards: 1, number_of_replicas: 1 }, mappings: { properties: {} }, aliases: {} };

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

/** The template's settings in the short form people type (index.number_of_shards -> number_of_shards). */
function editable(p: Preview["fromTemplates"]) {
  const settings = Object.fromEntries(Object.entries(p.settings)
    .filter(([k]) => k !== "index.routing.allocation.include._tier_preference")
    .map(([k, v]) => [k.replace(/^index\./, ""), v]));
  return { settings: Object.keys(settings).length ? settings : DEFAULT_BODY.settings, mappings: Object.keys(p.mappings).length ? p.mappings : DEFAULT_BODY.mappings, aliases: p.aliases };
}

export function CreateIndexDialog({ clusterId, onClose }: { clusterId: string; onClose: () => void }) {
  const needsApproval = useNeedsApproval();
  const toast = useToast();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [text, setText] = useState(pretty(DEFAULT_BODY));
  const [touched, setTouched] = useState(false);
  const [result, setResult] = useState<Result | null>(null);
  const [reason, setReason] = useState("");
  const debounced = useDebounced(name.trim(), 400);
  const nameOk = NAME_RE.test(name.trim());
  const base = `/clusters/${enc(clusterId)}/indices/${enc(name.trim())}`;

  const preview = useQuery({
    queryKey: ["create-preview", clusterId, debounced],
    queryFn: () => get<Preview>(`/clusters/${enc(clusterId)}/indices/${enc(debounced)}/_create-preview`),
    enabled: NAME_RE.test(debounced),
    retry: false,
  });
  // pre-fill from the matching template until the user edits the JSON
  useEffect(() => {
    if (preview.data && !touched) setText(pretty(editable(preview.data.fromTemplates)));
  }, [preview.data, touched]);

  const [body, jsonError] = parseJson(text);
  const isObj = body !== null && typeof body === "object" && !Array.isArray(body);
  const dry = useMutation({
    mutationFn: async () => (await request<Result>(base, { method: "POST", query: { dryRun: true }, body })).data,
    onSuccess: setResult,
  });
  const create = useMutation({
    mutationFn: async () => (await request<Result>(base, { method: "POST", body: { ...(body as object), reason } })).data,
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["indices", clusterId] });
      qc.invalidateQueries({ queryKey: ["audit"] });
      toast(`Created ${r.index}. Roll back deletes it again while it's empty.`);
      onClose();
      navigate(`/c/${enc(clusterId)}/indices/${enc(r.index)}/settings`);
    },
  });
  const reset = () => { setResult(null); dry.reset(); create.reset(); };
  const exists = preview.data?.exists;
  const p = preview.data;

  return (
    <Dialog wide busy={create.isPending} onClose={onClose} title="Create index"
      subtitle="A dry run shows exactly what Elasticsearch will create (your settings combined with the matching index template). Nothing is created until you confirm."
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={create.isPending}>Cancel</button>
        {!result ? (
          <button type="button" className="btn btn-primary" disabled={!nameOk || !!exists || !isObj || !!jsonError || dry.isPending} onClick={() => dry.mutate()}>
            {dry.isPending ? <Spinner label="Checking" /> : <Icon name="eye" />} Dry run
          </button>
        ) : (
          <button type="button" className="btn btn-primary" disabled={!reason.trim() || create.isPending || create.error instanceof PendingApproval} onClick={() => create.mutate()}>
            <Icon name="plus" /> {create.isPending ? "Creating…" : applyLabel(needsApproval, "Create index")}
          </button>
        )}
      </>}>
      <div className="field">
        <label className="label" htmlFor="ci-name">Index name <span className="hint">· lowercase letters, digits, - _ + and . (not at the start)</span></label>
        <input id="ci-name" className="input mono" value={name} autoFocus spellCheck={false} autoComplete="off" placeholder="shoppremiumoutlets.myshopify.com-returns-2024.10"
          onChange={(e) => { setName(e.target.value.toLowerCase()); reset(); }} aria-invalid={name.length > 0 && !nameOk ? true : undefined} />
        {name.length > 0 && !nameOk && <span className="hint" style={{ color: "var(--danger)" }}>Not a valid index name</span>}
        {preview.isFetching && <span className="hint">Checking the name…</span>}
        {preview.error && <ErrorCallout error={preview.error} clusterId={clusterId} />}
        {exists && <span className="hint" style={{ color: "var(--danger)" }}>An {exists} with this name already exists</span>}
        {p && !exists && (p.template ? (
          <span className="hint">Index template <span className="mono">{p.template.name}</span> (priority {p.template.priority}) applies to this name{p.template.composedOf.length ? <>, with <span className="mono">{p.template.composedOf.join(", ")}</span></> : null}; its settings and mappings are filled in below.</span>
        ) : <span className="hint">No index template matches this name; Elasticsearch's defaults apply to anything you leave out.</span>)}
        {p?.template?.dataStream && <Callout tone="danger" icon="warn">This name matches a data-stream template, so it can't be a regular index. Choose another name.</Callout>}
      </div>
      <JsonEditor label="Settings, mappings and aliases" value={text} height={result ? "18vh" : "34vh"}
        onChange={(v) => { setText(v); setTouched(true); reset(); }} />
      {jsonError ? <span className="hint" style={{ color: "var(--danger)" }}>{jsonError}</span>
        : !isObj ? <span className="hint" style={{ color: "var(--danger)" }}>Must be a JSON object with settings, mappings and aliases</span>
        : <span className="hint">Settings such as <code>number_of_shards</code> can't be changed after the index exists (only by reindexing); mappings can only be added to.</span>}
      {dry.error && <ErrorCallout error={dry.error} clusterId={clusterId} />}
      {result && (
        <div className="stack">
          <Callout tone="success" icon="check" title={`Elasticsearch accepts this: ${result.index} will be created`}>
            {Object.keys(result.result.mappings.properties ?? {}).length} top-level fields, {Object.keys(result.result.settings).length} settings
            {Object.keys(result.result.aliases).length ? `, aliases ${Object.keys(result.result.aliases).join(", ")}` : ""}.
          </Callout>
          {result.warnings.map((w) => <Callout key={w} tone="warn" icon="warn">{w}</Callout>)}
          <details className="sample">
            <summary className="label">What it will get (with the template)</summary>
            <pre className="code-block" style={{ maxHeight: 240 }}>{pretty(result.result)}</pre>
          </details>
          <ReasonField value={reason} onChange={setReason} placeholder="e.g. Returns import for October" autoFocus />
          {create.error && <ErrorCallout error={create.error} clusterId={clusterId} />}
        </div>
      )}
    </Dialog>
  );
}
