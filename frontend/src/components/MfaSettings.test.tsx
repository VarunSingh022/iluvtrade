import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import MfaSettings from "./MfaSettings";
import { AuthProvider } from "../lib/auth";

/**
 * A fetch stub routed by path, so a component making several calls behaves as
 * it would against the real API.
 */
function routeFetch(routes: Record<string, { status?: number; body?: unknown }>) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const method = init.method ?? "GET";
      calls.push({
        url,
        method,
        body: init.body ? JSON.parse(init.body as string) : undefined,
      });
      const key = `${method} ${url}`;
      const match = routes[key] ?? routes[url] ?? { status: 200, body: {} };
      return {
        status: match.status ?? 200,
        ok: (match.status ?? 200) < 400,
        text: async () => (match.body === undefined ? "" : JSON.stringify(match.body)),
        json: async () => match.body,
      } as Response;
    }),
  );
  return calls;
}

const ENROLMENT = {
  secret: "JBSWY3DPEHPK3PXP",
  provisioning_uri: "otpauth://totp/iluvtrade:a@example.com?secret=JBSWY3DPEHPK3PXP&issuer=iluvtrade",
  recovery_codes: ["ABCD-EFGH-JKLM", "NPQR-STUV-WXYZ"],
};

const USER = {
  id: "u1",
  email: "a@example.com",
  display_name: "A",
  organization_id: "o1",
  organization_name: "W",
  role: "owner",
  live_trading_enabled: false,
  mfa_enabled: false,
};

function renderMfa() {
  return render(
    <AuthProvider>
      <MfaSettings />
    </AuthProvider>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("MFA settings", () => {
  it("offers enrolment when two-factor is off", async () => {
    routeFetch({
      "/api/v1/auth/me": { body: USER },
      "/api/v1/auth/mfa": { body: { enabled: false, enrolment_pending: false, recovery_codes_remaining: 0 } },
    });
    renderMfa();

    expect(
      await screen.findByRole("button", { name: /set up two-factor/i }),
    ).toBeInTheDocument();
  });

  it("shows the secret, a QR code and the recovery codes exactly once", async () => {
    routeFetch({
      "/api/v1/auth/me": { body: USER },
      "/api/v1/auth/mfa": { body: { enabled: false, enrolment_pending: false, recovery_codes_remaining: 0 } },
      "POST /api/v1/auth/mfa/enrol": { status: 201, body: ENROLMENT },
    });
    renderMfa();

    await userEvent.click(await screen.findByRole("button", { name: /set up two-factor/i }));

    expect(await screen.findByTestId("mfa-secret")).toHaveTextContent(ENROLMENT.secret);
    expect(screen.getByRole("img", { name: /QR code/i })).toBeInTheDocument();
    for (const code of ENROLMENT.recovery_codes) {
      expect(screen.getByText(code)).toBeInTheDocument();
    }
    // The warning has to be present, because this screen is the only time
    // either value exists outside the server.
    expect(screen.getByText(/cannot be shown again/i)).toBeInTheDocument();
  });

  it("never writes the secret or the recovery codes to browser storage", async () => {
    routeFetch({
      "/api/v1/auth/me": { body: USER },
      "/api/v1/auth/mfa": { body: { enabled: false, enrolment_pending: false, recovery_codes_remaining: 0 } },
      "POST /api/v1/auth/mfa/enrol": { status: 201, body: ENROLMENT },
    });
    renderMfa();

    await userEvent.click(await screen.findByRole("button", { name: /set up two-factor/i }));
    await screen.findByTestId("mfa-secret");

    const stored = [
      ...Object.values({ ...window.localStorage }),
      ...Object.values({ ...window.sessionStorage }),
    ].join(" ");
    expect(stored).not.toContain(ENROLMENT.secret);
    for (const code of ENROLMENT.recovery_codes) {
      expect(stored).not.toContain(code);
    }
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });

  it("clears the secret from the page once enrolment is confirmed", async () => {
    routeFetch({
      "/api/v1/auth/me": { body: USER },
      "/api/v1/auth/mfa": { body: { enabled: false, enrolment_pending: false, recovery_codes_remaining: 0 } },
      "POST /api/v1/auth/mfa/enrol": { status: 201, body: ENROLMENT },
      "POST /api/v1/auth/mfa/confirm": { body: { enabled: true, enrolment_pending: false, recovery_codes_remaining: 2 } },
    });
    renderMfa();

    await userEvent.click(await screen.findByRole("button", { name: /set up two-factor/i }));
    await screen.findByTestId("mfa-secret");
    await userEvent.type(screen.getByLabelText(/verification code/i), "123456");
    await userEvent.click(screen.getByRole("button", { name: /turn on/i }));

    await waitFor(() => {
      expect(screen.queryByTestId("mfa-secret")).not.toBeInTheDocument();
    });
    expect(screen.queryByText(ENROLMENT.recovery_codes[0]!)).not.toBeInTheDocument();
  });

  it("requires a code before turning two-factor off", async () => {
    routeFetch({
      "/api/v1/auth/me": { body: { ...USER, mfa_enabled: true } },
      "/api/v1/auth/mfa": { body: { enabled: true, enrolment_pending: false, recovery_codes_remaining: 7 } },
    });
    renderMfa();

    const off = await screen.findByRole("button", { name: /turn off/i });
    expect(off).toBeDisabled();

    await userEvent.type(screen.getByLabelText(/current code/i), "123456");
    expect(screen.getByRole("button", { name: /turn off/i })).toBeEnabled();
  });

  it("reports the server's refusal rather than swallowing it", async () => {
    routeFetch({
      "/api/v1/auth/me": { body: { ...USER, mfa_enabled: true } },
      "/api/v1/auth/mfa": { body: { enabled: true, enrolment_pending: false, recovery_codes_remaining: 7 } },
      "POST /api/v1/auth/mfa/disable": {
        status: 400,
        body: { error: { code: "MfaError", message: "That code is not valid." } },
      },
    });
    renderMfa();

    await userEvent.type(await screen.findByLabelText(/current code/i), "000000");
    await userEvent.click(screen.getByRole("button", { name: /turn off/i }));

    expect(await screen.findByText("That code is not valid.")).toBeInTheDocument();
  });
});
