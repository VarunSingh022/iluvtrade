import { useState } from "react";

import { ConfirmButton } from "../components/Confirm";
import { Badge, Banner, Card, ErrorBanner, Field, Loading, StatusBadge } from "../components/ui";
import { ApiError, api } from "../lib/api";
import type { BrokerConnection, SupportedBroker } from "../lib/api";
import { when } from "../lib/format";
import { useAsync } from "../lib/useAsync";

export default function BrokersPage() {
  const accounts = useAsync<BrokerConnection[]>(() => api.get<BrokerConnection[]>("/brokers"), []);
  const supported = useAsync<SupportedBroker[]>(() => api.get<SupportedBroker[]>("/brokers/supported"), []);
  const [broker, setBroker] = useState("paper");
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const [notice, setNotice] = useState<string | undefined>();

  async function create() {
    setBusy(true);
    setError(undefined);
    try {
      await api.post("/brokers", { broker, label, base_currency: "INR" });
      setLabel("");
      accounts.reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  async function connect(account: BrokerConnection) {
    setBusy(true);
    setError(undefined);
    setNotice(undefined);
    try {
      const response = await api.post<{ login_url: string }>(`/brokers/${account.account_id}/zerodha/login-url`);
      setNotice(
        `Open this URL to authorize with Zerodha, then paste the request_token from the ` +
          `redirect back here: ${response.login_url}`,
      );
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  async function authorize(account: BrokerConnection) {
    // The ``request_token`` is single-use and expires in minutes; Zerodha
    // delivers it in a redirect query string by design. It is not the access
    // token, which is exchanged server-side and never reaches the browser.
    const token = window.prompt("Paste the request_token from Zerodha's redirect:");
    if (!token) return;
    setBusy(true);
    setError(undefined);
    try {
      await api.post(`/brokers/${account.account_id}/zerodha/authorize`, { request_token: token });
      accounts.reload();
      setNotice("Connected. The access token is encrypted at rest and never returned to the browser.");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  async function disconnect(account: BrokerConnection) {
    setBusy(true);
    try {
      await api.post(`/brokers/${account.account_id}/disconnect`);
      accounts.reload();
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
          <h1>Brokers</h1>
          <p>
            Credentials are encrypted on the server and never sent to the browser. Connecting a
            venue is an authorization flow — this application never asks for your broker password.
          </p>
        </div>
      </div>

      {error && <Banner tone="err">{error}</Banner>}
      {notice && <Banner tone="info"><span className="small" style={{ wordBreak: "break-all" }}>{notice}</span></Banner>}
      <ErrorBanner error={accounts.error} />

      <Card title="Supported venues">
        {(supported.data ?? []).map((entry) => (
          <div key={entry.broker} style={{ marginBottom: "0.75rem" }}>
            <div className="row">
              <strong className="small">{entry.name}</strong>
              {entry.verified_against_live_venue ? (
                <Badge tone="ok">exercised</Badge>
              ) : (
                <Badge tone="warn">not verified against the live venue</Badge>
              )}
            </div>
            <div className="tiny dim">{entry.notes}</div>
          </div>
        ))}
      </Card>

      <Card title="Add an account">
        <div className="grid cols-3">
          <Field label="Venue">
            <select value={broker} onChange={(event) => setBroker(event.target.value)}>
              {(supported.data ?? []).map((entry) => (
                <option key={entry.broker} value={entry.broker}>{entry.name}</option>
              ))}
            </select>
          </Field>
          <Field label="Label">
            <input value={label} onChange={(event) => setLabel(event.target.value)} placeholder="Main account" />
          </Field>
        </div>
        <button className="primary" disabled={busy || !label} onClick={() => void create()} type="button">
          Add
        </button>
      </Card>

      <h2 style={{ marginTop: "1.5rem" }}>Your accounts</h2>
      {accounts.loading ? (
        <Loading />
      ) : (accounts.data ?? []).length === 0 ? (
        <Card><p className="dim" style={{ margin: 0 }}>No broker accounts yet.</p></Card>
      ) : (
        <Card>
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>Label</th><th>Venue</th><th>State</th><th>Account at venue</th><th>Credential</th><th>Token expires</th><th /></tr>
              </thead>
              <tbody>
                {(accounts.data ?? []).map((account) => (
                  <tr key={account.account_id}>
                    <td>{account.label}</td>
                    <td>
                      {account.broker}
                      {!account.verified_against_live_venue && account.broker !== "paper" && (
                        <> <Badge tone="warn">unverified</Badge></>
                      )}
                    </td>
                    <td><StatusBadge status={account.state} /></td>
                    <td className="mono tiny">{account.venue_account_id ?? "—"}</td>
                    <td className="mono tiny faint">{account.credential_hint ?? "—"}</td>
                    <td className="tiny faint">{when(account.token_expires_at)}</td>
                    <td>
                      <div className="row">
                        {account.broker === "zerodha" && account.state !== "connected" && (
                          <>
                            <button className="small" disabled={busy} onClick={() => void connect(account)} type="button">Get login URL</button>
                            <button className="small" disabled={busy} onClick={() => void authorize(account)} type="button">Paste token</button>
                          </>
                        )}
                        {account.state === "connected" && account.broker !== "paper" && (
                          <ConfirmButton
                            label="Disconnect"
                            confirmLabel="Disconnect and destroy credential"
                            consequence="The stored access token is deleted and cannot be recovered."
                            disabled={busy}
                            onConfirm={() => disconnect(account)}
                          />
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      <Banner tone="warn">
        Zerodha invalidates access tokens at 06:00 IST every day. A connection will need
        reauthorizing each trading day — that is a Zerodha requirement, not a choice this
        application makes.
      </Banner>
    </>
  );
}
