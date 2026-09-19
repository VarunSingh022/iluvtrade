import type { ReactNode } from "react";

import { statusTone, titleCase } from "../lib/format";

export function Card({ title, actions, children }: { title?: ReactNode; actions?: ReactNode; children: ReactNode }) {
  return (
    <div className="card">
      {(title || actions) && (
        <div className="card-title">
          <span>{title}</span>
          {actions}
        </div>
      )}
      {children}
    </div>
  );
}

export function Stat({ label, value, sub, mono }: { label: string; value: ReactNode; sub?: ReactNode; mono?: boolean }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className={mono ? "stat-value mono" : "stat-value"}>{value}</div>
      {sub !== undefined && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

export function Badge({ tone, children }: { tone: string; children: ReactNode }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

export function StatusBadge({ status }: { status: string }) {
  return <Badge tone={statusTone(status)}>{titleCase(status)}</Badge>;
}

/**
 * The mode badge.
 *
 * Paper and live are visually distinct by deliberate design, not by accident of
 * palette: PHASE 14 requires the two never be confusable, and a trader glancing
 * at a screen must be able to tell which one they are looking at without
 * reading.
 */
export function ModeBadge({ mode }: { mode: string }) {
  return <Badge tone={mode === "live" ? "live" : "paper"}>{mode.toUpperCase()}</Badge>;
}

export function Banner({ tone, children }: { tone: "info" | "warn" | "err" | "ok"; children: ReactNode }) {
  return <div className={`banner ${tone}`}>{children}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Loading({ what = "Loading" }: { what?: string }) {
  return (
    <div className="empty">
      <span className="spinner" /> {what}…
    </div>
  );
}

export function ErrorBanner({ error }: { error: Error | undefined }) {
  if (!error) return null;
  return <Banner tone="err">{error.message}</Banner>;
}

/** A 0–100 quality score, coloured by band. */
export function ScoreMeter({ score }: { score: number | null }) {
  if (score === null) return <span className="dim">—</span>;
  const tone = score >= 90 ? "ok" : score >= 60 ? "warn" : "err";
  return (
    <div style={{ minWidth: 90 }}>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 2 }}>
        <span className="mono tiny">{score.toFixed(1)}</span>
      </div>
      <div className={`meter ${tone}`}>
        <span style={{ width: `${Math.max(2, Math.min(100, score))}%` }} />
      </div>
    </div>
  );
}

export function Tabs({ tabs, active, onChange }: { tabs: { id: string; label: string }[]; active: string; onChange: (id: string) => void }) {
  return (
    <div className="tabs">
      {tabs.map((tab) => (
        <button key={tab.id} className={`tab ${tab.id === active ? "active" : ""}`} onClick={() => onChange(tab.id)} type="button">
          {tab.label}
        </button>
      ))}
    </div>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="field">
      <label>{label}</label>
      {children}
      {hint && <div className="tiny faint" style={{ marginTop: 2 }}>{hint}</div>}
    </div>
  );
}
