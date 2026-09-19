import { useState } from "react";

import QrCode from "./QrCode";
import { Banner, Card, Field, Loading } from "./ui";
import { ApiError, api } from "../lib/api";
import type { MfaEnrolment, MfaStatus } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useAsync } from "../lib/useAsync";

/**
 * Two-factor authentication, end to end.
 *
 * Three rules this component keeps, and they are the reason it is worth
 * reading rather than skimming:
 *
 * 1. **Nothing here is persisted.** The secret and the recovery codes live in
 *    React state for the length of the enrolment and are gone on navigation.
 *    No localStorage, no sessionStorage, no URL, no analytics call. They exist
 *    outside the server exactly once, and this screen is that once.
 * 2. **The secret is never shown again.** After confirmation the component has
 *    no way to retrieve it, because the server has none either — it is stored
 *    encrypted and the API has no endpoint that returns it.
 * 3. **Turning it off needs a code.** The server enforces that; the UI says so
 *    rather than presenting a bare "disable" button, because the whole point
 *    of the requirement is that a stolen session cannot undo the protection.
 */
export default function MfaSettings() {
  const { refresh } = useAuth();
  const status = useAsync<MfaStatus>(() => api.get<MfaStatus>("/auth/mfa"), []);
  const [enrolment, setEnrolment] = useState<MfaEnrolment | undefined>();
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const [notice, setNotice] = useState<string | undefined>();
  const [freshCodes, setFreshCodes] = useState<string[] | undefined>();

  function fail(caught: unknown) {
    setError(caught instanceof ApiError ? caught.message : String(caught));
  }

  async function begin() {
    setBusy(true);
    setError(undefined);
    setNotice(undefined);
    try {
      setEnrolment(await api.post<MfaEnrolment>("/auth/mfa/enrol"));
    } catch (caught) {
      fail(caught);
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    setBusy(true);
    setError(undefined);
    try {
      await api.post<MfaStatus>("/auth/mfa/confirm", { code });
      // Cleared immediately on success: from here the app must not hold it.
      setEnrolment(undefined);
      setCode("");
      setNotice("Two-factor authentication is on. You will be asked for a code at sign-in.");
      status.reload();
      await refresh();
    } catch (caught) {
      fail(caught);
    } finally {
      setBusy(false);
    }
  }

  async function disable() {
    setBusy(true);
    setError(undefined);
    try {
      await api.post<MfaStatus>("/auth/mfa/disable", { code });
      setCode("");
      setNotice("Two-factor authentication is off.");
      status.reload();
      await refresh();
    } catch (caught) {
      fail(caught);
    } finally {
      setBusy(false);
    }
  }

  async function regenerate() {
    setBusy(true);
    setError(undefined);
    try {
      const issued = await api.post<MfaEnrolment>("/auth/mfa/recovery-codes", { code });
      setFreshCodes(issued.recovery_codes);
      setCode("");
      setNotice("New recovery codes issued. The previous ones no longer work.");
      status.reload();
    } catch (caught) {
      fail(caught);
    } finally {
      setBusy(false);
    }
  }

  if (status.loading && !status.data) return <Card title="Two-factor authentication"><Loading /></Card>;

  const enabled = status.data?.enabled ?? false;

  return (
    <Card title="Two-factor authentication">
      {error && <Banner tone="err">{error}</Banner>}
      {notice && <Banner tone="ok">{notice}</Banner>}

      <div className="row" style={{ marginBottom: "0.6rem" }}>
        <span className="small">Status:</span>
        <span className={`badge ${enabled ? "ok" : "neutral"}`}>{enabled ? "on" : "off"}</span>
        {enabled && (
          <span className="tiny faint">
            {status.data?.recovery_codes_remaining ?? 0} recovery code(s) left
          </span>
        )}
      </div>

      {!enabled && !enrolment && (
        <>
          <p className="small dim">
            A code from an authenticator app will be required at sign-in, in addition to your
            password.
          </p>
          <button className="primary" disabled={busy} onClick={() => void begin()} type="button">
            Set up two-factor authentication
          </button>
        </>
      )}

      {enrolment && (
        <div>
          <Banner tone="warn">
            Save your recovery codes now. This screen is the only time they, and the secret, exist
            outside the server — they are stored hashed and cannot be shown again.
          </Banner>

          <p className="small" style={{ marginBottom: "0.3rem" }}>
            <strong>1.</strong> Scan this with your authenticator app:
          </p>
          <div style={{ display: "flex", justifyContent: "center", padding: "0.4rem 0" }}>
            <QrCode value={enrolment.provisioning_uri} />
          </div>
          <p className="tiny faint" style={{ marginTop: 0 }}>
            Cannot scan? Enter this key manually:
          </p>
          <div className="secret-box" data-testid="mfa-secret">{enrolment.secret}</div>

          <p className="small" style={{ marginBottom: "0.3rem" }}>
            <strong>2.</strong> Write down your recovery codes. Each works once, and they are the
            only way in if you lose the authenticator.
          </p>
          <div className="code-grid" data-testid="recovery-codes">
            {enrolment.recovery_codes.map((entry) => (
              <span key={entry}>{entry}</span>
            ))}
          </div>

          <p className="small" style={{ marginBottom: "0.3rem" }}>
            <strong>3.</strong> Enter a code from the app to turn it on:
          </p>
          <Field label="Verification code">
            <input value={code} onChange={(event) => setCode(event.target.value)} autoComplete="one-time-code" />
          </Field>
          <div className="row">
            <button className="primary" disabled={busy || code.length < 6} onClick={() => void confirm()} type="button">
              Turn on
            </button>
            <button disabled={busy} onClick={() => { setEnrolment(undefined); setCode(""); }} type="button">
              Cancel
            </button>
          </div>
        </div>
      )}

      {enabled && (
        <div>
          {freshCodes && (
            <>
              <Banner tone="warn">Your new recovery codes. Shown once.</Banner>
              <div className="code-grid" data-testid="recovery-codes">
                {freshCodes.map((entry) => (
                  <span key={entry}>{entry}</span>
                ))}
              </div>
            </>
          )}
          <p className="small dim">
            A current code is required to change either of these. Being signed in is not enough —
            that is exactly what someone with a stolen session would rely on.
          </p>
          <Field label="Current code (or a recovery code)">
            <input value={code} onChange={(event) => setCode(event.target.value)} autoComplete="one-time-code" />
          </Field>
          <div className="row">
            <button disabled={busy || code.length < 6} onClick={() => void regenerate()} type="button">
              Replace recovery codes
            </button>
            <button className="danger" disabled={busy || code.length < 6} onClick={() => void disable()} type="button">
              Turn off
            </button>
          </div>
        </div>
      )}
    </Card>
  );
}
