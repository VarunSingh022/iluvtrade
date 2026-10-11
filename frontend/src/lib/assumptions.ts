/**
 * The words the UI uses for two research-integrity choices, kept in one place
 * so a backtest form, a session form and a result page cannot describe them
 * differently.
 *
 * Fill timing is AlphaLab's (`alphalab.execution.FillTiming`); the meanings
 * below paraphrase its own definitions. Timestamp conventions are this
 * application's declaration vocabulary (`iluvtrade.data.temporal`).
 */

export type FillTiming = "next_event" | "same_event";

export const DEFAULT_FILL_TIMING: FillTiming = "next_event";

export const FILL_TIMINGS: Record<FillTiming, { label: string; meaning: string }> = {
  next_event: {
    label: "Next bar (no look-ahead)",
    meaning:
      "An order fills at its instrument's next bar. The strategy decides on one bar and trades on " +
      "the next, at the cost of one bar's delay.",
  },
  same_event: {
    label: "Same bar (optimistic)",
    meaning:
      "An order fills at the close of the bar that produced it. No real order trades at the price " +
      "that triggered it, so AlphaLab calls this optimistic by construction.",
  },
};

export function describeFillTiming(value: unknown): { label: string; meaning: string } {
  return value === "next_event" || value === "same_event"
    ? FILL_TIMINGS[value]
    : { label: String(value ?? "not recorded"), meaning: "Recorded before fill timing was stored." };
}

export type TimestampConvention = "session_date" | "interval_start" | "interval_end";

export const TIMESTAMP_CONVENTIONS: Record<TimestampConvention, { label: string; meaning: string }> = {
  session_date: {
    label: "Trading-session date",
    meaning:
      "Each date names the trading session the daily bar summarizes. The bar is stamped at " +
      "23:59:59.999999 of that date in the zone you give — the end of the civil day, not the " +
      "exchange's closing time; no exchange calendar is assumed.",
  },
  interval_end: {
    label: "End of the bar's interval",
    meaning: "Each timestamp is when the bar closed. It is used as written.",
  },
  interval_start: {
    label: "Start of the bar's interval",
    meaning:
      "Each timestamp is when the bar opened (\"the 09:15 bar\"). It is moved to the end of its " +
      "interval, so the close is never seen before it existed.",
  },
};

/**
 * Bar intervals offered for declaration (AlphaLab interval codes). The interval
 * is declared, never inferred from the data; the data can only refuse it.
 */
export const BAR_INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"] as const;

/** Zones this deployment already knows exchanges by; offered, never preselected. */
export const KNOWN_EXCHANGE_ZONES: Array<{ label: string; zone: string }> = [
  { label: "NSE / BSE", zone: "Asia/Kolkata" },
];
