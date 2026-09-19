import { useEffect, useState } from "react";

import { Banner, Card } from "../components/ui";
import { ApiError, api } from "../lib/api";
import type { PasswordResetAvailability } from "../lib/api";
import { isMfaChallenge, useAuth } from "../lib/auth";

type Mode = "login" | "register" | "reset";

export default function LoginPage() {
  const { login, register } = useAuth();
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [organization, setOrganization] = useState("");
  const [error, setError] = useState<string | undefined>();
  const [busy, setBusy] = useState(false);

  /**
   * Set when the server answers a correct password with "a code is needed".
   *
   * The email and password already typed are kept in state and resent with the
   * code — the server has no half-authenticated state to resume, which means
   * there is no short-lived "MFA ticket" for anyone to steal.
   */
  const [challenge, setChallenge] = useState(false);
  const [code, setCode] = useState("");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      if (mode === "login") await login(email, password, challenge ? code : undefined);
      else await register(email, password, displayName, organization);
    } catch (caught) {
      if (isMfaChallenge(caught)) {
        setChallenge(true);
        setCode("");
        setError(undefined);
      } else {
        setError(caught instanceof ApiError ? caught.message : String(caught));
        // A wrong code should not silently drop the user back to the password
        // form; they are still mid-challenge.
        if (challenge) setCode("");
      }
    } finally {
      setBusy(false);
    }
  }

  function changeMode(next: Mode) {
    setMode(next);
    setChallenge(false);
    setCode("");
    setError(undefined);
  }

  if (mode === "reset") {
    return <ResetPanel email={email} onBack={() => changeMode("login")} />;
  }

  return (
    <div className="auth-shell">
      <div className="auth-card">
        <div style={{ textAlign: "center", marginBottom: "1.25rem" }}>
          <h1 style={{ marginBottom: "0.15rem" }}>iluvtrade</h1>
          <p className="dim small" style={{ margin: 0 }}>
            Research, backtest and trade on AlphaLab.
          </p>
        </div>

        <Card>
          {/* Real tab semantics below: otherwise the "Sign in" tab and the
              "Sign in" submit button are two controls with the same accessible
              name, and nothing distinguishes them to a screen reader — or to
              anyone navigating by keyboard. */}
          {!challenge && (
            <div className="tabs" role="tablist">
              <button
                type="button"
                role="tab"
                aria-selected={mode === "login"}
                className={`tab ${mode === "login" ? "active" : ""}`}
                onClick={() => changeMode("login")}
              >
                Sign in
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={mode === "register"}
                className={`tab ${mode === "register" ? "active" : ""}`}
                onClick={() => changeMode("register")}
              >
                Create account
              </button>
            </div>
          )}

          {error && <Banner tone="err">{error}</Banner>}

          {challenge ? (
            <form onSubmit={(event) => void submit(event)}>
              <p className="small dim" style={{ marginTop: 0 }}>
                Enter the six-digit code from your authenticator app for <strong>{email}</strong>,
                or one of your recovery codes.
              </p>
              <div className="field">
                <label htmlFor="mfa-code">Verification code</label>
                <input
                  id="mfa-code"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  required
                  autoFocus
                  autoComplete="one-time-code"
                  inputMode="text"
                  placeholder="123456 or ABCD-EFGH-JKLM"
                />
              </div>
              <button className="primary" style={{ width: "100%" }} disabled={busy} type="submit">
                {busy ? "Verifying…" : "Verify and sign in"}
              </button>
              <button
                type="button"
                className="link"
                style={{ width: "100%", marginTop: "0.5rem" }}
                onClick={() => changeMode("login")}
              >
                Use a different account
              </button>
            </form>
          ) : (
            <form onSubmit={(event) => void submit(event)}>
              {mode === "register" && (
                <>
                  <div className="field">
                    <label htmlFor="name">Your name</label>
                    <input id="name" value={displayName} onChange={(e) => setDisplayName(e.target.value)} required />
                  </div>
                  <div className="field">
                    <label htmlFor="org">Workspace name</label>
                    <input
                      id="org"
                      value={organization}
                      onChange={(e) => setOrganization(e.target.value)}
                      placeholder="Optional"
                    />
                  </div>
                </>
              )}
              <div className="field">
                <label htmlFor="email">Email</label>
                <input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="username" />
              </div>
              <div className="field">
                <label htmlFor="password">Password</label>
                <input
                  id="password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  minLength={mode === "register" ? 12 : 1}
                  autoComplete={mode === "register" ? "new-password" : "current-password"}
                />
                {mode === "register" && <div className="tiny faint" style={{ marginTop: 2 }}>At least 12 characters.</div>}
              </div>
              <button className="primary" style={{ width: "100%" }} disabled={busy} type="submit">
                {busy ? "Working…" : mode === "login" ? "Sign in" : "Create account"}
              </button>
              {mode === "login" && (
                <button
                  type="button"
                  className="link"
                  style={{ width: "100%", marginTop: "0.5rem" }}
                  onClick={() => changeMode("reset")}
                >
                  Forgot your password?
                </button>
              )}
            </form>
          )}
        </Card>

        <p className="tiny faint" style={{ textAlign: "center", marginTop: "1rem" }}>
          Paper trading only unless an administrator enables live trading.
        </p>
      </div>
    </div>
  );
}

/**
 * Password reset, which on this deployment stops at a boundary.
 *
 * The screen asks the server whether a delivery channel exists before offering
 * anything, and says plainly when one does not. Offering a "send reset link"
 * button that silently goes nowhere would be the easy thing to build and would
 * leave the user waiting for a message that was never sent.
 *
 * The token form is always available, because an administrator can issue a
 * token out of band — that is the path that actually works here.
 */
function ResetPanel({ email: initialEmail, onBack }: { email: string; onBack: () => void }) {
  const [availability, setAvailability] = useState<PasswordResetAvailability | undefined>();
  const [email, setEmail] = useState(initialEmail);
  const [token, setToken] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [notice, setNotice] = useState<string | undefined>();
  const [error, setError] = useState<string | undefined>();
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .get<PasswordResetAvailability>("/auth/password-reset")
      .then(setAvailability)
      .catch(() => setAvailability({ available: false, channel: "unknown", notice: "" }));
  }, []);

  async function requestLink(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(undefined);
    setNotice(undefined);
    try {
      await api.post("/auth/password-reset/request", { email });
      // Deliberately does not say whether the address has an account.
      setNotice("If that address has an account, a reset link is on its way.");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  async function applyToken(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(undefined);
    setNotice(undefined);
    try {
      await api.post("/auth/password-reset/confirm", { token, new_password: newPassword });
      setToken("");
      setNewPassword("");
      setNotice("Password changed. Every session was signed out — sign in with the new password.");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-shell">
      <div className="auth-card">
        <div style={{ textAlign: "center", marginBottom: "1.25rem" }}>
          <h1 style={{ marginBottom: "0.15rem" }}>Reset password</h1>
        </div>

        <Card>
          {error && <Banner tone="err">{error}</Banner>}
          {notice && <Banner tone="ok">{notice}</Banner>}

          {availability && !availability.available && (
            <Banner tone="warn">
              {availability.notice || "Email delivery is not configured on this deployment."}
            </Banner>
          )}

          {availability?.available && (
            <form onSubmit={(event) => void requestLink(event)} style={{ marginBottom: "1rem" }}>
              <div className="field">
                <label htmlFor="reset-email">Email</label>
                <input id="reset-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
              </div>
              <button className="primary" style={{ width: "100%" }} disabled={busy} type="submit">
                {busy ? "Working…" : "Email me a reset link"}
              </button>
            </form>
          )}

          <form onSubmit={(event) => void applyToken(event)}>
            <p className="small dim" style={{ marginTop: 0 }}>
              {availability?.available
                ? "Already have a reset code? Enter it here."
                : "An administrator can issue you a reset code directly. Enter it here."}
            </p>
            <div className="field">
              <label htmlFor="reset-token">Reset code</label>
              <input id="reset-token" value={token} onChange={(e) => setToken(e.target.value)} required autoComplete="off" />
            </div>
            <div className="field">
              <label htmlFor="reset-password">New password</label>
              <input
                id="reset-password"
                type="password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                required
                minLength={12}
                autoComplete="new-password"
              />
              <div className="tiny faint" style={{ marginTop: 2 }}>At least 12 characters.</div>
            </div>
            <button className="primary" style={{ width: "100%" }} disabled={busy} type="submit">
              {busy ? "Working…" : "Set new password"}
            </button>
          </form>

          <button type="button" className="link" style={{ width: "100%", marginTop: "0.75rem" }} onClick={onBack}>
            Back to sign in
          </button>
        </Card>
      </div>
    </div>
  );
}
