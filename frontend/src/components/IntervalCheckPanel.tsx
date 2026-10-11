import type { IntervalCheck } from "../lib/api";
import { count } from "../lib/format";
import { Banner } from "./ui";

/**
 * The declared interval checked against one file's bars, and the per-instrument
 * decisions it asks for.
 *
 * It distinguishes what the timestamps show (consistency with the declared
 * interval, never proof of bar length) from what only the user can assert (that
 * an instrument's observations are missing at that interval), and says what
 * asserting it means.
 */
export function IntervalCheckPanel({
  check,
  interval,
  confirmed,
  onToggle,
  scope,
}: {
  check: IntervalCheck;
  interval: string;
  confirmed: string[];
  onToggle: (symbol: string, checked: boolean) => void;
  /** Prefix for control labels, so two panels on one screen stay distinct. */
  scope: string;
}) {
  if (check.refused) return <Banner tone="err">{check.refused}</Banner>;
  if (check.ambiguous.length === 0) {
    return (
      <p className="small">
        Every instrument&apos;s own bars are consistent with {interval} bars and sit one interval apart at least once.
        Timestamps cannot prove how long a bar is: {interval} remains your declaration.
      </p>
    );
  }
  return (
    <Banner tone="warn">
      <strong>
        {interval} is your declared nominal bar interval. For {check.ambiguous.length} instrument(s) below, nothing in the
        timestamps shows it.
      </strong>{" "}
      Their bars are never one interval apart, so their longer gaps fit {interval} bars with observations missing and bars
      aggregated over a longer interval equally well; the importer cannot tell which. Ticking an instrument asserts that its
      observations are genuinely missing at {interval}. It applies only to that instrument in this file, is recorded with the
      version and shown wherever the version is used — it is never treated as verified. If it is wrong, start-stamped bars
      are moved to the wrong instant and backtests see closes before they happened. An instrument left unticked fails the
      import.
      {check.ambiguous.map((symbol) => {
        const spacing = check.spacing?.instruments[symbol];
        return (
          <label key={symbol} className="small" style={{ display: "flex", gap: "0.4rem", marginTop: "0.3rem" }}>
            <input
              type="checkbox"
              aria-label={`${scope}: ${symbol}`}
              checked={confirmed.includes(symbol)}
              onChange={(event) => onToggle(symbol, event.target.checked)}
            />
            <span>
              <span className="mono">{symbol}</span>: its observations are missing at {interval}
              {spacing && (
                <span className="tiny dim">
                  {" "}({count(spacing.bars)} bars, {count(spacing.longer_gaps)} longer gaps, none one interval apart)
                </span>
              )}
            </span>
          </label>
        );
      })}
    </Banner>
  );
}

/** SHA-256 of a file's bytes, recomputed when asked; `null` where WebCrypto is unavailable (non-secure context). */
export async function sha256Hex(file: Blob): Promise<string | null> {
  if (typeof crypto === "undefined" || !crypto.subtle) return null;
  const buffer = await new Promise<ArrayBuffer>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () => reject(reader.error ?? new Error("The file could not be read."));
    reader.readAsArrayBuffer(file);
  });
  const digest = await crypto.subtle.digest("SHA-256", new Uint8Array(buffer));
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}
