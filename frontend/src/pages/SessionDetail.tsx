import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { Badge, Banner, Card, ErrorBanner, Loading, ModeBadge, Stat, StatusBadge, Tabs } from "../components/ui";
import { ApiError, api } from "../lib/api";
import type { SessionEvent, SessionFill, SessionOrder, SessionPosition, TradingSession } from "../lib/api";
import { epochDateTime, money, relative, shortId, signOf, titleCase, when } from "../lib/format";
import { useAsync, usePolling } from "../lib/useAsync";

export default function SessionDetailPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const session = useAsync<TradingSession>(() => api.get<TradingSession>(`/trading/sessions/${sessionId}`), [sessionId]);
  const orders = useAsync<SessionOrder[]>(() => api.get<SessionOrder[]>(`/trading/sessions/${sessionId}/orders`), [sessionId]);
  const fills = useAsync<SessionFill[]>(() => api.get<SessionFill[]>(`/trading/sessions/${sessionId}/fills`), [sessionId]);
  const positions = useAsync<SessionPosition[]>(() => api.get<SessionPosition[]>(`/trading/sessions/${sessionId}/positions`), [sessionId]);
  const events = useAsync<SessionEvent[]>(() => api.get<SessionEvent[]>(`/trading/sessions/${sessionId}/events`), [sessionId]);

  const active = ["running", "starting", "stopping"].includes(session.data?.status ?? "");
  usePolling(() => {
    session.reload();
    orders.reload();
    fills.reload();
    positions.reload();
    events.reload();
  }, active, 2000);

  const [tab, setTab] = useState("positions");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();

  async function control(action: "start" | "pause" | "resume" | "stop") {
    setBusy(true);
    setError(undefined);
    try {
      await api.post(`/trading/sessions/${sessionId}/${action}`);
      session.reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  async function kill() {
    const reason = window.prompt("Why is this session being halted? The reason is recorded.");
    if (!reason) return;
    setBusy(true);
    try {
      await api.post(`/trading/sessions/${sessionId}/kill`, { reason });
      session.reload();
      events.reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  if (session.loading) return <Loading what="Loading session" />;
  if (session.error) return <ErrorBanner error={session.error} />;
  if (!session.data) return null;

  const data = session.data;
  const stale = data.status === "running" && !data.is_live_state;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>
            {data.name} <ModeBadge mode={data.mode} />
          </h1>
          <p>
            <StatusBadge status={data.status} /> · strategy version{" "}
            <span className="mono">{shortId(data.strategy_version_id)}</span>
            {data.entitlement_id && <> · run under entitlement <span className="mono">{shortId(data.entitlement_id)}</span></>}
            {" · "}seed <span className="mono">{data.seed}</span>
          </p>
        </div>
        <div className="row">
          <Link className="btn" to="/sessions">All sessions</Link>
          {data.status === "created" && <button className="primary" disabled={busy} onClick={() => void control("start")} type="button">Start</button>}
          {data.status === "running" && <button disabled={busy} onClick={() => void control("pause")} type="button">Pause</button>}
          {data.status === "paused" && <button disabled={busy} onClick={() => void control("resume")} type="button">Resume</button>}
          {["created", "starting", "running", "paused"].includes(data.status) && (
            <>
              <button disabled={busy} onClick={() => void control("stop")} type="button">Stop</button>
              <button className="danger" disabled={busy} onClick={() => void kill()} type="button">Kill switch</button>
            </>
          )}
        </div>
      </div>

      {error && <Banner tone="err">{error}</Banner>}
      {data.kill_switch_engaged && (
        <Banner tone="err">
          <strong>Halted by the kill switch.</strong> {data.kill_switch_reason} — a halted session
          cannot be resumed; create a new one once the cause has been dealt with.
        </Banner>
      )}
      {data.failure_reason && <Banner tone="err">{data.failure_reason}</Banner>}
      {stale && (
        <Banner tone="warn">
          This session is marked running but has not advanced since{" "}
          {relative(data.last_advanced_at)}. The figures below are the last known state, not live
          state.
        </Banner>
      )}
      {data.mode === "live" && (
        <Banner tone="err">
          <strong>LIVE.</strong> Orders from this session are routed to a real venue.
        </Banner>
      )}

      <div className="grid cols-4">
        <Stat label="Equity" value={money(data.equity)} sub={`from ${money(data.starting_cash)} ${data.base_currency}`} mono />
        <Stat label="Cash" value={money(data.cash)} mono />
        <Stat label="Realized P&L" value={<span className={signOf(data.realized_pnl)}>{money(data.realized_pnl)}</span>} mono />
        <Stat label="Unrealized P&L" value={<span className={signOf(data.unrealized_pnl)}>{money(data.unrealized_pnl)}</span>} mono />
      </div>

      <div className="grid cols-4" style={{ marginTop: "0.75rem" }}>
        <Stat label="Records processed" value={data.records_processed} />
        <Stat label="Orders" value={(orders.data ?? []).length} sub={`${(fills.data ?? []).length} fills`} />
        <Stat label="Commission" value={money(data.commission_paid)} mono />
        <Stat label="Started" value={<span className="small">{when(data.started_at)}</span>} sub={data.stopped_at ? `stopped ${when(data.stopped_at)}` : undefined} />
      </div>

      <div style={{ marginTop: "1.25rem" }}>
        <Tabs
          active={tab}
          onChange={setTab}
          tabs={[
            { id: "positions", label: `Positions (${(positions.data ?? []).length})` },
            { id: "orders", label: `Orders (${(orders.data ?? []).length})` },
            { id: "fills", label: `Fills (${(fills.data ?? []).length})` },
            { id: "events", label: `Events (${(events.data ?? []).length})` },
            { id: "risk", label: "Risk limits" },
          ]}
        />

        {tab === "positions" && (
          <Card>
            {(positions.data ?? []).length === 0 ? (
              <p className="dim" style={{ margin: 0 }}>Flat.</p>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr><th>Instrument</th><th className="num">Quantity</th><th className="num">Average cost</th><th className="num">Mark</th><th className="num">Unrealized</th><th className="num">Realized</th></tr>
                  </thead>
                  <tbody>
                    {(positions.data ?? []).map((position) => (
                      <tr key={position.instrument}>
                        <td className="mono">{position.instrument}</td>
                        <td className="num">{position.quantity}</td>
                        <td className="num">{money(position.average_price)}</td>
                        <td className="num">{money(position.market_price)}</td>
                        <td className={`num ${signOf(position.unrealized_pnl)}`}>{money(position.unrealized_pnl)}</td>
                        <td className={`num ${signOf(position.realized_pnl)}`}>{money(position.realized_pnl)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        )}

        {tab === "orders" && (
          <Card>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>#</th><th>Instrument</th><th>Side</th><th className="num">Quantity</th><th className="num">Filled</th><th className="num">Avg price</th><th>Status</th></tr>
                </thead>
                <tbody>
                  {(orders.data ?? []).map((order) => (
                    <tr key={order.engine_order_id}>
                      <td className="num">{order.sequence}</td>
                      <td className="mono">{order.instrument}</td>
                      <td><Badge tone={order.side === "buy" ? "accent" : "neutral"}>{order.side}</Badge></td>
                      <td className="num">{order.quantity}</td>
                      <td className="num">{order.filled_quantity}</td>
                      <td className="num">{money(order.average_fill_price)}</td>
                      <td><StatusBadge status={order.status} /></td>
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
                  {(fills.data ?? []).map((fill) => (
                    <tr key={fill.engine_fill_id}>
                      <td className="num">{fill.sequence}</td>
                      <td className="mono">{fill.instrument}</td>
                      <td>{fill.side}</td>
                      <td className="num">{fill.quantity}</td>
                      <td className="num">{money(fill.price)}</td>
                      <td className="num">{money(fill.commission)}</td>
                      <td className="tiny faint">{epochDateTime(fill.fill_timestamp)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}

        {tab === "events" && (
          <Card>
            <p className="small dim">
              Every refusal appears here — a risk rejection, a skipped record, an unpriced
              instrument. A session that declined orders never reports as a clean success.
            </p>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>#</th><th>Severity</th><th>Kind</th><th>Message</th><th>When</th></tr>
                </thead>
                <tbody>
                  {(events.data ?? []).map((event) => (
                    <tr key={event.sequence}>
                      <td className="num">{event.sequence}</td>
                      <td>
                        <Badge tone={event.severity === "critical" || event.severity === "error" ? "err" : event.severity === "warning" ? "warn" : "neutral"}>
                          {event.severity}
                        </Badge>
                      </td>
                      <td className="mono tiny">{titleCase(event.kind)}</td>
                      <td className="small">{event.message}</td>
                      <td className="tiny faint">{when(event.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}

        {tab === "risk" && (
          <Card>
            <p className="small dim">
              These limits are enforced inside AlphaLab, before the OMS sees an order. Nothing in
              this application — not this UI, not an API call — can route around them.
            </p>
            <pre className="code">{JSON.stringify(data.risk_config, null, 2)}</pre>
          </Card>
        )}
      </div>
    </>
  );
}
