import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, enc, get, request, type AdminCluster, type ConnectionTest } from "../../api";
import { Icon } from "../../components/icons";
import { Page } from "../../components/Shell";
import { Badge, Callout, Dialog, Empty, ErrorCallout, HealthDot, Loading, useToast } from "../../components/ui";
import { when } from "../../format";

const ID_RE = /^[a-z0-9][a-z0-9_-]{0,62}$/;

interface Form {
  id: string;
  name: string;
  description: string;
  hosts: string;
  authType: "basic" | "api_key" | "none";
  username: string;
  password: string;
  apiKey: string;
  verifyCerts: boolean;
  caCertPem: string;
  removeCa: boolean;
  requestTimeout: number;
  tags: string;
}

const EMPTY: Form = {
  id: "", name: "", description: "", hosts: "", authType: "basic", username: "config_api", password: "",
  apiKey: "", verifyCerts: true, caCertPem: "", removeCa: false, requestTimeout: 30, tags: "",
};

function fromCluster(c: AdminCluster): Form {
  return {
    ...EMPTY, id: c.id, name: c.name, description: c.description ?? "", hosts: c.hosts.join("\n"),
    authType: c.authType, username: c.username ?? "", verifyCerts: c.verifyCerts,
    requestTimeout: c.requestTimeout, tags: (c.tags ?? []).join(", "),
  };
}

function body(f: Form, editing: AdminCluster | null) {
  const auth: Record<string, string> = { type: f.authType };
  if (f.authType === "basic") {
    auth.username = f.username.trim();
    if (f.password) auth.password = f.password;
  }
  if (f.authType === "api_key" && f.apiKey) auth.apiKey = f.apiKey.trim();
  const out: Record<string, unknown> = {
    name: f.name.trim() || f.id.trim(),
    description: f.description.trim(),
    hosts: f.hosts.split(/[\s,]+/).map((h) => h.trim()).filter(Boolean),
    auth,
    verifyCerts: f.verifyCerts,
    requestTimeout: Number(f.requestTimeout) || 30,
    tags: f.tags.split(",").map((t) => t.trim()).filter(Boolean),
  };
  if (f.caCertPem.trim()) out.caCertPem = f.caCertPem.trim();
  else if (editing && f.removeCa) out.caCertPem = "";
  if (!editing) out.id = f.id.trim();
  return out;
}

function TestResult({ t }: { t: ConnectionTest }) {
  if (!t.reachable) {
    return (
      <Callout tone="danger" title="Can't connect">
        {t.error}
        {t.errorCode && <div className="meta">{t.errorCode}</div>}
      </Callout>
    );
  }
  return (
    <Callout tone="success" title={`Connected · Elasticsearch ${t.version ?? "?"}`}>
      {[t.clusterName && `cluster ${t.clusterName}`, t.health && `health ${t.health}`, t.numberOfNodes !== undefined && `${t.numberOfNodes} node${t.numberOfNodes === 1 ? "" : "s"}`].filter(Boolean).join(" · ")}
      {t.warnings?.map((w) => <div key={w}>{w}</div>)}
    </Callout>
  );
}

export function Clusters() {
  const q = useQuery({
    queryKey: ["admin-clusters"],
    queryFn: () => get<{ items: AdminCluster[]; managedFile: string | null }>("/admin/clusters"),
    staleTime: 30_000,
  });
  const [open, setOpen] = useState<{ mode: "add" } | { mode: "edit" | "view"; cluster: AdminCluster } | null>(null);
  const items = q.data?.items ?? [];
  const managedOff = q.data && q.data.managedFile === null;

  return (
    <Page crumbs={[{ label: "Administration" }, { label: "Clusters" }]} title="Clusters">
      <div className="page-head" style={{ alignItems: "center" }}>
        <div className="grow">
          <h1>Clusters</h1>
          <span className="sub">Elasticsearch clusters this console manages. Give people access to a new cluster on the Users page.</span>
        </div>
        <button type="button" className="btn" onClick={() => q.refetch()} disabled={q.isFetching}>
          <Icon name="refresh" /> {q.isFetching ? "Checking…" : "Check again"}
        </button>
        <button type="button" className="btn btn-primary" onClick={() => setOpen({ mode: "add" })} disabled={!!managedOff}>
          <Icon name="plus" /> Add cluster
        </button>
      </div>
      {managedOff && (
        <Callout tone="warn" title="Adding clusters is turned off">
          The server has no <code>MANAGED_CLUSTERS_FILE</code>. Clusters can only be listed in <code>config/clusters.yaml</code>.
        </Callout>
      )}
      <section className="card" style={{ overflow: "hidden" }}>
        {q.isLoading ? <Loading what="Checking every cluster…" /> : q.error ? <div className="card-body"><ErrorCallout error={q.error} /></div> : items.length === 0 ? (
          <Empty title="No clusters yet">Add one with <strong>Add cluster</strong>.</Empty>
        ) : (
          <div className="table-scroll" style={{ maxHeight: "calc(100vh - 300px)" }}>
            <table className="table">
              <thead><tr><th>Cluster</th><th>Nodes</th><th>Status</th><th>Added</th><th style={{ textAlign: "right" }}>Actions</th></tr></thead>
              <tbody>
                {items.map((c) => (
                  <tr key={c.id}>
                    <td>
                      <div className="stack-sm" style={{ gap: 2 }}>
                        <Link to={`/c/${enc(c.id)}`} className="mono" style={{ fontWeight: 600 }}>{c.id}</Link>
                        <span className="hint">{c.name}{c.description ? ` · ${c.description}` : ""}</span>
                      </div>
                    </td>
                    <td className="cell-mono" style={{ fontSize: 12 }}>{c.hosts.map((h) => <div key={h}>{h}</div>)}</td>
                    <td>
                      {c.reachable ? (
                        <span className="row nowrap" style={{ gap: 6 }}><HealthDot status={c.health} />{c.health ?? "up"} · {c.version}</span>
                      ) : (
                        <span className="row nowrap" style={{ gap: 6, color: "var(--danger)" }} title={c.error}><Icon name="alert" size={14} /> {c.errorCode === "ES_AUTH_FAILED" ? "Login refused" : "Unreachable"}</span>
                      )}
                    </td>
                    <td>
                      {c.source === "file" ? (
                        <Badge title="Defined in config/clusters.yaml on the server">clusters.yaml</Badge>
                      ) : (
                        <span className="stack-sm" style={{ gap: 2, alignItems: "flex-start" }}>
                          <Badge tone="blue">In console</Badge>
                          {c.createdBy && <span className="hint">{c.createdBy.split("@")[0]}, {when(c.createdAt)}</span>}
                        </span>
                      )}
                    </td>
                    <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                      {c.editable ? (
                        <button type="button" className="btn btn-ghost btn-sm" onClick={() => setOpen({ mode: "edit", cluster: c })}>Edit</button>
                      ) : (
                        <button type="button" className="btn btn-ghost btn-sm" onClick={() => setOpen({ mode: "view", cluster: c })}>Details</button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <span className="hint">
        Clusters added here are saved on the API server{q.data?.managedFile ? <> in <code>{q.data.managedFile}</code></> : ""}, not in S3. Passwords and API keys are write-only: they are never shown again.
      </span>
      {open?.mode === "add" && <ClusterDialog onClose={() => setOpen(null)} />}
      {open?.mode === "edit" && <ClusterDialog cluster={open.cluster} onClose={() => setOpen(null)} />}
      {open?.mode === "view" && <FileClusterDialog cluster={open.cluster} onClose={() => setOpen(null)} />}
    </Page>
  );
}

function FileClusterDialog({ cluster: c, onClose }: { cluster: AdminCluster; onClose: () => void }) {
  return (
    <Dialog title={<>Cluster <span className="mono">{c.id}</span></>} subtitle="Defined in config/clusters.yaml on the server. Change it there and restart the API." onClose={onClose}
      footer={<button type="button" className="btn btn-primary" onClick={onClose}>Close</button>}>
      <div className="kv">
        <div><span className="k">Name</span><span className="v">{c.name}</span></div>
        <div><span className="k">Authentication</span><span className="v">{c.authType === "basic" ? `User ${c.username}` : c.authType === "api_key" ? "API key" : "None"}</span></div>
        <div><span className="k">TLS</span><span className="v">{c.verifyCerts ? "Verified" : "Not verified"}{c.hasCaCert ? " · own CA" : ""}</span></div>
        <div><span className="k">Timeout</span><span className="v">{c.requestTimeout} s</span></div>
      </div>
      <div className="field"><span className="label">Nodes</span><code className="code-block" style={{ maxHeight: 120 }}>{c.hosts.join("\n")}</code></div>
      {c.reachable ? <TestResult t={c as ConnectionTest} /> : <TestResult t={{ reachable: false, error: c.error, errorCode: c.errorCode }} />}
    </Dialog>
  );
}

function ClusterDialog({ cluster, onClose }: { cluster?: AdminCluster; onClose: () => void }) {
  const editing = cluster ?? null;
  const qc = useQueryClient();
  const toast = useToast();
  const base = useId();
  const [f, setF] = useState<Form>(editing ? fromCluster(editing) : EMPTY);
  const [test, setTest] = useState<ConnectionTest | null>(null);
  const [skipTest, setSkipTest] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [confirm, setConfirm] = useState("");
  const set = <K extends keyof Form>(k: K, v: Form[K]) => {
    setF((x) => ({ ...x, [k]: v }));
    setTest(null);
  };

  const idOk = !!editing || ID_RE.test(f.id.trim());
  const hostsOk = f.hosts.split(/[\s,]+/).filter(Boolean).every((h) => /^https?:\/\/[^\s/]+/.test(h)) && f.hosts.trim().length > 0;
  const authOk = f.authType === "none" || (f.authType === "basic" ? !!f.username.trim() && (!!f.password || !!editing) : !!f.apiKey || !!editing);
  const valid = idOk && hostsOk && authOk && f.requestTimeout >= 5 && f.requestTimeout <= 300;

  const invalidate = () => {
    ["admin-clusters", "clusters", "me", "admin-users"].forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
  };
  const testIt = useMutation({
    mutationFn: async () => (await request<ConnectionTest>("/admin/clusters/test", { method: "POST", body: { ...body(f, editing), id: editing?.id ?? (f.id.trim() || undefined) } })).data,
    onSuccess: setTest,
  });
  const save = useMutation({
    mutationFn: async () => (await request<{ cluster: AdminCluster; test: ConnectionTest }>(
      editing ? `/admin/clusters/${enc(editing.id)}` : "/admin/clusters",
      { method: editing ? "PUT" : "POST", query: { skipTest }, body: body(f, editing) })).data,
    onSuccess: (r) => {
      invalidate();
      toast(editing ? `Saved ${r.cluster.id}` : `Added ${r.cluster.id}. Give people access on the Users page.`);
      onClose();
    },
    onError: (e) => {
      if (e instanceof ApiError && e.code === "CLUSTER_TEST_FAILED") setTest(e.details as ConnectionTest);
    },
  });
  const remove = useMutation({
    mutationFn: async () => (await request<{ removedFromUsers: string[] }>(`/admin/clusters/${enc(editing!.id)}`, { method: "DELETE", query: { confirm } })).data,
    onSuccess: (r) => {
      invalidate();
      toast(`Removed ${editing!.id}${r.removedFromUsers.length ? `; access removed from ${r.removedFromUsers.length} user(s)` : ""}`);
      onClose();
    },
  });
  const testFailed = save.error instanceof ApiError && save.error.code === "CLUSTER_TEST_FAILED";
  const busy = save.isPending || remove.isPending;

  const id = (s: string) => `${base}-${s}`;
  return (
    <Dialog
      wide
      busy={busy}
      title={editing ? <>Edit cluster <span className="mono">{editing.id}</span></> : "Add cluster"}
      subtitle="The connection is tested before saving. Credentials are stored on the API server only, never in S3."
      onClose={onClose}
      footer={removing ? (
        <>
          <span className="hint" style={{ marginRight: "auto" }}>Type <span className="mono">{editing!.id}</span> to remove it.</span>
          <input className="input mono" style={{ width: 200 }} value={confirm} onChange={(e) => setConfirm(e.target.value)} aria-label="Cluster id to confirm" autoFocus />
          <button type="button" className="btn" onClick={() => { setRemoving(false); setConfirm(""); }}>Keep</button>
          <button type="button" className="btn btn-danger" disabled={confirm !== editing!.id || remove.isPending} onClick={() => remove.mutate()}>
            <Icon name="trash" /> {remove.isPending ? "Removing…" : "Remove cluster"}
          </button>
        </>
      ) : (
        <>
          {editing && <button type="button" className="btn btn-ghost danger" style={{ marginRight: "auto" }} onClick={() => setRemoving(true)}>Remove cluster</button>}
          <button type="button" className="btn" onClick={() => testIt.mutate()} disabled={!valid || testIt.isPending}>
            <Icon name="plug" /> {testIt.isPending ? "Testing…" : "Test connection"}
          </button>
          <button type="button" className="btn btn-primary" onClick={() => save.mutate()} disabled={!valid || busy || (testFailed && !skipTest)}>
            {save.isPending ? "Saving…" : editing ? "Save" : "Add cluster"}
          </button>
        </>
      )}
    >
      <div className="grid-2" style={{ gap: 14 }}>
        <div className="field">
          <label htmlFor={id("id")} className="label">Cluster id <span className="hint">· used in URLs, can't change later</span></label>
          <input id={id("id")} className="input mono" value={f.id} readOnly={!!editing} placeholder="elkm2-staging" spellCheck={false}
            onChange={(e) => set("id", e.target.value.toLowerCase())} aria-invalid={!idOk && f.id.length > 0 ? true : undefined} autoFocus={!editing} />
          {!idOk && f.id.length > 0 && <span className="hint" style={{ color: "var(--danger)" }}>Lowercase letters, digits, - and _</span>}
        </div>
        <div className="field">
          <label htmlFor={id("name")} className="label">Display name</label>
          <input id={id("name")} className="input" value={f.name} placeholder="ELK M2 Staging" onChange={(e) => set("name", e.target.value)} />
        </div>
      </div>
      <div className="field">
        <label htmlFor={id("desc")} className="label">Description <span className="hint">· optional</span></label>
        <input id={id("desc")} className="input" value={f.description} onChange={(e) => set("description", e.target.value)} />
      </div>
      <div className="field">
        <label htmlFor={id("hosts")} className="label">Node URLs <span className="hint">· one per line, including http:// or https:// and the port</span></label>
        <textarea id={id("hosts")} className="textarea mono" rows={3} value={f.hosts} spellCheck={false}
          placeholder={"https://node1.elk.internal:9200\nhttps://node2.elk.internal:9200"} onChange={(e) => set("hosts", e.target.value)}
          aria-invalid={!hostsOk && f.hosts.length > 0 ? true : undefined} />
      </div>
      <div className="grid-2" style={{ gap: 14 }}>
        <div className="field">
          <label htmlFor={id("auth")} className="label">Authentication</label>
          <select id={id("auth")} className="select" value={f.authType} onChange={(e) => set("authType", e.target.value as Form["authType"])}>
            <option value="basic">Username and password</option>
            <option value="api_key">API key</option>
            <option value="none">None (security off)</option>
          </select>
        </div>
        {f.authType === "basic" && (
          <div className="field">
            <label htmlFor={id("user")} className="label">Username</label>
            <input id={id("user")} className="input mono" value={f.username} autoComplete="off" spellCheck={false} onChange={(e) => set("username", e.target.value)} />
          </div>
        )}
      </div>
      {f.authType === "basic" && (
        <div className="field">
          <label htmlFor={id("pw")} className="label">Password</label>
          <input id={id("pw")} className="input" type="password" autoComplete="new-password" value={f.password}
            placeholder={editing?.authType === "basic" ? "Leave empty to keep the saved password" : ""} onChange={(e) => set("password", e.target.value)} />
        </div>
      )}
      {f.authType === "api_key" && (
        <div className="field">
          <label htmlFor={id("key")} className="label">API key <span className="hint">· the base64 "encoded" value</span></label>
          <input id={id("key")} className="input mono" type="password" autoComplete="off" value={f.apiKey}
            placeholder={editing?.authType === "api_key" ? "Leave empty to keep the saved key" : ""} onChange={(e) => set("apiKey", e.target.value)} />
        </div>
      )}
      <Callout icon="lock">Use a dedicated service account with the <code>config_api_writer</code> role (see <code>docs/es-lockdown.md</code>), not a personal or superuser login.</Callout>
      <details>
        <summary className="label">HTTPS and advanced settings</summary>
        <div className="stack" style={{ marginTop: 12 }}>
          <label className="check"><input type="checkbox" checked={f.verifyCerts} onChange={(e) => set("verifyCerts", e.target.checked)} />
            <span>Verify TLS certificates <span className="hint">· turn off only for testing</span></span></label>
          <div className="field">
            <label htmlFor={id("ca")} className="label">CA certificate (PEM) <span className="hint">· for clusters with their own certificate authority</span></label>
            {editing?.hasCaCert && !f.caCertPem && (
              <label className="check" style={{ fontSize: 13 }}><input type="checkbox" checked={f.removeCa} onChange={(e) => set("removeCa", e.target.checked)} /> A CA certificate is saved. Tick to remove it, or paste a new one to replace it.</label>
            )}
            <textarea id={id("ca")} className="textarea mono" rows={4} value={f.caCertPem} spellCheck={false}
              placeholder={"-----BEGIN CERTIFICATE-----\n…\n-----END CERTIFICATE-----"} onChange={(e) => set("caCertPem", e.target.value)} />
          </div>
          <div className="grid-2" style={{ gap: 14 }}>
            <div className="field">
              <label htmlFor={id("timeout")} className="label">Request timeout (seconds)</label>
              <input id={id("timeout")} className="input" type="number" min={5} max={300} value={f.requestTimeout} onChange={(e) => set("requestTimeout", Number(e.target.value))} />
            </div>
            <div className="field">
              <label htmlFor={id("tags")} className="label">Tags <span className="hint">· comma separated</span></label>
              <input id={id("tags")} className="input" value={f.tags} placeholder="staging, logs" onChange={(e) => set("tags", e.target.value)} />
            </div>
          </div>
        </div>
      </details>
      {testIt.error && <ErrorCallout error={testIt.error} />}
      {test && <TestResult t={test} />}
      {testFailed && (
        <label className="check"><input type="checkbox" checked={skipTest} onChange={(e) => setSkipTest(e.target.checked)} /> Save anyway, without a working connection</label>
      )}
      {save.error && !testFailed && <ErrorCallout error={save.error} />}
      {remove.error && <ErrorCallout error={remove.error} />}
      {removing && (
        <Callout tone="danger" title={`Remove ${editing!.id}?`}>
          The console stops managing it and everyone's access to it is removed. Nothing changes on the Elasticsearch cluster itself; its snapshots and audit history stay in S3.
        </Callout>
      )}
    </Dialog>
  );
}
