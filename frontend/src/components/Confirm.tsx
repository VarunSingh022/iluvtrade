import { useState } from "react";

/**
 * A button that requires a second, deliberate click before it acts.
 *
 * `window.confirm` was the alternative and is worse here: it is suppressible by
 * the browser, it cannot state consequences in the product's own words, and a
 * user who has dismissed one native dialog tends to dismiss the next without
 * reading. An inline confirmation stays on the page, names what will happen,
 * and cannot be turned off.
 *
 * Used for anything that destroys something or changes a safety posture:
 * revoking a broker credential, rejecting a dataset, halting a session,
 * enabling live trading.
 */
export function ConfirmButton({
  label,
  confirmLabel,
  consequence,
  onConfirm,
  disabled,
  tone = "danger",
}: {
  label: string;
  confirmLabel: string;
  /** What will actually happen, in one sentence. Shown before the second click. */
  consequence: string;
  onConfirm: () => void | Promise<void>;
  disabled?: boolean;
  tone?: "danger" | "default";
}) {
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);

  if (!armed) {
    return (
      <button
        type="button"
        className={tone === "danger" ? "danger small" : "small"}
        disabled={disabled}
        onClick={() => setArmed(true)}
      >
        {label}
      </button>
    );
  }

  return (
    <span className="row" style={{ gap: "0.35rem", alignItems: "center" }}>
      <span className="tiny" style={{ color: "var(--err)" }}>{consequence}</span>
      <button
        type="button"
        className={tone === "danger" ? "danger small" : "primary small"}
        disabled={disabled || busy}
        onClick={async () => {
          setBusy(true);
          try {
            await onConfirm();
          } finally {
            setBusy(false);
            setArmed(false);
          }
        }}
      >
        {busy ? "Working…" : confirmLabel}
      </button>
      <button type="button" className="small" disabled={busy} onClick={() => setArmed(false)}>
        Cancel
      </button>
    </span>
  );
}
