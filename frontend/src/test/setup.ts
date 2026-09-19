import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// React Testing Library does not unmount between tests on its own when
// `globals` is enabled, and a leaked component keeps its timers running.
afterEach(cleanup);
