import { useRef, useState } from "react";
import { Link } from "react-router-dom";

import { BarInterval } from "../components/BarInterval";
import { IntervalCheckPanel, sha256Hex } from "../components/IntervalCheckPanel";
import { Banner, Card, ErrorBanner, Field, Loading, ScoreMeter, StatusBadge } from "../components/ui";
import { ApiError, api } from "../lib/api";
import type { Dataset, DatasetVersionDetail, DetectedSchema, IntervalCheck } from "../lib/api";
import { BAR_INTERVALS, KNOWN_EXCHANGE_ZONES, TIMESTAMP_CONVENTIONS } from "../lib/assumptions";
import type { TimestampConvention } from "../lib/assumptions";
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
  // The selected file, with the selection it belongs to. Every selection gets a
  // new number; a check, a confirmation or an inspect response made for an
  // earlier one is never applied to a later one -- even the same file chosen
  // again.
  const [pending, setPending] = useState<{ file: File; selection: number } | undefined>();
  const selection = useRef(0);
  const checkRequest = useRef(0);
  const fetchRequest = useRef(0);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  // What the file's timestamps mean. Never preselected: the importer refuses a
  // file without it, because a wrong guess shifts every bar silently.
  const [convention, setConvention] = useState<TimestampConvention | "">("");
  const [timezone, setTimezone] = useState("");
  // The interval each bar covers. Declared, never inferred; implied 1d for a
  // session date. The importer checks it against the data and refuses a misfit.
  const [barInterval, setBarInterval] = useState("");
  // The declared interval checked against the selected file's bars (nothing
  // stored), with the selection and settings it was checked for. Its
  // preview_id binds it to the exact bytes on the server too.
  const [check, setCheck] = useState<{ selection: number; key: string; result: IntervalCheck } | undefined>();
  // The ambiguous instruments the user has declared, one by one, to have
  // missing observations at the declared interval. Never preselected: the
  // timestamps cannot tell a sparse 15m series from an hourly one.
  const [confirmedFor, setConfirmedFor] = useState<string[]>([]);
  // The same, for a URL: checked by fetching it now; the server re-fetches on
  // import and refuses the confirmation if the resource changed.
  const [fetchCheck, setFetchCheck] = useState<{ key: string; result: IntervalCheck } | undefined>();
  const [fetchConfirmedFor, setFetchConfirmedFor] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const [notice, setNotice] = useState<string | undefined>();
  const fileInput = useRef<HTMLInputElement>(null);
  // No cleaning-policy controls exist on this screen, so the default policy is
  // what both the check and the import use; it is part of the binding anyway.
  const policyKey = "default";

  function select(file: File | undefined) {
    // Everything decided about the previous selection goes, now.
    selection.current += 1;
    setCheck(undefined);
    setConfirmedFor([]);
    setDetected(undefined);
    setPending(undefined);
    setConvention("");
    if (file) void inspect(file, selection.current);
  }

  async function inspect(file: File, mine: number) {
    setBusy(true);
    setError(undefined);
    setNotice(undefined);
    try {
      const form = new FormData();
      form.append("file", file);
      const result = await api.postForm<DetectedSchema>("/datasets/inspect", form);
      if (mine !== selection.current) return; // a later file was chosen meanwhile
      setDetected(result);
      setPending({ file, selection: mine });
      if (!name) setName(file.name.replace(/\.[^.]+$/, ""));
    } catch (caught) {
      if (mine === selection.current) setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  async function upload() {
    const target = pending;
    const bound = currentCheck;
    if (!target || !bound) return;
    setBusy(true);
    setError(undefined);
    try {
      // Recompute the selected file's digest now rather than trust earlier
      // state; the server recomputes it again from the bytes it receives.
      const digest = await sha256Hex(target.file);
      if (target.selection !== selection.current) return;
      if (digest !== null && digest !== bound.content_sha256) {
        setCheck(undefined);
        setConfirmedFor([]);
        setError("The selected file is not the one that was checked. Check the declared interval again.");
        return;
      }
      const form = new FormData();
      form.append("file", target.file);
      if (name) form.append("dataset_name", name);
      if (convention) form.append("timestamp_convention", convention);
      if (needsZone && timezone) form.append("timezone", timezone);
      if (convention !== "session_date" && barInterval) form.append("bar_interval", barInterval);
      form.append("preview_id", bound.preview_id);
      for (const symbol of bound.ambiguous) {
        if (confirmedFor.includes(symbol)) form.append("missing_observations_confirmed_for", symbol);
      }
      const version = await api.postForm<DatasetVersionDetail>("/datasets/upload", form);
      setNotice(
        `Imported ${count(version.row_count)} rows as version ${version.version}. ` +
          `It is awaiting your approval — nothing can use it until you approve it.`,
      );
      if (fileInput.current) fileInput.current.value = "";
      select(undefined);
      setName("");
      datasets.reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  async function fetchUrl() {
    const bound = fetchCurrentCheck;
    if (!bound) return;
    setBusy(true);
    setError(undefined);
    try {
      const version = await api.post<DatasetVersionDetail>("/datasets/fetch", {
        url,
        dataset_name: name || null,
        timestamp_convention: convention || null,
        timezone: timezone || null,
        bar_interval: convention === "session_date" ? null : barInterval || null,
        missing_observations_confirmed_for: bound.ambiguous.filter((symbol) => fetchConfirmedFor.includes(symbol)),
        preview_id: bound.preview_id,
      });
      setNotice(`Fetched ${count(version.row_count)} rows as version ${version.version}, awaiting approval.`);
      setUrl("");
      setFetchCheck(undefined);
      setFetchConfirmedFor([]);
      datasets.reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  const semantics = detected?.timestamp_semantics;
  const needsZone = semantics?.requires_timezone ?? false;
  const declared =
    convention !== "" &&
    (semantics?.conventions.includes(convention) ?? true) &&
    (!needsZone || timezone.trim() !== "") &&
    (convention === "session_date" || barInterval !== "");
  const interval = convention === "session_date" ? "1d" : barInterval;
  const declarationKey = `${convention}|${needsZone ? timezone.trim() : ""}|${interval}|${policyKey}`;
  // A check counts only for the file selected now and the settings shown now.
  const currentCheck =
    check &&
    pending &&
    check.selection === pending.selection &&
    check.selection === selection.current &&
    check.key === declarationKey
      ? check.result
      : undefined;
  // Importable only once the declared interval has been checked against this
  // file and every instrument it cannot show the interval for has been declared
  // one by one.
  const ready =
    declared &&
    currentCheck !== undefined &&
    currentCheck.refused === null &&
    currentCheck.ambiguous.every((symbol) => confirmedFor.includes(symbol));

  const fetchDeclared = url.trim() !== "" && convention !== "" && (convention === "session_date" || barInterval !== "");
  const fetchKey = `${url.trim()}|${convention}|${timezone.trim()}|${interval}|${policyKey}`;
  const fetchCurrentCheck = fetchCheck && fetchCheck.key === fetchKey ? fetchCheck.result : undefined;
  const fetchReady =
    fetchDeclared &&
    fetchCurrentCheck !== undefined &&
    fetchCurrentCheck.refused === null &&
    fetchCurrentCheck.ambiguous.every((symbol) => fetchConfirmedFor.includes(symbol));

  async function checkInterval() {
    if (!pending) return;
    const mine = pending.selection;
    const key = declarationKey;
    const request = ++checkRequest.current;
    setBusy(true);
    setError(undefined);
    try {
      const form = new FormData();
      form.append("file", pending.file);
      form.append("timestamp_convention", convention);
      if (needsZone && timezone) form.append("timezone", timezone);
      if (convention !== "session_date") form.append("bar_interval", barInterval);
      const result = await api.postForm<DetectedSchema>("/datasets/inspect", form);
      // A response for an earlier file, or overtaken by a newer check, is dropped.
      if (mine !== selection.current || request !== checkRequest.current) return;
      if (result.interval_check) setCheck({ selection: mine, key, result: result.interval_check });
      setConfirmedFor([]);
    } catch (caught) {
      if (request === checkRequest.current) setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  async function checkFetch() {
    const key = fetchKey;
    const request = ++fetchRequest.current;
    setBusy(true);
    setError(undefined);
    try {
      const result = await api.post<DetectedSchema>("/datasets/inspect-url", {
        url,
        timestamp_convention: convention || null,
        timezone: timezone || null,
        bar_interval: convention === "session_date" ? null : barInterval || null,
      });
      if (request !== fetchRequest.current) return;
      if (result.interval_check) setFetchCheck({ key, result: result.interval_check });
      setFetchConfirmedFor([]);
    } catch (caught) {
      if (request === fetchRequest.current) setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  const declaration = (choices: TimestampConvention[], zoneWanted: boolean) => (
    <>
      <Field
        label="What do the timestamps mean?"
        hint="Required. AlphaLab stamps a bar when its close became known; this says how to get there."
      >
        <select
          aria-label="Timestamp convention"
          value={convention}
          onChange={(event) => setConvention(event.target.value as TimestampConvention | "")}
        >
          <option value="">Choose…</option>
          {choices.map((choice) => (
            <option key={choice} value={choice}>{TIMESTAMP_CONVENTIONS[choice].label}</option>
          ))}
        </select>
      </Field>
      {convention && <p className="tiny dim">{TIMESTAMP_CONVENTIONS[convention].meaning}</p>}
      {convention && convention !== "session_date" && (
        <Field
          label="Bar interval"
          hint="Required. Never guessed from the spacing; bars that do not fit it are refused with the reason."
        >
          <select aria-label="Bar interval" value={barInterval} onChange={(event) => setBarInterval(event.target.value)}>
            <option value="">Choose…</option>
            {BAR_INTERVALS.map((code) => (
              <option key={code} value={code}>{code}</option>
            ))}
          </select>
        </Field>
      )}
      {convention === "session_date" && <p className="tiny dim">Bar interval: 1d (a session date names one day).</p>}
      {zoneWanted && (
        <Field label="Time zone" hint="The IANA zone the values are written in, e.g. Asia/Kolkata.">
          <input
            aria-label="Time zone"
            value={timezone}
            onChange={(event) => setTimezone(event.target.value)}
            placeholder="Area/City"
          />
          <div className="row" style={{ gap: "0.4rem", marginTop: "0.3rem" }}>
            {KNOWN_EXCHANGE_ZONES.map((known) => (
              <button key={known.zone} type="button" className="tiny" onClick={() => setTimezone(known.zone)}>
                {known.label}: {known.zone}
              </button>
            ))}
          </div>
        </Field>
      )}
    </>
  );

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
              onChange={(event) => select(event.target.files?.[0])}
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

              {semantics && semantics.conventions.length > 0 ? (
                declaration(semantics.conventions as TimestampConvention[], needsZone)
              ) : (
                detected.missing_required.length === 0 && (
                  <Banner tone="err">
                    The timestamp column mixes forms that need different readings; re-export it in one form.
                  </Banner>
                )
              )}

              {declared && (
                <div style={{ margin: "0.6rem 0" }}>
                  <button type="button" onClick={() => void checkInterval()} disabled={busy}>
                    Check the declared interval against this file
                  </button>
                  {currentCheck && (
                    <IntervalCheckPanel
                      check={currentCheck}
                      interval={interval}
                      confirmed={confirmedFor}
                      scope="Missing observations"
                      onToggle={(symbol, checked) =>
                        setConfirmedFor((current) =>
                          checked ? [...current, symbol] : current.filter((s) => s !== symbol),
                        )
                      }
                    />
                  )}
                </div>
              )}

              <Field label="Dataset name">
                <input value={name} onChange={(event) => setName(event.target.value)} />
              </Field>
              <button
                className="primary"
                onClick={() => void upload()}
                disabled={busy || detected.missing_required.length > 0 || !ready}
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
          {!detected &&
            // The file is not seen until it is fetched, so the zone is always
            // offered; the server says whether these values need or refuse one.
            declaration(["session_date", "interval_start", "interval_end"], true)}
          <p className="tiny dim">
            The file is checked against this declaration before anything is stored, and refused with the
            reason if it does not fit.
          </p>
          {!detected && fetchDeclared && (
            <div style={{ margin: "0.6rem 0" }}>
              <button type="button" onClick={() => void checkFetch()} disabled={busy}>
                Check the declared interval against this URL
              </button>
              {fetchCurrentCheck && (
                <IntervalCheckPanel
                  check={fetchCurrentCheck}
                  interval={interval}
                  confirmed={fetchConfirmedFor}
                  scope="Missing observations (URL)"
                  onToggle={(symbol, checked) =>
                    setFetchConfirmedFor((current) =>
                      checked ? [...current, symbol] : current.filter((s) => s !== symbol),
                    )
                  }
                />
              )}
              <p className="tiny dim">
                The check fetches the URL now. The import fetches it again, and a confirmation is refused if the resource
                no longer serves the same bytes.
              </p>
            </div>
          )}
          <button
            onClick={() => void fetchUrl()}
            disabled={busy || !fetchReady}
            type="button"
          >
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
                    <th>Bar interval</th>
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
                      <td className="tiny"><BarInterval version={version} /></td>
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
