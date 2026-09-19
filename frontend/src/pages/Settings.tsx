import { useState } from "react";

import { ConfirmButton } from "../components/Confirm";
import MfaSettings from "../components/MfaSettings";
import Workspace from "../components/Workspace";
import { Banner, Card, ErrorBanner, Field, Loading } from "../components/ui";
import { ApiError, api, health } from "../lib/api";
import type { HealthResponse, Notification, PasswordResetAvailability, User } from "../lib/api";
import { useAuth } from "../lib/auth";
import { relative } from "../lib/format";
import { useAsync } from "../lib/useAsync";

export default function SettingsPage() {
  const { user, refresh } = useAuth();
  const status = useAsync<HealthResponse>(() => health(), []);
  const notifications = useAsync<Notification[]>(() => api.get<Notification[]>("/notifications?limit=50"), []);
  // Read from the endpoint that owns the answer rather than inferred from the
  // notification channels: they are different transports and conflating them
  // would make this panel quietly wrong the moment one is configured.
  const reset = useAsync<PasswordResetAvailability>(
    () => api.get<PasswordResetAvailability>("/auth/password-reset"),
    [],
  );
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
          {user.live_trading_enabled ? (
            <button
              disabled={busy}
              onClick={() => void save({ live_trading_enabled: false })}
              type="button"
            >
              Disable on my account
            </button>
          ) : (
            <ConfirmButton
              label="Enable on my account"
              confirmLabel="Enable live trading for my account"
              consequence="This is one of three gates. With all three open, orders reach a real venue."
              tone="default"
              disabled={busy}
              onConfirm={() => save({ live_trading_enabled: true })}
            />
          )}
          {!status.data?.live_trading_enabled && (
            <p className="tiny faint" style={{ marginTop: "0.5rem", marginBottom: 0 }}>
              Even with this on, live sessions stay refused until an administrator enables live
              trading for the deployment.
            </p>
          )}
        </Card>
      </div>

      <div className="grid cols-2">
        <MfaSettings />

        <Card title="Sessions and sign-in">
          <p className="small dim">
            Signing in issues a session that lasts until it expires or you sign out. Resetting your
            password signs out every session on the account, which is the point of resetting one.
          </p>
          <table>
            <tbody>
              <tr>
                <td className="dim small">Password reset delivery</td>
                <td className="mono small">{reset.data?.channel ?? "—"}</td>
              </tr>
              <tr>
                <td className="dim small">Two-factor</td>
                <td className="mono small">{user.mfa_enabled ? "required at sign-in" : "not enabled"}</td>
              </tr>
            </tbody>
          </table>
          <p className="tiny faint" style={{ marginBottom: 0 }}>
            {reset.data?.notice}
          </p>
        </Card>
      </div>

      <h2 style={{ marginTop: "1.4rem", marginBottom: "0.6rem", fontSize: "1rem" }}>Workspace</h2>
      <Workspace />

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
              <tr>
                <td className="dim">Payments</td>
                <td className="mono small">{status.data?.payment_provider ?? "—"}</td>
              </tr>
              <tr>
                <td className="dim">Notification channels</td>
                <td className="mono small">
                  {status.data ? (status.data.notification_channels.join(", ") || "in-app only") : "—"}
                </td>
              </tr>
              <tr>
                <td className="dim">Rate limiting</td>
                <td className="small">
                  <span className={`badge ${status.data?.rate_limiting.enabled ? "ok" : "err"}`}>
                    {status.data?.rate_limiting.enabled ? "on" : "off"}
                  </span>{" "}
                  <span className="tiny faint">{status.data?.rate_limiting.caveat}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </Card>
    </>
  );
}