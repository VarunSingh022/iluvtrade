import { useRef, useState } from "react";
import { Link } from "react-router-dom";

import { Banner, Card, ErrorBanner, Field, Loading, ScoreMeter, StatusBadge } from "../components/ui";
import { ApiError, api } from "../lib/api";
import type { Dataset, DatasetVersionDetail, DetectedSchema } from "../lib/api";
import { count, epochDate, when } from "../lib/format";
import { useAsync } from "../lib/useAsync";

/**
 * The data workspace.
 *
 * The inspect-before-import step is the point of this screen: a user sees what
 * the importer detected, and what it could not, *before* anything is stored.
 */
export default function DatasetsPage() {
  const datasets = useAsync<Dataset[]>(() => api.get<Dataset[]>("/datasets"), []);
  const [detected, setDetected] = useState<DetectedSchema | undefined>();
  const [pending, setPending] = useState<File | undefined>();
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const [notice, setNotice] = useState<string | undefined>();
  const fileInput = useRef<HTMLInputElement>(null);

  async function inspect(file: File) {
    setBusy(true);
    setError(undefined);
    setNotice(undefined);
    try {
      const form = new FormData();
      form.append("file", file);
      setDetected(await api.postForm<DetectedSchema>("/datasets/inspect", form));
      setPending(file);
      if (!name) setName(file.name.replace(/\.[^.]+$/, ""));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  async function upload() {
    if (!pending) return;
    setBusy(true);
    setError(undefined);
    try {
      const form = new FormData();
      form.append("file", pending);
      if (name) form.append("dataset_name", name);
      const version = await api.postForm<DatasetVersionDetail>("/datasets/upload", form);
      setNotice(
        `Imported ${count(version.row_count)} rows as version ${version.version}. ` +
          `It is awaiting your approval — nothing can use it until you approve it.`,
      );
      setDetected(undefined);
      setPending(undefined);
      setName("");
      if (fileInput.current) fileInput.current.value = "";
      datasets.reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  async function fetchUrl() {
    setBusy(true);
    setError(undefined);
    try {
      const version = await api.post<DatasetVersionDetail>("/datasets/fetch", {
        url,
        dataset_name: name || null,
      });
      setNotice(`Fetched ${count(version.row_count)} rows as version ${version.version}, awaiting approval.`);
      setUrl("");
      datasets.reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Datasets</h1>
          <p>
            Upload a CSV and the importer will tell you what it found, what it changed and what it
            refused — before you approve it for research.
          </p>
        </div>
      </div>

      {error && <Banner tone="err">{error}</Banner>}
      {notice && <Banner tone="ok">{notice}</Banner>}
      <ErrorBanner error={datasets.error} />

      <div className="grid cols-2">
        <Card title="Upload a CSV">
          <Field label="File">
            <input
              ref={fileInput}
              type="file"
              accept=".csv,text/csv,text/plain"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void inspect(file);
              }}
            />
          </Field>

          {detected && (
            <>
              <div className="grid cols-2" style={{ marginBottom: "0.6rem" }}>
                <div>
                  <div className="stat-label">Columns</div>
                  <div className="small mono">{detected.columns.join(", ")}</div>
                </div>
                <div>
                  <div className="stat-label">Timestamp format</div>
                  <div className="small mono">{detected.timestamp_format}</div>
                </div>
              </div>

              <div className="table-wrap" style={{ marginBottom: "0.6rem" }}>
                <table>
                  <thead>
                    <tr>
                      <th>Field</th>
                      <th>Detected column</th>
                      <th>How</th>
                      <th className="num">Confidence</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.values(detected.fields).map((field) => (
                      <tr key={field.role}>
                        <td>{field.role}</td>
                        <td className="mono">{field.column ?? <span className="faint">not found</span>}</td>
                        <td className="tiny faint">{field.method}</td>
                        <td className="num">{field.column ? field.confidence.toFixed(2) : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {detected.warnings.map((warning) => (
                <Banner tone="warn" key={warning}>{warning}</Banner>
              ))}
              {detected.missing_required.length > 0 && (
                <Banner tone="err">
                  Missing required field(s): {detected.missing_required.join(", ")}. No bars can be
                  built from this file.
                </Banner>
              )}

              <Field label="Dataset name">
                <input value={name} onChange={(event) => setName(event.target.value)} />
              </Field>
              <button
                className="primary"
                onClick={() => void upload()}
                disabled={busy || detected.missing_required.length > 0}
                type="button"
              >
                {busy ? "Importing…" : "Import and run quality checks"}
              </button>
            </>
          )}
        </Card>

        <Card title="Fetch from a URL">
          <p className="small dim">
            Only hosts an administrator has allowlisted can be fetched, and every redirect is
            re-checked against the private address ranges. The fetched file goes through exactly the
            same cleaning pipeline as an upload.
          </p>
          <Field label="CSV URL">
            <input value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://…" />
          </Field>
          <button onClick={() => void fetchUrl()} disabled={busy || !url} type="button">
            {busy ? "Fetching…" : "Fetch"}
          </button>
        </Card>
      </div>

      <h2 style={{ marginTop: "1.5rem" }}>Your datasets</h2>
      {datasets.loading ? (
        <Loading />
      ) : (datasets.data ?? []).length === 0 ? (
        <Card><p className="dim" style={{ margin: 0 }}>No datasets yet.</p></Card>
      ) : (
        (datasets.data ?? []).map((dataset) => (
          <Card key={dataset.id} title={dataset.name}>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Version</th>
                    <th>Status</th>
                    <th className="num">Rows</th>
                    <th className="num">Rejected</th>
                    <th className="num">Symbols</th>
                    <th>Range</th>
                    <th>Frequency</th>
                    <th>Quality</th>
                    <th>Created</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {dataset.versions.map((version) => (
                    <tr key={version.id}>
                      <td className="mono">v{version.version}</td>
                      <td><StatusBadge status={version.status} /></td>
                      <td className="num">{count(version.row_count)}</td>
                      <td className="num">{version.rejected_row_count || "—"}</td>
                      <td className="num">{version.symbol_count}</td>
                      <td className="tiny">
                        {epochDate(version.start_timestamp)} → {epochDate(version.end_timestamp)}
                      </td>
                      <td className="tiny mono">{version.inferred_frequency ?? "irregular"}</td>
                      <td><ScoreMeter score={version.quality_score} /></td>
                      <td className="tiny faint">{when(version.created_at)}</td>
                      <td><Link className="tiny" to={`/datasets/${version.id}`}>Review</Link></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        ))
      )}
    </>
  );
}
