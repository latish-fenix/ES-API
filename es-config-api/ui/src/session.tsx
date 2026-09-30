import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, type ReactNode } from "react";
import { Navigate, useLocation, useNavigate, useParams } from "react-router-dom";
import { ApiError, get, setUnauthenticatedHandler, type Access, type AdminCluster, type Cluster, type Health, type Level, type Me } from "./api";
import { globToRegExp } from "./glob";
import { Loading } from "./components/ui";

export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: () => get<Me>("/me"), staleTime: 60_000, retry: false });
}

export function useClusters() {
  return useQuery({ queryKey: ["clusters"], queryFn: () => get<{ items: Cluster[] }>("/clusters").then((r) => r.items), staleTime: 60_000 });
}

export function useAdminClusters(enabled: boolean) {
  return useQuery({
    queryKey: ["admin-clusters"],
    queryFn: () => get<{ items: AdminCluster[]; managedFile: string | null }>("/admin/clusters").then((r) => r.items),
    enabled,
    staleTime: 5 * 60_000,
  });
}

export function useHealth(clusterId: string | undefined) {
  return useQuery({
    queryKey: ["health", clusterId],
    queryFn: () => get<Health>(`/clusters/${encodeURIComponent(clusterId!)}/health`),
    enabled: !!clusterId,
    refetchInterval: 60_000,
    retry: 1,
  });
}

const ORDER: Record<string, number> = { none: 0, view: 1, edit: 2, delete: 3 };

function specificity(p: string): [number, number, number] {
  const wild = p.includes("*") || p.includes("?") ? 1 : 0;
  return [wild, -p.replace(/[*?]/g, "").length, -p.length];
}

/** Level on one index: the most specific matching rule, else the cluster default (same rules as the API). */
export function indexLevel(access: Access | null | undefined, index: string): Level | null {
  if (!access) return null;
  const hits = access.indices.filter((r) => globToRegExp(r.pattern).test(index));
  if (hits.length) {
    const best = hits.sort((a, b) => {
      const x = specificity(a.pattern), y = specificity(b.pattern);
      return x[0] - y[0] || x[1] - y[1] || x[2] - y[2];
    })[0];
    return best.level === "none" ? null : best.level;
  }
  return access.default;
}

/** The cluster in the URL and the caller's access on it. */
export function useCluster() {
  const { clusterId = "" } = useParams();
  const me = useMe().data;
  const access: Access | null = me ? (me.admin ? { default: "delete", indices: [] } : me.access?.[clusterId] ?? null) : null;
  const level: Level | null = access?.default ?? null;
  const can = (needed: Level) => !!level && ORDER[level] >= ORDER[needed];
  const canIndex = (index: string, needed: Level) => ORDER[indexLevel(access, index) ?? "none"] >= ORDER[needed];
  const hasAccess = !!access && (!!access.default || access.indices.some((r) => r.level !== "none"));
  return { clusterId, level, can, canIndex, hasAccess, access, admin: !!me?.admin, me };
}

const LAST_CLUSTER = "esc.lastCluster";
export function rememberCluster(id: string) {
  try {
    localStorage.setItem(LAST_CLUSTER, id);
  } catch {
    /* storage unavailable: fine */
  }
}
export function lastCluster(): string | null {
  try {
    return localStorage.getItem(LAST_CLUSTER);
  } catch {
    return null;
  }
}

/** Gate for signed-in pages: loads /me, sends signed-out visitors to /login. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const me = useMe();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    setUnauthenticatedHandler(() => {
      qc.clear();
      const next = location.pathname + location.search;
      navigate(`/login?next=${encodeURIComponent(next)}&expired=1`, { replace: true });
    });
    return () => setUnauthenticatedHandler(() => {});
  }, [qc, navigate, location]);

  if (me.isLoading) return <div style={{ paddingTop: "30vh" }}><Loading what="Signing you in…" /></div>;
  if (me.error) {
    const e = me.error;
    if (e instanceof ApiError && e.status === 401) {
      const next = location.pathname + location.search;
      return <Navigate to={`/login${next && next !== "/" ? `?next=${encodeURIComponent(next)}` : ""}`} replace />;
    }
    return (
      <div className="login-side" style={{ minHeight: "100%" }}>
        <div className="login-card">
          <h2>Can't load your account</h2>
          <p className="sub">{(e as Error).message}</p>
          <button className="btn btn-primary" onClick={() => me.refetch()}>Try again</button>
        </div>
      </div>
    );
  }
  return <>{children}</>;
}

export function RequireAdmin({ children }: { children: ReactNode }) {
  const me = useMe().data;
  if (!me?.admin) return <Navigate to="/" replace />;
  return <>{children}</>;
}
