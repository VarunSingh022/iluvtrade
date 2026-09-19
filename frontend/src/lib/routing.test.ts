import { describe, expect, it } from "vitest";

/**
 * The premise behind accepting the react-router advisory, enforced.
 *
 * Two advisories are open against `react-router` 6.30.6, which ships in the
 * bundle. Both were assessed as unreachable *in this application*, and the fix
 * for either is a major version bump. That assessment is only worth anything
 * while it stays true, so it is a test rather than a paragraph:
 *
 * 1. **Arbitrary constructor injection via `deserializeErrors()` in SSR
 *    hydration.** Needs the data-router APIs. This app uses `BrowserRouter`
 *    and has no SSR.
 * 2. **Open redirect via a backslash in `<Link>` / `useNavigate`.** Needs a
 *    route target built from user-controlled input. Every target here is a
 *    literal path or a server-generated id.
 *
 * If either premise stops holding, this fails and the upgrade stops being
 * optional. See docs/SECURITY.md, "Dependency risk".
 */

/**
 * Every source file, read through Vite rather than through `node:fs`.
 *
 * `import.meta.glob` is resolved at build time, so this needs no Node types
 * and no filesystem access — which keeps `tsc --noEmit` working without adding
 * `@types/node` to a browser project purely for one test.
 */
const modules = import.meta.glob("../**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

const sources = Object.entries(modules)
  .filter(([path]) => !/\.test\.tsx?$/.test(path))
  .map(([path, text]) => ({ path, text }));

describe("the react-router advisory premises", () => {
  it("uses no data-router or SSR API, so the hydration advisory cannot apply", () => {
    const ssrApis = [
      "createBrowserRouter",
      "createMemoryRouter",
      "createStaticRouter",
      "RouterProvider",
      "StaticRouterProvider",
      "StaticRouter",
      "hydrateRoot",
      "renderToString",
      "deserializeErrors",
    ];
    const found = sources.flatMap(({ path, text }) =>
      ssrApis.filter((api) => text.includes(api)).map((api) => `${path}: ${api}`),
    );
    expect(found).toEqual([]);
  });

  it("builds every route target from a literal or an id, never from user input", () => {
    // A target is safe when it is a quoted literal, or a template whose only
    // interpolations are `<something>.id`-shaped — a value the server issued,
    // not text a person typed.
    const targets = sources.flatMap(({ path, text }) => {
      const matches = text.match(
        /(?:to=\{`[^`]+`\}|to="[^"]+"|navigate\(\s*`[^`]+`\s*\)|navigate\(\s*"[^"]+"\s*\))/g,
      );
      return (matches ?? []).map((target) => ({ path, target }));
    });

    expect(targets.length).toBeGreaterThan(10);

    const unsafe = targets.filter(({ target }) => {
      const literal = /^(?:to=|navigate\(\s*)"\/[A-Za-z0-9/_-]*"\)?$/.test(target);
      if (literal) return false;
      const interpolations = target.match(/\$\{([^}]+)\}/g) ?? [];
      // Every interpolation must end in an id-shaped property.
      return !interpolations.every((slot) => /\.(id|[a-z_]*_id)\s*\}$/.test(slot));
    });

    expect(unsafe).toEqual([]);
  });

  it("never routes to a value read from the address bar or a form", () => {
    // The concrete shape of the open redirect: a redirect target taken from a
    // query parameter, which is how "?next=..." becomes an open redirect.
    const smells = ["searchParams.get", "location.search", "window.location.href ="];
    const found = sources.flatMap(({ path, text }) =>
      smells.filter((smell) => text.includes(smell)).map((smell) => `${path}: ${smell}`),
    );
    expect(found).toEqual([]);
  });
});
