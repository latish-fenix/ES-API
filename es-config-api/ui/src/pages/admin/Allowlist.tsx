import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useState, type KeyboardEvent } from "react";
import { enc, get, request, type Rule, type Rules } from "../../api";
import { Icon } from "../../components/icons";
import { JsonEditor, parseJson } from "../../components/JsonEditor";
import { Page } from "../../components/Shell";
import { Badge, Callout, Dialog, ErrorCallout, Loading, useToast } from "../../components/ui";
import { CONFIG_TYPE_LABEL, pretty, when } from "../../format";
import { useClusters } from "../../session";

type Extra = "indices" | "indexPatterns";
const TYPES: { type: string; matches: string; extra?: { key: Extra; label: string; hint: string }; note?: string; danger?: boolean }[] = [
  { type: "cluster-settings", matches: "matches setting keys" },
  { type: "index-settings", matches: "matches setting keys", extra: { key: "indices", label: "On indices", hint: "Which indices these settings may be changed on (default: all non-system)" } },
  { type: "index-mappings", matches: "matches index names · add-only" },
  { type: "index-delete", matches: "matches index names · permanent", danger: true, note: "Keep patterns narrow. Dot/system indices match only patterns that start with a dot." },
  { type: "index-templates", matches: "matches template names", extra: { key: "indexPatterns", label: "Index patterns", hint: "Which index_patterns templates may target. Without it, * and dot patterns are refused" } },
  { type: "component-templates", matches: "matches template names" },
  { type: "ilm-policies", matches: "matches policy names" },
  { type: "ingest-pipelines", matches: "matches pipeline names" },
];

function PatternList({ label, patterns, onChange, deny, placeholder }: { label: string; patterns: string[]; onChange: (p: string[]) => void; deny?: boolean; placeholder: string }) {
  const [value, setValue] = useState("");
  const id = useId();
  const add = () => {
    const v = value.trim();
    if (v && !patterns.includes(v)) onChange([...patterns, v]);
    setValue("");
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      add();
    }
  };
  return (
    <div className="stack-sm" style={{ gap: 8 }}>
      <span className="label" style={{ fontSize: 12, textTransform: "uppercase", letterSpacing: "0.05em", color: "var(--muted)" }}>{label}</span>
      <div className="row" style={{ gap: 6 }}>
        {patterns.length === 0 && <span className="hint">None</span>}
        {patterns.map((p) => (
          <span key={p} className={`chip ${deny ? "deny" : ""}`}>
            {p}
            <button type="button" aria-label={`Remove ${p}`} onClick={() => onChange(patterns.filter((x) => x !== p))}><Icon name="x" size={13} /></button>
          </span>
        ))}
      </div>
      <div className="row" style={{ flexWrap: "nowrap", gap: 6 }}>
        <label htmlFor={id} className="sr-only">Add {label.toLowerCase()} pattern</label>
        <input id={id} className="input mono" style={{ minHeight: 34 }} value={value} placeholder={placeholder} onChange={(e) => setValue(e.target.value)} onKeyDown={onKey} spellCheck={false} />
        <button type="button" className="btn btn-sm" onClick={add} disabled={!value.trim()}>Add</button>
      </div>
    </div>
  );
}

function RuleCard({ meta, rule, onChange }: { meta: (typeof TYPES)[number]; rule: Rule | undefined; onChange: (r: Rule | undefined) => void }) {
  const title = CONFIG_TYPE_LABEL[meta.type];
  if (!rule) {
    return (
      <section className="card" aria-label={title}>
        <div className="card-head" style={{ borderBottom: 0 }}>
          <div className="grow"><h2>{title}</h2><span className="hint">Locked · no changes allowed</span></div>
          <Badge><Icon name="lock" size={12} /> Locked</Badge>
          <button type="button" className="btn btn-sm" onClick={() => onChange({ allow: [], deny: [] })}>Unlock</button>
        </div>
      </section>
    );
  }
  const ph = meta.type.includes("settings") ? "e.g. indices.recovery.*" : meta.type.startsWith("index-") && meta.type !== "index-templates" ? "e.g. delest-log-*" : "e.g. logs-*";
  return (
    <section className="card" aria-label={title}>
      <div className="card-head">
        <div className="grow"><h2 style={{ color: meta.danger ? "var(--danger)" : undefined }}>{title}</h2><span className="hint">{meta.matches}</span></div>
        {rule.allow.length === 0 && <Badge tone="amber" title="Unlocked, but no pattern allows anything yet">Nothing allowed yet</Badge>}
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => onChange(undefined)}><Icon name="lock" size={14} /> Lock</button>
      </div>
      <div className="card-body">
        <PatternList label="Allow" patterns={rule.allow} placeholder={ph} onChange={(allow) => onChange({ ...rule, allow })} />
        <PatternList label="Deny" deny patterns={rule.deny} placeholder="Deny wins over allow" onChange={(deny) => onChange({ ...rule, deny })} />
        {meta.extra && (
          <div className="stack-sm">
            <PatternList label={meta.extra.label} patterns={rule[meta.extra.key] ?? []} placeholder="e.g. delest-log-*"
              onChange={(v) => { const next = { ...rule }; if (v.length) next[meta.extra!.key] = v; else delete next[meta.extra!.key]; onChange(next); }} />
            <span className="hint">{meta.extra.hint}</span>
          </div>
        )}
        {meta.note && <span className="hint">{meta.note}</span>}
      </div>
    </section>
  );
}

function normalize(r: Rules): Rules {
  const out: Rules = {};
  for (const [k, v] of Object.entries(r)) {
    if (!v) continue;
    const rule: Rule = { allow: v.allow ?? [], deny: v.deny ?? [] };
    if (v.indices?.length) rule.indices = v.indices;
    if (v.indexPatterns?.length) rule.indexPatterns = v.indexPatterns;
    out[k] = rule;
  }
  return out;
}

export function Allowlist() {
  const qc = useQueryClient();
  const toast = useToast();
  const clusters = useClusters().data ?? [];
  const [scope, setScope] = useState<string>("global");
  const isGlobal = scope === "global";
  const key = ["allowlist", scope];
  const q = useQuery({
    queryKey: key,
    queryFn: () => isGlobal
      ? get<{ rules: Rules; updatedAt?: string; updatedBy?: string; note?: string }>("/admin/allowlist").then((d) => ({ ...d, source: "global" }))
      : get<{ rules: Rules; effectiveSource: string }>(`/admin/allowlist/${enc(scope)}`).then((d) => ({ rules: d.rules, source: d.effectiveSource, updatedAt: undefined, updatedBy: undefined })),
  });
  const [draft, setDraft] = useState<Rules>({});
  const [json, setJson] = useState<string | null>(null);
  const [jsonErr, setJsonErr] = useState<string | null>(null);
  useEffect(() => { if (q.data) setDraft(normalize(q.data.rules)); }, [q.data]);

  const usesGlobal = !isGlobal && q.data?.source === "global";
  const dirty = !!q.data && !usesGlobal && JSON.stringify(normalize(q.data.rules)) !== JSON.stringify(normalize(draft));
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["allowlist"] });
    qc.invalidateQueries({ queryKey: ["allowlist-effective"] });
  };

  const save = useMutation({
    mutationFn: (rules: Rules) => request(isGlobal ? "/admin/allowlist" : `/admin/allowlist/${enc(scope)}`, { method: "PUT", body: normalize(rules) }),
    onSuccess: () => { invalidate(); toast(isGlobal ? "Global allowlist saved" : `Override for ${scope} saved`); },
  });
  const removeOverride = useMutation({
    mutationFn: () => request(`/admin/allowlist/${enc(scope)}`, { method: "DELETE" }),
    onSuccess: () => { invalidate(); toast(`${scope} uses the global allowlist again`); },
  });

  const applyJson = () => {
    const [v, err] = parseJson(json ?? "");
    if (err) return setJsonErr(err);
    if (!v || typeof v !== "object" || Array.isArray(v)) return setJsonErr("The allowlist must be a JSON object keyed by config type");
    setDraft(normalize(v as Rules));
    setJson(null);
  };

  return (
    <Page crumbs={[{ label: "Administration" }, { label: "Allowlist" }]} title="Allowlist">
      <div className="page-head">
        <div className="grow">
          <h1>Allowlist</h1>
          <span className="sub">What may be changed at all. Anything not listed is locked, even for editors. Deny wins over allow.</span>
        </div>
      </div>
      <div className="row">
        <div className="tabs" role="tablist" aria-label="Allowlist scope">
          <button type="button" role="tab" className="tab" aria-selected={isGlobal} onClick={() => setScope("global")}>Global</button>
          {clusters.map((c) => (
            <button key={c.id} type="button" role="tab" className="tab" aria-selected={scope === c.id} onClick={() => setScope(c.id)}>
              <span className="mono">{c.id}</span>&nbsp;override
            </button>
          ))}
        </div>
        <div className="grow" />
        {dirty && <Badge tone="amber">Unsaved changes</Badge>}
        {!usesGlobal && (
          <>
            <button type="button" className="btn" onClick={() => { setJson(pretty(normalize(draft))); setJsonErr(null); }} disabled={!q.data}>Edit as JSON</button>
            {dirty && <button type="button" className="btn" onClick={() => q.data && setDraft(normalize(q.data.rules))}>Discard</button>}
            <button type="button" className="btn btn-primary" disabled={!dirty || save.isPending} onClick={() => save.mutate(draft)}>{save.isPending ? "Saving…" : "Save allowlist"}</button>
          </>
        )}
      </div>

      {q.isLoading && <Loading />}
      {q.error && <ErrorCallout error={q.error} />}
      {save.error && <ErrorCallout error={save.error} />}
      {removeOverride.error && <ErrorCallout error={removeOverride.error} />}
      {isGlobal && q.data?.updatedBy && <span className="hint">Last saved by {q.data.updatedBy}, {when(q.data.updatedAt)}.</span>}
      {isGlobal && q.data && !Object.keys(q.data.rules).length && <Callout tone="warn" title="No allowlist yet">Every change is blocked until you unlock something below and save.</Callout>}

      {usesGlobal ? (
        <Callout tone="info" title={`${scope} uses the global allowlist`}>
          An override replaces the global list for this cluster only.
          <div className="row" style={{ marginTop: 10 }}>
            <button type="button" className="btn btn-sm" onClick={() => save.mutate(normalize(q.data!.rules))} disabled={save.isPending}>Create override from global</button>
          </div>
        </Callout>
      ) : q.data && (
        <>
          {!isGlobal && (
            <Callout tone="info" title={`Override for ${scope}`}>
              These rules replace the global allowlist on this cluster.{" "}
              <button type="button" className="btn btn-ghost btn-sm danger" style={{ minHeight: 0, padding: 0 }} onClick={() => removeOverride.mutate()} disabled={removeOverride.isPending}>
                Remove override
              </button>
            </Callout>
          )}
          <div className="grid-2">
            {TYPES.map((m) => (
              <RuleCard key={m.type} meta={m} rule={draft[m.type]}
                onChange={(r) => setDraft((d) => { const n = { ...d }; if (r) n[m.type] = r; else delete n[m.type]; return n; })} />
            ))}
          </div>
        </>
      )}

      {json !== null && (
        <Dialog title="Edit allowlist as JSON" subtitle="Keyed by config type. Apply puts it in the editor; nothing is saved until you click Save." onClose={() => setJson(null)} wide
          footer={<>
            <button type="button" className="btn" onClick={() => setJson(null)}>Cancel</button>
            <button type="button" className="btn btn-primary" onClick={applyJson}>Apply</button>
          </>}>
          <JsonEditor label="Allowlist JSON" value={json} onChange={(v) => { setJson(v); setJsonErr(null); }} height="420px" />
          {jsonErr && <Callout tone="danger">{jsonErr}</Callout>}
        </Dialog>
      )}
    </Page>
  );
}
