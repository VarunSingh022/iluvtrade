import { useState } from "react";

import { Banner, Card, ErrorBanner, Field, Loading } from "../components/ui";
import { ApiError, api, health } from "../lib/api";
import type { HealthResponse, Notification, User } from "../lib/api";
import { useAuth } from "../lib/auth";
import { relative } from "../lib/format";
import { useAsync } from "../lib/useAsync";

export default function SettingsPage() {
  const { user, refresh } = useAuth();
  const status = useAsync<HealthResponse>(() => health(), []);
  const notifications = useAsync<Notification[]>(() => api.get<Notification[]>("/notifications?limit=50"), []);
  const [displayName, setDisplayName] = useState(user?.display_name ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const [notice, setNotice] = useState<string | undefined>();

  async function save(patch: Record<string, unknown>) {
    setBusy(true);
    setError(undefined);
    setNotice(undefined);
    try {
      await api.patch<User>("/auth/me", patch);
      await refresh();
      setNotice("Saved.");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  if (!user) return <Loading />;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p>{user.organization_name} · you are {user.role} in this workspace.</p>
        </div>
      </div>

      {error && <Banner tone="err">{error}</Banner>}
      {notice && <Banner tone="ok">{notice}</Banner>}

      <div className="grid cols-2">
        <Card title="Profile">
          <Field label="Display name">
            <input value={displayName} onChange={(event) => setDisplayName(event.target.value)} />
          </Field>
          <Field label="Email"><input value={user.email} disabled /></Field>
          <button className="primary" disabled={busy} onClick={() => void save({ display_name: displayName })} type="button">
            Save
          </button>
        </Card>

        <Card title="Live trading">
          <p className="small dim">
            Live trading needs three independent things to be true: this deployment allows it, your
            account enables it, and each session is confirmed individually. This switch is only the
            second of the three.
          </p>
          <div className="row" style={{ marginBottom: "0.6rem" }}>
            <span className="small">Deployment:</span>
            <span className={`badge ${status.data?.live_trading_enabled ? "ok" : "neutral"}`}>
              {status.data?.live_trading_enabled ? "enabled" : "disabled"}
            </span>
            <span className="small">Your account:</span>
            <span className={`badge ${user.live_trading_enabled ? "warn" : "neutral"}`}>
              {user.live_trading_enabled ? "enabled" : "disabled"}
            </span>
          </div>
          <button
            disabled={busy}
            onClick={() => void save({ live_trading_enabled: !user.live_trading_enabled })}
            type="button"
          >
            {user.live_trading_enabled ? "Disable on my account" : "Enable on my account"}
          </button>
          {!status.data?.live_trading_enabled && (
            <p className="tiny faint" style={{ marginTop: "0.5rem", marginBottom: 0 }}>
              Even with this on, live sessions stay refused until an administrator enables live
              trading for the deployment.
            </p>
          )}
        </Card>
      </div>

      <Card
        title="Notifications"
        actions={
          <button className="small" onClick={() => void api.post("/notifications/read-all").then(() => notifications.reload())} type="button">
            Mark all read
          </button>
        }
      >
        <ErrorBanner error={notifications.error} />
        {(notifications.data ?? []).length === 0 ? (
          <p className="dim small" style={{ margin: 0 }}>Nothing yet.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Severity</th><th>Title</th><th>Detail</th><th>When</th></tr></thead>
              <tbody>
                {(notifications.data ?? []).map((note) => (
                  <tr key={note.id} style={{ opacity: note.read_at ? 0.55 : 1 }}>
                    <td>
                      <span className={`badge ${note.severity === "error" || note.severity === "critical" ? "err" : note.severity === "warning" ? "warn" : "neutral"}`}>
                        {note.severity}
                      </span>
                    </td>
                    <td className="small">{note.title}</td>
                    <td className="tiny faint">{note.body}</td>
                    <td className="tiny faint">{relative(note.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="This deployment">
        <div className="table-wrap">
          <table>
            <tbody>
              <tr><td className="dim">Application version</td><td className="mono small">{status.data?.version}</td></tr>
              <tr><td className="dim">Environment</td><td className="mono small">{status.data?.environment}</td></tr>
              <tr><td className="dim">Engine</td><td className="mono small">{status.data?.engine.name} {status.data?.engine.version}</td></tr>
            </tbody>
          </table>
        </div>
      </Card>
    </>
  );
}