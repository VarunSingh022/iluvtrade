import { describe, expect, it } from "vitest";

import packageJson from "../../package.json";

/**
 * The supply chain, pinned deliberately.
 *
 * Everything in `dependencies` is compiled into the bundle and executes in the
 * user's browser alongside a session cookie. Everything in `devDependencies`
 * runs only on a developer's machine. The distinction matters because an
 * advisory against `vitest` and an advisory against `react-router` are not the
 * same kind of problem, and treating them the same either panics people or
 * teaches them to ignore both.
 *
 * These tests run offline. `scripts/audit-dependencies.sh` is the part that
 * needs the registry; it is a script rather than a test so a failing network
 * never looks like a failing build.
 */
describe("the shipped dependency set", () => {
  it("is exactly the four packages that reach the browser", () => {
    // A fifth entry is a supply-chain decision, not a line to append: it means
    // one more package with script access to a signed-in session.
    expect(Object.keys(packageJson.dependencies).sort()).toEqual([
      "qrcode-generator",
      "react",
      "react-dom",
      "react-router-dom",
    ]);
  });

  it("keeps test and build tooling out of the shipped set", () => {
    const shipped = Object.keys(packageJson.dependencies);
    for (const tool of ["vitest", "vite", "esbuild", "typescript", "jsdom"]) {
      expect(shipped).not.toContain(tool);
    }
  });

  it("pins every dependency to a major version at least", () => {
    // `*`, `latest` or a git URL means the build is not reproducible and an
    // upstream release lands without review.
    for (const [name, range] of Object.entries({
      ...packageJson.dependencies,
      ...packageJson.devDependencies,
    })) {
      expect(range, `${name} has an unpinned range`).toMatch(/^[\^~]?\d+\.\d+/);
    }
  });
});
