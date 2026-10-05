import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ApiError, PendingApproval, enc, get, request, type DeleteResult, type IndexRow, type Rules, type Tombstone } from "../api";
import { Icon } from "../components/icons";
import { Page, useClusterCrumbs } from "../components/Shell";
import { applyLabel, Callout, Dialog, Empty, ErrorCallout, HealthDot, Loading, ReasonField, SearchInput, copyText, useToast } from "../components/ui";
import { bytes, LEVEL_LABEL, num, pretty, when } from "../format";
import { indexAllowed } from "../glob";
import { CreateIndexDialog } from "./CreateIndex";
import { RollbackDialog, type RollbackTarget } from "./Rollback";
import { useCluster, useHealth, useNeedsApproval } from "../session";

const MAX_ROWS = 500;

export function Indices() {
  const { clusterId, canIndex, admin, access, can } = useCluster();
  const [creating, setCreating] = useState(false);
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "deleted" ? "deleted" : "live";
  const [filter, setFilter] = useState("");
  const [toDelete, setToDelete] = useState<string | null>(null);
  const [definition, setDefinition] = useState<Tombstone | null>(null);
  const [recreate, setRecreate] = useState<RollbackTarget | null>(null);
  const health = useHealth(clusterId).data;
  const base = `/clusters/${enc(clusterId)}`;
  const c = `/c/${enc(clusterId)}`;

  const live = useQuery({ queryKey: ["indices", clusterId], queryFn: () => get<{ items: IndexRow[] }>(`${base}/indices`).then((r) => r.items) });
  const deleted = useQuery({
    queryKey: ["deleted-indices", clusterId],
    queryFn: () => get<{ items: Tombstone[] }>(`${base}/deleted-indices`).then((r) => r.items),
    enabled: tab === "deleted",
  });
  const allow = useQuery({ queryKey: ["allowlist-effective", clusterId], queryFn: () => get<{ rules: Rules }>(`/admin/allowlist/${enc(clusterId)}`), enabled: admin });
  const deleteRule = allow.data?.rules["index-delete"];

  const rows = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return (live.data ?? []).filter((r) => !f || r.index.toLowerCase().includes(f));
  }, [live.data, filter]);
  const tombs = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return (deleted.data ?? []).filter((r) => !f || r.index.toLowerCase().includes(f));
  }, [deleted.data, filter]);

  const canDelete = (index: string) => canIndex(index, "delete");
  const anyDelete = admin || access?.default === "delete" || !!access?.indices.some((r) => r.level === "delete");
  const deletable = (index: string) => canDelete(index) && (!admin || !allow.data || indexAllowed(index, deleteRule));

  return (
    <Page crumbs={useClusterCrumbs(clusterId, { label: "Indices" })} title="Indices" health={health?.status ?? null}>
      <div className="page-head" style={{ alignItems: "center" }}>
        <h1 className="grow" style={{ flex: "0 0 auto", minWidth: 0 }}>Indices</h1>
        <div className="tabs" role="tablist" aria-label="Index lists">
          <button type="button" role="tab" className="tab" aria-selected={tab === "live"} onClick={() => setParams({})}>
            Live{live.data ? ` · ${num(live.data.length)}` : ""}
          </button>
          <button type="button" role="tab" className="tab" aria-selected={tab === "deleted"} onClick={() => setParams({ tab: "deleted" })}>Deleted through the API</button>
        </div>
        <div className="grow" />
        {can("edit") && <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}><Icon name="plus" /> Create index</button>}
        <div style={{ width: 340 }}><SearchInput label="Filter indices" value={filter} onChange={setFilter} placeholder="Filter by name, e.g. delest-log-2026.09" /></div>
      </div>

      {tab === "live" ? (
        <>
          <section className="card" style={{ overflow: "hidden" }}>
            {live.isLoading ? <Loading what="Loading indices…" /> : live.error ? <div className="card-body"><ErrorCallout error={live.error} clusterId={clusterId} admin={admin} /></div> : rows.length === 0 ? (
              <Empty title={filter ? "No matching indices" : "No indices"} />
            ) : (
              <div className="table-scroll" style={{ maxHeight: "calc(100vh - 260px)" }}>
                <table className="table">
                  <thead><tr><th>Index</th><th>Health</th><th>Status</th><th className="num">Documents</th>{!admin && <th>Your access</th>}<th style={{ textAlign: "right" }}>Actions</th></tr></thead>
                  <tbody>
                    {rows.slice(0, MAX_ROWS).map((r) => (
                      <tr key={r.index}>
                        <td className="cell-mono"><Link to={`${c}/indices/${enc(r.index)}/settings`}>{r.index}</Link></td>
                        <td><span className="row" style={{ gap: 6 }}><HealthDot status={r.health} />{r.health ?? "—"}</span></td>
                        <td>{r.status}</td>
                        <td className="num">{num(r["docs.count"])}</td>
                        {!admin && <td>{r.permission ? <span className="hint">{LEVEL_LABEL[r.permission]}</span> : null}</td>}
                        <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                          <Link className="btn btn-ghost btn-sm" to={`${c}/data?index=${enc(r.index)}`}>Browse</Link>
                          <Link className="btn btn-ghost btn-sm" to={`${c}/indices/${enc(r.index)}/settings`}>Settings</Link>
                          <Link className="btn btn-ghost btn-sm" to={`${c}/indices/${enc(r.index)}/mapping`}>Mapping</Link>
                          {canDelete(r.index) && (deletable(r.index) ? (
                            <button type="button" className="btn btn-ghost btn-sm danger" onClick={() => setToDelete(r.index)}>Delete</button>
                          ) : (
                            <span className="btn btn-ghost btn-sm" style={{ color: "var(--faint)", cursor: "default" }} title="Not on the index-delete allowlist">
                              <Icon name="lock" size={13} /> Delete
                            </span>
                          ))}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
          {rows.length > MAX_ROWS && <span className="hint">Showing the first {MAX_ROWS} of {num(rows.length)}. Filter to narrow the list.</span>}
          <span className="hint">
            {anyDelete
              ? "Delete is available only for indices on the index-delete allowlist. System (dot) indices are hidden."
              : "System (dot) indices are hidden."}{access?.indices.length ? " Only indices you have access to are listed." : ""}
          </span>
        </>
      ) : (
        <section className="card" style={{ overflow: "hidden" }}>
          {deleted.isLoading ? <Loading /> : deleted.error ? <div className="card-body"><ErrorCallout error={deleted.error} /></div> : tombs.length === 0 ? (
            <Empty title="No indices deleted through the API">Each delete keeps the index's settings, mappings and aliases here, so an empty index can be recreated.</Empty>
          ) : (
            <table className="table">
              <thead><tr><th>Index</th><th>Deleted</th><th>By</th><th>Reason</th><th className="num">Documents</th><th className="num">Size</th><th /></tr></thead>
              <tbody>
                {tombs.map((t) => (
                  <tr key={t.key}>
                    <td className="cell-mono">{t.index}</td>
                    <td>{when(t.deletedAt)}</td>
                    <td>{t.deletedBy}</td>
                    <td>{t.reason ?? "—"}</td>
                    <td className="num">{num(t.docsCount)}</td>
                    <td className="num">{bytes(t.storeSizeBytes)}</td>
                    <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                      <button type="button" className="btn btn-ghost btn-sm" onClick={() => setDefinition(t)}>Definition</button>
                      {t.recreatedAt ? <span className="hint" title={`Recreated by ${t.recreatedBy}, ${when(t.recreatedAt)}`}> · recreated</span>
                        : canIndex(t.index, "edit") && !(live.data ?? []).some((r) => r.index === t.index) && (
                        <button type="button" className="btn btn-ghost btn-sm" onClick={() => setRecreate({ kind: "index", clusterId, key: t.key, index: t.index, label: `the delete of index ${t.index}` })}>
                          <Icon name="undo" size={14} /> Recreate
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}

      {toDelete && <DeleteIndexDialog clusterId={clusterId} index={toDelete} onClose={() => setToDelete(null)} />}
      {definition && <DefinitionDialog t={definition} onClose={() => setDefinition(null)} />}
      {recreate && <RollbackDialog target={recreate} onClose={() => setRecreate(null)} />}
      {creating && <CreateIndexDialog clusterId={clusterId} onClose={() => setCreating(false)} />}
    </Page>
  );
}

function DefinitionDialog({ t, onClose }: { t: Tombstone; onClose: () => void }) {
  const toast = useToast();
  const text = pretty(t.definition);
  return (
    <Dialog title={`Saved definition · ${t.index}`} subtitle="Settings, mappings and aliases at the time of the delete. Documents are not kept." onClose={onClose} wide
      footer={<>
        <button type="button" className="btn" onClick={() => copyText(text).then(() => toast("Copied"))}><Icon name="copy" /> Copy JSON</button>
        <button type="button" className="btn btn-primary" onClick={onClose}>Close</button>
      </>}>
      <pre className="code-block">{text}</pre>
    </Dialog>
  );
}

export function DeleteIndexDialog({ clusterId, index, onClose, onDeleted }: { clusterId: string; index: string; onClose: () => void; onDeleted?: () => void }) {
  const needsApproval = useNeedsApproval();
  const { admin } = useCluster();
  const qc = useQueryClient();
  const toast = useToast();
  const [confirm, setConfirm] = useState("");
  const [reason, setReason] = useState("");
  const path = `/clusters/${enc(clusterId)}/indices/${enc(index)}`;
  const preview = useQuery({
    queryKey: ["delete-preview", clusterId, index],
    queryFn: async () => (await request<DeleteResult>(path, { method: "DELETE", query: { dryRun: true } })).data,
    retry: false,
    gcTime: 0,
  });
  const del = useMutation({
    mutationFn: async () => (await request<DeleteResult>(path, { method: "DELETE", query: { confirm, reason: reason.trim() } })).data,
    onSuccess: () => {
      toast(`Deleted ${index}. Its definition is saved under "Deleted through the API".`);
      qc.invalidateQueries({ queryKey: ["indices", clusterId] });
      qc.invalidateQueries({ queryKey: ["deleted-indices", clusterId] });
      onDeleted?.();
      onClose();
    },
  });
  const p = preview.data;
  const pe = preview.error instanceof ApiError ? preview.error : null;
  const checks = [
    { text: `You have delete access on ${clusterId}`, state: pe?.code === "PERMISSION_DENIED" ? "bad" : p || pe ? "ok" : "todo" },
    { text: `${index} is on the index-delete allowlist`, state: pe?.code === "NOT_ALLOWLISTED" ? "bad" : p ? "ok" : "todo" },
    { text: "Not the write index of a data stream", state: pe?.code === "DATA_STREAM_WRITE_INDEX" ? "bad" : p ? "ok" : "todo" },
    { text: "A concrete index, not an alias or data stream", state: pe?.code === "NOT_A_CONCRETE_INDEX" ? "bad" : p ? "ok" : "todo" },
  ];
  const ready = !!p && confirm === index && reason.trim().length > 0 && !del.isPending;

  return (
    <Dialog
      title={<>Delete index <span className="mono">{index}</span></>}
      subtitle="Its documents are deleted for good. Settings, mappings and aliases are saved to S3 so an empty index can be recreated."
      onClose={onClose}
      busy={del.isPending}
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={del.isPending}>Cancel</button>
        <button type="button" className="btn btn-danger" disabled={!ready || del.error instanceof PendingApproval} onClick={() => del.mutate()}>
          <Icon name="trash" /> {del.isPending ? "Deleting…" : applyLabel(needsApproval, "Delete index")}
        </button>
      </>}
    >
      {preview.isLoading && <Loading what="Checking the index…" />}
      {p && (
        <div className="kv">
          <div><span className="k">Documents</span><span className="v">{num(p.docsCount)}</span></div>
          <div><span className="k">Size</span><span className="v">{bytes(p.storeSizeBytes)}</span></div>
          <div><span className="k">Aliases</span><span className="v">{p.aliases.length ? p.aliases.join(", ") : "None"}</span></div>
          <div><span className="k">Data stream</span><span className="v">{p.dataStream ?? "None"}</span></div>
        </div>
      )}
      <ul className="checklist">
        {checks.map((c) => (
          <li key={c.text} className={c.state}>
            <Icon name={c.state === "ok" ? "check" : c.state === "bad" ? "x" : "circle"} size={16} strokeWidth={2.4} />
            {c.text}
          </li>
        ))}
      </ul>
      {pe && <ErrorCallout error={pe} clusterId={clusterId} admin={admin} />}
      {p && p.warnings.length > 1 && (
        <Callout tone="warn" title="Before you delete"><ul>{p.warnings.slice(1).map((w) => <li key={w}>{w}</li>)}</ul></Callout>
      )}
      {p && (
        <>
          <div className="field">
            <label htmlFor="confirm-name" className="label">Type <span className="mono">{index}</span> to confirm</label>
            <input id="confirm-name" className="input mono" autoComplete="off" spellCheck={false} value={confirm} onChange={(e) => setConfirm(e.target.value)}
              aria-invalid={confirm.length > 0 && confirm !== index ? true : undefined} />
          </div>
          <ReasonField value={reason} onChange={setReason} />
        </>
      )}
      {del.error && <ErrorCallout error={del.error} clusterId={clusterId} admin={admin} />}
    </Dialog>
  );
}
