/**
 * Display helpers.
 *
 * Money arrives from the API as a **string** and stays one. Parsing it to a
 * float to format it would reintroduce exactly the precision loss the backend
 * takes care to avoid, so the grouping below is done on the string itself.
 */

export function money(value: string | null | undefined, currency = ""): string {
  if (value === null || value === undefined || value === "") return "—";
  const negative = value.startsWith("-");
  const bare = negative ? value.slice(1) : value;
  const [whole = "0", fraction] = bare.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const shown = fraction ? `${grouped}.${fraction.slice(0, 2)}` : grouped;
  return `${negative ? "-" : ""}${shown}${currency ? ` ${currency}` : ""}`;
}

export function percent(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function ratio(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toFixed(digits);
}

export function count(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString();
}

/** A unix timestamp (seconds) as a date. */
export function epochDate(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return new Date(value * 1000).toISOString().slice(0, 10);
}

export function epochDateTime(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return new Date(value * 1000).toISOString().slice(0, 16).replace("T", " ");
}

export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export function relative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function shortId(value: string | null | undefined): string {
  return value ? value.slice(0, 8) : "—";
}

/** Whether a money string is negative, for colouring a P&L cell. */
export function signOf(value: string | null | undefined): "pos" | "neg" | "" {
  if (!value) return "";
  if (value.startsWith("-")) return "neg";
  return Number(value) === 0 ? "" : "pos";
}

export function titleCase(value: string): string {
  return value.replace(/[_.]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** The badge class for a status string, so statuses look the same everywhere. */
export function statusTone(status: string): string {
  switch (status) {
    case "approved":
    case "completed":
    case "published":
    case "connected":
    case "active":
    case "stopped":
      return "ok";
    case "failed":
    case "rejected":
    case "halted":
    case "error":
    case "revoked":
    case "expired":
      return "err";
    case "pending_approval":
    case "queued":
    case "running":
    case "starting":
    case "paused":
    case "submitted":
    case "validating":
    case "authorizing":
      return "warn";
    default:
      return "neutral";
  }
}
