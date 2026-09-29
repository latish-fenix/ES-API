import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ApiError, enc, get, type ConfigDoc, type NamedType } from "../api";
import { ChangeFlow, RollbackDialog, SnapshotDialog, StatusBadges } from "../components/ChangeFlow";
import { Icon } from "../components/icons";
import { Page, useClusterCrumbs } from "../components/Shell";
import { Callout, Empty, ErrorCallout, Loading, SearchInput } from "../components/ui";
import { NAMED_META, NEW_TEMPLATES, pretty } from "../format";
import { useCluster, useHealth } from "../session";

const NAME_RE = /^[^\s*,"<>|\\/?#]{1,255}$/;
const isBuiltIn = (n: string) => n.startsWith(".") || n.includes("@");

export function NamedResources({ type }: { type: NamedType }) {
  const { clusterId, can, admin } = useCluster();
  const { name } = useParams();
  const [search] = useSearchParams();
  const navigate = useNavigate();
  const meta = NAMED_META[type];
  const health = useHealth(clusterId).data;
  const base = `/clusters/${enc(clusterId)}/${type}`;
  const c = `/c/${enc(clusterId)}/${type}`;
  const [filter, setFilter] = useState("");
  const [showBuiltIn, setShowBuiltIn] = useState(false);
  const list = useQuery({ queryKey: ["named", clusterId, type], queryFn: () => get<{ items: string[] }>(base).then((r) => r.items) });

  const names = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return (list.data ?? []).filter((n) => (showBuiltIn || !isBuiltIn(n) || n === name) && (!f || n.toLowerCase().includes(f)));
  }, [list.data, filter, showBuiltIn, name]);
  const hiddenCount = (list.data ?? []).filter(isBuiltIn).length;
  const isNew = name === undefined && search.has("new");

  const crumbs = useClusterCrumbs(clusterId, { label: meta.label, to: c }, ...(name ? [{ label: name }] : []));
  return (
    <Page crumbs={crumbs} title={name ?? meta.label} health={health?.status ?? null}>
      <div className="grid-list">
        <section className="card" aria-label={`${meta.label} list`}>
          <div className="card-head">
            <div className="grow"><h1 style={{ fontSize: 20 }}>{meta.label}</h1></div>
            {can("edit") && <Link to={`${c}?new=1`} className="btn btn-sm"><Icon name="plus" size={15} /> New</Link>}
          </div>
          <div className="card-body" style={{ gap: 10, padding: 12 }}>
            <SearchInput label={`Filter ${meta.label.toLowerCase()}`} value={filter} onChange={setFilter} placeholder="Filter" />
            {hiddenCount > 0 && (
              <label className="check" style={{ fontSize: 13 }}>
                <input type="checkbox" checked={showBuiltIn} onChange={(e) => setShowBuiltIn(e.target.checked)} />
                Show built-in ({hiddenCount})
              </label>
            )}
            <div className="stack-sm" style={{ gap: 2, maxHeight: "calc(100vh - 300px)", overflow: "auto" }}>
              {list.isLoading ? <Loading /> : list.error ? <ErrorCallout error={list.error} /> : names.length === 0 ? (
                <Empty title={filter ? "No matches" : `No ${meta.label.toLowerCase()}`} />
              ) : names.map((n) => (
                <Link key={n} to={`${c}/${enc(n)}`} className={`list-item ${n === name ? "active" : ""}`} aria-current={n === name ? "page" : undefined}>{n}</Link>
              ))}
            </div>
          </div>
        </section>
        <div className="stack" style={{ minWidth: 0 }}>
          {name ? (
            <Resource key={`${clusterId}/${name}`} type={type} name={name} clusterId={clusterId} canEdit={can("edit")} admin={admin} />
          ) : isNew && can("edit") ? (
            <NewResource type={type} clusterId={clusterId} admin={admin} onCreated={(n) => navigate(`${c}/${enc(n)}`)} />
          ) : (
            <section className="card"><Empty title={`Pick a ${meta.singular}`}>{meta.blurb}. Select one on the left{can("edit") ? " or create a new one" : ""}.</Empty></section>
          )}
          {type === "ilm-policies" && (
            <Callout tone="info" title="Careful with delete phases">
              A shorter delete <code>min_age</code> removes every matching index older than it on the next ILM run. Rollback restores the policy, not deleted data.
            </Callout>
          )}
        </div>
      </div>
    </Page>
  );
}

function Resource({ type, name, clusterId, canEdit, admin }: { type: NamedType; name: string; clusterId: string; canEdit: boolean; admin: boolean }) {
  const qc = useQueryClient();
  const meta = NAMED_META[type];
  const path = `/clusters/${enc(clusterId)}/${type}/${enc(name)}`;
  const doc = useQuery({ queryKey: ["config", path], queryFn: () => get<ConfigDoc<unknown>>(path), retry: false });
  const original = doc.data ? pretty(doc.data.config) : "";
  const [text, setText] = useState("");
  const [dialog, setDialog] = useState<"rollback" | "snapshot" | null>(null);
  useEffect(() => { if (doc.data) setText(pretty(doc.data.config)); }, [doc.data]);
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["config", path] });
    qc.invalidateQueries({ queryKey: ["previous", path] });
    qc.invalidateQueries({ queryKey: ["named", clusterId, type] });
  };

  if (doc.isLoading) return <section className="card"><Loading /></section>;
  const notFound = doc.error instanceof ApiError && doc.error.code === "RESOURCE_NOT_FOUND";
  const d = doc.data;
  return (
    <>
      {doc.error && (notFound ? (
        <Callout tone="info" title={`No ${meta.singular} named ${name}`}>It may have been deleted, or rolled back to not existing.</Callout>
      ) : <ErrorCallout error={doc.error} clusterId={clusterId} admin={admin} />)}
      {d && (
        <>
          <div className="page-head">
            <div className="grow">
              <h2 className="mono" style={{ fontSize: 19, wordBreak: "break-all" }}>{name}</h2>
              <StatusBadges version={d.version} drift={d.driftDetected} rollbackAvailable={d.rollbackAvailable} lastApplied={d.lastApplied} />
            </div>
            <button type="button" className="btn" disabled={!d.rollbackAvailable} onClick={() => setDialog("snapshot")}>View snapshot</button>
            {canEdit && <button type="button" className="btn" disabled={!d.rollbackAvailable} onClick={() => setDialog("rollback")}><Icon name="undo" /> Roll back</button>}
          </div>
          {d.warnings.map((w) => <Callout key={w} tone="warn">{w}</Callout>)}
          {canEdit ? (
            <ChangeFlow path={path} clusterId={clusterId} admin={admin} text={text} setText={setText} version={d.version}
              title={`Edit ${meta.singular}`} editorHeight="360px"
              editorLabel={<>Full definition <span className="hint">· replaces the whole {meta.singular}</span></>}
              headerExtra={text !== original ? <button type="button" className="btn btn-sm" onClick={() => setText(original)}>Reset edits</button> : undefined}
              absent="—" sampleDocs={type === "ingest-pipelines"} onApplied={refresh} />
          ) : (
            <section className="card"><div className="card-body"><pre className="code-block" style={{ maxHeight: 560 }}>{original}</pre></div></section>
          )}
        </>
      )}
      {dialog === "rollback" && <RollbackDialog path={path} label={name} absent="—" clusterId={clusterId} admin={admin} onClose={() => setDialog(null)} onDone={refresh} />}
      {dialog === "snapshot" && <SnapshotDialog path={path} label={name} onClose={() => setDialog(null)} />}
    </>
  );
}

function NewResource({ type, clusterId, admin, onCreated }: { type: NamedType; clusterId: string; admin: boolean; onCreated: (name: string) => void }) {
  const qc = useQueryClient();
  const meta = NAMED_META[type];
  const [name, setName] = useState("");
  const [text, setText] = useState(pretty(NEW_TEMPLATES[type]));
  const validName = NAME_RE.test(name) && !name.startsWith("_");
  return (
    <>
      <section className="card">
        <div className="card-body">
          <div className="field">
            <label htmlFor="new-name" className="label">Name of the new {meta.singular}</label>
            <input id="new-name" className="input mono" value={name} autoFocus spellCheck={false} placeholder="my-logs-policy"
              onChange={(e) => setName(e.target.value.trim())} aria-invalid={name.length > 0 && !validName ? true : undefined} />
            {name.length > 0 && !validName && <span className="hint" style={{ color: "var(--danger)" }}>Use one name without spaces, * , " &lt; &gt; | \ / ? #, not starting with _</span>}
          </div>
        </div>
      </section>
      {validName ? (
        <ChangeFlow path={`/clusters/${enc(clusterId)}/${type}/${enc(name)}`} clusterId={clusterId} admin={admin} text={text} setText={setText}
          title={`Create ${meta.singular}`} editorHeight="360px" editorLabel="Full definition" absent="—"
          sampleDocs={type === "ingest-pipelines"} applyLabel="Create"
          onApplied={() => { qc.invalidateQueries({ queryKey: ["named", clusterId, type] }); onCreated(name); }} />
      ) : (
        <Callout>Enter a name to continue. If a {meta.singular} with that name exists, the dry run shows what would change.</Callout>
      )}
    </>
  );
}
