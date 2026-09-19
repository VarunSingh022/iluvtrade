import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { Banner, Card, ErrorBanner, Field, Loading, ModeBadge, StatusBadge } from "../components/ui";
import { ApiError, api, health } from "../lib/api";
import type { Dataset, Entitlement, HealthResponse, Strategy, TradingSession } from "../lib/api";
import { money, relative, signOf } from "../lib/format";
import { useAsync, usePolling } from "../lib/useAsync";

export default function SessionsPage() {
  const sessions = useAsync<TradingSession[]>(() => api.get<TradingSession[]>("/trading/sessions"), []);
  const datasets = useAsync<Dataset[]>(() => api.get<Dataset[]>("/datasets"), []);
  const strategies = useAsync<Strategy[]>(() => api.get<Strategy[]>("/strategies"), []);
  const entitlements = useAsync<Entitlement[]>(() => api.get<Entitlement[]>("/reddesk/entitlements"), []);
  const status = useAsync<HealthResponse>(() => health(), []);

  const busy = (sessions.data ?? []).some((s) => ["running", "starting", "stopping"].includes(s.status));
  usePolling(sessions.reload, busy, 2000);

  const [name, setName] = useState("");
  const [strategyVersionId, setStrategyVersionId] = useState("");
  const [datasetVersionId, setDatasetVersionId] = useState("");
  const [cash, setCash] = useState("1000000.00");
  const [riskProfile, setRiskProfile] = useState<"research" | "conservative">("conservative");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | undefined>();

  const approved = useMemo(
    () =>
      (datasets.data ?? []).flatMap((dataset) =>
        dataset.versions.filter((v) => v.status === "approved").map((version) => ({ dataset, version })),
      ),
    [datasets.data],
  );

  /** Everything runnable: versions this workspace owns, plus entitled ones. */
  const runnable = useMemo(() => {
    const owned = (strategies.data ?? []).flatMap((strategy) =>
      strategy.versions
        .filter((version) => version.status === "published")
        .map((version) => ({ id: version.id, label: `${strategy.name} · v${version.version}`, basis: "owned" })),
    );
    const licensed = (entitlements.data ?? []).map((entitlement) => ({
      id: entitlement.granted_strategy_version_id,
      label: `Licensed version ${entitlement.granted_strategy_version_id.slice(0, 8)} (${entitlement.version_access_policy})`,
      basis: "entitlement",
    }));
    const seen = new Set<string>();
    return [...owned, ...licensed].filter((entry) => !seen.has(entry.id) && seen.add(entry.id));
  }, [strategies.data, entitlements.data]);

  async function create(start: boolean) {
    setWorking(true);
    setError(undefined);
    try {
      const session = await api.post<TradingSession>("/trading/sessions", {
        name,
        mode: "paper",
        strategy_version_id: strategyVersionId,
        dataset_version_id: datasetVersionId,
        starting_cash: cash,
        risk_profile: riskProfile,
      });
      if (start) await api.post(`/trading/sessions/${session.id}/start`);
      setName("");
      sessions.reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setWorking(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Trading sessions</h1>
          <p>
            A session deploys one exact strategy version. Paper sessions replay an approved dataset
            through the same AlphaLab path a backtest uses — same orders, same accounting.
          </p>
        </div>
      </div>

      {error && <Banner tone="err">{error}</Banner>}
      <ErrorBanner error={sessions.error} />

      {status.data && !status.data.live_trading_enabled && (
        <Banner tone="info">
          Live trading is disabled in this deployment. Enabling it requires a configuration change,
          a per-user setting and a per-session confirmation — three separate gates.
        </Banner>
      )}

      <Card title="Deploy to paper">
        <div className="grid cols-3">
          <Field label="Session name">
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Trend Rider — paper" />
          </Field>
          <Field label="Strategy version">
            <select value={strategyVersionId} onChange={(event) => setStrategyVersionId(event.target.value)}>
              <option value="">Choose…</option>
              {runnable.map((entry) => (
                <option key={entry.id} value={entry.id}>{entry.label}</option>
              ))}
            </select>
          </Field>
          <Field label="Dataset to replay">
            <select value={datasetVersionId} onChange={(event) => setDatasetVersionId(event.target.value)}>
              <option value="">Choose…</option>
              {approved.map(({ dataset, version }) => (
                <option key={version.id} value={version.id}>{dataset.name} · v{version.version}</option>
              ))}
            </select>
          </Field>
          <Field label="Starting capital">
            <input value={cash} onChange={(event) => setCash(event.target.value)} />
          </Field>
          <Field label="Risk profile">
            <select value={riskProfile} onChange={(event) => setRiskProfile(event.target.value as "research" | "conservative")}>
              <option value="conservative">Conservative</option>
              <option value="research">Research</option>
            </select>
          </Field>
        </div>
        <div className="row end">
          <button disabled={working || !name || !strategyVersionId || !datasetVersionId} onClick={() => void create(false)} type="button">
            Create only
          </button>
          <button className="primary" disabled={working || !name || !strategyVersionId || !datasetVersionId} onClick={() => void create(true)} type="button">
            Create and start
          </button>
        </div>
      </Card>

      <h2 style={{ marginTop: "1.5rem" }}>Sessions</h2>
      {sessions.loading ? (
        <Loading />
      ) : (sessions.data ?? []).length === 0 ? (
        <Card><p className="dim" style={{ margin: 0 }}>No sessions yet.</p></Card>
      ) : (
        <Card>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Session</th><th>Mode</th><th>Status</th><th className="num">Records</th>
                  <th className="num">Equity</th><th className="num">Realized</th><th>Updated</th><th />
                </tr>
              </thead>
              <tbody>
                {(sessions.data ?? []).map((session) => (
                  <tr key={session.id}>
                    <td><Link to={`/sessions/${session.id}`}>{session.name}</Link></td>
                    <td><ModeBadge mode={session.mode} /></td>
                    <td>
                      <StatusBadge status={session.status} />
                      {session.status === "running" && !session.is_live_state && (
                        <> <span className="badge warn">stale</span></>
                      )}
                    </td>
                    <td className="num">{session.records_processed}</td>
                    <td className="num">{money(session.equity)}</td>
                    <td className={`num ${signOf(session.realized_pnl)}`}>{money(session.realized_pnl)}</td>
                    <td className="tiny faint">{relative(session.last_advanced_at ?? session.started_at)}</td>
                    <td><Link className="tiny" to={`/sessions/${session.id}`}>Open</Link></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  );
}
