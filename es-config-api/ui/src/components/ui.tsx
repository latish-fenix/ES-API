import { createContext, useCallback, useContext, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ApiError, PendingApproval } from "../api";
import { Icon, type IconName } from "./icons";

// ------------------------------------------------------------------ small bits

export function Spinner({ label = "Loading" }: { label?: string }) {
  return <span className="spinner" role="status" aria-label={label} />;
}

export function Loading({ what = "Loading…" }: { what?: string }) {
  return (
    <div className="empty" aria-busy="true">
      <Spinner />
      <span>{what}</span>
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <span className="t">{title}</span>
      {children && <span>{children}</span>}
    </div>
  );
}

type Tone = "info" | "success" | "warn" | "danger" | "neutral";
const TONE_ICON: Record<Tone, IconName> = { info: "info", success: "check", warn: "warn", danger: "alert", neutral: "info" };

export function Callout({ tone = "neutral", title, icon, children, role }: { tone?: Tone; title?: ReactNode; icon?: IconName; children?: ReactNode; role?: string }) {
  return (
    <div className={`callout ${tone === "neutral" ? "" : tone}`} role={role ?? (tone === "danger" ? "alert" : undefined)}>
      <Icon name={icon ?? TONE_ICON[tone]} />
      <div className="stack-sm" style={{ gap: 2, minWidth: 0 }}>
        {title && <span className="title">{title}</span>}
        {children && <div>{children}</div>}
      </div>
    </div>
  );
}

export function Badge({ tone, mono, children, title }: { tone?: "blue" | "green" | "amber" | "red"; mono?: boolean; children: ReactNode; title?: string }) {
  return <span className={`badge ${tone ?? ""} ${mono ? "mono" : ""}`} title={title}>{children}</span>;
}

export function HealthDot({ status }: { status?: string | null }) {
  return <span className={`dot ${status ?? ""}`} aria-hidden="true" />;
}

export function HealthPill({ status }: { status?: string | null }) {
  if (!status) return null;
  return (
    <span className="health-pill" title={`Cluster health: ${status}`}>
      <HealthDot status={status} />
      {status}
    </span>
  );
}

export function SearchInput({ label, value, onChange, placeholder }: { label: string; value: string; onChange: (v: string) => void; placeholder?: string }) {
  const id = useId();
  return (
    <div className="search">
      <label htmlFor={id} className="sr-only">{label}</label>
      <Icon name="search" size={16} />
      <input id={id} className="input" type="search" value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}

export function ReasonField({ value, onChange, placeholder, autoFocus }: { value: string; onChange: (v: string) => void; placeholder?: string; autoFocus?: boolean }) {
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id} className="label">
        Reason <span className="hint">· required, saved in the audit log</span>
      </label>
      <input id={id} className="input" value={value} autoFocus={autoFocus} maxLength={500}
        placeholder={placeholder ?? "Why is this change needed?"} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}

// ------------------------------------------------------------------ errors

interface Explained {
  title: string;
  hint?: ReactNode;
}

function explain(e: ApiError, ctx: { clusterId?: string; admin?: boolean }): Explained {
  const blocked = (e.details as { blocked?: string[] } | null)?.blocked;
  switch (e.code) {
    case "NOT_ALLOWLISTED":
      return {
        title: "Not on the allowlist",
        hint: (
          <>
            Nothing was changed.{" "}
            {blocked?.length ? <>Blocked: <code>{blocked.join(", ")}</code>. </> : null}
            {ctx.admin ? <Link to="/admin/allowlist">Open the allowlist</Link> : "Ask an admin to add it to the allowlist."}
          </>
        ),
      };
    case "PERMISSION_DENIED":
      return { title: "You don't have access for this", hint: "Ask an admin to raise your level on this cluster." };
    case "DRIFT_DETECTED":
      return { title: "Changed outside the API", hint: "Someone changed this directly in Elasticsearch since the API last applied it. Check the live config first." };
    case "CLUSTER_UNHEALTHY":
      return { title: "Cluster health is red", hint: "Fix the cluster first, or force the change if you are sure." };
    case "VERSION_MISMATCH":
      return { title: "The config changed since you loaded it", hint: "Reload to see the latest version, then try again." };
    case "CHANGE_IN_PROGRESS":
    case "CONCURRENT_CHANGE":
    case "LOCK_LOST":
      return { title: "Another change is in progress", hint: "Wait a moment and try again." };
    case "REASON_REQUIRED":
      return { title: "A reason is required" };
    case "STATIC_SETTING":
    case "READ_ONLY_SETTING":
      return { title: "This setting can't be changed on an open index" };
    case "MAPPING_CONFLICT":
    case "MAPPING_NOT_ADD_ONLY":
      return { title: "Mappings can only gain new fields", hint: "Existing fields can't be changed or removed." };
    case "ES_REJECTED":
      return { title: "Elasticsearch rejected the change" };
    case "CLUSTER_UNREACHABLE":
      return { title: "Can't reach the cluster", hint: "Check the network path from the API server to Elasticsearch." };
    case "ES_AUTH_FAILED":
      return { title: "The API's Elasticsearch credentials were refused", hint: "Check the cluster password in the server's .env." };
    case "NO_SNAPSHOT":
      return { title: "Nothing to roll back yet", hint: "No change has been made through the API." };
    case "NETWORK_ERROR":
      return { title: "Can't reach the API" };
    default:
      return { title: e.status >= 500 ? "Something went wrong" : "Request refused" };
  }
}

export function ErrorCallout({ error, clusterId, admin }: { error: unknown; clusterId?: string; admin?: boolean }) {
  if (!error) return null;
  if (error instanceof PendingApproval) return <ApprovalSent approval={error.approval} />;
  if (!(error instanceof ApiError)) {
    return <Callout tone="danger" title="Something went wrong">{String((error as Error)?.message ?? error)}</Callout>;
  }
  const ex = explain(error, { clusterId, admin });
  return (
    <Callout tone="danger" title={ex.title}>
      <div className="stack-sm" style={{ gap: 4 }}>
        <span>{error.message}</span>
        {ex.hint && <span>{ex.hint}</span>}
        <span className="meta">
          {error.code}
          {error.status ? ` (${error.status})` : ""}
          {error.requestId ? ` · request ${error.requestId.slice(0, 8)}` : ""}
        </span>
      </div>
    </Callout>
  );
}

/** Shown where an "applied" result would be: the change waits for an admin. */
export function ApprovalSent({ approval }: { approval: { id: string; label: string; resource: string } }) {
  return (
    <Callout tone="info" icon="inbox" title="Sent to the admins for approval" role="status">
      <div className="stack-sm" style={{ gap: 4 }}>
        <span>Nothing has changed yet. {approval.label} <b>{approval.resource}</b> runs as soon as an admin approves it; you get an email either way.</span>
        <span><Link to={`/requests/${encodeURIComponent(approval.id)}`}>Open the request</Link> · <Link to="/requests">All my requests</Link></span>
      </div>
    </Callout>
  );
}

/** Label for the button that applies a change: non-admins send a request instead. */
export function applyLabel(needsApproval: boolean | undefined, label: string): string {
  return needsApproval ? "Request approval" : label;
}

export function errorMessage(e: unknown): string {
  return e instanceof ApiError ? e.message : String((e as Error)?.message ?? e);
}

// ------------------------------------------------------------------ dialog

export function Dialog({ title, subtitle, onClose, children, footer, wide, busy }: {
  title: ReactNode;
  subtitle?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
  busy?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const busyRef = useRef(busy);
  busyRef.current = busy;

  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    const el = ref.current;
    const first = el?.querySelector<HTMLElement>("[autofocus], input:not([type=hidden]):not([disabled]), textarea, select, button:not([disabled])");
    (first ?? el)?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busyRef.current) {
        e.stopPropagation();
        closeRef.current();
      }
      if (e.key === "Tab" && el) {
        const items = Array.from(el.querySelectorAll<HTMLElement>("a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex='-1'])"));
        if (!items.length) return;
        const i = items.indexOf(document.activeElement as HTMLElement);
        if (e.shiftKey && i <= 0) {
          e.preventDefault();
          items[items.length - 1].focus();
        } else if (!e.shiftKey && i === items.length - 1) {
          e.preventDefault();
          items[0].focus();
        }
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      prev?.focus?.();
    };
  }, []);

  return (
    <div className="overlay" onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <div ref={ref} className={`dialog ${wide ? "wide" : ""}`} role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1}>
        <div className="dialog-head">
          <div>
            <h2 id={titleId}>{title}</h2>
            {subtitle && <span className="sub" style={{ fontSize: 13 }}>{subtitle}</span>}
          </div>
          <button type="button" className="btn btn-ghost icon-btn" aria-label="Close" onClick={onClose} disabled={busy}>
            <Icon name="x" />
          </button>
        </div>
        <div className="dialog-body">{children}</div>
        {footer && <div className="dialog-foot">{footer}</div>}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ toasts

interface ToastItem {
  id: number;
  text: string;
  tone: "ok" | "error";
}
const ToastCtx = createContext<(text: string, tone?: "ok" | "error") => void>(() => {});

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const push = useCallback((text: string, tone: "ok" | "error" = "ok") => {
    const id = Date.now() + Math.random();
    setItems((xs) => [...xs, { id, text, tone }]);
    setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), tone === "error" ? 8000 : 4500);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast ${t.tone === "error" ? "error" : ""}`} role="status">
            <Icon name={t.tone === "error" ? "alert" : "check"} />
            <span style={{ flex: 1 }}>{t.text}</span>
            <button type="button" aria-label="Dismiss" onClick={() => setItems((xs) => xs.filter((x) => x.id !== t.id))}>×</button>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export const useToast = () => useContext(ToastCtx);

// ------------------------------------------------------------------ misc helpers

export function copyText(text: string): Promise<void> {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  // Plain-HTTP deployments have no Clipboard API: fall back to a hidden textarea.
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand("copy");
  } finally {
    ta.remove();
  }
  return Promise.resolve();
}

export function downloadFile(name: string, content: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function usePageTitle(title: string) {
  useEffect(() => {
    document.title = title ? `${title} · ES Config Console` : "ES Config Console";
  }, [title]);
}
