import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "./api";

/** A fetch stub that records what the client actually sent. */
function stubFetch(response: { status: number; body?: unknown }) {
  const calls: { url: string; init: RequestInit }[] = [];
  const fake = vi.fn(async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    return {
      status: response.status,
      ok: response.status < 400,
      text: async () => (response.body === undefined ? "" : JSON.stringify(response.body)),
    } as Response;
  });
  vi.stubGlobal("fetch", fake);
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the API client", () => {
  it("sends the CSRF header on state-changing requests", async () => {
    const calls = stubFetch({ status: 200, body: {} });
    await api.post("/strategies", { name: "S" });

    const headers = calls[0]!.init.headers as Record<string, string>;
    expect(headers["X-Requested-With"]).toBe("XMLHttpRequest");
  });

  it("does not send the CSRF header on reads", async () => {
    const calls = stubFetch({ status: 200, body: [] });
    await api.get("/datasets");

    const headers = calls[0]!.init.headers as Record<string, string>;
    expect(headers["X-Requested-With"]).toBeUndefined();
  });

  it("always includes credentials, so the session cookie is sent", async () => {
    const calls = stubFetch({ status: 200, body: {} });
    await api.get("/auth/me");
    expect(calls[0]!.init.credentials).toBe("include");
  });

  it("raises a typed error carrying the envelope's code", async () => {
    stubFetch({
      status: 403,
      body: { error: { code: "EntitlementError", message: "No entitlement." } },
    });

    await expect(api.get("/strategies")).rejects.toMatchObject({
      name: "ApiError",
      status: 403,
      code: "EntitlementError",
      message: "No entitlement.",
    });
  });

  it("flags an unauthenticated response so the caller can redirect", async () => {
    stubFetch({ status: 401, body: { error: { code: "NotAuthenticated", message: "no" } } });

    try {
      await api.get("/auth/me");
      expect.unreachable("should have thrown");
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError);
      expect((error as ApiError).isUnauthenticated).toBe(true);
    }
  });

  it("carries validation field errors through", async () => {
    stubFetch({
      status: 422,
      body: {
        error: {
          code: "ValidationError",
          message: "password: too short",
          fields: [{ field: "password", message: "too short" }],
        },
      },
    });

    try {
      await api.post("/auth/register", {});
      expect.unreachable("should have thrown");
    } catch (error) {
      expect((error as ApiError).fields).toEqual([{ field: "password", message: "too short" }]);
    }
  });

  it("survives an error body that is not the envelope", async () => {
    stubFetch({ status: 502, body: "<html>bad gateway</html>" });
    await expect(api.get("/datasets")).rejects.toBeInstanceOf(ApiError);
  });

  it("returns undefined for 204 rather than trying to parse a body", async () => {
    stubFetch({ status: 204 });
    await expect(api.post("/auth/logout")).resolves.toBeUndefined();
  });
});

describe("browser storage", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
  });

  it("is never written by an API call", async () => {
    stubFetch({ status: 200, body: { token: "SECRET-TOKEN", user: { id: "1" } } });
    await api.post("/auth/login", { email: "a@b.com", password: "x" });

    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    expect(JSON.stringify(localStorage)).not.toContain("SECRET-TOKEN");
  });
});
