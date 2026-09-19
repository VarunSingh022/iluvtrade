import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { ConfirmButton } from "../components/Confirm";
import { Badge, Banner, Card, ErrorBanner, Loading, ScoreMeter, Stat, StatusBadge, Tabs } from "../components/ui";
import { ApiError, api } from "../lib/api";
import type { DatasetVersionDetail } from "../lib/api";
import { count, epochDate, titleCase, when } from "../lib/format";
import { useAsync } from "../lib/useAsync";

/**
 * The data-quality report, and the approval gate.
 *
 * This is the screen PHASE 3 describes: a user can see exactly what happened to
 * their file — every row rejected with its line number and reason, every value
 * changed with its before and after — and only then approve it.
 */
export default function DatasetDetailPage() {
  const { versionId } = useParams<{ versionId: string }>();
  const navigate = useNavigate();
  const version = useAsync<DatasetVersionDetail>(
    () => api.get<DatasetVersionDetail>(`/datasets/versions/${versionId}`),
    [versionId],
  );
  const [tab, setTab] = useState("findings");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();

  async function act(action: "approve" | "reject") {
    setBusy(true);
    setError(undefined);
    try {
      await api.post(`/datasets/versions/${versionId}/${action}`, action === "reject" ? { reason: "Rejected from the review screen." } : undefined);
      version.reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  if (version.loading) return <Loading what="Loading dataset" />;
  if (version.error) return <ErrorBanner error={version.error} />;
  if (!version.data) return null;

  const data = version.data;
  const quality = data.quality;
  const transformations = data.transformations;
  const rejected = data.rejected_rows;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>
            {data.dataset_name} <span className="faint mono" style={{ fontSize: "1rem" }}>v{data.version}</span>
          </h1>
          <p>
            <StatusBadge status={data.status} />{" "}
            {data.status === "pending_approval" &&
              "Nothing can use this dataset until you approve it."}
            {data.status === "approved" && `Approved ${when(data.approved_at)}.`}
          </p>
        </div>
        <div className="row">
          <button onClick={() => navigate("/datasets")} type="button">Back</button>
          {data.status === "pending_approval" && (
            <>
              <ConfirmButton
                label="Reject"
                confirmLabel="Reject this version"
                consequence="It can never be used for research or trading."
                disabled={busy}
                onConfirm={() => act("reject")}
              />
              <button className="primary" onClick={() => void act("approve")} disabled={busy} type="button">
                Approve for research
              </button>
            </>
          )}
        </div>
      </div>

      {error && <Banner tone="err">{error}</Banner>}
      {data.failure_reason && <Banner tone="err">{data.failure_reason}</Banner>}

      <div className="grid cols-4">
        <Stat label="Rows accepted" value={count(quality.accepted_rows)} sub={`of ${count(quality.total_input_rows)} read`} />
        <Stat label="Rows rejected" value={count(quality.rejected_rows)} sub={quality.rejected_rows ? "each with a reason below" : "none"} />
        <Stat label="Symbols" value={quality.symbol_count} sub={quality.symbols.slice(0, 3).join(", ")} />
        <Stat
          label="Quality score"
          value={<ScoreMeter score={quality.score} />}
          sub="ordering, not a verdict"
        />
      </div>

      <div className="grid cols-4" style={{ marginTop: "0.75rem" }}>
        <Stat label="Date range" value={<span className="small">{epochDate(quality.start_timestamp)} → {epochDate(quality.end_timestamp)}</span>} />
        <Stat label="Frequency" value={<span className="small mono">{quality.inferred_frequency ?? "irregular"}</span>} sub={quality.median_gap_seconds ? `median gap ${Math.round(quality.median_gap_seconds)}s` : undefined} />
        <Stat label="Gaps" value={quality.gap_count} sub="two or more intervals" />
        <Stat label="Canonical hash" value={<span className="mono tiny">{data.canonical_hash?.slice(0, 16) ?? "—"}…</span>} sub="pinned by any backtest" />
      </div>

      <div style={{ marginTop: "1.25rem" }}>
        <Tabs
          active={tab}
          onChange={setTab}
          tabs={[
            { id: "findings", label: `Findings (${quality.findings.length})` },
            { id: "rejected", label: `Rejected rows (${quality.rejected_rows})` },
            { id: "changes", label: `Transformations (${Object.values(transformations.counts).reduce((a, b) => a + b, 0)})` },
            { id: "schema", label: "Detected schema" },
            { id: "provenance", label: "Provenance" },
          ]}
        />

        {tab === "findings" && (
          <Card>
            {quality.findings.length === 0 ? (
              <p className="dim" style={{ margin: 0 }}>Nothing of note. The file was clean.</p>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
                {quality.findings.map((finding, index) => (
                  <div key={`${finding.code}-${index}`} className="row" style={{ alignItems: "flex-start", flexWrap: "nowrap" }}>
                    <Badge tone={finding.severity === "error" ? "err" : finding.severity === "warning" ? "warn" : "neutral"}>
                      {finding.severity}
                    </Badge>
                    <span className="small">{finding.message}</span>
                  </div>
                ))}
              </div>
            )}
          </Card>
        )}

        {tab === "rejected" && (
          <Card>
            {rejected.logged.length === 0 ? (
              <p className="dim" style={{ margin: 0 }}>No rows were rejected.</p>
            ) : (
              <>
                {rejected.logged_is_truncated && (
                  <Banner tone="info">
                    Showing the first {rejected.logged.length} of {quality.rejected_rows}. The counts
                    below are exact.
                  </Banner>
                )}
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th className="num">Line</th>
                        <th>Reason</th>
                        <th>Detail</th>
                        <th>Row as it was read</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rejected.logged.map((row) => (
                        <tr key={`${row.line_number}-${row.reason}`}>
                          <td className="num">{row.line_number}</td>
                          <td><Badge tone="warn">{titleCase(row.reason)}</Badge></td>
                          <td className="small">{row.detail}</td>
                          <td className="mono tiny faint" style={{ maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                            {row.raw}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </Card>
        )}

        {tab === "changes" && (
          <Card>
            <p className="small dim">
              Every value the importer changed, with what it was and what it became. Nothing is
              altered without an entry here.
            </p>
            <div className="grid cols-3" style={{ marginBottom: "0.75rem" }}>
              {Object.entries(transformations.counts).map(([kind, n]) => (
                <Stat key={kind} label={titleCase(kind)} value={count(n)} />
              ))}
            </div>
            {transformations.logged.length > 0 && (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th className="num">Line</th>
                      <th>Change</th>
                      <th>Column</th>
                      <th>Before</th>
                      <th>After</th>
                    </tr>
                  </thead>
                  <tbody>
                    {transformations.logged.slice(0, 200).map((row, index) => (
                      <tr key={`${row.line_number}-${row.column}-${index}`}>
                        <td className="num">{row.line_number || "—"}</td>
                        <td className="small">{titleCase(row.kind)}</td>
                        <td className="mono tiny">{row.column}</td>
                        <td className="mono tiny faint">{row.before || "(empty)"}</td>
                        <td className="mono tiny">{row.after}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        )}

        {tab === "schema" && (
          <Card>
            <div className="table-wrap" style={{ marginBottom: "0.75rem" }}>
              <table>
                <thead>
                  <tr>
                    <th>Field</th>
                    <th>Column</th>
                    <th>How it was matched</th>
                    <th className="num">Confidence</th>
                    <th>Note</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.values(data.schema_detection.fields).map((field) => (
                    <tr key={field.role}>
                      <td>{titleCase(field.role)}</td>
                      <td className="mono">{field.column ?? <span className="faint">not found</span>}</td>
                      <td className="tiny faint">{field.method}</td>
                      <td className="num">{field.column ? field.confidence.toFixed(2) : "—"}</td>
                      <td className="tiny faint">{field.note}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="small">
              <strong>Timestamp format:</strong> <span className="mono">{data.schema_detection.timestamp_format}</span>
              {data.schema_detection.timestamp_ambiguous && (
                <Badge tone="err"> ambiguous</Badge>
              )}
            </div>
            <h3 style={{ marginTop: "1rem" }}>Cleaning policy applied</h3>
            <pre className="code">{JSON.stringify(data.cleaning_policy, null, 2)}</pre>
          </Card>
        )}

        {tab === "provenance" && (
          <Card>
            <div className="table-wrap">
              <table>
                <tbody>
                  <tr><td className="dim">Source</td><td className="mono small">{data.source.kind}</td></tr>
                  <tr><td className="dim">Origin</td><td className="mono small">{data.source.origin}</td></tr>
                  {data.source.requested_uri && (
                    <tr><td className="dim">Requested URL</td><td className="mono small">{data.source.requested_uri}</td></tr>
                  )}
                  <tr><td className="dim">Bytes received</td><td className="mono small">{count(data.source.size_bytes)}</td></tr>
                  <tr><td className="dim">SHA-256 of what arrived</td><td className="mono tiny">{data.source.content_hash}</td></tr>
                  <tr><td className="dim">Retrieved</td><td className="small">{when(data.source.retrieved_at)}</td></tr>
                  <tr><td className="dim">Canonical file hash</td><td className="mono tiny">{data.canonical_hash}</td></tr>
                </tbody>
              </table>
            </div>
            <p className="tiny faint" style={{ marginTop: "0.75rem", marginBottom: 0 }}>
              The bytes exactly as received are kept unchanged. A backtest records the canonical
              hash, so a dataset that changed underneath a result is detectable rather than assumed
              impossible.
            </p>
          </Card>
        )}
      </div>
    </>
  );
}
