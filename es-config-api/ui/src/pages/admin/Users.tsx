import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useMemo, useState } from "react";
import { ApiError, enc, get, request, type Credential, type Level, type UserRec } from "../../api";
import { Icon } from "../../components/icons";
import { Page } from "../../components/Shell";
import { Badge, Callout, Dialog, Empty, ErrorCallout, Loading, SearchInput, copyText, downloadFile, useToast } from "../../components/ui";
import { ago, LEVEL_LABEL, LEVEL_NOTE } from "../../format";
import { useClusters, useMe } from "../../session";

type Access = Level | "";
const LEVELS: Level[] = ["view", "edit", "delete"];
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function csvOf(rows: Credential[]): string {
  const q = (v: string) => (/[",\r\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);
  return "username,password\r\n" + rows.map((r) => `${q(r.username)},${q(r.password)}\r\n`).join("");
}

function passwordStatus(u: UserRec) {
  if (u.lockedUntil && u.lockedUntil * 1000 > Date.now()) return <Badge tone="red" title="Too many failed sign-ins">Locked</Badge>;
  if (!u.hasPassword) return <Badge title="Can't sign in until an admin resets the password">No password</Badge>;
  if (u.usingGeneratedPassword && u.bootstrap) return <Badge tone="amber" title="Still using BOOTSTRAP_ADMIN_PASSWORD from .env">Initial (.env)</Badge>;
  if (u.usingGeneratedPassword) return <Badge tone="amber" title="Still using the password an admin generated">Generated</Badge>;
  return <Badge tone="green">Set by user</Badge>;
}

function AccessBadge({ level }: { level?: Level | null }) {
  if (!level) return <span className="hint">No access</span>;
  return <Badge tone={level === "delete" ? "red" : level === "edit" ? "blue" : undefined}>{LEVEL_LABEL[level]}</Badge>;
}

export function Users() {
  const me = useMe().data!;
  const clusters = useClusters().data ?? [];
  const users = useQuery({ queryKey: ["admin-users"], queryFn: () => get<{ items: UserRec[] }>("/admin/users").then((r) => r.items) });
  const [selected, setSelected] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [filter, setFilter] = useState("");
  const columns = clusters.length <= 3 ? clusters.map((c) => c.id) : [];

  const rows = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return (users.data ?? []).filter((u) => !f || u.username.includes(f));
  }, [users.data, filter]);
  const sel = users.data?.find((u) => u.username === selected) ?? null;

  return (
    <Page crumbs={[{ label: "Administration" }, { label: "Users & permissions" }]} title="Users & permissions">
      <div className="page-head" style={{ alignItems: "center" }}>
        <div className="grow">
          <h1>Users &amp; permissions</h1>
          <span className="sub">Access is set per cluster. Each level includes the ones before it.</span>
        </div>
        <button type="button" className="btn btn-primary" onClick={() => setAdding(true)}><Icon name="plus" /> Add users</button>
      </div>
      <div className="stats" style={{ gridTemplateColumns: "repeat(3, minmax(0, 1fr))" }}>
        {LEVELS.map((l) => (
          <div key={l} className="stat" style={{ padding: "12px 14px" }}>
            <span style={{ fontSize: 13, fontWeight: 600, color: l === "delete" ? "var(--danger)" : undefined }}>{LEVEL_LABEL[l]}</span>
            <span className="stat-note">{LEVEL_NOTE[l]}</span>
          </div>
        ))}
      </div>
      <div className="grid-side users-grid">
        <section className="card" style={{ overflow: "hidden" }}>
          <div className="card-head">
            <div className="grow"><h2>{users.data ? `${users.data.length} user${users.data.length === 1 ? "" : "s"}` : "Users"}</h2></div>
            <div style={{ width: 260 }}><SearchInput label="Filter users" value={filter} onChange={setFilter} placeholder="Filter by email" /></div>
          </div>
          {users.isLoading ? <Loading /> : users.error ? <div className="card-body"><ErrorCallout error={users.error} /></div> : rows.length === 0 ? <Empty title="No users match" /> : (
            <div className="table-scroll" style={{ maxHeight: "calc(100vh - 330px)" }}>
              <table className="table compact">
                <thead>
                  <tr>
                    <th>User</th><th>Role</th>
                    {columns.length ? columns.map((c) => <th key={c} className="mono" style={{ textTransform: "none" }}>{c}</th>) : <th>Access</th>}
                    <th>Password</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((u) => (
                    <tr key={u.username} className={`clickable ${u.username === selected ? "selected" : ""}`} onClick={() => setSelected(u.username)}>
                      <td>
                        <button type="button" className="row" style={{ gap: 10, flexWrap: "nowrap", background: "none", border: 0, padding: 0, font: "inherit", color: "inherit", cursor: "pointer", textAlign: "left" }}
                          onClick={(e) => { e.stopPropagation(); setSelected(u.username); }} aria-pressed={u.username === selected}>
                          <span className="avatar" style={{ width: 30, height: 30, fontSize: 12, background: u.admin ? "#1e3a8a" : "#475569" }} aria-hidden="true">{u.username[0]}</span>
                          <span className="stack-sm" style={{ gap: 0 }}>
                            <span style={{ fontWeight: 600, overflowWrap: "anywhere" }}>{u.username}{u.username === me.username && <span className="hint"> (you)</span>}</span>
                            <span className="hint">Last sign-in {ago(u.lastLoginAt)}</span>
                          </span>
                        </button>
                      </td>
                      <td>
                        <span className="row" style={{ gap: 6 }}>
                          {u.admin ? <Badge tone="blue">Admin</Badge> : <span>Member</span>}
                          {u.bootstrap && <Badge title="Admin from BOOTSTRAP_ADMINS in .env">Bootstrap</Badge>}
                        </span>
                      </td>
                      {columns.length ? columns.map((c) => (
                        <td key={c} className="nowrap">{u.admin ? <span className="hint">All (admin)</span> : <AccessBadge level={u.clusters[c] ?? u.clusters["*"]} />}</td>
                      )) : (
                        <td>{u.admin ? <span className="hint">All (admin)</span> : <span className="hint">{Object.keys(u.clusters).length ? Object.entries(u.clusters).map(([k, v]) => `${k}: ${v}`).join(", ") : "No access"}</span>}</td>
                      )}
                      <td>{passwordStatus(u)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
        {sel ? <EditUser key={sel.username} user={sel} isMe={sel.username === me.username} passwordMode={me.authMode === "password"} onClose={() => setSelected(null)} /> : (
          <aside className="card"><Empty title="Select a user">Change their cluster access, reset their password or remove them.</Empty></aside>
        )}
      </div>
      {adding && <AddUsersDialog onClose={() => setAdding(false)} />}
    </Page>
  );
}

function AccessSelect({ id, value, onChange, disabled }: { id: string; value: Access; onChange: (v: Access) => void; disabled?: boolean }) {
  return (
    <select id={id} className="select" style={{ width: 150 }} value={value} disabled={disabled} onChange={(e) => onChange(e.target.value as Access)}>
      <option value="">No access</option>
      {LEVELS.map((l) => <option key={l} value={l}>{LEVEL_LABEL[l]}</option>)}
    </select>
  );
}

function ClusterAccess({ value, onChange, disabled }: { value: Record<string, Access>; onChange: (v: Record<string, Access>) => void; disabled?: boolean }) {
  const clusters = useClusters().data ?? [];
  const base = useId();
  const ids = [...clusters.map((c) => c.id), "*"];
  return (
    <div className="stack" style={{ gap: 10 }}>
      {ids.map((id, i) => (
        <div key={id} className="row" style={{ flexWrap: "nowrap" }}>
          <label htmlFor={`${base}-${i}`} className="grow" style={{ fontSize: 14, fontFamily: id === "*" ? undefined : "var(--font-mono)", minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
            {id === "*" ? <>All clusters <span className="mono hint">*</span></> : id}
          </label>
          <AccessSelect id={`${base}-${i}`} value={value[id] ?? ""} disabled={disabled} onChange={(v) => onChange({ ...value, [id]: v })} />
        </div>
      ))}
    </div>
  );
}

const clean = (m: Record<string, Access>) => Object.fromEntries(Object.entries(m).filter(([, v]) => v)) as Record<string, Level>;

function EditUser({ user, isMe, passwordMode, onClose }: { user: UserRec; isMe: boolean; passwordMode: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [admin, setAdmin] = useState(user.admin);
  const [access, setAccess] = useState<Record<string, Access>>(user.clusters);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [reset, setReset] = useState(false);
  useEffect(() => { setAdmin(user.admin); setAccess(user.clusters); }, [user]);
  const dirty = admin !== user.admin || JSON.stringify(clean(access)) !== JSON.stringify(user.clusters);

  const save = useMutation({
    mutationFn: () => request(`/admin/users/${enc(user.username)}`, { method: "PUT", body: { admin, clusters: clean(access) } }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["admin-users"] }); qc.invalidateQueries({ queryKey: ["me"] }); toast(`Saved ${user.username}`); },
  });
  const remove = useMutation({
    mutationFn: () => request(`/admin/users/${enc(user.username)}`, { method: "DELETE" }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["admin-users"] }); toast(`Removed ${user.username}`); onClose(); },
  });
  const locked = !!(user.lockedUntil && user.lockedUntil * 1000 > Date.now());

  return (
    <aside className="card" aria-label="Edit user">
      <div className="card-head">
        <div className="grow"><h2>Edit user</h2><span className="hint">Changes are saved to S3 and audited.</span></div>
        <button type="button" className="btn btn-ghost icon-btn" aria-label="Close" onClick={onClose}><Icon name="x" /></button>
      </div>
      <div className="card-body" style={{ gap: 18 }}>
        <div className="field">
          <label className="label" htmlFor="u-email">Email</label>
          <input id="u-email" className="input" readOnly value={user.username} />
        </div>
        {passwordMode && (
          <div className={`callout ${locked ? "danger" : user.usingGeneratedPassword ? "warn" : ""}`} style={{ flexDirection: "column", gap: 10 }}>
            <div className="stack-sm" style={{ gap: 2 }}>
              <span className="title" style={{ fontSize: 14 }}>Password</span>
              <span>
                {locked ? "Locked after too many failed sign-ins. A reset unlocks it." : !user.hasPassword ? "No password yet: they can't sign in." : user.usingGeneratedPassword && user.bootstrap ? "Still using the initial password from .env." : user.usingGeneratedPassword ? "Still using the generated password." : "Set by the user."}
                {" "}Last sign-in {ago(user.lastLoginAt)}.
              </span>
            </div>
            <button type="button" className="btn btn-sm" style={{ alignSelf: "flex-start" }} onClick={() => setReset(true)}><Icon name="key" size={15} /> Reset password…</button>
          </div>
        )}
        <label className="check" style={{ padding: "12px 14px", borderRadius: 10, border: "1px solid var(--border)" }}>
          <input type="checkbox" role="switch" checked={admin} disabled={user.bootstrap || isMe} onChange={(e) => setAdmin(e.target.checked)} />
          <span className="stack-sm" style={{ gap: 2 }}>
            <span style={{ fontWeight: 600 }}>Admin</span>
            <span className="hint">{user.bootstrap ? "Bootstrap admin from .env; can't be changed here" : isMe ? "You can't remove your own admin rights" : "Full access to every cluster, users and allowlist"}</span>
          </span>
        </label>
        <div className="stack-sm" style={{ gap: 10 }}>
          <span className="label">Cluster access</span>
          {admin ? <span className="hint">Admins have delete access on every cluster.</span> : <ClusterAccess value={access} onChange={setAccess} />}
        </div>
        {save.error && <ErrorCallout error={save.error} />}
        {remove.error && <ErrorCallout error={remove.error} />}
        {confirmRemove && (
          <Callout tone="danger" title={`Remove ${user.username}?`}>
            They can't sign in any more. Their past changes stay in the audit log.
            <div className="row" style={{ marginTop: 8 }}>
              <button type="button" className="btn btn-sm" onClick={() => setConfirmRemove(false)}>Keep</button>
              <button type="button" className="btn btn-danger btn-sm" onClick={() => remove.mutate()} disabled={remove.isPending}>Remove user</button>
            </div>
          </Callout>
        )}
      </div>
      <div className="card-foot">
        <button type="button" className="btn btn-ghost danger" style={{ marginRight: "auto" }} disabled={isMe || user.bootstrap || confirmRemove}
          title={isMe ? "You can't remove yourself" : user.bootstrap ? "Bootstrap admins come from .env" : undefined} onClick={() => setConfirmRemove(true)}>Remove user</button>
        <button type="button" className="btn" disabled={!dirty} onClick={() => { setAdmin(user.admin); setAccess(user.clusters); }}>Cancel</button>
        <button type="button" className="btn btn-primary" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>{save.isPending ? "Saving…" : "Save"}</button>
      </div>
      {reset && <ResetPasswordDialog username={user.username} onClose={() => setReset(false)} />}
    </aside>
  );
}

// ------------------------------------------------------------ credentials

function CredentialsTable({ rows, onCopy }: { rows: Credential[]; onCopy: () => void }) {
  const toast = useToast();
  return (
    <div className="card" style={{ overflow: "hidden" }}>
      <table className="table">
        <thead><tr><th>Username</th><th>Password</th><th><span className="sr-only">Copy</span></th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.username}>
              <td style={{ fontWeight: 600 }}>{r.username}</td>
              <td><code style={{ background: "var(--surface-3)", padding: "4px 8px", borderRadius: 6, fontSize: 14 }}>{r.password}</code></td>
              <td style={{ textAlign: "right" }}>
                <button type="button" className="btn icon-btn" aria-label={`Copy password for ${r.username}`}
                  onClick={() => copyText(r.password).then(() => { onCopy(); toast("Password copied"); })}>
                  <Icon name="copy" size={16} />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CredentialsFooter({ rows, filename, onDone }: { rows: Credential[]; filename: string; onDone: () => void }) {
  const [saved, setSaved] = useState(false);
  const [warned, setWarned] = useState(false);
  return (
    <>
      {warned && !saved && <span className="hint" style={{ color: "var(--danger)", marginRight: "auto" }}>Not downloaded. The passwords can't be shown again.</span>}
      <button type="button" className="btn" onClick={() => (saved || warned ? onDone() : setWarned(true))}>{warned && !saved ? "Close anyway" : "Done"}</button>
      <button type="button" className="btn btn-primary" onClick={() => { downloadFile(filename, csvOf(rows), "text/csv;charset=utf-8"); setSaved(true); }}>
        <Icon name="download" /> Download CSV
      </button>
    </>
  );
}

function OnceWarning() {
  return (
    <Callout tone="warn" icon="warn" role="status" title="Shown only once.">
      Download the CSV now and send each password privately. Anyone with the file can sign in as these users.
    </Callout>
  );
}

function AddUsersDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const clusters = useClusters().data ?? [];
  const [emails, setEmails] = useState("");
  const [admin, setAdmin] = useState(false);
  const [access, setAccess] = useState<Record<string, Access>>(clusters.length === 1 ? { [clusters[0].id]: "view" } : {});
  const [creds, setCreds] = useState<Credential[] | null>(null);
  const [, setCopied] = useState(false);
  const list = useMemo(() => Array.from(new Set(emails.split(/[\s,;]+/).map((e) => e.trim().toLowerCase()).filter(Boolean))), [emails]);
  const bad = list.filter((e) => !EMAIL_RE.test(e));

  const create = useMutation({
    mutationFn: async () => (await request<{ credentials: Credential[] }>("/admin/users/bulk", {
      method: "POST",
      body: { users: list.map((u) => ({ username: u, admin, clusters: admin ? {} : clean(access) })) },
    })).data,
    onSuccess: (r) => { setCreds(r.credentials); qc.invalidateQueries({ queryKey: ["admin-users"] }); },
  });
  const err = create.error instanceof ApiError ? create.error : null;
  const existing = (err?.details as { existing?: string[]; duplicates?: string[] } | null)?.existing;

  if (creds) {
    return (
      <Dialog title={`${creds.length} user${creds.length === 1 ? "" : "s"} created`} subtitle="They can sign in now with these passwords." onClose={onClose} wide
        footer={<CredentialsFooter rows={creds} filename="new-users-credentials.csv" onDone={onClose} />}>
        <OnceWarning />
        {creds.length ? <CredentialsTable rows={creds} onCopy={() => setCopied(true)} /> : <Callout>Password sign-in is off on this server, so no passwords were generated.</Callout>}
        <span className="hint">The CSV has two columns, <code>username,password</code>. Users can change their password after signing in (optional).</span>
      </Dialog>
    );
  }
  return (
    <Dialog title="Add users" subtitle="Passwords are generated for you." onClose={onClose} wide busy={create.isPending}
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={create.isPending}>Cancel</button>
        <button type="button" className="btn btn-primary" disabled={!list.length || bad.length > 0 || create.isPending} onClick={() => create.mutate()}>
          {create.isPending ? "Creating…" : `Create ${list.length || ""} user${list.length === 1 ? "" : "s"}`}
        </button>
      </>}>
      <div className="field">
        <label htmlFor="emails" className="label" style={{ fontSize: 14 }}>Email addresses</label>
        <textarea id="emails" className="textarea mono" rows={4} value={emails} onChange={(e) => setEmails(e.target.value)} placeholder={"priya@fenixcommerce.com\nsam@fenixcommerce.com"} spellCheck={false} />
        <span className="hint">One per line (commas work too). Each person gets the same access below; you can change it per user afterwards.</span>
        {bad.length > 0 && <span className="hint" style={{ color: "var(--danger)" }}>Not an email address: {bad.join(", ")}</span>}
      </div>
      <div className="stack-sm" style={{ gap: 10 }}>
        <span className="label" style={{ fontSize: 14 }}>Cluster access</span>
        {admin ? <span className="hint">Admins have delete access on every cluster.</span> : <ClusterAccess value={access} onChange={setAccess} />}
        <label className="check"><input type="checkbox" checked={admin} onChange={(e) => setAdmin(e.target.checked)} /> Make admin</label>
      </div>
      <Callout icon="lock">A 16-character password is generated for each person. You'll see it once, on the next screen, and can download it as a CSV.</Callout>
      {err && (existing?.length ? (
        <Callout tone="danger" title="Some users already exist · nothing was created">{existing.join(", ")}. Use “Reset password” for existing users.</Callout>
      ) : <ErrorCallout error={err} />)}
    </Dialog>
  );
}

function ResetPasswordDialog({ username, onClose }: { username: string; onClose: () => void }) {
  const qc = useQueryClient();
  const [creds, setCreds] = useState<Credential[] | null>(null);
  const reset = useMutation({
    mutationFn: async () => (await request<{ credentials: Credential }>(`/admin/users/${enc(username)}/reset-password`, { method: "POST" })).data,
    onSuccess: (r) => { setCreds([r.credentials]); qc.invalidateQueries({ queryKey: ["admin-users"] }); },
  });
  if (creds) {
    return (
      <Dialog title="Password reset" subtitle="The old password and all of this user's sessions no longer work." onClose={onClose} wide
        footer={<CredentialsFooter rows={creds} filename={`${username}-credentials.csv`} onDone={onClose} />}>
        <OnceWarning />
        <CredentialsTable rows={creds} onCopy={() => {}} />
      </Dialog>
    );
  }
  return (
    <Dialog title={`Reset password for ${username}?`} onClose={onClose} busy={reset.isPending}
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={reset.isPending}>Cancel</button>
        <button type="button" className="btn btn-primary" onClick={() => reset.mutate()} disabled={reset.isPending}>{reset.isPending ? "Resetting…" : "Generate new password"}</button>
      </>}>
      <p>A new password is generated and shown once. Their current password stops working and they are signed out everywhere. This also unlocks a locked account.</p>
      {reset.error && <ErrorCallout error={reset.error} />}
    </Dialog>
  );
}
