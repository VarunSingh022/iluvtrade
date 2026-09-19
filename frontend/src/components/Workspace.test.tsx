import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import Workspace from "./Workspace";
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

const OWNER = {
  id: "u1",
  email: "owner@example.com",
  display_name: "Owner",
  organization_id: "o1",
  organization_name: "Acme",
  role: "owner",
  live_trading_enabled: false,
  mfa_enabled: false,
};

const TOKEN = "invitation-token-value-0123456789abcdef";

function baseRoutes(user = OWNER) {
  return (url: string, init: RequestInit) => {
    if (url.endsWith("/auth/me")) return { body: user };
    if (url.endsWith("/organizations/members")) {
      return {
        body: [
          {
            user_id: user.id,
            email: user.email,
            display_name: user.display_name,
            role: user.role,
            joined_at: new Date().toISOString(),
          },
        ],
      };
    }
    if (url.endsWith("/auth/organizations")) {
      return {
        body: [
          { organization_id: "o1", organization_name: "Acme", role: user.role, is_current: true },
          { organization_id: "o2", organization_name: "Other", role: "trader", is_current: false },
        ],
      };
    }
    if (url.endsWith("/organizations/invitations") && init.method !== "POST") return { body: [] };
    if (url.endsWith("/organizations/invitations") && init.method === "POST") {
      return {
        status: 201,
        body: {
          invitation: {
            id: "i1",
            email: "new@example.com",
            role: "trader",
            status: "pending",
            invited_by_user_id: user.id,
            expires_at: new Date(Date.now() + 86400000).toISOString(),
            accepted_at: null,
            revoked_at: null,
            created_at: new Date().toISOString(),
          },
          token: TOKEN,
          share_instructions: "Send this code to the person you invited through a channel you trust.",
        },
      };
    }
    return { status: 200, body: {} };
  };
}

function renderWorkspace() {
  return render(
    <AuthProvider>
      <Workspace />
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

describe("the workspace screen", () => {
  it("lists the members of the current workspace", async () => {
    routeFetch(baseRoutes());
    renderWorkspace();
    expect(await screen.findByText("owner@example.com")).toBeInTheDocument();
  });

  it("shows the invitation token once, with instructions to pass it on", async () => {
    routeFetch(baseRoutes());
    renderWorkspace();

    await userEvent.type(await screen.findByLabelText(/email address/i), "new@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Invite" }));

    expect(await screen.findByTestId("invitation-token")).toHaveTextContent(TOKEN);
    // The screen must say *why* a token is shown instead of an email being
    // sent, or the inviter waits for a delivery that never happens.
    expect(screen.getByText(/channel you trust/i)).toBeInTheDocument();
  });

  it("does not persist the invitation token in browser storage", async () => {
    routeFetch(baseRoutes());
    renderWorkspace();

    await userEvent.type(await screen.findByLabelText(/email address/i), "new@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Invite" }));
    await screen.findByTestId("invitation-token");

    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });

  it("does not offer a role above the inviter's own", async () => {
    const admin = { ...OWNER, role: "admin" };
    routeFetch(baseRoutes(admin));
    renderWorkspace();

    const select = (await screen.findByLabelText(/^role$/i)) as HTMLSelectElement;
    const options = Array.from(select.options).map((option) => option.value);
    expect(options).toEqual(["viewer", "trader", "admin"]);
    expect(options).not.toContain("owner");
  });

  it("hides invitation administration from a member who cannot invite", async () => {
    routeFetch(baseRoutes({ ...OWNER, role: "trader" }));
    renderWorkspace();

    await screen.findByText("owner@example.com");
    expect(screen.queryByRole("button", { name: "Invite" })).not.toBeInTheDocument();
  });

  it("switches workspace through the server rather than locally", async () => {
    const calls = routeFetch((url, init) => {
      if (url.endsWith("/auth/switch-organization")) {
        return { body: { token: "t", expires_at: "", user: { ...OWNER, organization_id: "o2", organization_name: "Other", role: "trader" } } };
      }
      return baseRoutes()(url, init);
    });
    renderWorkspace();

    await userEvent.click(await screen.findByRole("button", { name: /switch to/i }));

    await waitFor(() => {
      const switches = calls.filter((call) => call.url.endsWith("/auth/switch-organization"));
      expect(switches).toHaveLength(1);
      expect(switches[0]!.body).toEqual({ organization_id: "o2" });
    });
  });

  it("reports a refused invitation code instead of appearing to succeed", async () => {
    routeFetch((url, init) => {
      if (url.endsWith("/organizations/invitations/accept")) {
        return {
          status: 400,
          body: { error: { code: "InvitationError", message: "That invitation is not valid. Ask for a new one." } },
        };
      }
      return baseRoutes()(url, init);
    });
    renderWorkspace();

    await userEvent.type(await screen.findByLabelText(/invitation code/i), "bad-token");
    await userEvent.click(screen.getByRole("button", { name: /join workspace/i }));

    expect(
      await screen.findByText("That invitation is not valid. Ask for a new one."),
    ).toBeInTheDocument();
  });
});
