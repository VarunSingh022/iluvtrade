import type { TemporalEvidence } from "../lib/api";
import { Badge } from "./ui";

/**
 * What a result's bar interval rested on, as recorded with that result.
 *
 * Three different things are kept apart, never blurred into "verified":
 * an interval the timestamps are consistent with (not proof of bar length),
 * an interval the importer declared for instruments whose timestamps cannot
 * show it (an assertion nobody verified), and no usable record at all.
 */
export function TemporalEvidencePanel({ evidence }: { evidence: TemporalEvidence | null | undefined }) {
  if (!evidence) return null;
  const declared = evidence.user_declared_instruments ?? [];
  return (
    <div className="small" data-testid="temporal-evidence">
      <div style={{ marginBottom: "0.3rem" }}>
        <strong>Bar interval: </strong>
        {evidence.bar_interval ? <span className="mono">{evidence.bar_interval}</span> : <span className="faint">—</span>}{" "}
        <StatusBadge evidence={evidence} />
        {evidence.convention && (
          <span className="tiny dim">
            {" "}
            · {evidence.convention}
            {evidence.timezone ? ` in ${evidence.timezone}` : ""}
          </span>
        )}
      </div>
      {declared.length > 0 && (
        <div style={{ marginBottom: "0.3rem" }}>
          User-declared missing observations, not verified, for:{" "}
          <span className="mono">{declared.join(", ")}</span>
        </div>
      )}
      <div className="tiny dim">{evidence.meaning}</div>
    </div>
  );
}

function StatusBadge({ evidence }: { evidence: TemporalEvidence }) {
  switch (evidence.status) {
    case "validated":
      return <Badge tone="ok">consistent with timestamps</Badge>;
    case "missing_observations_confirmed":
      return <Badge tone="warn">user-declared, not verified</Badge>;
    case "not_recorded":
      return <Badge tone="warn">not recorded</Badge>;
    case "unavailable":
      return <Badge tone="err">unavailable</Badge>;
    default:
      return <Badge tone="err">no validated provenance</Badge>;
  }
}
