import { Link } from "react-router-dom";

import { Card, ErrorBanner, Loading, ModeBadge, Stat, StatusBadge } from "../components/ui";
import { api, health } from "../lib/api";
import type { Dashboard, HealthResponse, Notification, Portfolio, TradingSession } from "../lib/api";
import { useAuth } from "../lib/auth";
import { money, relative, signOf } from "../lib/format";
import { useAsync } from "../lib/useAsync";

export default function DashboardPage() {
  const { user } = useAuth();
  const stats = useAsync<Dashboard>(() => api.get<Dashboard>("/dashboard"), []);
  const status = useAsync<HealthResponse>(() => health(), []);
  const sessions = useAsync<TradingSession[]>(() => api.get<TradingSession[]>("/trading/sessions"), []);
  const portfolio = useAsync<Portfolio>(() => api.get<Portfolio>("/portfolio"), []);
  const notifications = useAsync<Notification[]>(() => api.get<Notification[]>("/notifications?limit=6"), []);

  if (stats.loading) return <Loading what="Loading workspace" />;

  const paper = portfolio.data?.modes.paper;
  const live = portfolio.data?.modes.live;
  const active = (sessions.data ?? []).filter((s) => !["stopped", "failed", "halted"].includes(s.status));

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Welcome back, {user?.display_name.split(" ")[0]}</h1>
          <p>
            {user?.organization_name} · running on AlphaLab{" "}
            {status.data ? status.data.engine.version : "…"}, which computes every figure below.
          </p>
        </div>
        <Link className="btn primary" to="/research">
          New backtest
        </Link>
      </div>

      <ErrorBanner error={stats.error} />

      <div className="grid cols-4">
        <Stat
          label="Approved datasets"
          value={stats.data?.datasets.approved ?? 0}
          sub={stats.data?.datasets.pending ? <Link to="/datasets">{stats.data.datasets.pending} awaiting review</Link> : "none pending"}
        />
        <Stat label="Strategies" value={stats.data?.strategies ?? 0} sub={<Link to="/strategies">manage</Link>} />
        <Stat
          label="Backtests"
          value={stats.data?.backtests.completed ?? 0}
          sub={`${stats.data?.backtests.queued ?? 0} queued · ${stats.data?.backtests.failed ?? 0} failed`}
        />
        <Stat
          label="Sessions running"
          value={stats.data?.sessions.running ?? 0}
          sub={stats.data?.sessions.halted ? `${stats.data.sessions.halted} halted` : `${stats.data?.sessions.total ?? 0} total`}
        />
      </div>

      <div className="grid cols-2" style={{ marginTop: "0.75rem" }}>
        <Card title="Paper portfolio">
          {paper && paper.sessions.length > 0 ? (
            <div className="grid cols-2">
              <Stat label="Equity" value={money(paper.totals.equity)} mono />
              <Stat
                label="Realized P&L"
                value={<span className={signOf(paper.totals.realized_pnl)}>{money(paper.totals.realized_pnl)}</span>}
                mono
              />
            </div>
          ) : (
            <p className="dim small" style={{ margin: 0 }}>
              No paper sessions yet. <Link to="/sessions">Deploy a strategy</Link> to start one.
            </p>
          )}
        </Card>

        <Card title="Live portfolio">
          {live && live.sessions.length > 0 ? (
            <div className="grid cols-2">
              <Stat label="Equity" value={money(live.totals.equity)} mono />
              <Stat
                label="Realized P&L"
                value={<span className={signOf(live.totals.realized_pnl)}>{money(live.totals.realized_pnl)}</span>}
                mono
              />
            </div>
          ) : (
            <p className="dim small" style={{ margin: 0 }}>
              {status.data?.live_trading_enabled
                ? "No live sessions."
                : "Live trading is disabled in this deployment."}
            </p>
          )}
        </Card>
      </div>

      <div className="grid cols-2" style={{ marginTop: "0.75rem" }}>
        <Card title="Active sessions" actions={<Link className="tiny" to="/sessions">All sessions</Link>}>
          {active.length === 0 ? (
            <p className="dim small" style={{ margin: 0 }}>Nothing running.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Session</th>
                    <th>Mode</th>
                    <th>Status</th>
                    <th className="num">Equity</th>
                  </tr>
                </thead>
                <tbody>
                  {active.slice(0, 6).map((session) => (
                    <tr key={session.id}>
                      <td>
                        <Link to={`/sessions/${session.id}`}>{session.name}</Link>
                      </td>
                      <td><ModeBadge mode={session.mode} /></td>
                      <td><StatusBadge status={session.status} /></td>
                      <td className="num">{money(session.equity)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <Card title="Recent activity">
          {(notifications.data ?? []).length === 0 ? (
            <p className="dim small" style={{ margin: 0 }}>Nothing yet.</p>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
              {(notifications.data ?? []).map((note) => (
                <div key={note.id} style={{ display: "flex", gap: "0.5rem", alignItems: "flex-start" }}>
                  <span className={`badge ${note.severity === "error" || note.severity === "critical" ? "err" : note.severity === "warning" ? "warn" : "neutral"}`}>
                    {note.severity}
                  </span>
                  <div style={{ minWidth: 0 }}>
                    <div className="small" style={{ fontWeight: 500 }}>{note.title}</div>
                    <div className="tiny faint">{note.body}</div>
                    <div className="tiny faint">{relative(note.created_at)}</div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </>
  );
}
