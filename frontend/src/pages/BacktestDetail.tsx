import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ConfirmButton } from "../components/Confirm";
import { EquityCurve } from "../components/EquityCurve";
import { Badge, Banner, Card, ErrorBanner, Loading, Stat, StatusBadge, Tabs } from "../components/ui";
import { api } from "../lib/api";
import type { BacktestJob, BacktestResult } from "../lib/api";
import { count, epochDateTime, money, percent, ratio, shortId, signOf, when } from "../lib/format";
import { useAsync, usePolling } from "../lib/useAsync";

export default function BacktestDetailPage() {
  const { jobId } = useParams<{ jobId: string }>();
  const job = useAsync<BacktestJob>(() => api.get<BacktestJob>(`/backtests/${jobId}`), [jobId]);
  const running = job.data?.status === "queued" || job.data?.status === "running";
  usePolling(job.reload, Boolean(running));

  const result = useAsync<BacktestResult | undefined>(
    async () => (job.data?.status === "completed" ? api.get<BacktestResult>(`/backtests/${jobId}/result`) : undefined),
    [jobId, job.data?.status],
  );
  const [tab, setTab] = useState("summary");
  const [cancelError, setCancelError] = useState<string | undefined>();

  async function cancel() {
    setCancelError(undefined);
    try {
      await api.post(`/backtests/${jobId}/cancel`);
      job.reload();
    } catch (caught) {
      setCancelError(caught instanceof Error ? caught.message : String(caught));
    }
  }

  if (job.loading) return <Loading what="Loading backtest" />;
  if (job.error) return <ErrorBanner error={job.error} />;
  if (!job.data) return null;

  const metrics = result.data?.metrics ?? {};
  const document = result.data?.result;
  const repro = result.data?.reproducibility;
  const refusals = document?.risk_refusals;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Backtest <span className="mono faint" style={{ fontSize: "1rem" }}>{shortId(job.data.id)}</span></h1>
          <p>
            <StatusBadge status={job.data.status} /> queued {when(job.data.queued_at)}
            {job.data.finished_at && ` · finished ${when(job.data.finished_at)}`}
          </p>
        </div>
        <div className="row">
          {running && (
            <ConfirmButton
              label="Cancel run"
              confirmLabel="Cancel this backtest"
              consequence="The job stops and no result is stored. Submitting it again starts from the beginning."
              onConfirm={cancel}
            />
          )}
          <Link className="btn" to="/backtests">All backtests</Link>
        </div>
      </div>

      {cancelError && <Banner tone="err">{cancelError}</Banner>}

      {running && (
        <Banner tone="info">
          <span className="spinner" /> This run is {job.data.status}. The page refreshes itself.
        </Banner>
      )}
      {job.data.status === "failed" && (
        <Banner tone="err">
          <strong>{job.data.error_code}</strong> — {job.data.error_message}
        </Banner>
      )}
      {job.data.status === "cancelled" && <Banner tone="warn">This run was cancelled; no result was stored.</Banner>}

      {job.data.status === "completed" && result.data && document && repro && (
        <>
          <div className="grid cols-4">
            <Stat label="Ending equity" value={money(String(metrics.ending_equity ?? ""))} mono />
            <Stat
              label="Total return"
              value={<span className={typeof metrics.total_return === "number" && metrics.total_return < 0 ? "neg" : "pos"}>{percent(metrics.total_return as number | null)}</span>}
            />
            <Stat label="Sharpe" value={ratio(metrics.sharpe_ratio as number | null)} />
            <Stat label="Max drawdown" value={percent(metrics.max_drawdown as number | null)} />
          </div>

          <div className="grid cols-4" style={{ marginTop: "0.75rem" }}>
            <Stat label="Realized P&L" value={<span className={signOf(String(metrics.realized_pnl ?? ""))}>{money(String(metrics.realized_pnl ?? ""))}</span>} mono />
            <Stat label="Unrealized P&L" value={<span className={signOf(String(metrics.unrealized_pnl ?? ""))}>{money(String(metrics.unrealized_pnl ?? ""))}</span>} mono />
            <Stat label="Commission" value={money(String(metrics.commission_paid ?? ""))} mono />
            <Stat label="Orders / fills" value={`${count(metrics.order_count as number)} / ${count(metrics.fill_count as number)}`} sub={`${count(metrics.records_processed as number)} records`} />
          </div>

          {refusals && refusals.count > 0 && (
            <Banner tone="warn">
              {refusals.count} order(s) were refused by risk controls and never reached the venue.
              A run with refusals is not the same as a strategy that found no signals.
            </Banner>
          )}

          <div style={{ marginTop: "1.25rem" }}>
            <Tabs
              active={tab}
              onChange={setTab}
              tabs={[
                { id: "summary", label: "Equity" },
                { id: "orders", label: `Orders (${document.orders.length})` },
                { id: "fills", label: `Fills (${document.fills.length})` },
                { id: "positions", label: `Positions (${document.positions.length})` },
                { id: "analytics", label: "Analytics" },
                { id: "repro", label: "Reproducibility" },
              ]}
            />

            {tab === "summary" && (
              <Card>
                <EquityCurve
                  points={document.equity_curve}
                  startingCash={String((repro.configuration as Record<string, unknown>)?.starting_cash ?? "")}
                />
              </Card>
            )}

            {tab === "orders" && (
              <Card>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>#</th><th>Instrument</th><th>Side</th><th className="num">Quantity</th>
                        <th className="num">Filled</th><th className="num">Avg price</th><th>Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {document.orders.map((order, index) => (
                        <tr key={String(order.order_id ?? index)}>
                          <td className="num">{String(order.sequence)}</td>
                          <td className="mono">{String(order.symbol ?? order.instrument)}</td>
                          <td><Badge tone={order.side === "buy" ? "accent" : "neutral"}>{String(order.side)}</Badge></td>
                          <td className="num">{String(order.quantity)}</td>
                          <td className="num">{String(order.filled_quantity)}</td>
                          <td className="num">{money(order.average_fill_price as string)}</td>
                          <td><StatusBadge status={String(order.status)} /></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            )}

            {tab === "fills" && (
              <Card>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr><th>#</th><th>Instrument</th><th>Side</th><th className="num">Quantity</th><th className="num">Price</th><th className="num">Commission</th><th>When</th></tr>
                    </thead>
                    <tbody>
                      {document.fills.map((fill, index) => (
                        <tr key={String(fill.fill_id ?? index)}>
                          <td className="num">{String(fill.sequence)}</td>
                          <td className="mono">{String(fill.symbol ?? fill.instrument)}</td>
                          <td>{String(fill.side)}</td>
                          <td className="num">{String(fill.quantity)}</td>
                          <td className="num">{money(fill.price as string)}</td>
                          <td className="num">{money(fill.commission as string)}</td>
                          <td className="tiny faint">{epochDateTime(fill.timestamp as number)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            )}

            {tab === "positions" && (
              <Card>
                {document.positions.length === 0 ? (
                  <p className="dim" style={{ margin: 0 }}>Flat at the end of the run.</p>
                ) : (
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr><th>Instrument</th><th className="num">Quantity</th><th className="num">Average cost</th><th className="num">Mark</th><th className="num">Unrealized</th></tr>
                      </thead>
                      <tbody>
                        {document.positions.map((position) => (
                          <tr key={String(position.instrument)}>
                            <td className="mono">{String(position.symbol ?? position.instrument)}</td>
                            <td className="num">{String(position.quantity)}</td>
                            <td className="num">{money(position.average_price)}</td>
                            <td className="num">{money(position.market_price)}</td>
                            <td className={`num ${signOf(position.unrealized_pnl)}`}>{money(position.unrealized_pnl)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>
            )}

            {tab === "analytics" && (
              <Card>
                {document.report === null ? (
                  <p className="dim" style={{ margin: 0 }}>Analytics did not run for this backtest.</p>
                ) : (
                  <pre className="code">{JSON.stringify(document.report, null, 2)}</pre>
                )}
              </Card>
            )}

            {tab === "repro" && (
              <Card>
                <p className="small dim">
                  Everything needed to re-derive this result. AlphaLab produced every figure above;
                  these are the inputs it was given.
                </p>
                <div className="table-wrap">
                  <table>
                    <tbody>
                      <tr><td className="dim">Engine</td><td className="mono small">{repro.engine}</td></tr>
                      <tr><td className="dim">Seed</td><td className="mono small">{repro.seed}</td></tr>
                      <tr><td className="dim">Dataset version</td><td className="mono tiny">{repro.dataset_version_id}</td></tr>
                      <tr><td className="dim">Dataset canonical hash</td><td className="mono tiny">{repro.dataset_canonical_hash}</td></tr>
                      <tr><td className="dim">Strategy version</td><td className="mono tiny">{repro.strategy_version_id}</td></tr>
                      <tr><td className="dim">Strategy content hash</td><td className="mono tiny">{repro.strategy_content_hash}</td></tr>
                      <tr><td className="dim">Universe</td><td className="mono small">{repro.universe.join(", ")}</td></tr>
                      <tr><td className="dim">Parameters</td><td className="mono small">{JSON.stringify(repro.parameters)}</td></tr>
                    </tbody>
                  </table>
                </div>
                <h3 style={{ marginTop: "1rem" }}>Full configuration</h3>
                <pre className="code">{JSON.stringify(repro.configuration, null, 2)}</pre>
              </Card>
            )}
          </div>
        </>
      )}
    </>
  );
}
