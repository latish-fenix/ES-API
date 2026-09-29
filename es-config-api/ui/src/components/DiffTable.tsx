import type { Diff } from "../api";
import { showValue } from "../format";

export function diffCount(d?: Diff | null): number {
  return d ? d.added.length + d.removed.length + d.changed.length : 0;
}

export function DiffTable({ diff, absent = "not set", keyLabel = "Setting", beforeLabel = "Before", afterLabel = "After" }: {
  diff: Diff;
  absent?: string;
  keyLabel?: string;
  beforeLabel?: string;
  afterLabel?: string;
}) {
  const rows = [
    ...diff.added.map((r) => ({ kind: "added" as const, ...r })),
    ...diff.changed.map((r) => ({ kind: "changed" as const, ...r })),
    ...diff.removed.map((r) => ({ kind: "removed" as const, ...r })),
  ];
  if (!rows.length) return <div className="empty" style={{ padding: 16 }}>No differences.</div>;
  return (
    <div className="card" style={{ overflow: "hidden" }}>
      <div className="table-scroll" style={{ maxHeight: 320 }}>
        <table className="table">
          <thead>
            <tr>
              <th style={{ width: 90 }}>Change</th>
              <th style={{ width: "34%" }}>{keyLabel}</th>
              <th>{beforeLabel}</th>
              <th>{afterLabel}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.kind}:${r.path}`}>
                <td><span className={`diff-kind ${r.kind}`}>{r.kind[0].toUpperCase() + r.kind.slice(1)}</span></td>
                <td className="cell-mono">{r.path}</td>
                <td>{r.kind === "added" ? <span className="val none">{absent}</span> : <span className="val">{showValue(r.before)}</span>}</td>
                <td>{r.kind === "removed" ? <span className="val none">{absent}</span> : <span className="val after">{showValue(r.after)}</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
