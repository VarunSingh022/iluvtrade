import { Badge, Card, ErrorBanner, Loading } from "../components/ui";
import { api } from "../lib/api";
import type { AuditEvent } from "../lib/api";
import { shortId, titleCase, when } from "../lib/format";
import { useAsync } from "../lib/useAsync";

export default function AuditPage() {
  const events = useAsync<AuditEvent[]>(() => api.get<AuditEvent[]>("/audit?limit=300"), []);

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
