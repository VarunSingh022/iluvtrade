import type { DatasetVersion } from "../lib/api";
import { Badge } from "./ui";

/**
 * A version's bar interval as the server validated it — never the observed spacing.
 *
 * The interval is what the user declared. "Validated" means every instrument's
 * own bars were consistent with it and showed it at least once — consistency,
 * not proof: timestamps cannot show how long a bar is. Where an instrument's
 * bars could not show it, it was accepted only on the importer's named
 * declaration that its observations are missing, and that is said here as a
 * user declaration — never as something the engine verified. A version without
 * validated provenance shows no interval at all: nothing runs on it.
 */
export function BarInterval({ version }: { version: Pick<DatasetVersion, "bar_interval" | "interval_status" | "accepted_on_confirmation"> }) {
  switch (version.interval_status) {
    case "validated":
      return (
        <span
          className="mono"
          title="Declared. Every instrument's own bars are consistent with it and sit one interval apart at least once; timestamps cannot prove bar length."
        >
          {version.bar_interval}
        </span>
      );
    case "missing_observations_confirmed": {
      const names = version.accepted_on_confirmation;
      return (
        <span
          title={`Declared. Not shown by the bars of ${names.join(", ")}: accepted only on the importer's declaration that their observations are missing at this interval. Not verified.`}
        >
          <span className="mono">{version.bar_interval}</span>{" "}
          <Badge tone="warn">user-declared for {names.length}, not verified</Badge>
        </span>
      );
    }
    case "unvalidated":
      return <span title="No validated timestamp provenance; nothing can run on this version"><Badge tone="err">not validated</Badge></span>;
    default:
      return <span className="faint">—</span>;
  }
}
