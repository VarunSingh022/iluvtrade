import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import LoginPage from "./Login";
import { AuthProvider } from "../lib/auth";

function routeFetch(handler: (url: string, init: RequestInit) => { status?: number; body?: unknown }) {
  const calls: { url: string; method: string; body: Record<string, unknown> | undefined }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      calls.push({
        url,
        method: init.method ?? "GET",
        body: init.body ? JSON.parse(init.body as string) : undefined,
      });
      const result = handler(url, init);
      return {
        status: result.status ?? 200,
        ok: (result.status ?? 200) < 400,
        text: async () => (result.body === undefined ? "" : JSON.stringify(result.body)),
        json: async () => result.body,
      } as Response;
    }),
  );
  return calls;
}

const USER = {
  id: "u1",
  email: "a@example.com",
  display_name: "A",
  organization_id: "o1",
  organization_name: "W",
  role: "owner",
  live_trading_enabled: false,
  mfa_enabled: true,
};

async function signIn(email = "a@example.com", password = "correct-horse-battery-staple") {
  render(
    <AuthProvider>
      <LoginPage />
    </AuthProvider>,
  );
  await userEvent.type(await screen.findByLabelText("Email"), email);
  await userEvent.type(screen.getByLabelText("Password"), password);
  await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the sign-in screen", () => {
  it("asks for a code when the server says one is needed", async () => {
    routeFetch((url, init) => {
      if (url.endsWith("/auth/me")) return { status: 401, body: { error: { code: "NotAuthenticated", message: "" } } };
      if (init.method === "POST" && url.endsWith("/auth/login")) {
        return {
          status: 401,
          body: { error: { code: "MfaRequired", message: "This account requires a verification code." } },
        };
      }
      return { status: 200, body: {} };
    });

    await signIn();

    expect(await screen.findByLabelText(/verification code/i)).toBeInTheDocument();
    // And it must not say the password was wrong — it was not.
    expect(screen.queryByText(/invalid email or password/i)).not.toBeInTheDocument();
  });

  it("sends the code with the credentials, not as a separate ticket", async () => {
    let attempts = 0;
    const calls = routeFetch((url, init) => {
      if (url.endsWith("/auth/me")) return { status: 401, body: { error: { code: "NotAuthenticated", message: "" } } };
      if (init.method === "POST" && url.endsWith("/auth/login")) {
        attempts += 1;
        if (attempts === 1) {
          return { status: 401, body: { error: { code: "MfaRequired", message: "Code needed." } } };
        }
        return { status: 200, body: { token: "t", expires_at: "", user: USER } };
      }
      return { status: 200, body: {} };
    });

    await signIn();
    await userEvent.type(await screen.findByLabelText(/verification code/i), "123456");
    await userEvent.click(screen.getByRole("button", { name: /verify and sign in/i }));

    const logins = calls.filter((call) => call.url.endsWith("/auth/login") && call.method === "POST");
    expect(logins).toHaveLength(2);
    // The second attempt carries everything the first did, plus the code.
    // There is no half-authenticated server state to resume, and therefore no
    // short-lived "MFA ticket" for anyone to steal.
    expect(logins[1]!.body).toMatchObject({
      email: "a@example.com",
      password: "correct-horse-battery-staple",
      mfa_code: "123456",
    });
  });

  it("keeps the challenge open when the code is wrong", async () => {
    let attempts = 0;
    routeFetch((url, init) => {
      if (url.endsWith("/auth/me")) return { status: 401, body: { error: { code: "NotAuthenticated", message: "" } } };
      if (init.method === "POST" && url.endsWith("/auth/login")) {
        attempts += 1;
        if (attempts === 1) {
          return { status: 401, body: { error: { code: "MfaRequired", message: "Code needed." } } };
        }
        return {
          status: 401,
          body: { error: { code: "AuthError", message: "Invalid email, password, or verification code." } },
        };
      }
      return { status: 200, body: {} };
    });

    await signIn();
    await userEvent.type(await screen.findByLabelText(/verification code/i), "000000");
    await userEvent.click(screen.getByRole("button", { name: /verify and sign in/i }));

    expect(
      await screen.findByText("Invalid email, password, or verification code."),
    ).toBeInTheDocument();
    // Still on the challenge, not thrown back to the password form.
    expect(screen.getByLabelText(/verification code/i)).toBeInTheDocument();
  });

  it("reports a genuinely wrong password as a wrong password", async () => {
    routeFetch((url, init) => {
      if (url.endsWith("/auth/me")) return { status: 401, body: { error: { code: "NotAuthenticated", message: "" } } };
      if (init.method === "POST" && url.endsWith("/auth/login")) {
        return { status: 401, body: { error: { code: "AuthError", message: "Invalid email or password." } } };
      }
      return { status: 200, body: {} };
    });

    await signIn();

    expect(await screen.findByText("Invalid email or password.")).toBeInTheDocument();
    expect(screen.queryByLabelText(/verification code/i)).not.toBeInTheDocument();
  });
});

describe("the password reset panel", () => {
  it("says plainly when the deployment cannot send a link", async () => {
    routeFetch((url) => {
      if (url.endsWith("/auth/me")) return { status: 401, body: { error: { code: "NotAuthenticated", message: "" } } };
      if (url.endsWith("/auth/password-reset")) {
        return {
          body: {
            available: false,
            channel: "none",
            notice: "This deployment has no email delivery configured, so a reset link cannot be sent.",
          },
        };
      }
      return { status: 200, body: {} };
    });

    render(
      <AuthProvider>
        <LoginPage />
      </AuthProvider>,
    );
    await userEvent.click(await screen.findByRole("button", { name: /forgot your password/i }));

    expect(await screen.findByText(/no email delivery configured/i)).toBeInTheDocument();
    // No button offering something that would not happen.
    expect(screen.queryByRole("button", { name: /email me a reset link/i })).not.toBeInTheDocument();
    // But the token form is there, because an administrator can issue one.
    expect(screen.getByLabelText(/reset code/i)).toBeInTheDocument();
  });

  it("offers the link form when a channel does exist", async () => {
    routeFetch((url) => {
      if (url.endsWith("/auth/me")) return { status: 401, body: { error: { code: "NotAuthenticated", message: "" } } };
      if (url.endsWith("/auth/password-reset")) {
        return { body: { available: true, channel: "smtp", notice: "Enter your email address." } };
      }
      return { status: 200, body: {} };
    });

    render(
      <AuthProvider>
        <LoginPage />
      </AuthProvider>,
    );
    await userEvent.click(await screen.findByRole("button", { name: /forgot your password/i }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /email me a reset link/i })).toBeInTheDocument();
    });
  });
});
