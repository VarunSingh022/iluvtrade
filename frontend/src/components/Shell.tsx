import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";

import { api, health } from "../lib/api";
import type { Dashboard, HealthResponse } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useAsync } from "../lib/useAsync";

/**
 * The navigation makes the product's three parts explicit — ALPHALAB for
 * research and trading, REDDESK for the marketplace, and the platform surfaces
 * — because that separation is the architecture and a user benefits from seeing
 * it named.
 */
const GROUPS: { label: string; items: { to: string; label: string; badge?: (d: Dashboard) => string | null }[] }[] = [
  {
    label: "Alphalab",
    items: [
      { to: "/", label: "Dashboard" },
      { to: "/datasets", label: "Datasets", badge: (d) => (d.datasets.pending ? String(d.datasets.pending) : null) },
      { to: "/strategies", label: "Strategies" },
      { to: "/research", label: "Research" },
      { to: "/backtests", label: "Backtests", badge: (d) => {
        const busy = d.backtests.queued + d.backtests.running;
        return busy ? String(busy) : null;
      } },
    ],
  },
  {
    label: "Trade",
    items: [
      { to: "/sessions", label: "Sessions", badge: (d) => (d.sessions.running ? String(d.sessions.running) : null) },
      { to: "/portfolio", label: "Portfolio" },
      { to: "/orders", label: "Orders" },
      { to: "/brokers", label: "Brokers" },
    ],
  },
  {
    label: "RedDesk",
    items: [{ to: "/reddesk", label: "Marketplace" }],
  },
  {
    label: "Workspace",
    items: [
      { to: "/audit", label: "Audit" },
      { to: "/settings", label: "Settings" },
    ],
  },
];

export default function Shell({ children }: { children: ReactNode }) {
  const { user, logout } = useAuth();
  const dashboard = useAsync<Dashboard>(() => api.get<Dashboard>("/dashboard"), []);
  const status = useAsync<HealthResponse>(() => health(), []);

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          iluvtrade
          <small>
            {status.data ? `alphalab ${status.data.engine.version}` : " "}
          </small>
        </div>

        {GROUPS.map((group) => (
          <nav className="nav-group" key={group.label}>
            <div className="nav-label">{group.label}</div>
            {group.items.map((item) => {
              const badge = dashboard.data && item.badge ? item.badge(dashboard.data) : null;
              return (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.to === "/"}
                  className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}
                >
                  <span>{item.label}</span>
                  {badge && <span className="badge accent">{badge}</span>}
                </NavLink>
              );
            })}
          </nav>
        ))}

        <div style={{ marginTop: "auto", paddingTop: "1rem", borderTop: "1px solid var(--border)" }}>
          <div className="small" style={{ padding: "0 0.5rem 0.4rem" }}>
            <div style={{ fontWeight: 600 }}>{user?.display_name}</div>
            <div className="faint tiny">{user?.organization_name}</div>
            <div className="faint tiny">{user?.role}</div>
          </div>
          {status.data && !status.data.live_trading_enabled && (
            <div className="tiny faint" style={{ padding: "0 0.5rem 0.4rem" }}>
              Live trading disabled
            </div>
          )}
          <button className="small" style={{ width: "100%" }} onClick={() => void logout()} type="button">
            Sign out
          </button>
        </div>
      </aside>

      <main className="main">{children}</main>
    </div>
  );
}
