import { useMemo } from "react";

/**
 * The equity curve, as an inline SVG.
 *
 * No chart library: one series over time needs a path, and pulling in a
 * charting dependency for it would add build weight and a second theming system
 * for no gain. The line is drawn from the values AlphaLab produced, downsampled
 * only for rendering — the underlying series is not altered.
 */
export function EquityCurve({
  points,
  startingCash,
  height = 180,
}: {
  points: { timestamp: number; equity: string | null }[];
  startingCash?: string;
  height?: number;
}) {
  const series = useMemo(
    () =>
      points
        .map((point) => ({ t: point.timestamp, v: point.equity === null ? NaN : Number(point.equity) }))
        .filter((point) => Number.isFinite(point.v)),
    [points],
  );

  if (series.length < 2) {
    return <div className="empty">Not enough points to draw a curve.</div>;
  }

  // Downsample for rendering only; a 100k-point path is slow and no more legible.
  const maxPoints = 600;
  const step = Math.max(1, Math.floor(series.length / maxPoints));
  const sampled = series.filter((_, index) => index % step === 0 || index === series.length - 1);

  const values = sampled.map((point) => point.v);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const width = 1000;
  const pad = 4;

  const x = (index: number) => (index / (sampled.length - 1)) * (width - pad * 2) + pad;
  const y = (value: number) => height - pad - ((value - min) / span) * (height - pad * 2);

  const path = sampled.map((point, index) => `${index === 0 ? "M" : "L"}${x(index).toFixed(1)},${y(point.v).toFixed(1)}`).join(" ");
  const area = `${path} L${x(sampled.length - 1).toFixed(1)},${height - pad} L${x(0).toFixed(1)},${height - pad} Z`;

  const opening = startingCash ? Number(startingCash) : values[0]!;
  const closing = values[values.length - 1]!;
  const up = closing >= opening;
  const stroke = up ? "var(--ok)" : "var(--err)";
  const baseline = opening >= min && opening <= max ? y(opening) : null;

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} style={{ width: "100%", height, display: "block" }} preserveAspectRatio="none" role="img" aria-label="Equity curve">
        <defs>
          <linearGradient id="equity-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={stroke} stopOpacity="0.18" />
            <stop offset="100%" stopColor={stroke} stopOpacity="0" />
          </linearGradient>
        </defs>
        {baseline !== null && (
          <line
            x1={pad}
            x2={width - pad}
            y1={baseline}
            y2={baseline}
            stroke="var(--border-strong)"
            strokeDasharray="4 4"
            strokeWidth="1"
            vectorEffect="non-scaling-stroke"
          />
        )}
        <path d={area} fill="url(#equity-fill)" />
        <path d={path} fill="none" stroke={stroke} strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="row" style={{ justifyContent: "space-between", marginTop: 4 }}>
        <span className="tiny faint mono">{min.toLocaleString(undefined, { maximumFractionDigits: 2 })}</span>
        <span className="tiny faint">
          {sampled.length} of {series.length} points shown
          {baseline !== null && " · dashed line is starting capital"}
        </span>
        <span className="tiny faint mono">{max.toLocaleString(undefined, { maximumFractionDigits: 2 })}</span>
      </div>
    </div>
  );
}
