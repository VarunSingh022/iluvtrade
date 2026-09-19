import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ModeBadge, StatusBadge } from "./ui";

describe("ModeBadge", () => {
  it("renders paper and live with visibly distinct treatments", () => {
    const { container: paper } = render(<ModeBadge mode="paper" />);
    const paperClass = paper.querySelector(".badge")!.className;

    const { container: live } = render(<ModeBadge mode="live" />);
    const liveClass = live.querySelector(".badge")!.className;

    // The classes carry the distinction; a shared class would make the two
    // modes indistinguishable at a glance, which PHASE 14 forbids.
    expect(paperClass).toContain("paper");
    expect(liveClass).toContain("live");
    expect(paperClass).not.toBe(liveClass);
  });

  it("always shouts the mode in upper case", () => {
    render(<ModeBadge mode="live" />);
    expect(screen.getByText("LIVE")).toBeInTheDocument();
  });
});

describe("StatusBadge", () => {
  it("never shows a failure state as a success tone", () => {
    for (const status of ["failed", "halted", "rejected"]) {
      const { container, unmount } = render(<StatusBadge status={status} />);
      expect(container.querySelector(".badge")!.className).toContain("err");
      unmount();
    }
  });

  it("shows an in-flight state as a warning rather than done", () => {
    const { container } = render(<StatusBadge status="running" />);
    expect(container.querySelector(".badge")!.className).toContain("warn");
  });
});
