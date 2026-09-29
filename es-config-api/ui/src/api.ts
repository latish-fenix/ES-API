// Thin client for the ES Config API. Same origin: the session cookie rides along, and the
// X-Requested-With header satisfies the API's CSRF check for writes.

export class ApiError extends Error {
  status: number;
  code: string;
  details: unknown;
  requestId: string | null;

  constructor(status: number, code: string, message: string, details: unknown, requestId: string | null) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestId = requestId;
  }
}

type Query = Record<string, string | number | boolean | null | undefined>;

interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: Query;
  headers?: Record<string, string>;
}

let onUnauthenticated: (() => void) | null = null;
export function setUnauthenticatedHandler(fn: () => void) {
  onUnauthenticated = fn;
}

function buildUrl(path: string, query?: Query) {
  const url = new URL(`/api/v1${path}`, window.location.origin);
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v !== undefined && v !== null && v !== "" && v !== false) url.searchParams.set(k, String(v));
  }
  return url.pathname + url.search;
}

export interface Response<T> {
  data: T;
  etag: string | null;
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<Response<T>> {
  const headers: Record<string, string> = { Accept: "application/json", "X-Requested-With": "es-config-ui", ...opts.headers };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  let res: globalThis.Response;
  try {
    res = await fetch(buildUrl(path, opts.query), {
      method: opts.method ?? "GET",
      headers,
      credentials: "same-origin",
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", "Can't reach the API. Check your connection and try again.", null, null);
  }
  const requestId = res.headers.get("x-request-id");
  const text = await res.text();
  let json: unknown = null;
  if (text) {
    try {
      json = JSON.parse(text);
    } catch {
      json = null;
    }
  }
  if (!res.ok) {
    const err = (json as { error?: { code?: string; message?: string; details?: unknown } } | null)?.error;
    const e = new ApiError(
      res.status,
      err?.code ?? `HTTP_${res.status}`,
      err?.message ?? (res.statusText || "Request failed"),
      err?.details ?? null,
      requestId,
    );
    if (res.status === 401 && (e.code === "NOT_AUTHENTICATED" || e.code === "SESSION_EXPIRED") && !path.startsWith("/auth/")) {
      onUnauthenticated?.();
    }
    throw e;
  }
  return { data: json as T, etag: res.headers.get("etag") };
}

export const get = <T>(path: string, query?: Query) => request<T>(path, { query }).then((r) => r.data);

// ------------------------------------------------------------------ types

export type Level = "view" | "edit" | "delete";

export interface Me {
  username: string;
  admin: boolean;
  authMode: "password" | "header";
  usingGeneratedPassword: boolean;
  lastLoginAt: string | null;
  clusters: Record<string, Level>;
}

export interface Cluster {
  id: string;
  name?: string | null;
  description?: string | null;
  tags?: string[] | Record<string, string> | null;
  permission: Level;
}

export interface ConnectionTest {
  reachable: boolean;
  version?: string;
  clusterName?: string;
  clusterUuid?: string;
  health?: string;
  numberOfNodes?: number;
  warnings?: string[];
  error?: string;
  errorCode?: string;
}

export interface AdminCluster extends Partial<ConnectionTest> {
  id: string;
  name: string;
  description: string;
  tags: string[];
  hosts: string[];
  authType: "basic" | "api_key" | "none";
  username: string | null;
  verifyCerts: boolean;
  hasCaCert: boolean;
  requestTimeout: number;
  source: "file" | "managed";
  editable: boolean;
  createdAt?: string;
  createdBy?: string;
  updatedAt?: string;
  updatedBy?: string;
}

export interface Health {
  clusterId: string;
  status: "green" | "yellow" | "red";
  numberOfNodes: number;
  unassignedShards: number;
  clusterName: string;
}

export interface LastApplied {
  version: string;
  at: string;
  by: string;
  changeId: string;
  action: string;
}

export interface ConfigDoc<C = Record<string, unknown>> {
  clusterId: string;
  configType: string;
  resource: string;
  version: string;
  config: C;
  rollbackAvailable: boolean;
  driftDetected: boolean;
  lastApplied: LastApplied | null;
  warnings: string[];
}

export interface DiffItem {
  path: string;
  before?: unknown;
  after?: unknown;
}

export interface Diff {
  added: DiffItem[];
  removed: DiffItem[];
  changed: DiffItem[];
}

export interface ChangeResult {
  changeId: string;
  action: "UPDATE" | "ROLLBACK";
  dryRun: boolean;
  applied: boolean;
  noChange?: boolean;
  versionBefore: string;
  versionAfter?: string;
  diff: Diff;
  createsResource: boolean;
  deletesResource: boolean;
  warnings: string[];
  permanent: boolean;
  driftDetected: boolean;
  clusterHealth: string;
  simulation?: Record<string, unknown> | null;
  valid?: boolean;
  errors?: { code: string; message: string }[];
  rollbackAvailable?: boolean;
  rollbackAvailableAfter?: boolean;
}

export interface Snapshot {
  state: { exists: boolean; config: unknown };
  version: string;
  capturedAt: string;
  capturedBy: string;
  changeId: string;
  replacedBy: string;
  rollbackSupported: boolean;
}

export interface IndexRow {
  index: string;
  health: string;
  status: string;
  "docs.count": string | null;
}

export interface Tombstone {
  index: string;
  deletedAt: string;
  deletedBy: string;
  reason: string | null;
  docsCount: number | null;
  storeSizeBytes: number | null;
  key: string;
  definition: unknown;
}

export interface DeleteResult {
  index: string;
  status: string;
  health: string;
  docsCount: number | null;
  storeSizeBytes: number | null;
  primaryShards: number | null;
  replicas: number | null;
  createdAt: string | null;
  hidden: boolean;
  aliases: string[];
  dataStream: string | null;
  warnings: string[];
  applied: boolean;
  clusterHealth: string;
}

export interface UserRec {
  username: string;
  admin: boolean;
  bootstrap: boolean;
  clusters: Record<string, Level>;
  hasPassword: boolean;
  usingGeneratedPassword: boolean;
  lastLoginAt?: string | null;
  lockedUntil?: number | null;
  createdAt?: string;
  createdBy?: string;
  updatedAt?: string;
  updatedBy?: string;
}

export interface Credential {
  username: string;
  password: string;
}

export type Rule = { allow: string[]; deny: string[]; indices?: string[]; indexPatterns?: string[] };
export type Rules = Partial<Record<string, Rule>>;

export interface AuditEvent {
  eventId: string;
  timestamp: string;
  action: string;
  requestedAction?: string;
  actor: string;
  outcome: "SUCCESS" | "REJECTED" | "FAILED" | "NO_CHANGE";
  clusterId?: string;
  configType?: string;
  resource?: string;
  reason?: string | null;
  forced?: boolean;
  requestId?: string;
  sourceIp?: string | null;
  diff?: Diff;
  error?: { status: number; code: string; message: string };
  targetUser?: string;
  allowlist?: string;
  [k: string]: unknown;
}

// ------------------------------------------------------------ data browser

export interface DataField {
  name: string;
  type: string;
  types: string[] | null;
  searchable: boolean;
  aggregatable: boolean;
  object: boolean;
}

export interface DataFields {
  index: string;
  indices: string[];
  fields: DataField[];
  nestedPaths: string[];
  dateFields: string[];
}

export type FilterOp = "is" | "is_not" | "one_of" | "not_one_of" | "exists" | "not_exists" | "between" | "contains";

export interface DataFilter {
  field: string;
  op: FilterOp;
  value?: string;
  values?: string[];
  gte?: string;
  lte?: string;
}

export interface DataSort {
  field: string;
  order: "asc" | "desc";
  unmappedType?: string;
}

export interface DataSearchBody {
  query: string;
  filters: DataFilter[];
  timeRange?: { field: string; gte?: string; lte?: string } | null;
  sort: DataSort[];
  from: number;
  size: number;
}

export interface DataHit {
  _index: string;
  _id: string;
  _score: number | null;
  _source: Record<string, unknown>;
}

export interface DataSearchResult {
  total: number;
  totalRelation: "eq" | "gte";
  took: number;
  timedOut: boolean;
  from: number;
  size: number;
  hits: DataHit[];
  shardFailures?: string[];
  maxWindow: number;
}

/** POST that returns a file (export). Errors come back as the usual ApiError. */
export async function downloadPost(path: string, body: unknown): Promise<{ blob: Blob; filename: string; rows: number }> {
  let res: globalThis.Response;
  try {
    res = await fetch(buildUrl(path), {
      method: "POST",
      headers: { Accept: "*/*", "Content-Type": "application/json", "X-Requested-With": "es-config-ui" },
      credentials: "same-origin",
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", "Can't reach the API. Check your connection and try again.", null, null);
  }
  if (!res.ok) {
    let err: { code?: string; message?: string; details?: unknown } | undefined;
    try {
      err = (await res.json())?.error;
    } catch {
      err = undefined;
    }
    const e = new ApiError(res.status, err?.code ?? `HTTP_${res.status}`, err?.message ?? "Export failed", err?.details ?? null, res.headers.get("x-request-id"));
    if (res.status === 401) onUnauthenticated?.();
    throw e;
  }
  const cd = res.headers.get("content-disposition") ?? "";
  const filename = /filename="([^"]+)"/.exec(cd)?.[1] ?? "export";
  return { blob: await res.blob(), filename, rows: Number(res.headers.get("x-export-rows") ?? 0) };
}

// ------------------------------------------------------------ path helpers

export const NAMED_TYPES = ["index-templates", "component-templates", "ilm-policies", "ingest-pipelines"] as const;
export type NamedType = (typeof NAMED_TYPES)[number];

export const enc = encodeURIComponent;
