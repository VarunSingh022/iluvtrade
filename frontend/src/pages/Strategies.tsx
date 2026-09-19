import { useState } from "react";

import { Badge, Banner, Card, ErrorBanner, Field, Loading, StatusBadge } from "../components/ui";
import { ApiError, api } from "../lib/api";
import type { Implementation, Strategy, StrategyVersion } from "../lib/api";
import { shortId, when } from "../lib/format";
import { useAsync } from "../lib/useAsync";

export default function StrategiesPage() {
  const strategies = useAsync<Strategy[]>(() => api.get<Strategy[]>("/strategies"), []);
  const implementations = useAsync<Implementation[]>(() => api.get<Implementation[]>("/strategies/implementations"), []);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const [draftFor, setDraftFor] = useState<string | undefined>();
  const [implKey, setImplKey] = useState("");
  const [params, setParams] = useState<Record<string, string>>({});

  const selected = (implementations.data ?? []).find((i) => i.key === implKey);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(undefined);
    try {
      await action();
      strategies.reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  function chooseImplementation(key: string) {
    setImplKey(key);
    const impl = (implementations.data ?? []).find((i) => i.key === key);
    const next: Record<string, string> = {};
    for (const spec of impl?.parameters ?? []) next[spec.name] = String(spec.default);
    setParams(next);
  }

  function typedParams(): Record<string, number | string> {
    const out: Record<string, number | string> = {};
    for (const spec of selected?.parameters ?? []) {
      const raw = params[spec.name] ?? String(spec.default);
      out[spec.name] = spec.kind === "string" ? raw : Number(raw);
    }
    return out;
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Strategies</h1>
          <p>
            A strategy is an identity; a version is what actually runs. Publishing a version freezes
            it — a backtest or a live session records which exact version it used, and that version
            can never change afterwards.
          </p>
        </div>
      </div>

      {error && <Banner tone="err">{error}</Banner>}
      <ErrorBanner error={strategies.error} />

      <div className="grid cols-2">
        <Card title="New strategy">
          <Field label="Name">
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Trend Rider" />
          </Field>
          <button
            className="primary"
            disabled={busy || !name}
            type="button"
            onClick={() =>
              void run(async () => {
                await api.post("/strategies", { name, description: "" });
                setName("");
              })
            }
          >
            Create
          </button>
        </Card>

        <Card title="Available implementations">
          <p className="small dim">
            A version names one of these. Uploading arbitrary strategy code is deliberately not a
            feature — running untrusted Python needs an isolation boundary this deployment does not
            have.
          </p>
          {(implementations.data ?? []).map((impl) => (
            <div key={impl.key} style={{ marginBottom: "0.6rem" }}>
              <div className="small" style={{ fontWeight: 600 }}>{impl.name} <span className="mono tiny faint">{impl.key}</span></div>
              <div className="tiny dim">{impl.description}</div>
            </div>
          ))}
        </Card>
      </div>

      <h2 style={{ marginTop: "1.5rem" }}>Your strategies</h2>
      {strategies.loading ? (
        <Loading />
      ) : (strategies.data ?? []).length === 0 ? (
        <Card><p className="dim" style={{ margin: 0 }}>No strategies yet.</p></Card>
      ) : (
        (strategies.data ?? []).map((strategy) => (
          <Card
            key={strategy.id}
            title={strategy.name}
            actions={
              <button
                className="small"
                type="button"
                onClick={() => {
                  setDraftFor(draftFor === strategy.id ? undefined : strategy.id);
                  if (implementations.data?.[0]) chooseImplementation(implementations.data[0].key);
                }}
              >
                {draftFor === strategy.id ? "Cancel" : "New version"}
              </button>
            }
          >
            {draftFor === strategy.id && selected && (
              <div className="card" style={{ background: "var(--surface-2)", marginBottom: "0.75rem" }}>
                <Field label="Implementation">
                  <select value={implKey} onChange={(event) => chooseImplementation(event.target.value)}>
                    {(implementations.data ?? []).map((impl) => (
                      <option key={impl.key} value={impl.key}>{impl.name}</option>
                    ))}
                  </select>
                </Field>
                <div className="grid cols-3">
                  {selected.parameters.map((spec) => (
                    <Field key={spec.name} label={spec.name} hint={spec.description}>
                      <input
                        value={params[spec.name] ?? ""}
                        onChange={(event) => setParams({ ...params, [spec.name]: event.target.value })}
                        type={spec.kind === "string" ? "text" : "number"}
                        {...(spec.minimum !== null ? { min: spec.minimum } : {})}
                        {...(spec.maximum !== null ? { max: spec.maximum } : {})}
                        step="any"
                      />
                    </Field>
                  ))}
                </div>
                <button
                  className="primary"
                  disabled={busy}
                  type="button"
                  onClick={() =>
                    void run(async () => {
                      await api.post(`/strategies/${strategy.id}/versions`, {
                        implementation_key: implKey,
                        parameters: typedParams(),
                        changelog: "",
                      });
                      setDraftFor(undefined);
                    })
                  }
                >
                  Create draft version
                </button>
              </div>
            )}

            {strategy.versions.length === 0 ? (
              <p className="dim small" style={{ margin: 0 }}>No versions yet.</p>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Version</th>
                      <th>Status</th>
                      <th>Implementation</th>
                      <th>Parameters</th>
                      <th>Content hash</th>
                      <th>Published</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {strategy.versions.map((version: StrategyVersion) => (
                      <tr key={version.id}>
                        <td className="mono">v{version.version}</td>
                        <td>
                          <StatusBadge status={version.status} />
                          {version.frozen && <> <Badge tone="neutral">frozen</Badge></>}
                          {!version.integrity_ok && <> <Badge tone="err">hash mismatch</Badge></>}
                        </td>
                        <td className="mono tiny">{version.implementation_key}</td>
                        <td className="mono tiny faint">
                          {Object.entries(version.default_parameters).map(([k, v]) => `${k}=${String(v)}`).join(" ")}
                        </td>
                        <td className="mono tiny faint">{version.content_hash ? `${shortId(version.content_hash)}…` : "—"}</td>
                        <td className="tiny faint">{when(version.published_at)}</td>
                        <td>
                          {version.status === "draft" && (
                            <button
                              className="small"
                              disabled={busy}
                              type="button"
                              onClick={() => void run(() => api.post(`/strategies/versions/${version.id}/publish`))}
                            >
                              Publish
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        ))
      )}
    </>
  );
}
