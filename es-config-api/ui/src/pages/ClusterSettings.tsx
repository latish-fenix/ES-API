import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { enc, get, type ConfigDoc, type Rules } from "../api";
import { ChangeFlow, RollbackDialog, SnapshotDialog, StatusBadges } from "../components/ChangeFlow";
import { Icon } from "../components/icons";
import { parseJson } from "../components/JsonEditor";
import { Page, useClusterCrumbs } from "../components/Shell";
import { Callout, Empty, ErrorCallout, Loading, SearchInput } from "../components/ui";
import { showValue } from "../format";
import { useCluster, useHealth } from "../session";

const STARTER = '{\n  \n}';

export function ClusterSettings() {
  const { clusterId, can, admin } = useCluster();
  const qc = useQueryClient();
  const path = `/clusters/${enc(clusterId)}/cluster-settings`;
  const health = useHealth(clusterId).data;
  const doc = useQuery({ queryKey: ["config", path], queryFn: () => get<ConfigDoc<Record<string, unknown>>>(path) });
  const allow = useQuery({
    queryKey: ["allowlist-effective", clusterId],
    queryFn: () => get<{ rules: Rules }>(`/admin/allowlist/${enc(clusterId)}`),
    enabled: admin,
  });
  const [text, setText] = useState(STARTER);
  const [filter, setFilter] = useState("");
  const [dialog, setDialog] = useState<"rollback" | "snapshot" | null>(null);
  const crumbs = useClusterCrumbs(clusterId, { label: "Cluster settings" });

  useEffect(() => setText(STARTER), [clusterId]);

  const entries = useMemo(() => {
    const cfg = doc.data?.config ?? {};
    const f = filter.trim().toLowerCase();
    return Object.entries(cfg).filter(([k]) => !f || k.toLowerCase().includes(f)).sort(([a], [b]) => a.localeCompare(b));
  }, [doc.data, filter]);

  const addKey = (key: string) => {
    const [parsed] = parseJson(text);
    const obj = parsed && typeof parsed === "object" && !Array.isArray(parsed) ? { ...(parsed as Record<string, unknown>) } : {};
    if (!(key in obj)) obj[key] = key.includes("*") ? "" : doc.data?.config[key] ?? "";
    setText(JSON.stringify(obj, null, 2));
  };

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["config", path] });
    qc.invalidateQueries({ queryKey: ["previous", path] });
  };

  const d = doc.data;
  const allowRule = allow.data?.rules["cluster-settings"];
  return (
    <Page crumbs={crumbs} title="Cluster settings" health={health?.status ?? null}>
      <div className="page-head">
        <div className="grow">
          <h1>Cluster settings</h1>
          {d && <StatusBadges version={d.version} drift={d.driftDetected} rollbackAvailable={d.rollbackAvailable} lastApplied={d.lastApplied} />}
        </div>
        <button type="button" className="btn" disabled={!d?.rollbackAvailable} onClick={() => setDialog("snapshot")}>View snapshot</button>
        {can("edit") && (
          <button type="button" className="btn" disabled={!d?.rollbackAvailable} onClick={() => setDialog("rollback")}>
            <Icon name="undo" /> Roll back
          </button>
        )}
      </div>

      {doc.error && <ErrorCallout error={doc.error} clusterId={clusterId} admin={admin} />}
      {d?.driftDetected && (
        <Callout tone="warn" title="Changed outside the API">
          The live settings differ from what the API last applied ({d.lastApplied?.by}). A rollback now would overwrite that outside change.
        </Callout>
      )}
      {d?.warnings.map((w) => <Callout key={w} tone="warn">{w}</Callout>)}

      <div className="grid-2">
        <section className="card">
          <div className="card-head">
            <div className="grow"><h2>Current persistent settings</h2></div>
            <div style={{ width: 240 }}><SearchInput label="Filter settings" value={filter} onChange={setFilter} placeholder="Filter keys" /></div>
          </div>
          {doc.isLoading ? <Loading /> : d && Object.keys(d.config).length === 0 ? (
            <Empty title="No persistent settings">This cluster uses Elasticsearch defaults. Transient settings are not managed here.</Empty>
          ) : entries.length === 0 ? (
            <Empty title="No matching settings" />
          ) : (
            <div className="table-scroll" style={{ maxHeight: 420, borderRadius: 0 }}>
              <table className="table">
                <thead><tr><th>Setting</th><th>Value</th>{can("edit") && <th><span className="sr-only">Actions</span></th>}</tr></thead>
                <tbody>
                  {entries.map(([k, v]) => (
                    <tr key={k}>
                      <td className="cell-mono">{k}</td>
                      <td className="cell-mono">{showValue(v)}</td>
                      {can("edit") && <td style={{ textAlign: "right" }}><button type="button" className="btn btn-ghost btn-sm" onClick={() => addKey(k)}>Edit</button></td>}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {admin && (
            <>
              <div className="card-head" style={{ borderTop: "1px solid var(--border)" }}>
                <div className="grow"><h3>You can change</h3><span className="hint">Keys matching the allowlist</span></div>
                <Link to="/admin/allowlist" className="btn btn-sm">Allowlist</Link>
              </div>
              {!allowRule?.allow.length ? (
                <Empty title="Nothing is allowlisted">Every cluster setting is locked.</Empty>
              ) : (
                <table className="table">
                  <tbody>
                    {allowRule.allow.map((p) => (
                      <tr key={p}>
                        <td className="cell-mono">{p}</td>
                        <td className="cell-mono" style={{ color: "var(--faint)" }}>{p.includes("*") ? "pattern" : d?.config[p] !== undefined ? showValue(d.config[p]) : "default"}</td>
                        <td style={{ textAlign: "right" }}>{!p.includes("*") && can("edit") && <button type="button" className="btn btn-ghost btn-sm" onClick={() => addKey(p)}>Add to change</button>}</td>
                      </tr>
                    ))}
                    {allowRule.deny.map((p) => (
                      <tr key={`deny:${p}`}><td className="cell-mono">{p}</td><td colSpan={2} style={{ textAlign: "right" }}><span className="badge red">Denied</span></td></tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          )}
        </section>

        {can("edit") ? (
          <ChangeFlow
            path={path}
            clusterId={clusterId}
            admin={admin}
            text={text}
            setText={setText}
            version={d?.version}
            editorLabel={<>Settings to change <span className="hint">· send only the keys to change; null resets to default</span></>}
            absent="default"
            onApplied={() => { setText(STARTER); refresh(); }}
          />
        ) : (
          <Callout tone="info" title="View only">You can read this cluster's settings. Ask an admin for edit access to change them.</Callout>
        )}
      </div>

      {dialog === "rollback" && <RollbackDialog path={path} label="cluster settings" absent="default" clusterId={clusterId} admin={admin} onClose={() => setDialog(null)} onDone={refresh} />}
      {dialog === "snapshot" && <SnapshotDialog path={path} label="cluster settings" onClose={() => setDialog(null)} />}
    </Page>
  );
}
