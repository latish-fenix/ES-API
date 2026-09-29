import type { Level, NamedType } from "./api";

export const nf = new Intl.NumberFormat("en-US");

export function num(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? nf.format(n) : String(v);
}

export function bytes(b: number | null | undefined): string {
  if (b === null || b === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = b;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

/** "2026-09-29T06:08:11.123Z" -> "Sep 29, 06:08 UTC" (all times shown in UTC, like the audit log). */
export function when(iso: string | null | undefined, withDate = true): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const time = d.toISOString().slice(11, 16);
  if (!withDate) return time;
  const date = d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
  return `${date}, ${time} UTC`;
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  const d = Math.floor(s / 86400);
  return d === 1 ? "yesterday" : `${d} days ago`;
}

export function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

export function showValue(v: unknown): string {
  if (v === undefined) return "";
  if (typeof v === "string") return v;
  return JSON.stringify(v);
}

export function pretty(v: unknown): string {
  return JSON.stringify(v, null, 2);
}

export const LEVEL_LABEL: Record<Level, string> = { view: "View", edit: "Edit", delete: "Delete" };
export const LEVEL_NOTE: Record<Level, string> = {
  view: "Read config, health, snapshots",
  edit: "Also dry run, apply, roll back",
  delete: "Also delete indices",
};

export const NAMED_META: Record<NamedType, { label: string; singular: string; blurb: string; icon: "template" | "layers" | "clock" | "pipeline" }> = {
  "index-templates": { label: "Index templates", singular: "index template", blurb: "Settings and mappings for new indices", icon: "template" },
  "component-templates": { label: "Component templates", singular: "component template", blurb: "Reusable building blocks for templates", icon: "layers" },
  "ilm-policies": { label: "ILM policies", singular: "ILM policy", blurb: "Rollover and retention", icon: "clock" },
  "ingest-pipelines": { label: "Ingest pipelines", singular: "ingest pipeline", blurb: "Processing at index time, with sample-doc preview", icon: "pipeline" },
};

export const CONFIG_TYPE_LABEL: Record<string, string> = {
  "cluster-settings": "Cluster settings",
  "index-settings": "Index settings",
  "index-mappings": "Index mappings",
  "index-delete": "Index delete",
  ...Object.fromEntries(Object.entries(NAMED_META).map(([k, v]) => [k, v.label])),
};

export const NEW_TEMPLATES: Record<NamedType, unknown> = {
  "index-templates": { index_patterns: ["my-logs-*"], priority: 100, template: { settings: { number_of_replicas: 1 } } },
  "component-templates": { template: { settings: { number_of_replicas: 1 } } },
  "ilm-policies": {
    policy: {
      phases: {
        hot: { actions: { rollover: { max_age: "1d", max_primary_shard_size: "50gb" } } },
        delete: { min_age: "30d", actions: { delete: {} } },
      },
    },
  },
  "ingest-pipelines": { description: "", processors: [{ set: { field: "env", value: "prod" } }] },
};
