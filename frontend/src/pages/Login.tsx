import { useState } from "react";

import { Banner, Card } from "../components/ui";
import { ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";

export default function LoginPage() {
  const { login, register } = useAuth();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [organization, setOrganization] = useState("");
  const [error, setError] = useState<string | undefined>();
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      if (mode === "login") await login(email, password);
      else await register(email, password, displayName, organization);
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
          <h1 style={{ marginBottom: "0.15rem" }}>iluvtrade</h1>
          <p className="dim small" style={{ margin: 0 }}>
            Research, backtest and trade on AlphaLab.
          </p>
        </div>

        <Card>
          <div className="tabs">
            <button type="button" className={`tab ${mode === "login" ? "active" : ""}`} onClick={() => setMode("login")}>
              Sign in
            </button>
            <button type="button" className={`tab ${mode === "register" ? "active" : ""}`} onClick={() => setMode("register")}>
              Create account
            </button>
          </div>

          {error && <Banner tone="err">{error}</Banner>}

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
          </form>
        </Card>

        <p className="tiny faint" style={{ textAlign: "center", marginTop: "1rem" }}>
          Paper trading only unless an administrator enables live trading.
        </p>
      </div>
    </div>
  );
}
