import { useState } from "react";

import { Badge, Banner, Card, ErrorBanner, Loading } from "../components/ui";
import { ApiError, api } from "../lib/api";
import type { AuditEvent, AuditVerification } from "../lib/api";
import { shortId, titleCase, when } from "../lib/format";
import { useAsync } from "../lib/useAsync";

export default function AuditPage() {
  const events = useAsync<AuditEvent[]>(() => api.get<AuditEvent[]>("/audit?limit=300"), []);
  const [verification, setVerification] = useState<AuditVerification | undefined>();
  const [verifying, setVerifying] = useState(false);
  const [error, setError] = useState<string | undefined>();

  /**
   * Recompute the hash chain and report the result.
   *
   * The chain is the property that makes this table worth more than a log, and
   * until now nothing in the product exercised it — the endpoint existed and
   * no screen called it. A tamper-evident record nobody can check is a claim,
   * not a control.
   */
  async function verify() {
    setVerifying(true);
    setError(undefined);
    try {
      setVerification(await api.get<AuditVerification>("/audit/verify"));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setVerifying(false);
    }
  }

  if (events.loading) return <Loading what="Loading audit trail" />;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Audit</h1>
          <p>
            An append-only record of what happened in this workspace. Credential-shaped values are
            redacted before anything is written, so a secret has no path into this table.
          </p>
        </div>
      </div>

      <ErrorBanner error={events.error} />
      {error && <Banner tone="err">{error}</Banner>}

      <Card
        title="Chain integrity"
        actions={
          <button className="small" disabled={verifying} onClick={() => void verify()} type="button">
            {verifying ? "Verifying…" : "Verify chain"}
          </button>
        }
      >
        <p className="small dim" style={{ marginTop: 0 }}>
          Each event carries a hash covering its own content and its predecessor's hash, so altering
          a payload, deleting an event or reordering two of them all break verification. This does
          not defend against someone who can rewrite the whole table and recompute it — it detects
          tampering with individual rows, which is the realistic case.
        </p>
        {verification ? (
          <>
            <Banner tone={verification.intact ? "ok" : "err"}>
              {verification.intact
                ? `Verified ${verification.events_checked} event(s). The chain is intact.`
                : `Verification failed: ${verification.breaks.length} break(s) in ${verification.events_checked} event(s).`}
            </Banner>
            {verification.breaks.length > 0 && (
              <div className="table-wrap">
                <table>
                  <thead><tr><th>Sequence</th><th>Event</th><th>Reason</th></tr></thead>
                  <tbody>
                    {verification.breaks.map((entry) => (
                      <tr key={`${entry.sequence}-${entry.event_id}`}>
                        <td className="mono tiny">{entry.sequence}</td>
                        <td className="mono tiny">{shortId(entry.event_id)}</td>
                        <td className="small">{entry.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <p className="tiny faint" style={{ marginBottom: 0 }}>
              Head hash <span className="mono">{verification.head_hash.slice(0, 16)}…</span>
            </p>
          </>
        ) : (
          <p className="tiny faint" style={{ marginBottom: 0 }}>Not checked in this session.</p>
        )}
      </Card>

      <Card>
        {(events.data ?? []).length === 0 ? (
          <p className="dim" style={{ margin: 0 }}>Nothing recorded yet.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>When</th><th>Action</th><th>Resource</th><th>Outcome</th><th>Actor</th><th>Detail</th></tr>
              </thead>
              <tbody>
                {(events.data ?? []).map((event) => (
                  <tr key={event.id}>
                    <td className="tiny faint">{when(event.created_at)}</td>
                    <td className="small">{titleCase(event.action)}</td>
                    <td className="tiny">
                      {event.resource_type}
                      {event.resource_id && <span className="mono faint"> {shortId(event.resource_id)}</span>}
                    </td>
                    <td>
                      <Badge tone={event.outcome === "success" ? "ok" : "err"}>{event.outcome}</Badge>
                    </td>
                    <td className="mono tiny faint">{shortId(event.actor_user_id)}</td>
                    <td className="mono tiny faint" style={{ maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {JSON.stringify(event.payload)}
                    </td>
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
