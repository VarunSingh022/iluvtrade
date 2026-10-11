import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BarInterval } from "./BarInterval";

describe("the bar interval a version shows", () => {
  it("is the declared interval, described as consistent rather than proven", () => {
    render(<BarInterval version={{ bar_interval: "15m", interval_status: "validated", accepted_on_confirmation: [] }} />);
    expect(screen.getByText("15m")).toBeInTheDocument();
    expect(screen.queryByText(/confirmed/)).not.toBeInTheDocument();
    expect(screen.getByText("15m").getAttribute("title")).toContain("timestamps cannot prove bar length");
  });

  it("says when instruments were accepted only on the missing-observation confirmation", () => {
    const { container } = render(
      <BarInterval
        version={{ bar_interval: "15m", interval_status: "missing_observations_confirmed", accepted_on_confirmation: ["BBB", "CCC"] }}
      />,
    );
    expect(screen.getByText("15m")).toBeInTheDocument();
    expect(screen.getByText("user-declared for 2, not verified")).toBeInTheDocument();
    expect(container.querySelector("[title*='BBB, CCC: accepted only on the importer']")).not.toBeNull();
  });

  it("shows no interval for a version without validated provenance", () => {
    render(<BarInterval version={{ bar_interval: null, interval_status: "unvalidated", accepted_on_confirmation: [] }} />);
    expect(screen.getByText("not validated")).toBeInTheDocument();
  });

  it("shows nothing for a failed import", () => {
    render(<BarInterval version={{ bar_interval: null, interval_status: "none", accepted_on_confirmation: [] }} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });
});
