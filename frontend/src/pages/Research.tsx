import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Banner, Card, ErrorBanner, Field, Loading } from "../components/ui";
import { ApiError, api } from "../lib/api";
import type { BacktestJob, Dataset, Implementation, Strategy } from "../lib/api";
import { count, epochDate } from "../lib/format";
import { useAsync } from "../lib/useAsync";

/** The research workspace: choose data, choose a version, configure, submit. */
export default function ResearchPage() {
  const navigate = useNavigate();
  const datasets = useAsync<Dataset[]>(() => api.get<Dataset[]>("/datasets"), []);
  const strategies = useAsync<Strategy[]>(() => api.get<Strategy[]>("/strategies"), []);
  const implementations = useAsync<Implementation[]>(() => api.get<Implementation[]>("/strategies/implementations"), []);

  const [datasetVersionId, setDatasetVersionId] = useState("");
  const [strategyVersionId, setStrategyVersionId] = useState("");
  const [universe, setUniverse] = useState<string[]>([]);
  const [cash, setCash] = useState("1000000.00");
  const [currency, setCurrency] = useState("INR");
  const [riskProfile, setRiskProfile] = useState<"research" | "conservative">("research");
  const [commissionKind, setCommissionKind] = useState<"percentage" | "per_share">("percentage");
  const [commissionRate, setCommissionRate] = useState("0.0003");
  const [seed, setSeed] = useState("");
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();

  const approved = useMemo(
    () =>
      (datasets.data ?? []).flatMap((dataset) =>
        dataset.versions
          .filter((version) => version.status === "approved")
          .map((version) => ({ dataset, version })),
      ),
    [datasets.data],
  );

  const chosenDataset = approved.find((entry) => entry.version.id === datasetVersionId);

  const published = useMemo(
    () =>
      (strategies.data ?? []).flatMap((strategy) =>
        strategy.versions
          .filter((version) => version.status === "published")
          .map((version) => ({ strategy, version })),
      ),
    [strategies.data],
  );

  const chosenStrategy = published.find((entry) => entry.version.id === strategyVersionId);
  const spec = (implementations.data ?? []).find((i) => i.key === chosenStrategy?.version.implementation_key);

  async function submit() {
    setBusy(true);
    setError(undefined);
    try {
      const parameters: Record<string, number | string> = {};
      for (const parameter of spec?.parameters ?? []) {
        const raw = overrides[parameter.name];
        if (raw !== undefined && raw !== "") {
          parameters[parameter.name] = parameter.kind === "string" ? raw : Number(raw);
        }
      }
      const job = await api.post<BacktestJob>("/backtests", {
        dataset_version_id: datasetVersionId,
        strategy_version_id: strategyVersionId,
        parameters,
        universe,
        starting_cash: cash,
        currency,
        risk_profile: riskProfile,
        commission_kind: commissionKind,
        commission_rate: commissionRate,
        seed: seed ? Number(seed) : null,
      });
      navigate(`/backtests/${job.id}`);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  if (datasets.loading || strategies.loading) return <Loading what="Loading workspace" />;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Research</h1>
          <p>
            Backtests run asynchronously on AlphaLab. Every run records the dataset version, the
            strategy version, the parameters and the seed, so a result can be re-derived rather than
            merely believed.
          </p>
        </div>
      </div>

      {error && <Banner tone="err">{error}</Banner>}
      <ErrorBanner error={datasets.error ?? strategies.error} />

      {approved.length === 0 && (
        <Banner tone="warn">
          No approved dataset. Upload a CSV and approve it before running a backtest.
        </Banner>
      )}
      {published.length === 0 && (
        <Banner tone="warn">
          No published strategy version. Create one and publish it — a draft cannot be run, because
          a draft can still change.
        </Banner>
      )}

      <div className="grid cols-2">
        <Card title="Data">
          <Field label="Dataset version">
            <select value={datasetVersionId} onChange={(event) => { setDatasetVersionId(event.target.value); setUniverse([]); }}>
              <option value="">Choose…</option>
              {approved.map(({ dataset, version }) => (
                <option key={version.id} value={version.id}>
                  {dataset.name} · v{version.version} · {count(version.row_count)} rows
                </option>
              ))}
            </select>
          </Field>

          {chosenDataset && (
            <>
              <div className="small dim" style={{ marginBottom: "0.6rem" }}>
                {epochDate(chosenDataset.version.start_timestamp)} → {epochDate(chosenDataset.version.end_timestamp)} ·{" "}
                {chosenDataset.version.inferred_frequency ?? "irregular"} ·{" "}
                {chosenDataset.version.symbol_count} symbol(s) · quality{" "}
                {chosenDataset.version.quality_score?.toFixed(1)}
              </div>
              <Field label="Universe" hint="Leave empty to use every symbol in the dataset.">
                <input
                  value={universe.join(", ")}
                  onChange={(event) =>
                    setUniverse(event.target.value.split(",").map((s) => s.trim()).filter(Boolean))
                  }
                  placeholder="all symbols"
                />
              </Field>
            </>
          )}
        </Card>

        <Card title="Strategy">
          <Field label="Strategy version">
            <select
              value={strategyVersionId}
              onChange={(event) => {
                setStrategyVersionId(event.target.value);
                setOverrides({});
              }}
            >
              <option value="">Choose…</option>
              {published.map(({ strategy, version }) => (
                <option key={version.id} value={version.id}>
                  {strategy.name} · v{version.version} · {version.implementation_key}
                </option>
              ))}
            </select>
          </Field>

          {chosenStrategy && spec && (
            <>
              <p className="tiny dim">{spec.description}</p>
              <div className="grid cols-3">
                {spec.parameters.map((parameter) => (
                  <Field key={parameter.name} label={parameter.name} hint={`version default: ${String(chosenStrategy.version.default_parameters[parameter.name] ?? parameter.default)}`}>
                    <input
                      type={parameter.kind === "string" ? "text" : "number"}
                      step="any"
                      value={overrides[parameter.name] ?? ""}
                      placeholder={String(chosenStrategy.version.default_parameters[parameter.name] ?? parameter.default)}
                      onChange={(event) => setOverrides({ ...overrides, [parameter.name]: event.target.value })}
                    />
                  </Field>
                ))}
              </div>
            </>
          )}
        </Card>
      </div>

      <Card title="Execution assumptions">
        <div className="grid cols-3">
          <Field label="Starting capital">
            <input value={cash} onChange={(event) => setCash(event.target.value)} />
          </Field>
          <Field label="Currency">
            <input value={currency} onChange={(event) => setCurrency(event.target.value)} />
          </Field>
          <Field label="Risk profile" hint="Limits the engine enforces before the OMS sees an order.">
            <select value={riskProfile} onChange={(event) => setRiskProfile(event.target.value as "research" | "conservative")}>
              <option value="research">Research (wider limits, shorting allowed)</option>
              <option value="conservative">Conservative (no shorting, tight limits)</option>
            </select>
          </Field>
          <Field label="Commission model">
            <select value={commissionKind} onChange={(event) => setCommissionKind(event.target.value as "percentage" | "per_share")}>
              <option value="percentage">Percentage of notional</option>
              <option value="per_share">Per share</option>
            </select>
          </Field>
          <Field label="Commission rate">
            <input value={commissionRate} onChange={(event) => setCommissionRate(event.target.value)} />
          </Field>
          <Field label="Seed" hint="Leave empty to derive one from the request, so re-running reproduces it.">
            <input value={seed} onChange={(event) => setSeed(event.target.value)} placeholder="derived" />
          </Field>
        </div>

        <div className="row end" style={{ marginTop: "0.5rem" }}>
          <button
            className="primary"
            disabled={busy || !datasetVersionId || !strategyVersionId}
            onClick={() => void submit()}
            type="button"
          >
            {busy ? "Submitting…" : "Run backtest"}
          </button>
        </div>
      </Card>
    </>
  );
}
