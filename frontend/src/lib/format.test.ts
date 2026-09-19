import { describe, expect, it } from "vitest";

import { money, percent, signOf, statusTone } from "./format";

describe("money formatting", () => {
  it("keeps full precision by grouping the string, not parsing it", () => {
    // 17 significant digits: a float round-trip would lose the last one.
    expect(money("12345678901234567.89")).toBe("12,345,678,901,234,567.89");
  });

  it("does not round a value a float would", () => {
    expect(money("0.1")).toBe("0.1");
    expect(money("1000000.01")).toBe("1,000,000.01");
  });

  it("handles negatives", () => {
    expect(money("-4307.10")).toBe("-4,307.10");
  });

  it("renders an absent value as a dash rather than zero", () => {
    expect(money(null)).toBe("—");
    expect(money(undefined)).toBe("—");
    expect(money("")).toBe("—");
  });

  it("appends a currency when given one", () => {
    expect(money("1000", "INR")).toBe("1,000 INR");
  });
});

describe("percent and ratio", () => {
  it("renders null as a dash, never as zero", () => {
    expect(percent(null)).toBe("—");
    expect(percent(undefined)).toBe("—");
  });

  it("formats a ratio as a percentage", () => {
    expect(percent(0.00555517, 2)).toBe("0.56%");
  });
});

describe("signOf", () => {
  it("distinguishes gains, losses and flat", () => {
    expect(signOf("100")).toBe("pos");
    expect(signOf("-100")).toBe("neg");
    expect(signOf("0")).toBe("");
    expect(signOf(null)).toBe("");
  });
});

describe("statusTone", () => {
  it("marks failure states as errors", () => {
    for (const status of ["failed", "rejected", "halted", "revoked", "expired"]) {
      expect(statusTone(status)).toBe("err");
    }
  });

  it("marks in-flight states as warnings, not successes", () => {
    for (const status of ["queued", "running", "pending_approval", "paused"]) {
      expect(statusTone(status)).toBe("warn");
    }
  });
});
