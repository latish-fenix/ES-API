import type { CompletionContext, CompletionResult } from "@codemirror/autocomplete";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { PendingApproval, enc, get, request, type DataFields, type IndexRow } from "../api";
import { Icon } from "../components/icons";
import { JsonEditor, parseJson } from "../components/JsonEditor";
import { Page, useClusterCrumbs } from "../components/Shell";
import { applyLabel, Badge, Callout, ErrorCallout, ReasonField, Spinner, copyText, useToast } from "../components/ui";
import { ago, num, pretty } from "../format";
import { useCluster, useHealth, useNeedsApproval } from "../session";
import { ChangePreview } from "./Requests";

type Method = "GET" | "POST" | "PUT" | "DELETE";
const METHODS: Method[] = ["GET", "POST", "PUT", "DELETE"];

interface ReadResult { kind: "read"; status: number; response: unknown; indices: string[]; tookMs: number }
interface WriteResult {
  kind: "write"; op: string; label: string; dryRun: boolean; result: Record<string, unknown>;
  needs: { reason: boolean; confirm?: string; count?: number }; approvalRequired: boolean;
}
type ShellResult = ReadResult | WriteResult;
interface HistoryItem { at: string; clusterId: string; method: Method; path: string; body: string }

const TARGET_ENDPOINTS = ["_search", "_count", "_mapping", "_settings", "_field_caps", "_msearch", "_validate/query",
  "_explain/", "_doc/", "_update/", "_update_by_query", "_delete_by_query", "_eql/search", "_terms_enum", "_alias", "_stats"];
const ROOT_ENDPOINTS = ["_cat/indices", "_cat/count", "_cat/aliases", "_cat/shards", "_cat/health", "_cat/nodes", "_cluster/health",
  "_sql?format=json", "_msearch", "_resolve/index/", "_index_template/", "_component_template/", "_ilm/policy/", "_ingest/pipeline/",
  "_analyze", "_cluster/settings"];

const DSL_WORDS = ["query", "bool", "must", "filter", "should", "must_not", "minimum_should_match", "term", "terms", "match",
  "match_phrase", "multi_match", "range", "gte", "lte", "gt", "lt", "exists", "field", "prefix", "wildcard", "query_string",
  "simple_query_string", "ids", "nested", "path", "size", "from", "sort", "order", "asc", "desc", "_source", "includes", "excludes",
  "track_total_hits", "aggs", "aggregations", "date_histogram", "calendar_interval", "fixed_interval", "histogram", "interval",
  "avg", "sum", "min", "max", "cardinality", "value_count", "stats", "extended_stats", "percentiles", "top_hits", "filters",
  "missing", "composite", "sources", "after", "min_doc_count", "format", "time_zone", "highlight", "fields", "set", "remove",
  "doc", "settings", "mappings", "properties", "type", "keyword", "text", "date", "long", "double", "boolean", "number_of_replicas",
  "refresh_interval", "persistent", "index_patterns", "priority", "template", "policy", "phases", "processors"];

function examples(index: string): { label: string; method: Method; path: string; body?: unknown }[] {
  const i = index || "my-index-*";
  return [
    { label: "Count by a field (terms aggregation)", method: "POST", path: `${i}/_search`, body: { size: 0, aggs: { by_status: { terms: { field: "status", size: 20 } } } } },
    { label: "Documents per day (date histogram)", method: "POST", path: `${i}/_search`, body: { size: 0, query: { range: { created_date: { gte: "now-30d/d" } } }, aggs: { per_day: { date_histogram: { field: "created_date", calendar_interval: "day" } } } } },
    { label: "Search with a bool query", method: "POST", path: `${i}/_search`, body: { size: 10, query: { bool: { filter: [{ term: { status: "new" } }, { range: { created_date: { gte: "now-7d" } } }] } }, sort: [{ created_date: "desc" }] } },
    { label: "Count matching documents", method: "POST", path: `${i}/_count`, body: { query: { term: { status: "new" } } } },
    { label: "Several searches at once (_msearch)", method: "POST", path: "_msearch", body: `{"index": "${i}"}\n{"size": 0, "query": {"term": {"status": "new"}}}\n{"index": "${i}"}\n{"size": 0, "query": {"term": {"status": "paid"}}}\n` },
    { label: "SQL", method: "POST", path: "_sql?format=json", body: { query: `SELECT status, COUNT(*) FROM "${i}" GROUP BY status` } },
    { label: "Mapping", method: "GET", path: `${i}/_mapping` },
    { label: "Indices (_cat/indices)", method: "GET", path: "_cat/indices" },
    { label: "Change a setting (dry run first)", method: "PUT", path: `${index || "my-index"}/_settings`, body: { index: { refresh_interval: "30s" } } },
    { label: "Update by query (set a field)", method: "POST", path: `${index || "my-index"}/_update_by_query`, body: { query: { term: { status: "new" } }, set: { status: "archived" } } },
    { label: "Delete by query", method: "POST", path: `${index || "my-index"}/_delete_by_query`, body: { query: { range: { created_date: { lt: "now-2y" } } } } },
  ];
}

function target(path: string): string | null {
  const first = path.replace(/^\//, "").split(/[/?]/)[0];
  return first && !first.startsWith("_") ? first : null;
}

function shQuote(s: string) {
  return `'${s.replace(/'/g, `'\\''`)}'`;
}

function MethodBadge({ m }: { m: string }) {
  const tone = m === "GET" ? "blue" : m === "DELETE" ? "red" : m === "PUT" ? "amber" : "green";
  return <Badge tone={tone} mono>{m}</Badge>;
}

/** Path input with suggestions for the segment being typed. */
function PathInput({ value, onChange, onRun, indices }: { value: string; onChange: (v: string, method?: Method) => void; onRun: () => void; indices: string[] }) {
  const [open, setOpen] = useState(false);
  const [sel, setSel] = useState(0);
  const ref = useRef<HTMLInputElement>(null);
  const suggestions = useMemo(() => {
    const v = value.replace(/^\//, "");
    if (v.includes("?")) return [];
    const parts = v.split("/");
    const last = parts[parts.length - 1].toLowerCase();
    let pool: string[];
    if (parts.length === 1) {
      const pats = Array.from(new Set(indices.map((i) => i.replace(/[-_.]?\d{4}[.-]\d{2}([.-]\d{2})?$/, "-*")).filter((p) => p.endsWith("*"))));
      pool = [...ROOT_ENDPOINTS, ...pats, ...indices];
    } else if (parts.length === 2 && !parts[0].startsWith("_")) {
      pool = TARGET_ENDPOINTS;
    } else return [];
    return pool.filter((s) => s.toLowerCase().includes(last) && s.toLowerCase() !== last).slice(0, 12);
  }, [value, indices]);
  const pick = (s: string) => {
    const parts = value.replace(/^\//, "").split("/");
    parts[parts.length - 1] = s;
    onChange(parts.join("/") + (parts.length === 1 && !s.startsWith("_") ? "/" : ""));
    setSel(0);
    ref.current?.focus();
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); setOpen(false); onRun(); return; }
    if (!open || !suggestions.length) { if (e.key === "Enter") { e.preventDefault(); onRun(); } return; }
    if (e.key === "ArrowDown") { e.preventDefault(); setSel((x) => (x + 1) % suggestions.length); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setSel((x) => (x - 1 + suggestions.length) % suggestions.length); }
    else if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); pick(suggestions[sel]); }
    else if (e.key === "Escape") setOpen(false);
  };
  return (
    <div className="sh-path">
      <input ref={ref} className="input mono" aria-label="Path" value={value} spellCheck={false} autoComplete="off"
        placeholder="my-index-*/_search"
        role="combobox" aria-expanded={open && suggestions.length > 0} aria-controls="sh-suggest"
        onFocus={() => setOpen(true)} onBlur={() => setTimeout(() => setOpen(false), 120)}
        onChange={(e) => {
          const m = e.target.value.match(/^\s*(GET|POST|PUT|DELETE)\s+(.*)$/i);   // pasted "GET my-index/_search"
          if (m) onChange(m[2].trim(), m[1].toUpperCase() as Method);
          else onChange(e.target.value);
          setOpen(true); setSel(0);
        }}
        onKeyDown={onKey} />
      {open && suggestions.length > 0 && (
        <ul className="sh-suggest" id="sh-suggest" role="listbox">
          {suggestions.map((s, i) => (
            <li key={s} role="option" aria-selected={i === sel} className={i === sel ? "on" : ""} onMouseDown={(e) => { e.preventDefault(); pick(s); }}>
              <span className="mono">{s}</span>
              <span className="hint">{s.startsWith("_") ? "endpoint" : s.includes("*") ? "pattern" : "index"}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function ShellPage() {
  const { clusterId, admin, can, access } = useCluster();
  const anyEdit = can("edit") || !!access?.indices.some((r) => r.level === "edit" || r.level === "delete");
  const health = useHealth(clusterId).data;
  const toast = useToast();
  const qc = useQueryClient();
  const needsApproval = useNeedsApproval();
  const base = `/clusters/${enc(clusterId)}`;
  const [method, setMethod] = useState<Method>("POST");
  const [path, setPath] = useState("");
  const [text, setText] = useState('{\n  "size": 0,\n  "aggs": {\n    "by_status": { "terms": { "field": "status" } }\n  }\n}');
  const [res, setRes] = useState<ShellResult | null>(null);
  const [reason, setReason] = useState("");
  const [confirm, setConfirm] = useState("");
  const [typed, setTyped] = useState("");
  const [applied, setApplied] = useState<Record<string, unknown> | null>(null);
  const [showEx, setShowEx] = useState(false);

  const indices = useQuery({
    queryKey: ["indices", clusterId],
    queryFn: () => get<{ items: IndexRow[] }>(`${base}/indices`).then((r) => r.items),
    staleTime: 60_000,
  });
  const names = useMemo(() => (indices.data ?? []).map((r) => r.index), [indices.data]);
  useEffect(() => {
    if (!path && names.length) setPath(`${names[0]}/_search`);
  }, [names, path]);

  const tgt = target(path);
  const fields = useQuery({
    queryKey: ["data-fields", clusterId, tgt],
    queryFn: () => get<DataFields>(`${base}/data/${enc(tgt!)}/_fields`),
    enabled: !!tgt && !tgt.includes(","),
    staleTime: 5 * 60_000,
    retry: false,
  });
  const fieldsRef = useRef<DataFields["fields"]>([]);
  fieldsRef.current = fields.data?.fields ?? [];
  const completions = useCallback((ctx: CompletionContext): CompletionResult | null => {
    const w = ctx.matchBefore(/"?[\w.@-]*/);
    if (!w || (w.from === w.to && !ctx.explicit)) return null;
    const quoted = w.text.startsWith('"');
    return {
      from: quoted ? w.from + 1 : w.from,
      options: [
        ...fieldsRef.current.filter((f) => !f.object).map((f) => ({ label: f.name, type: "property", detail: f.type, boost: 2 })),
        ...DSL_WORDS.map((k) => ({ label: k, type: "keyword" })),
      ],
      validFor: /^[\w.@-]*$/,
    };
  }, []);

  const history = useQuery({
    queryKey: ["shell-history", clusterId],
    queryFn: () => get<{ items: HistoryItem[] }>(`${base}/shell/history`).then((r) => r.items),
  });

  const hasBody = method !== "DELETE" && text.trim().length > 0;
  const bodyValue = (): unknown => {
    if (!text.trim() || method === "DELETE") return undefined;
    if (path.includes("_msearch")) return text.endsWith("\n") ? text : text + "\n";
    const [v, err] = parseJson(text);
    if (err) throw new Error(err);
    return v;
  };

  const send = useMutation({
    mutationFn: async (opts: { dryRun: boolean }) => {
      const body = bodyValue();
      const payload: Record<string, unknown> = { method, path: path.trim(), body, dryRun: opts.dryRun };
      if (!opts.dryRun) {
        payload.reason = reason.trim();
        if (res?.kind === "write") {
          if (res.needs.confirm) payload.confirm = confirm;
          if (res.result.dryRunToken) { payload.dryRunToken = res.result.dryRunToken; payload.expectedCount = res.result.count; }
        }
      }
      return (await request<ShellResult>(`${base}/shell`, { method: "POST", body: payload })).data;
    },
    onSuccess: (r, v) => {
      if (r.kind === "write" && !v.dryRun) {
        setApplied(r.result);
        toast(`${r.label}: ${r.result.noChange ? "no change" : "applied"}`);
        qc.invalidateQueries({ queryKey: ["indices", clusterId] });
      } else {
        setRes(r);
        setApplied(null);
        setReason(""); setConfirm(""); setTyped("");
      }
      qc.invalidateQueries({ queryKey: ["shell-history", clusterId] });
    },
    onError: () => qc.invalidateQueries({ queryKey: ["shell-history", clusterId] }),
  });
  const run = () => { setApplied(null); send.mutate({ dryRun: method !== "GET" }); };
  const runRef = useRef(run);
  runRef.current = run;
  const editorKeys = useMemo(() => ({
    keydown: (e: globalThis.KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); runRef.current(); return true; }
      return false;
    },
  }), []);

  const load = (h: { method: Method; path: string; body?: unknown }) => {
    setMethod(h.method);
    setPath(h.path);
    setText(typeof h.body === "string" ? h.body : h.body === undefined || h.body === "" ? "" : pretty(h.body));
    setRes(null); setApplied(null); send.reset();
  };

  const curl = () => {
    let body: unknown;
    try { body = bodyValue(); } catch { body = text; }
    const payload = JSON.stringify({ method, path: path.trim(), ...(body !== undefined ? { body } : {}) });
    const cmd = `curl -s -X POST ${shQuote(`${window.location.origin}/api/v1${base}/shell`)} \\\n  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \\\n  -d ${shQuote(payload)}`;
    copyText(cmd).then(() => toast("Copied as curl (set TOKEN from POST /api/v1/auth/login)"));
  };

  const w = res?.kind === "write" ? res : null;
  const sent = send.error instanceof PendingApproval;
  const n = w?.needs.count;
  const ready = !!w && !w.result.noChange && reason.trim().length > 0 && (!w.needs.confirm || confirm === w.needs.confirm) && (n === undefined || typed === String(n)) && !sent;
  const kindOf = (op: string) => (op.split(".")[0] as "config" | "index" | "doc" | "bulk");
  const responseText = res?.kind === "read" ? (typeof res.response === "string" ? res.response : pretty(res.response)) : "";

  return (
    <Page crumbs={useClusterCrumbs(clusterId, { label: "Shell" })} title="Shell" health={health?.status ?? null}>
      <div className="page-head" style={{ alignItems: "center" }}>
        <div className="stack-sm grow">
          <h1>Shell</h1>
          <span className="sub">
            Run Elasticsearch requests like in Kibana Dev Tools: searches, aggregations, SQL, mappings. You only reach indices you can view.
            {anyEdit ? " Changes run through the console's dry run, roll back" + (needsApproval ? " and admin approval." : ".") : " Changes need Edit access."}
          </span>
        </div>
        <div className="menu-wrap">
          <button type="button" className="btn" onClick={() => setShowEx((x) => !x)} aria-expanded={showEx}><Icon name="list" /> Examples</button>
          {showEx && (
            <div className="menu" role="menu" style={{ right: 0, minWidth: 320 }} onMouseLeave={() => setShowEx(false)}>
              {examples(tgt ?? names[0] ?? "").map((x) => (
                <button key={x.label} type="button" role="menuitem" onClick={() => { load(x); setShowEx(false); }}>
                  <MethodBadge m={x.method} /> {x.label}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      <section className="card sh-reqbar" aria-label="Request line">
            <select className="select sh-method" aria-label="Method" value={method} onChange={(e) => { setMethod(e.target.value as Method); setRes(null); }}>
              {METHODS.map((m) => <option key={m}>{m}</option>)}
            </select>
            <PathInput value={path} indices={names} onRun={run} onChange={(v, m) => { setPath(v); if (m) setMethod(m); }} />
            <button type="button" className="btn btn-primary" onClick={run} disabled={send.isPending || !path.trim()} title="Ctrl+Enter">
              {send.isPending ? <Spinner label="Running" /> : null} Run
            </button>
            <button type="button" className="btn" onClick={curl}><Icon name="copy" /> Copy as curl</button>
      </section>
      <div className="sh-grid">
        <section className="card sh-pane" aria-label="Request">
          <div className="sh-bar"><h2 className="grow" style={{ fontSize: 15 }}>Body</h2><span className="hint">JSON{path.includes("_msearch") ? " lines (NDJSON)" : ""}</span></div>
          <div className="sh-editor">
            {method === "DELETE" ? <div className="empty" style={{ padding: 20 }}>DELETE requests have no body.</div> : (
              <JsonEditor value={text} onChange={setText} height="440px" label="Request body" completions={completions}
                lint={!path.includes("_msearch") && text.trim().length > 0} extraKeys={editorKeys} />
            )}
          </div>
          <div className="sh-foot hint">
            {fields.data ? `${num(fields.data.fields.length)} fields of ${tgt} for autocomplete` : tgt ? "Autocomplete: DSL words" : "Start the path with an index or pattern"} · Ctrl+Enter runs · {hasBody ? "JSON body" : "no body"}
          </div>
        </section>

        <section className="card sh-pane" aria-label="Response">
          <div className="sh-bar">
            <h2 className="grow" style={{ fontSize: 15 }}>{w ? (w.dryRun ? `Dry run · ${w.label}` : w.label) : "Response"}</h2>
            {res?.kind === "read" && <>
              <Badge tone={res.status < 300 ? "green" : "red"} mono>{res.status}</Badge>
              <span className="hint">{num(res.tookMs)} ms{res.indices.length ? ` · ${res.indices.length} ${res.indices.length === 1 ? "index" : "indices"}` : ""}</span>
              <button type="button" className="btn btn-ghost icon-btn" aria-label="Copy response" title="Copy response" onClick={() => copyText(responseText).then(() => toast("Response copied"))}><Icon name="copy" /></button>
            </>}
          </div>
          <div className="sh-out">
            {send.error && !(send.error instanceof PendingApproval) && <div style={{ padding: 14 }}><ErrorCallout error={send.error} clusterId={clusterId} admin={admin} /></div>}
            {!res && !send.error && <div className="empty" style={{ padding: 30 }}><Icon name="terminal" size={22} /> Run a request to see the response here.</div>}
            {res?.kind === "read" && <JsonEditor value={responseText} readOnly height="490px" label="Response" lint={false} />}
            {w && (
              <div className="stack" style={{ padding: 14 }}>
                {applied ? (
                  <Callout tone="success" title={applied.noChange ? "No change: it already matched" : "Applied"}>
                    {applied.changeId ? <>Change <span className="mono">{String(applied.changeId).slice(0, 12)}</span> is in the audit log and can be rolled back there. </> : null}
                    {applied.succeeded !== undefined ? `${num(applied.succeeded as number)} documents changed.` : null}
                  </Callout>
                ) : sent ? <ErrorCallout error={send.error} /> : (
                  <>
                    <Callout tone="info" title="This is a change: nothing has been done yet">
                      It runs through the console's {w.label.toLowerCase()} flow: the dry run below, a reason, the usual snapshot or backup so it can be rolled back{w.approvalRequired ? ", and an admin's approval" : ""}.
                    </Callout>
                    <ChangePreview r={{ op: w.op, kind: kindOf(w.op), body: (parseJson(text)[0] as Record<string, unknown>) ?? {} }} preview={w.result} />
                  </>
                )}
                {!applied && !sent && !w.result.noChange && (
                  <div className="stack-sm">
                    <ReasonField value={reason} onChange={setReason} placeholder="Why is this change needed?" />
                    {w.needs.confirm && (
                      <div className="field">
                        <label className="label" htmlFor="sh-confirm">To confirm, type <code>{w.needs.confirm}</code></label>
                        <input id="sh-confirm" className="input mono" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="off" />
                      </div>
                    )}
                    {n !== undefined && (
                      <div className="field" style={{ maxWidth: 320 }}>
                        <label className="label" htmlFor="sh-count">To confirm, type the number of documents: <code>{n}</code></label>
                        <input id="sh-count" className="input mono" inputMode="numeric" value={typed} onChange={(e) => setTyped(e.target.value.trim())} autoComplete="off" />
                      </div>
                    )}
                    <div className="row" style={{ gap: 8 }}>
                      <button type="button" className={`btn ${w.op.endsWith("delete") ? "btn-danger" : "btn-primary"}`} disabled={!ready || send.isPending} onClick={() => send.mutate({ dryRun: false })}>
                        {send.isPending ? "Working…" : applyLabel(w.approvalRequired, "Apply change")}
                      </button>
                      {w.approvalRequired && <span className="hint">Applied when an admin approves it; you get an email.</span>}
                    </div>
                  </div>
                )}
                {w.result.noChange ? <Callout title="No change">The live state already matches this request.</Callout> : null}
              </div>
            )}
          </div>
        </section>
      </div>

      <section className="card">
        <div className="card-head">
          <div className="grow"><h2>History</h2><span className="sub">Your last 50 requests on {clusterId}; only you see them</span></div>
          {!!history.data?.length && (
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => request(`${base}/shell/history/_clear`, { method: "POST" }).then(() => qc.invalidateQueries({ queryKey: ["shell-history", clusterId] }))}>Clear</button>
          )}
        </div>
        {history.error ? <div className="card-body"><ErrorCallout error={history.error} /></div> : !history.data?.length ? (
          <div className="empty" style={{ padding: 18 }}>Nothing yet.</div>
        ) : (
          <ul className="sh-history">
            {history.data.map((h, i) => (
              <li key={i}>
                <button type="button" onClick={() => load({ method: h.method, path: h.path, body: h.body ? (h.path.includes("_msearch") ? h.body : (parseJson(h.body)[0] ?? h.body)) : undefined })}>
                  <MethodBadge m={h.method} />
                  <span className="mono sh-hpath">{h.path}</span>
                  <span className="hint">{ago(h.at)}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </Page>
  );
}
