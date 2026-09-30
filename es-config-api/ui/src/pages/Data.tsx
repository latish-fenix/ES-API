import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Fragment, useEffect, useId, useMemo, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import {
  downloadPost, enc, get, request,
  type DataField, type DataFields, type DataFilter, type DataHit, type DataSearchBody, type DataSearchResult, type FilterOp, type IndexRow,
} from "../api";
import { Icon } from "../components/icons";
import { Page, useClusterCrumbs } from "../components/Shell";
import { Badge, Callout, Dialog, Empty, ErrorCallout, Loading, Spinner, copyText, useToast } from "../components/ui";
import { num } from "../format";
import { BulkDialog, ChangesDialog, DeleteDoc, DocHistory, EditDoc, NewDocDialog } from "./DataEdit";
import { useCluster, useHealth } from "../session";

// ------------------------------------------------------------------ helpers

const PAGE_SIZES = [10, 25, 50, 100];
const MAX_EXPORT = 10_000;

const OPS: { op: FilterOp; label: string; short: string }[] = [
  { op: "is", label: "is", short: ":" },
  { op: "is_not", label: "is not", short: "≠" },
  { op: "one_of", label: "is one of", short: "∈" },
  { op: "not_one_of", label: "is not one of", short: "∉" },
  { op: "contains", label: "contains", short: "~" },
  { op: "between", label: "is between", short: "↔" },
  { op: "exists", label: "exists", short: "" },
  { op: "not_exists", label: "does not exist", short: "" },
];
const OP_LABEL = Object.fromEntries(OPS.map((o) => [o.op, o.label])) as Record<FilterOp, string>;

const TIME_PRESETS: [string, string][] = [
  ["now-15m", "Last 15 minutes"], ["now-1h", "Last hour"], ["now-24h", "Last 24 hours"], ["now-7d", "Last 7 days"],
  ["now-30d", "Last 30 days"], ["now-90d", "Last 90 days"], ["now-1y", "Last year"],
];

/** Value at a dotted path; arrays of objects give a list (same rules as the API's CSV export). */
export function getPath(src: unknown, path: string): unknown {
  if (!src || typeof src !== "object" || Array.isArray(src)) return undefined;
  const o = src as Record<string, unknown>;
  if (path in o) return o[path];
  const parts = path.split(".");
  for (let i = parts.length - 1; i > 0; i--) {
    const key = parts.slice(0, i).join(".");
    if (key in o) {
      const sub = o[key];
      const rest = parts.slice(i).join(".");
      if (Array.isArray(sub)) {
        const vals = sub.map((x) => getPath(x, rest)).filter((v) => v !== undefined && v !== null);
        return vals.length ? vals : undefined;
      }
      return getPath(sub, rest);
    }
  }
  return undefined;
}

function leafEntries(src: unknown, prefix = ""): [string, unknown][] {
  if (!src || typeof src !== "object" || Array.isArray(src)) return [[prefix, src]];
  const out: [string, unknown][] = [];
  for (const [k, v] of Object.entries(src as Record<string, unknown>)) {
    const p = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === "object" && !Array.isArray(v) && Object.keys(v).length) out.push(...leafEntries(v, p));
    else out.push([p, v]);
  }
  return out;
}

function cellText(v: unknown): string {
  if (v === undefined || v === null) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return JSON.stringify(v);
}

function filterText(f: DataFilter): string {
  switch (f.op) {
    case "exists": return `${f.field} exists`;
    case "not_exists": return `${f.field} does not exist`;
    case "between": return `${f.field}: ${f.gte || "…"} to ${f.lte || "…"}`;
    case "one_of": case "not_one_of": return `${f.field} ${OP_LABEL[f.op]} ${(f.values ?? []).join(", ")}`;
    default: return `${f.field} ${OP_LABEL[f.op]} ${f.value ?? ""}`;
  }
}

function readJson<T>(s: string | null, fallback: T): T {
  if (!s) return fallback;
  try {
    return JSON.parse(s) as T;
  } catch {
    return fallback;
  }
}

function storedCols(key: string): string[] | null {
  try {
    const v = localStorage.getItem(key);
    return v ? (JSON.parse(v) as string[]) : null;
  } catch {
    return null;
  }
}
function storeCols(key: string, cols: string[]) {
  try {
    localStorage.setItem(key, JSON.stringify(cols));
  } catch {
    /* private mode: ignore */
  }
}

function defaultColumns(fields: DataField[], sample?: Record<string, unknown>): string[] {
  // The first document's top-level fields, in its own order (objects show as JSON), up to 8.
  if (sample && Object.keys(sample).length) return Object.keys(sample).slice(0, 8);
  const top = fields.filter((f) => !f.name.includes("."));
  return (top.length ? top : fields.filter((f) => !f.object)).slice(0, 8).map((f) => f.name);
}

function toLocalInput(v: string | undefined): string {
  if (!v || v.startsWith("now")) return "";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// ------------------------------------------------------------------ page

export function Data() {
  const { clusterId, admin, canIndex } = useCluster();
  const health = useHealth(clusterId).data;
  const [params, setParams] = useSearchParams();
  const toast = useToast();
  const base = `/clusters/${enc(clusterId)}`;

  const index = params.get("index") ?? "";
  const q = params.get("q") ?? "";
  const filters = useMemo(() => readJson<DataFilter[]>(params.get("f"), []), [params]);
  const timeField = params.get("tf") ?? "";
  const tg = params.get("tg") ?? "";
  const tl = params.get("tl") ?? "";
  const sortParam = params.get("sort") ?? "";
  const size = PAGE_SIZES.includes(Number(params.get("size"))) ? Number(params.get("size")) : 25;
  const from = Math.max(0, Number(params.get("from")) || 0);

  const set = (patch: Record<string, string | null>, resetPage = true) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === "") next.delete(k);
      else next.set(k, v);
    }
    if (resetPage && !("from" in patch)) next.delete("from");
    setParams(next);
  };

  const [indexDraft, setIndexDraft] = useState(index);
  const [qDraft, setQDraft] = useState(q);
  useEffect(() => setIndexDraft(index), [index]);
  useEffect(() => setQDraft(q), [q]);

  const [filterEdit, setFilterEdit] = useState<{ i: number | null; f: DataFilter } | null>(null);
  const [colsOpen, setColsOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [docAt, setDocAt] = useState<number | null>(null);
  const [newDoc, setNewDoc] = useState(false);
  const [bulk, setBulk] = useState<"update" | "delete" | null>(null);
  const [bulkMenu, setBulkMenu] = useState(false);
  const [changes, setChanges] = useState(false);

  const indices = useQuery({
    queryKey: ["indices", clusterId],
    queryFn: () => get<{ items: IndexRow[] }>(`${base}/indices`).then((r) => r.items),
  });
  const fields = useQuery({
    queryKey: ["data-fields", clusterId, index],
    queryFn: () => get<DataFields>(`${base}/data/${enc(index)}/_fields`),
    enabled: !!index,
    retry: false,
  });
  const fieldMap = useMemo(() => new Map((fields.data?.fields ?? []).map((f) => [f.name, f])), [fields.data]);

  const colsKey = `esc.cols.${clusterId}.${index}`;
  const colsParam = params.get("cols");
  // Default columns come from the first document seen for this index and then stay put
  // (so sorting or paging doesn't reshuffle them).
  const [auto, setAuto] = useState<{ key: string; cols: string[] } | null>(null);
  const setColumns = (cols: string[]) => {
    storeCols(colsKey, cols);
    set({ cols: cols.join(",") }, false);
  };

  const sort = useMemo(() => {
    if (!sortParam) return [];
    const i = sortParam.lastIndexOf(":");
    const field = sortParam.slice(0, i);
    const order = sortParam.slice(i + 1) === "asc" ? "asc" : "desc";
    const f = fieldMap.get(field);
    return [{ field, order, ...(f && f.type !== "conflict" && !f.object ? { unmappedType: f.type } : {}) } as DataSearchBody["sort"][number]];
  }, [sortParam, fieldMap]);

  const body: DataSearchBody = useMemo(() => ({
    query: q, filters, sort, from, size,
    timeRange: timeField && (tg || tl) ? { field: timeField, gte: tg || undefined, lte: tl || undefined } : null,
  }), [q, filters, sort, from, size, timeField, tg, tl]);

  const search = useQuery({
    queryKey: ["data-search", clusterId, index, body],
    queryFn: () => request<DataSearchResult>(`${base}/data/${enc(index)}/_search`, { method: "POST", body }).then((r) => r.data),
    enabled: !!index,
    placeholderData: keepPreviousData,
    retry: false,
    staleTime: 30_000,
  });
  const res = search.data;
  const hits = res?.hits ?? [];
  const sample = hits[0]?._source;
  useEffect(() => {
    if (fields.data && auto?.key !== colsKey && (sample || res)) setAuto({ key: colsKey, cols: defaultColumns(fields.data.fields, sample) });
  }, [fields.data, sample, res, colsKey, auto?.key]);
  const columns = useMemo(() => {
    if (colsParam) return colsParam.split(",").filter(Boolean);
    const saved = storedCols(colsKey);
    if (saved?.length) return saved;
    if (auto?.key === colsKey) return auto.cols;
    return fields.data ? defaultColumns(fields.data.fields, sample) : [];
  }, [colsParam, colsKey, fields.data, auto, sample]);

  const openIndex = (e?: FormEvent) => {
    e?.preventDefault();
    const v = indexDraft.trim();
    if (v && v !== index) set({ index: v, cols: null, sort: null, tf: null, tg: null, tl: null });
  };
  const submitQuery = (e: FormEvent) => {
    e.preventDefault();
    if (indexDraft.trim() !== index) {
      set({ index: indexDraft.trim(), q: qDraft.trim(), cols: null, sort: null, tf: null, tg: null, tl: null });
    } else if (qDraft.trim() === q) {
      search.refetch();
    } else set({ q: qDraft.trim() });
  };
  const setFilters = (next: DataFilter[]) => set({ f: next.length ? JSON.stringify(next) : null });
  const addFilter = (f: DataFilter) => setFilters([...filters, f]);

  const toggleSort = (field: string) => {
    const f = fieldMap.get(field);
    if (!f?.aggregatable) return;
    const cur = sort[0]?.field === field ? sort[0].order : null;
    set({ sort: cur === "desc" ? `${field}:asc` : cur === "asc" ? null : `${field}:desc` });
  };

  const total = res?.total ?? 0;
  const editable = fields.data?.editableIndices ?? [];
  const allIdx = fields.data?.indices ?? [];
  const fullScope = !!fields.data && !fields.data.hiddenIndices && allIdx.length > 0;
  const canBulkUpdate = fullScope && allIdx.every((i) => canIndex(i, "edit"));
  const canBulkDelete = fullScope && allIdx.every((i) => canIndex(i, "delete"));
  const describe = [
    `index ${index}`, q ? `query ${q}` : "no query",
    ...filters.map(filterText),
    timeField && (tg || tl) ? `${timeField} from ${tg || "…"} to ${tl || "…"}` : "",
  ].filter(Boolean).join(" · ");
  const maxWin = res?.maxWindow ?? 10_000;
  const pageable = Math.min(total, maxWin);
  const lastFrom = Math.max(0, Math.floor((pageable - 1) / size) * size);

  const crumbs = useClusterCrumbs(clusterId, { label: "Data" }, ...(index ? [{ label: index }] : []));
  const indexNames = (indices.data ?? []).map((r) => r.index);
  const listId = useId();

  return (
    <Page crumbs={crumbs} title={index ? `Data · ${index}` : "Data"} health={health?.status ?? null}>
      <div className="page-head">
        <div className="grow">
          <h1>Data</h1>
          <p className="sub">Search, read and export documents; with edit access, change them too (every change needs a reason and can be undone). System (dot) indices can't be opened. Searches, exports and changes are audited: the query and field names, never the documents.</p>
        </div>
      </div>

      <section className="card">
        <form className="data-bar" onSubmit={submitQuery}>
          <div className="field" style={{ flex: "0 1 380px", minWidth: 220 }}>
            <label className="label" htmlFor="d-index">Index or pattern</label>
            <input id="d-index" className="input mono" list={listId} value={indexDraft} placeholder="e.g. shoppremiumoutlets*-2024.09"
              onChange={(e) => setIndexDraft(e.target.value)} onBlur={() => openIndex()} spellCheck={false} autoComplete="off" />
            <datalist id={listId}>{indexNames.map((n) => <option key={n} value={n} />)}</datalist>
          </div>
          <div className="field grow" style={{ minWidth: 260 }}>
            <label className="label" htmlFor="d-q">Query <span className="hint">· Lucene syntax, e.g. <code>vendor:2593 AND order_info.order_number:SP0286*</code></span></label>
            <input id="d-q" className="input mono" value={qDraft} placeholder="Leave empty to match everything" onChange={(e) => setQDraft(e.target.value)} spellCheck={false} />
          </div>
          <div className="field" style={{ justifyContent: "flex-end" }}>
            <button type="submit" className="btn btn-primary" disabled={!indexDraft.trim()}><Icon name="search" /> Search</button>
          </div>
        </form>

        {index && (
          <div className="data-bar" style={{ paddingTop: 0, alignItems: "center" }}>
            <TimeRange fields={fields.data?.dateFields ?? []} field={timeField} gte={tg} lte={tl}
              onChange={(p) => set(p)} />
            <div className="row filter-row">
              <Icon name="filter" size={16} style={{ color: "var(--faint)" }} />
              {filters.map((f, i) => (
                <span key={i} className={`chip ${["is_not", "not_one_of", "not_exists"].includes(f.op) ? "deny" : ""}`}>
                  <button type="button" className="chip-text" onClick={() => setFilterEdit({ i, f })} title="Edit filter">{filterText(f)}</button>
                  <button type="button" aria-label={`Remove filter ${filterText(f)}`} onClick={() => setFilters(filters.filter((_, j) => j !== i))}><Icon name="x" size={14} /></button>
                </span>
              ))}
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => setFilterEdit({ i: null, f: { field: "", op: "is", value: "" } })}>
                <Icon name="plus" size={15} /> Add filter
              </button>
              {filters.length > 1 && <button type="button" className="btn btn-ghost btn-sm" onClick={() => setFilters([])}>Clear filters</button>}
            </div>
          </div>
        )}
      </section>

      {!index ? (
        <section className="card">
          <Empty title="Pick an index to browse">
            Type an index name or a pattern with <code>*</code> above, or choose one from the list.
            {indexNames.length > 0 && (
              <div className="row" style={{ gap: 6, flexWrap: "wrap", justifyContent: "center", marginTop: 12 }}>
                {indexNames.slice(0, 12).map((n) => (
                  <button key={n} type="button" className="btn btn-sm" onClick={() => set({ index: n })}><span className="mono">{n}</span></button>
                ))}
              </div>
            )}
          </Empty>
        </section>
      ) : fields.error ? (
        <section className="card"><div className="card-body"><ErrorCallout error={fields.error} clusterId={clusterId} admin={admin} /></div></section>
      ) : (
        <section className="card" style={{ overflow: "hidden" }}>
          <div className="data-toolbar">
            <div className="row" style={{ gap: 10, minWidth: 0 }}>
              <strong style={{ fontSize: 15 }}>{res ? `${num(total)}${res.totalRelation === "gte" ? "+" : ""} documents` : "Searching…"}</strong>
              {res && <span className="hint">{num(res.took)} ms{fields.data && fields.data.indices.length > 1 ? ` · ${fields.data.indices.length} indices` : ""}</span>}
              {search.isFetching && <Spinner label="Searching" />}
              {res?.timedOut && <Badge tone="amber">Timed out: partial results</Badge>}
            </div>
            <div className="grow" />
            {editable.length > 0 && <button type="button" className="btn btn-sm" onClick={() => setNewDoc(true)}><Icon name="plus" size={15} /> New document</button>}
            <button type="button" className="btn btn-sm" onClick={() => setColsOpen(true)} disabled={!fields.data}><Icon name="columns" size={15} /> Columns · {columns.length}</button>
            <button type="button" className="btn btn-sm" onClick={() => setExportOpen(true)} disabled={!res || total === 0}><Icon name="download" size={15} /> Export</button>
            {canBulkUpdate || canBulkDelete ? (
              <div className="menu-wrap">
                <button type="button" className="btn btn-sm" aria-haspopup="menu" aria-expanded={bulkMenu} onClick={() => setBulkMenu(!bulkMenu)}>Bulk <Icon name="caret" size={14} /></button>
                {bulkMenu && (
                  <div className="menu" role="menu" onMouseLeave={() => setBulkMenu(false)}>
                    <button type="button" role="menuitem" disabled={!canBulkUpdate || !total} onClick={() => { setBulk("update"); setBulkMenu(false); }}>Update matching documents…</button>
                    <button type="button" role="menuitem" className="danger" disabled={!canBulkDelete || !total} onClick={() => { setBulk("delete"); setBulkMenu(false); }}>Delete matching documents…</button>
                    <button type="button" role="menuitem" onClick={() => { setChanges(true); setBulkMenu(false); }}>Bulk changes and undo…</button>
                  </div>
                )}
              </div>
            ) : null}
          </div>

          {search.error ? (
            <div className="card-body"><ErrorCallout error={search.error} clusterId={clusterId} admin={admin} /></div>
          ) : !res ? (
            <Loading what="Searching…" />
          ) : hits.length === 0 ? (
            <Empty title="No documents match">Try a broader query, remove a filter or widen the time range.</Empty>
          ) : (
            <div className="table-scroll data-scroll">
              <table className="table compact data-table">
                <thead>
                  <tr>
                    <th>_index</th>
                    <th>_id</th>
                    {columns.map((c) => {
                      const f = fieldMap.get(c);
                      const active = sort[0]?.field === c ? sort[0].order : null;
                      return (
                        <th key={c} aria-sort={active === "asc" ? "ascending" : active === "desc" ? "descending" : undefined}>
                          {f?.aggregatable ? (
                            <button type="button" className="th-sort" onClick={() => toggleSort(c)} title={`Sort by ${c}`}>
                              {c}
                              <Icon name="caret" size={14} style={{ opacity: active ? 1 : 0.35, transform: active === "asc" ? "rotate(180deg)" : undefined }} />
                            </button>
                          ) : <span title={f ? `${f.type}${f.object ? "" : " · not sortable"}` : "not in the mapping"}>{c}</span>}
                        </th>
                      );
                    })}
                  </tr>
                </thead>
                <tbody>
                  {hits.map((h, i) => (
                    <tr key={`${h._index}/${h._id}`} className="clickable" onClick={() => setDocAt(i)} tabIndex={0}
                      onKeyDown={(e) => { if (e.key === "Enter") setDocAt(i); }}>
                      <td className="cell-mono">{h._index}</td>
                      <td className="cell-mono">{h._id}</td>
                      {columns.map((c) => {
                        const t = cellText(getPath(h._source, c));
                        return <td key={c} className="cell-mono data-cell" title={t.length > 60 ? t.slice(0, 2000) : undefined}>{t || <span className="faint">—</span>}</td>;
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {res && total > 0 && (
            <div className="data-toolbar" style={{ borderTop: "1px solid var(--row-border)", borderBottom: 0 }}>
              {total > maxWin && <span className="hint">Only the first {num(maxWin)} can be paged through; narrow the search to see the rest.</span>}
              <div className="grow" />
              <label className="row hint" style={{ gap: 6 }}>Rows per page
                <select className="select" style={{ width: 78, minHeight: 32 }} value={size} onChange={(e) => set({ size: e.target.value })}>
                  {PAGE_SIZES.map((n) => <option key={n} value={n}>{n}</option>)}
                </select>
              </label>
              <span className="hint nowrap">{num(from + 1)}–{num(Math.min(from + size, total))} of {num(total)}{res.totalRelation === "gte" ? "+" : ""}</span>
              <div className="row" style={{ gap: 2 }}>
                <button type="button" className="btn btn-ghost icon-btn" aria-label="First page" disabled={from === 0} onClick={() => set({ from: null }, false)}><Icon name="first" /></button>
                <button type="button" className="btn btn-ghost icon-btn" aria-label="Previous page" disabled={from === 0} onClick={() => set({ from: String(Math.max(0, from - size)) }, false)}><Icon name="chevronLeft" /></button>
                <button type="button" className="btn btn-ghost icon-btn" aria-label="Next page" disabled={from + size >= pageable} onClick={() => set({ from: String(from + size) }, false)}><Icon name="chevron" /></button>
                <button type="button" className="btn btn-ghost icon-btn" aria-label="Last page" disabled={from >= lastFrom} onClick={() => set({ from: String(lastFrom) }, false)}><Icon name="last" /></button>
              </div>
            </div>
          )}
          {res?.shardFailures?.length ? (
            <div className="card-body" style={{ paddingTop: 0 }}>
              <Callout tone="warn" title="Some shards failed">{res.shardFailures.filter(Boolean).join("; ")}</Callout>
            </div>
          ) : null}
        </section>
      )}

      {filterEdit && (
        <FilterDialog fields={fields.data?.fields ?? []} initial={filterEdit.f} editing={filterEdit.i !== null}
          onClose={() => setFilterEdit(null)}
          onSave={(f) => {
            if (filterEdit.i === null) addFilter(f);
            else setFilters(filters.map((x, j) => (j === filterEdit.i ? f : x)));
            setFilterEdit(null);
          }} />
      )}
      {colsOpen && fields.data && (
        <ColumnsDialog fields={fields.data.fields} columns={columns} onClose={() => setColsOpen(false)}
          onSave={(c) => { setColumns(c); setColsOpen(false); }}
          onReset={() => { setColumns(defaultColumns(fields.data!.fields, sample)); setColsOpen(false); }} />
      )}
      {exportOpen && res && (
        <ExportDialog total={total} columns={columns} onClose={() => setExportOpen(false)}
          run={async (format, cols, limit) => {
            const { blob, filename, rows } = await downloadPost(`${base}/data/${enc(index)}/_export`, { ...body, from: 0, size: 0, format, columns: cols, limit });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
            toast(`Downloaded ${num(rows)} documents as ${filename}`);
            setExportOpen(false);
          }} />
      )}
      {newDoc && (
        <NewDocDialog clusterId={clusterId} indices={editable} initialIndex={index} sample={sample} onClose={() => setNewDoc(false)} />
      )}
      {bulk && <BulkDialog op={bulk} clusterId={clusterId} index={index} search={body} describe={describe} onClose={() => setBulk(null)} />}
      {changes && <ChangesDialog clusterId={clusterId} onClose={() => setChanges(false)} />}
      {docAt !== null && hits[docAt] && (
        <DocDialog clusterId={clusterId} hit={hits[docAt]} fieldMap={fieldMap} position={`${from + docAt + 1} of ${num(total)}`}
          onPrev={docAt > 0 ? () => setDocAt(docAt - 1) : undefined}
          onNext={docAt < hits.length - 1 ? () => setDocAt(docAt + 1) : undefined}
          onClose={() => setDocAt(null)}
          onFilter={(f) => { addFilter(f); setDocAt(null); }} />
      )}
    </Page>
  );
}

// ------------------------------------------------------------------ time range

function TimeRange({ fields, field, gte, lte, onChange }: {
  fields: string[];
  field: string;
  gte: string;
  lte: string;
  onChange: (p: Record<string, string | null>) => void;
}) {
  const preset = !gte && !lte ? "" : gte.startsWith("now") && !lte ? gte : "custom";
  const [custom, setCustom] = useState(preset === "custom");
  if (!fields.length) return null;
  const f = field || fields[0];
  const mode = custom ? "custom" : preset;
  return (
    <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
      <Icon name="clock" size={16} style={{ color: "var(--faint)" }} />
      <select className="select" aria-label="Time field" style={{ width: "auto", minHeight: 34 }} value={f}
        onChange={(e) => onChange({ tf: e.target.value })} disabled={!gte && !lte && !custom}>
        {fields.map((n) => <option key={n} value={n}>{n}</option>)}
      </select>
      <select className="select" aria-label="Time range" style={{ width: "auto", minHeight: 34 }} value={mode}
        onChange={(e) => {
          const v = e.target.value;
          if (v === "custom") setCustom(true);
          else {
            setCustom(false);
            onChange(v ? { tf: f, tg: v, tl: null } : { tf: null, tg: null, tl: null });
          }
        }}>
        <option value="">Any time</option>
        {TIME_PRESETS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        <option value="custom">Custom range…</option>
      </select>
      {mode === "custom" && (
        <>
          <input type="datetime-local" className="input" aria-label="From" style={{ width: "auto", minHeight: 34 }} defaultValue={toLocalInput(gte)}
            onChange={(e) => onChange({ tf: f, tg: e.target.value ? new Date(e.target.value).toISOString() : null })} />
          <span className="hint">to</span>
          <input type="datetime-local" className="input" aria-label="To" style={{ width: "auto", minHeight: 34 }} defaultValue={toLocalInput(lte)}
            onChange={(e) => onChange({ tf: f, tl: e.target.value ? new Date(e.target.value).toISOString() : null })} />
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ filter dialog

function FilterDialog({ fields, initial, editing, onSave, onClose }: {
  fields: DataField[];
  initial: DataFilter;
  editing: boolean;
  onSave: (f: DataFilter) => void;
  onClose: () => void;
}) {
  const [f, setF] = useState<DataFilter>(initial);
  const [valuesText, setValuesText] = useState((initial.values ?? []).join("\n"));
  const listId = useId();
  const meta = fields.find((x) => x.name === f.field);
  const needsValue = f.op === "is" || f.op === "is_not" || f.op === "contains";
  const needsValues = f.op === "one_of" || f.op === "not_one_of";
  const values = valuesText.split(/[\n,]/).map((v) => v.trim()).filter(Boolean);
  const valid = !!f.field.trim() && (!needsValue || !!(f.value ?? "").trim()) && (!needsValues || values.length > 0) &&
    (f.op !== "between" || !!(f.gte || f.lte));
  const save = () => {
    const out: DataFilter = { field: f.field.trim(), op: f.op };
    if (needsValue) out.value = (f.value ?? "").trim();
    if (needsValues) out.values = values;
    if (f.op === "between") {
      if (f.gte) out.gte = f.gte.trim();
      if (f.lte) out.lte = f.lte.trim();
    }
    onSave(out);
  };
  return (
    <Dialog title={editing ? "Edit filter" : "Add filter"} onClose={onClose}
      footer={<>
        <button type="button" className="btn" onClick={onClose}>Cancel</button>
        <button type="button" className="btn btn-primary" disabled={!valid} onClick={save}>{editing ? "Save filter" : "Add filter"}</button>
      </>}>
      <form className="stack" onSubmit={(e) => { e.preventDefault(); if (valid) save(); }}>
        <div className="field">
          <label className="label" htmlFor="flt-field">Field</label>
          <input id="flt-field" className="input mono" list={listId} value={f.field} autoComplete="off" spellCheck={false}
            placeholder="e.g. order_info.order_number" onChange={(e) => setF({ ...f, field: e.target.value })} />
          <datalist id={listId}>{fields.filter((x) => !x.object).map((x) => <option key={x.name} value={x.name}>{x.type}</option>)}</datalist>
          {meta && <span className="hint">Type: {meta.type}{meta.type === "text" ? " (full text: “is” matches the phrase; “contains” matches words)" : ""}</span>}
        </div>
        <div className="field">
          <label className="label" htmlFor="flt-op">Operator</label>
          <select id="flt-op" className="select" value={f.op} onChange={(e) => setF({ ...f, op: e.target.value as FilterOp })}>
            {OPS.map((o) => <option key={o.op} value={o.op}>{o.label}</option>)}
          </select>
        </div>
        {needsValue && (
          <div className="field">
            <label className="label" htmlFor="flt-value">Value</label>
            <input id="flt-value" className="input mono" value={f.value ?? ""} onChange={(e) => setF({ ...f, value: e.target.value })} />
            {f.op === "contains" && <span className="hint">Case-insensitive; best on keyword fields.</span>}
          </div>
        )}
        {needsValues && (
          <div className="field">
            <label className="label" htmlFor="flt-values">Values <span className="hint">· one per line or comma separated</span></label>
            <textarea id="flt-values" className="textarea mono" rows={4} value={valuesText} onChange={(e) => setValuesText(e.target.value)} />
          </div>
        )}
        {f.op === "between" && (
          <div className="grid-2" style={{ gap: 12 }}>
            <div className="field">
              <label className="label" htmlFor="flt-gte">From <span className="hint">· inclusive</span></label>
              <input id="flt-gte" className="input mono" value={f.gte ?? ""} placeholder={meta?.type === "date" ? "2024-09-01 or now-7d" : "10"} onChange={(e) => setF({ ...f, gte: e.target.value })} />
            </div>
            <div className="field">
              <label className="label" htmlFor="flt-lte">To <span className="hint">· inclusive</span></label>
              <input id="flt-lte" className="input mono" value={f.lte ?? ""} placeholder={meta?.type === "date" ? "2024-09-30" : "100"} onChange={(e) => setF({ ...f, lte: e.target.value })} />
            </div>
          </div>
        )}
        <button type="submit" hidden />
      </form>
    </Dialog>
  );
}

// ------------------------------------------------------------------ columns dialog

function ColumnsDialog({ fields, columns, onSave, onReset, onClose }: {
  fields: DataField[];
  columns: string[];
  onSave: (c: string[]) => void;
  onReset: () => void;
  onClose: () => void;
}) {
  const [sel, setSel] = useState<string[]>(columns);
  const [filter, setFilter] = useState("");
  const shown = fields.filter((f) => !filter || f.name.toLowerCase().includes(filter.toLowerCase()));
  const toggle = (n: string) => setSel((s) => (s.includes(n) ? s.filter((x) => x !== n) : [...s, n]));
  const move = (i: number, d: -1 | 1) => setSel((s) => {
    const j = i + d;
    if (j < 0 || j >= s.length) return s;
    const c = [...s];
    [c[i], c[j]] = [c[j], c[i]];
    return c;
  });
  return (
    <Dialog wide title="Columns" subtitle="Pick the fields to show. Objects show as JSON; a field inside an object shows just that value." onClose={onClose}
      footer={<>
        <button type="button" className="btn btn-ghost" onClick={onReset}>Reset to default</button>
        <div className="grow" />
        <button type="button" className="btn" onClick={onClose}>Cancel</button>
        <button type="button" className="btn btn-primary" onClick={() => onSave(sel)}>Show {sel.length} column{sel.length === 1 ? "" : "s"}</button>
      </>}>
      <div className="grid-2" style={{ gap: 16, alignItems: "start" }}>
        <div className="stack-sm">
          <span className="label">All fields · {fields.length}</span>
          <input className="input" aria-label="Filter fields" placeholder="Filter fields" value={filter} onChange={(e) => setFilter(e.target.value)} />
          <div className="cols-list">
            {shown.map((f) => (
              <label key={f.name} className="check cols-item" style={{ paddingLeft: f.name.split(".").length > 1 ? (f.name.split(".").length - 1) * 14 : 0 }}>
                <input type="checkbox" checked={sel.includes(f.name)} onChange={() => toggle(f.name)} />
                <span className="mono grow" style={{ fontSize: 13 }}>{f.name}</span>
                <span className="hint">{f.type}</span>
              </label>
            ))}
          </div>
        </div>
        <div className="stack-sm">
          <span className="label">Shown, in order · {sel.length}</span>
          {sel.length === 0 ? <span className="hint">Only _index and _id will show.</span> : (
            <ol className="cols-order">
              {sel.map((n, i) => (
                <li key={n}>
                  <span className="mono grow" style={{ fontSize: 13, overflowWrap: "anywhere" }}>{n}</span>
                  <button type="button" className="btn btn-ghost icon-btn" aria-label={`Move ${n} up`} disabled={i === 0} onClick={() => move(i, -1)}><Icon name="caret" size={14} style={{ transform: "rotate(180deg)" }} /></button>
                  <button type="button" className="btn btn-ghost icon-btn" aria-label={`Move ${n} down`} disabled={i === sel.length - 1} onClick={() => move(i, 1)}><Icon name="caret" size={14} /></button>
                  <button type="button" className="btn btn-ghost icon-btn" aria-label={`Hide ${n}`} onClick={() => toggle(n)}><Icon name="x" size={14} /></button>
                </li>
              ))}
            </ol>
          )}
        </div>
      </div>
    </Dialog>
  );
}

// ------------------------------------------------------------------ export dialog

function ExportDialog({ total, columns, run, onClose }: {
  total: number;
  columns: string[];
  run: (format: "csv" | "json" | "ndjson", columns: string[], limit: number) => Promise<void>;
  onClose: () => void;
}) {
  const [format, setFormat] = useState<"csv" | "json" | "ndjson">("csv");
  const [which, setWhich] = useState<"shown" | "all">("shown");
  const max = Math.min(total, MAX_EXPORT);
  const [limit, setLimit] = useState(max);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const go = async () => {
    setBusy(true);
    setError(null);
    try {
      await run(format, format === "csv" && which === "shown" ? columns : [], Math.max(1, Math.min(limit, max)));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog title="Export documents" subtitle="Downloads the documents that match the current search, filters and sort." onClose={onClose} busy={busy}
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={busy}>Cancel</button>
        <button type="button" className="btn btn-primary" onClick={go} disabled={busy}>{busy ? <Spinner label="Exporting" /> : <Icon name="download" />} Download</button>
      </>}>
      <div className="stack">
        <fieldset className="stack-sm" style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="label" style={{ marginBottom: 6 }}>Format</legend>
          {([["csv", "CSV", "opens in Excel; one row per document"], ["json", "JSON", "an array of documents"], ["ndjson", "NDJSON", "one JSON document per line"]] as const).map(([v, l, h]) => (
            <label key={v} className="check"><input type="radio" name="fmt" checked={format === v} onChange={() => setFormat(v)} /> <span><strong>{l}</strong> <span className="hint">· {h}</span></span></label>
          ))}
        </fieldset>
        {format === "csv" && (
          <fieldset className="stack-sm" style={{ border: 0, padding: 0, margin: 0 }}>
            <legend className="label" style={{ marginBottom: 6 }}>Columns</legend>
            <label className="check"><input type="radio" name="cols" checked={which === "shown"} onChange={() => setWhich("shown")} /> <span>The {columns.length} shown columns</span></label>
            <label className="check"><input type="radio" name="cols" checked={which === "all"} onChange={() => setWhich("all")} /> <span>Every field found in the documents</span></label>
          </fieldset>
        )}
        <div className="field" style={{ maxWidth: 260 }}>
          <label className="label" htmlFor="exp-limit">Documents <span className="hint">· up to {num(max)}</span></label>
          <input id="exp-limit" className="input" type="number" min={1} max={max} value={limit} onChange={(e) => setLimit(Number(e.target.value))} />
        </div>
        <Callout tone="neutral" icon="info">The export is recorded in the audit log with your query. Treat the file like the data it holds: it may contain customer details.</Callout>
        {error ? <ErrorCallout error={error} /> : null}
      </div>
    </Dialog>
  );
}

// ------------------------------------------------------------------ document dialog

function DocDialog({ clusterId, hit, fieldMap, position, onPrev, onNext, onClose, onFilter }: {
  clusterId: string;
  hit: DataHit;
  fieldMap: Map<string, DataField>;
  position: string;
  onPrev?: () => void;
  onNext?: () => void;
  onClose: () => void;
  onFilter: (f: DataFilter) => void;
}) {
  const toast = useToast();
  const [tab, setTab] = useState<"table" | "json" | "history">("table");
  const [mode, setMode] = useState<"view" | "edit" | "delete">("view");
  useEffect(() => setMode("view"), [hit._index, hit._id]);
  const canEdit = (hit._permission === "edit" || hit._permission === "delete") && !hit._dataStream;
  const [filter, setFilter] = useState("");
  const json = useMemo(() => JSON.stringify(hit._source, null, 2), [hit]);
  const rows = useMemo(() => leafEntries(hit._source).filter(([k]) => !filter || k.toLowerCase().includes(filter.toLowerCase())), [hit, filter]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || (e.target as HTMLElement)?.closest?.(".cm-editor") || mode !== "view") return;
      if (e.key === "ArrowLeft" && onPrev) onPrev();
      if (e.key === "ArrowRight" && onNext) onNext();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onPrev, onNext, mode]);
  if (mode !== "view") {
    return (
      <Dialog wide title={<>{mode === "edit" ? "Edit" : "Delete"} <span className="mono">{hit._id}</span></>} subtitle={<span className="mono">{hit._index}</span>} onClose={onClose}>
        {mode === "edit"
          ? <EditDoc clusterId={clusterId} hit={hit} onDone={onClose} onCancel={() => setMode("view")} />
          : <DeleteDoc clusterId={clusterId} hit={hit} onDone={onClose} onCancel={() => setMode("view")} />}
      </Dialog>
    );
  }
  return (
    <Dialog wide title={<span className="mono">{hit._id}</span>} subtitle={<><span className="mono">{hit._index}</span> · document {position}{hit._dataStream ? <> · data stream <span className="mono">{hit._dataStream}</span> (read-only here)</> : null}</>} onClose={onClose}
      footer={<>
        <button type="button" className="btn btn-ghost" onClick={onPrev} disabled={!onPrev}><Icon name="chevronLeft" /> Previous</button>
        <button type="button" className="btn btn-ghost" onClick={onNext} disabled={!onNext}>Next <Icon name="chevron" /></button>
        <div className="grow" />
        {canEdit && <button type="button" className="btn btn-ghost danger" onClick={() => setMode("delete")}><Icon name="trash" /> Delete</button>}
        <button type="button" className="btn" onClick={() => copyText(json).then(() => toast("Copied the document JSON"))}><Icon name="copy" /> Copy JSON</button>
        {canEdit && <button type="button" className="btn" onClick={() => setMode("edit")}><Icon name="sliders" /> Edit</button>}
        <button type="button" className="btn btn-primary" onClick={onClose}>Close</button>
      </>}>
      <div className="stack">
        <div className="row" style={{ gap: 12 }}>
          <div className="tabs" role="tablist" aria-label="Document view">
            <button type="button" role="tab" className="tab" aria-selected={tab === "table"} onClick={() => setTab("table")}>Fields</button>
            <button type="button" role="tab" className="tab" aria-selected={tab === "json"} onClick={() => setTab("json")}>JSON</button>
            <button type="button" role="tab" className="tab" aria-selected={tab === "history"} onClick={() => setTab("history")}>History</button>
          </div>
          <div className="grow" />
          {tab === "table" && <input className="input" aria-label="Filter fields" placeholder="Filter fields" style={{ maxWidth: 240, minHeight: 34 }} value={filter} onChange={(e) => setFilter(e.target.value)} />}
        </div>
        {tab === "history" ? (
          <DocHistory clusterId={clusterId} hit={hit} canEdit={canEdit} onRestored={onClose} />
        ) : tab === "json" ? (
          <pre className="code-block" style={{ maxHeight: "60vh", whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{json}</pre>
        ) : (
          <div className="table-scroll" style={{ maxHeight: "60vh", border: "1px solid var(--border)", borderRadius: "var(--radius)" }}>
            <table className="table compact">
              <thead><tr><th style={{ width: "34%" }}>Field</th><th>Value</th><th style={{ width: 86 }}><span className="sr-only">Filter</span></th></tr></thead>
              <tbody>
                {rows.map(([k, v]) => {
                  const t = cellText(v);
                  const scalar = v !== null && v !== undefined && typeof v !== "object";
                  const f = fieldMap.get(k);
                  return (
                    <Fragment key={k}>
                      <tr>
                        <td style={{ overflowWrap: "anywhere", verticalAlign: "top" }}><div className="mono" style={{ fontSize: 13 }}>{k}</div>{f && <div className="hint">{f.type}</div>}</td>
                        <td className="cell-mono" style={{ overflowWrap: "anywhere", whiteSpace: "pre-wrap", verticalAlign: "top" }}>{t || <span className="faint">{v === null ? "null" : "—"}</span>}</td>
                        <td style={{ whiteSpace: "nowrap", verticalAlign: "top" }}>
                          {scalar && f && !f.object && (
                            <>
                              <button type="button" className="btn btn-ghost mini-btn" title="Filter for this value" aria-label={`Filter for ${k} = ${t}`} onClick={() => onFilter({ field: k, op: "is", value: t })}><Icon name="plus" size={15} /></button>
                              <button type="button" className="btn btn-ghost mini-btn" title="Filter out this value" aria-label={`Filter out ${k} = ${t}`} onClick={() => onFilter({ field: k, op: "is_not", value: t })}><Icon name="minus" size={15} /></button>
                            </>
                          )}
                        </td>
                      </tr>
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Dialog>
  );
}
