import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link, Navigate, useNavigate, useParams } from "react-router-dom";
import { enc, get, type ConfigDoc, type IndexRow } from "../api";
import { ChangeFlow, RollbackDialog, SnapshotDialog, StatusBadges } from "../components/ChangeFlow";
import { Icon } from "../components/icons";
import { Page, useClusterCrumbs } from "../components/Shell";
import { Callout, Empty, ErrorCallout, HealthDot, Loading, SearchInput } from "../components/ui";
import { num, showValue, when } from "../format";
import { useCluster, useHealth } from "../session";
import { DeleteIndexDialog } from "./Indices";
import { RollbackDialog as UndoDialog } from "./Rollback";

interface Field {
  path: string;
  type: string;
  extra: string;
}

function fieldsOf(props: Record<string, unknown> | undefined, prefix = ""): Field[] {
  const out: Field[] = [];
  for (const [name, raw] of Object.entries(props ?? {})) {
    const def = raw as Record<string, unknown>;
    const path = prefix ? `${prefix}.${name}` : name;
    const sub = def.properties as Record<string, unknown> | undefined;
    const type = (def.type as string) ?? (sub ? "object" : "—");
    const extra = [
      def.fields ? `multi-field: ${Object.keys(def.fields as object).join(", ")}` : "",
      def.index === false ? "not indexed" : "",
      def.format ? `format ${def.format}` : "",
    ].filter(Boolean).join(" · ");
    out.push({ path, type, extra });
    if (sub) out.push(...fieldsOf(sub, path));
  }
  return out;
}

const SETTINGS_STARTER = '{\n  "index.refresh_interval": "30s"\n}';
const MAPPING_STARTER = '{\n  "properties": {\n    "new_field": { "type": "keyword" }\n  }\n}';

export function IndexDetail() {
  const { clusterId, canIndex, admin } = useCluster();
  const { index = "", part } = useParams();
  const navigate = useNavigate();
  const [deleting, setDeleting] = useState(false);
  const health = useHealth(clusterId).data;
  const rows = useQuery({ queryKey: ["indices", clusterId], queryFn: () => get<{ items: IndexRow[] }>(`/clusters/${enc(clusterId)}/indices`).then((r) => r.items) });
  const info = rows.data?.find((r) => r.index === index);
  const [undoing, setUndoing] = useState(false);
  const created = useQuery({
    queryKey: ["index-created", clusterId, index],
    queryFn: () => get<{ changeId: string; at: string; by: string; reason: string | null; canUndo: boolean; docs: number | null }>(
      `/clusters/${enc(clusterId)}/indices/${enc(index)}/_created`),
    retry: false,
  });
  const c = `/c/${enc(clusterId)}`;
  const crumbs = useClusterCrumbs(clusterId, { label: "Indices", to: `${c}/indices` }, { label: index });

  if (part !== "settings" && part !== "mapping") return <Navigate to={`${c}/indices/${enc(index)}/settings`} replace />;

  return (
    <Page crumbs={crumbs} title={index} health={health?.status ?? null}>
      <div className="page-head">
        <div className="grow">
          <h1 className="mono" style={{ fontSize: 22, wordBreak: "break-all" }}>{index}</h1>
          {info && (
            <div className="row" style={{ gap: 12 }}>
              <span className="row" style={{ gap: 6 }}><HealthDot status={info.health} />{info.health}</span>
              <span className="sub">{info.status}</span>
              <span className="sub">{num(info["docs.count"])} documents</span>
            </div>
          )}
        </div>
        {canIndex(index, "delete") && <button type="button" className="btn" style={{ color: "var(--danger)" }} onClick={() => setDeleting(true)}><Icon name="trash" /> Delete index</button>}
      </div>
      {created.data?.canUndo && (
        <Callout icon="undo" title="Created in the console and still empty">
          {created.data.by} created it {when(created.data.at)}{created.data.reason ? ` (“${created.data.reason}”)` : ""}. While it has no documents, <strong>Roll back</strong> deletes it again.
          <div style={{ marginTop: 8 }}><button type="button" className="btn btn-sm" onClick={() => setUndoing(true)}><Icon name="undo" size={14} /> Roll back creation</button></div>
        </Callout>
      )}
      <div className="tabs" role="tablist" aria-label="Index configuration" style={{ alignSelf: "flex-start" }}>
        <Link role="tab" className={`tab ${part === "settings" ? "active" : ""}`} aria-selected={part === "settings"} to={`${c}/indices/${enc(index)}/settings`}>Settings</Link>
        <Link role="tab" className={`tab ${part === "mapping" ? "active" : ""}`} aria-selected={part === "mapping"} to={`${c}/indices/${enc(index)}/mapping`}>Mapping</Link>
      </div>
      {part === "settings" ? <SettingsTab key={index} clusterId={clusterId} index={index} canEdit={canIndex(index, "edit")} admin={admin} /> : <MappingTab key={index} clusterId={clusterId} index={index} canEdit={canIndex(index, "edit")} admin={admin} />}
      {undoing && created.data && (
        <UndoDialog target={{ kind: "create", clusterId, index, changeId: created.data.changeId, label: `the creation of index ${index}`,
          detail: `${created.data.by}, ${when(created.data.at)}` }} onClose={() => setUndoing(false)} onDone={() => navigate(`${c}/indices`)} />
      )}
      {deleting && <DeleteIndexDialog clusterId={clusterId} index={index} onClose={() => setDeleting(false)} onDeleted={() => navigate(`${c}/indices`)} />}
    </Page>
  );
}

function SettingsTab({ clusterId, index, canEdit, admin }: { clusterId: string; index: string; canEdit: boolean; admin: boolean }) {
  const qc = useQueryClient();
  const path = `/clusters/${enc(clusterId)}/indices/${enc(index)}/settings`;
  const doc = useQuery({ queryKey: ["config", path], queryFn: () => get<ConfigDoc<Record<string, unknown>>>(path) });
  const [text, setText] = useState(SETTINGS_STARTER);
  const [filter, setFilter] = useState("");
  const [dialog, setDialog] = useState<"rollback" | "snapshot" | null>(null);
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["config", path] });
    qc.invalidateQueries({ queryKey: ["previous", path] });
  };
  const d = doc.data;
  const entries = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return Object.entries(d?.config ?? {}).filter(([k]) => !f || k.toLowerCase().includes(f)).sort(([a], [b]) => a.localeCompare(b));
  }, [d, filter]);

  const edit = (k: string, v: unknown) => setText(JSON.stringify({ [k]: v }, null, 2));

  return (
    <>
      {doc.error && <ErrorCallout error={doc.error} clusterId={clusterId} admin={admin} />}
      {d && (
        <div className="row">
          <StatusBadges version={d.version} drift={d.driftDetected} rollbackAvailable={d.rollbackAvailable} lastApplied={d.lastApplied} />
          <div className="grow" />
          <button type="button" className="btn btn-sm" disabled={!d.rollbackAvailable} onClick={() => setDialog("snapshot")}>View snapshot</button>
          {canEdit && <button type="button" className="btn btn-sm" disabled={!d.rollbackAvailable} onClick={() => setDialog("rollback")}><Icon name="undo" size={15} /> Roll back</button>}
        </div>
      )}
      {d?.warnings.map((w) => <Callout key={w} tone="warn">{w}</Callout>)}
      <div className="grid-2">
        <section className="card">
          <div className="card-head">
            <div className="grow"><h2>Current settings</h2><span className="hint">Only dynamic settings can be changed on an open index</span></div>
            <div style={{ width: 220 }}><SearchInput label="Filter settings" value={filter} onChange={setFilter} placeholder="Filter keys" /></div>
          </div>
          {doc.isLoading ? <Loading /> : entries.length === 0 ? <Empty title="No matching settings" /> : (
            <div className="table-scroll" style={{ maxHeight: 520, borderRadius: 0 }}>
              <table className="table">
                <thead><tr><th>Setting</th><th>Value</th>{canEdit && <th><span className="sr-only">Actions</span></th>}</tr></thead>
                <tbody>
                  {entries.map(([k, v]) => (
                    <tr key={k}>
                      <td className="cell-mono">{k}</td>
                      <td className="cell-mono">{showValue(v)}</td>
                      {canEdit && <td style={{ textAlign: "right" }}><button type="button" className="btn btn-ghost btn-sm" onClick={() => edit(k, v)}>Edit</button></td>}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
        {canEdit ? (
          <ChangeFlow path={path} clusterId={clusterId} admin={admin} text={text} setText={setText} version={d?.version}
            editorLabel={<>Settings to change <span className="hint">· dynamic settings only; null resets to default</span></>}
            absent="default" onApplied={() => { setText(SETTINGS_STARTER); refresh(); }} />
        ) : (
          <Callout tone="info" title="View only">Ask an admin for edit access to change index settings.</Callout>
        )}
      </div>
      {dialog === "rollback" && <RollbackDialog path={path} label={`settings of ${index}`} absent="default" clusterId={clusterId} admin={admin} onClose={() => setDialog(null)} onDone={refresh} />}
      {dialog === "snapshot" && <SnapshotDialog path={path} label={`settings of ${index}`} onClose={() => setDialog(null)} />}
    </>
  );
}

function MappingTab({ clusterId, index, canEdit, admin }: { clusterId: string; index: string; canEdit: boolean; admin: boolean }) {
  const qc = useQueryClient();
  const path = `/clusters/${enc(clusterId)}/indices/${enc(index)}/mapping`;
  const doc = useQuery({ queryKey: ["config", path], queryFn: () => get<ConfigDoc<{ properties?: Record<string, unknown> }>>(path) });
  const [text, setText] = useState(MAPPING_STARTER);
  const [filter, setFilter] = useState("");
  useEffect(() => setText(MAPPING_STARTER), [index]);
  const fields = useMemo(() => fieldsOf(doc.data?.config.properties), [doc.data]);
  const shown = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return fields.filter((x) => !f || x.path.toLowerCase().includes(f) || x.type.includes(f));
  }, [fields, filter]);

  return (
    <>
      <Callout tone="warn" title="Mapping changes are permanent" icon="warn" role="note">
        Elasticsearch lets you add fields, never change or remove them. There is no rollback for mappings.
      </Callout>
      {doc.error && <ErrorCallout error={doc.error} clusterId={clusterId} admin={admin} />}
      <div className="grid-2">
        <section className="card">
          <div className="card-head">
            <div className="grow"><h2>Current fields</h2><span className="hint">{doc.data ? `${num(fields.length)} fields` : ""}</span></div>
            <div style={{ width: 220 }}><SearchInput label="Filter fields" value={filter} onChange={setFilter} placeholder="Filter fields or types" /></div>
          </div>
          {doc.isLoading ? <Loading /> : shown.length === 0 ? <Empty title={fields.length ? "No matching fields" : "No fields mapped yet"} /> : (
            <div className="table-scroll" style={{ maxHeight: 520, borderRadius: 0 }}>
              <table className="table">
                <thead><tr><th>Field</th><th>Type</th><th>Notes</th></tr></thead>
                <tbody>
                  {shown.map((f) => (
                    <tr key={f.path}>
                      <td className="cell-mono">{f.path}</td>
                      <td><span className="badge mono">{f.type}</span></td>
                      <td className="hint">{f.extra}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
        {canEdit ? (
          <ChangeFlow path={path} clusterId={clusterId} admin={admin} text={text} setText={setText} version={doc.data?.version}
            title="Add fields" editorLabel={<>New fields <span className="hint">· {"{ \"properties\": { … } }"}, add-only</span></>}
            absent="—" permanent applyLabel="Add fields permanently"
            onApplied={() => { setText(MAPPING_STARTER); qc.invalidateQueries({ queryKey: ["config", path] }); }} />
        ) : (
          <Callout tone="info" title="View only">Ask an admin for edit access to add fields.</Callout>
        )}
      </div>
    </>
  );
}
