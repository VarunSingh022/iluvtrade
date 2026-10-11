import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { TemporalEvidence } from "../lib/api";
import { TemporalEvidencePanel } from "./TemporalEvidence";

// The shape `ingest.temporal_evidence` produces (backend tests pin the values).
const DECLARED: TemporalEvidence = {
  evidence_version: 1,
  status: "missing_observations_confirmed",
  meaning: "… That assertion is the user's. Neither iluvtrade nor AlphaLab verified it …",
  convention: "interval_start",
  timezone: null,
  bar_interval: "15m",
  provenance_record_version: 4,
  user_declared_instruments: ["BBB", "CCC"],
};

describe("the temporal evidence a result carries", () => {
  it("names the user-declared instruments and never calls them verified", () => {
    render(<TemporalEvidencePanel evidence={DECLARED} />);
    const panel = screen.getByTestId("temporal-evidence");
    expect(panel.textContent).toContain("user-declared, not verified");
    expect(panel.textContent).toContain("User-declared missing observations, not verified, for: BBB, CCC");
    expect(panel.textContent).toContain("15m");
    expect(panel.textContent).not.toMatch(/consistent with timestamps/);
  });

  it("describes a validated interval as consistency, not proof", () => {
    render(<TemporalEvidencePanel evidence={{ ...DECLARED, status: "validated", user_declared_instruments: [], meaning: "consistency with the observations, not proof." }} />);
    const panel = screen.getByTestId("temporal-evidence");
    expect(panel.textContent).toContain("consistent with timestamps");
    expect(panel.textContent).not.toContain("User-declared");
  });

  it.each([
    ["not_recorded", "not recorded"],
    ["unavailable", "unavailable"],
    ["unvalidated", "no validated provenance"],
  ] as const)("shows %s records without an interval claim", (status, label) => {
    render(<TemporalEvidencePanel evidence={{ evidence_version: 1, status, meaning: "…" }} />);
    const panel = screen.getByTestId("temporal-evidence");
    expect(panel.textContent).toContain(label);
    expect(panel.textContent).toContain("Bar interval: —");
  });
});
