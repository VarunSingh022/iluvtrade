import { Link } from "react-router-dom";

import { Banner, Card, ErrorBanner, Loading, ModeBadge, Stat, StatusBadge } from "../components/ui";
import { api } from "../lib/api";
import type { Portfolio, SessionPosition } from "../lib/api";
import { money, signOf } from "../lib/format";
import { useAsync } from "../lib/useAsync";

interface TaggedPosition extends SessionPosition {
  session_id: string;
  session_name: string;
  mode: string;
}

export default function PortfolioPage() {
  const portfolio = useAsync<Portfolio>(() => api.get<Portfolio>("/portfolio"), []);
  const positions = useAsync<TaggedPosition[]>(() => api.get<TaggedPosition[]>("/positions"), []);

  if (portfolio.loading) return <Loading what="Loading portfolio" />;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Portfolio</h1>
          <p>{portfolio.data?.note}</p>
        </div>
      </div>

      <ErrorBanner error={portfolio.error} />

      <Banner tone="info">
        Paper and live are shown separately and never summed. One is simulated money.
      </Banner>

      {(["paper", "live"] as const).map((mode) => {
        const bucket = portfolio.data?.modes[mode];
        return (
          <Card
            key={mode}
            title={<span><ModeBadge mode={mode} /> portfolio</span>}
          >
            {!bucket || bucket.sessions.length === 0 ? (
              <p className="dim small" style={{ margin: 0 }}>No {mode} sessions.</p>
            ) : (
              <>
                <div className="grid cols-4" style={{ marginBottom: "0.75rem" }}>
                  <Stat label="Equity" value={money(bucket.totals.equity)} mono />
                  <Stat label="Realized P&L" value={<span className={signOf(bucket.totals.realized_pnl)}>{money(bucket.totals.realized_pnl)}</span>} mono />
                  <Stat label="Unrealized P&L" value={<span className={signOf(bucket.totals.unrealized_pnl)}>{money(bucket.totals.unrealized_pnl)}</span>} mono />
                  <Stat label="Commission" value={money(bucket.totals.commission_paid)} mono />
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr><th>Session</th><th>Status</th><th className="num">Started with</th><th className="num">Cash</th><th className="num">Equity</th><th className="num">Realized</th><th className="num">Records</th></tr>
                    </thead>
                    <tbody>
                      {bucket.sessions.map((row) => (
                        <tr key={row.session_id}>
                          <td><Link to={`/sessions/${row.session_id}`}>{row.name}</Link></td>
                          <td><StatusBadge status={row.status} /></td>
                          <td className="num">{money(row.starting_cash)}</td>
                          <td className="num">{money(row.cash)}</td>
                          <td className="num">{money(row.equity)}</td>
                          <td className={`num ${signOf(row.realized_pnl)}`}>{money(row.realized_pnl)}</td>
                          <td className="num">{row.records_processed}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </Card>
        );
      })}

      <Card title="Open positions">
        {(positions.data ?? []).length === 0 ? (
          <p className="dim small" style={{ margin: 0 }}>No open positions.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>Instrument</th><th>Session</th><th>Mode</th><th className="num">Quantity</th><th className="num">Average cost</th><th className="num">Mark</th><th className="num">Unrealized</th></tr>
              </thead>
              <tbody>
                {(positions.data ?? []).map((position) => (
                  <tr key={`${position.session_id}-${position.instrument}`}>
                    <td className="mono">{position.instrument}</td>
                    <td><Link to={`/sessions/${position.session_id}`}>{position.session_name}</Link></td>
                    <td><ModeBadge mode={position.mode} /></td>
                    <td className="num">{position.quantity}</td>
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
    </>
  );
}
