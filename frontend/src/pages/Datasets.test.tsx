import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { sha256Hex } from "../components/IntervalCheckPanel";
import { ExecutionPanel } from "./BacktestDetail";
import DatasetsPage from "./Datasets";

const DETECTED = {
  columns: ["Date", "Symbol", "Open", "High", "Low", "Close", "Volume"],
  delimiter: ",",
  has_header: true,
  fields: {},
  timestamp_format: "date_only",
  timestamp_ambiguous: false,
  missing_required: [],
  ohlc_complete: true,
  sample_rows: [],
  warnings: [],
  timestamp_semantics: {
    kind: "date",
    kinds: { date: 5 },
    requires_timezone: true,
    conventions: ["session_date", "interval_end"],
  },
};

function version(overrides: Record<string, unknown>) {
  return {
    id: "v",
    dataset_id: "d",
    dataset_name: "bars",
    version: 1,
    status: "pending_approval",
    row_count: 10,
    rejected_row_count: 0,
    symbol_count: 2,
    start_timestamp: 1704167100,
    end_timestamp: 1704177900,
    inferred_frequency: "1h",
    bar_interval: null,
    interval_status: "none",
    accepted_on_confirmation: [],
    quality_score: 90,
    canonical_hash: null,
    failure_reason: null,
    created_at: "2026-10-11T00:00:00Z",
    approved_at: null,
    ...overrides,
  };
}

const CONSISTENT = { refused: null, spacing: null, ambiguous: [] };

interface StubOptions {
  /** Answer the upload with this error instead of creating a version. */
  uploadError?: string;
  /** Answer the fetch with this error. */
  fetchError?: string;
  /** Hold a response until the returned promise settles (to reorder responses). */
  hold?: (url: string, form: FormData | undefined) => Promise<void> | undefined;
  /** Override the content digest the server reports (a file that is not the one checked). */
  reportedDigest?: string;
}

/**
 * A stand-in for the API that answers as the server does: an interval check
 * carries the SHA-256 of the bytes actually sent and a preview_id that changes
 * with the bytes and the declaration.
 */
function stubApi(
  detected: unknown = DETECTED,
  datasets: unknown = [],
  intervalCheck: unknown = CONSISTENT,
  options: StubOptions = {},
) {
  const uploads: FormData[] = [];
  const checks: FormData[] = [];
  const fetches: Record<string, unknown>[] = [];
  const urlChecks: Record<string, unknown>[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const form = init.body instanceof FormData ? init.body : undefined;
      await options.hold?.(url, form);
      let status = 200;
      let body: unknown = url.endsWith("/datasets") ? datasets : [];
      if (url.endsWith("/datasets/inspect") && form) {
        if (form.get("timestamp_convention")) {
          checks.push(form);
          const digest = options.reportedDigest ?? (await sha256Hex(form.get("file") as File));
          const settings = ["timestamp_convention", "bar_interval", "timezone"].map((k) => form.get(k) ?? "").join("|");
          const result = typeof intervalCheck === "function" ? intervalCheck(form) : intervalCheck;
          body = {
            ...(detected as object),
            interval_check: { ...(result as object), content_sha256: digest, preview_id: `preview:${digest}:${settings}` },
          };
        } else body = detected;
      }
      if (url.endsWith("/datasets/inspect-url")) {
        const json = JSON.parse(String(init.body)) as Record<string, unknown>;
        urlChecks.push(json);
        const result = typeof intervalCheck === "function" ? intervalCheck(undefined) : intervalCheck;
        body = {
          ...(detected as object),
          interval_check: { ...(result as object), content_sha256: "remote", preview_id: `preview:remote:${String(json.url)}` },
        };
      }
      if (url.endsWith("/datasets/upload")) {
        uploads.push(init.body as FormData);
        if (options.uploadError) {
          status = 400;
          body = { error: { code: "IngestError", message: options.uploadError } };
        } else body = { id: "v1", version: 1, row_count: 5 };
      }
      if (url.endsWith("/datasets/fetch")) {
        fetches.push(JSON.parse(String(init.body)) as Record<string, unknown>);
        if (options.fetchError) {
          status = 400;
          body = { error: { code: "IngestError", message: options.fetchError } };
        } else body = { id: "v2", version: 2, row_count: 5 };
      }
      return {
        status,
        ok: status < 400,
        text: async () => JSON.stringify(body),
        json: async () => body,
      } as Response;
    }),
  );
  return Object.assign(uploads, { checks, fetches, urlChecks });
}

async function checkTheInterval() {
  await userEvent.click(screen.getByRole("button", { name: "Check the declared interval against this file" }));
}

afterEach(() => vi.unstubAllGlobals());

describe("importing a dataset", () => {
  it("cannot import until the timestamps' meaning and zone are declared, and sends both", async () => {
    const uploads = stubApi();
    const { container } = render(
      <MemoryRouter>
        <DatasetsPage />
      </MemoryRouter>,
    );
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    await userEvent.upload(input, new File(["Date,Close\n"], "nse.csv", { type: "text/csv" }));

    const importButton = await screen.findByRole("button", { name: "Import and run quality checks" });
    expect(importButton).toBeDisabled();

    // Only the conventions that fit bare dates are offered; interval_start is not.
    const [convention] = screen.getAllByLabelText("Timestamp convention") as HTMLSelectElement[];
    if (!convention) throw new Error("no timestamp convention control");
    const offered = Array.from(convention.options).map((option) => option.value);
    expect(offered).toEqual(["", "session_date", "interval_end"]);

    await userEvent.selectOptions(convention, "session_date");
    expect(importButton).toBeDisabled(); // a bare date names no instant without a zone

    const [ist] = screen.getAllByRole("button", { name: "NSE / BSE: Asia/Kolkata" });
    if (!ist) throw new Error("no zone quick-pick");
    await userEvent.click(ist);
    expect(importButton).toBeDisabled(); // not until the declaration is checked against the file
    await checkTheInterval();
    await waitFor(() => expect(importButton).toBeEnabled());
    expect(screen.getByText(/remains your declaration/)).toBeInTheDocument();

    await userEvent.click(importButton);
    await waitFor(() => expect(uploads).toHaveLength(1));
    const [sent] = uploads;
    expect(sent?.get("timestamp_convention")).toBe("session_date");
    expect(sent?.get("timezone")).toBe("Asia/Kolkata");
  });
});

describe("declaring a bar interval", () => {
  it("requires the interval for time-stamped bars and never offers a guess", async () => {
    const uploads = stubApi({
      ...DETECTED,
      timestamp_format: "iso_8601",
      timestamp_semantics: {
        kind: "aware",
        kinds: { aware: 12 },
        requires_timezone: false,
        conventions: ["interval_start", "interval_end"],
      },
    });
    const { container } = render(
      <MemoryRouter>
        <DatasetsPage />
      </MemoryRouter>,
    );
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    await userEvent.upload(input, new File(["Timestamp,Close\n"], "tcs.csv", { type: "text/csv" }));
    const importButton = await screen.findByRole("button", { name: "Import and run quality checks" });

    const [convention] = screen.getAllByLabelText("Timestamp convention") as HTMLSelectElement[];
    if (!convention) throw new Error("no timestamp convention control");
    await userEvent.selectOptions(convention, "interval_start");
    // Offset-aware values name instants: no zone is asked for (and the fetch
    // card's own declaration is hidden while an upload is being declared).
    expect(screen.queryAllByLabelText("Time zone")).toHaveLength(0);
    expect(importButton).toBeDisabled();

    const [interval] = screen.getAllByLabelText("Bar interval") as HTMLSelectElement[];
    if (!interval) throw new Error("no bar interval control");
    expect(interval.value).toBe(""); // nothing preselected
    await userEvent.selectOptions(interval, "15m");
    expect(importButton).toBeDisabled();
    await checkTheInterval();
    await waitFor(() => expect(importButton).toBeEnabled());

    await userEvent.click(importButton);
    await waitFor(() => expect(uploads).toHaveLength(1));
    const [sent] = uploads;
    expect(sent?.get("timestamp_convention")).toBe("interval_start");
    expect(sent?.get("bar_interval")).toBe("15m");
    expect(sent?.get("timezone")).toBeNull();
  });
});

describe("declaring missing observations", () => {
  const AWARE = {
    ...DETECTED,
    timestamp_format: "iso_8601",
    timestamp_semantics: {
      kind: "aware",
      kinds: { aware: 16 },
      requires_timezone: false,
      conventions: ["interval_start", "interval_end"],
    },
  };
  const AMBIGUOUS = {
    refused: null,
    bar_interval: "15m",
    ambiguous: ["BBB"],
    spacing: {
      arithmetic: "absolute",
      instruments: {
        AAA: { bars: 12, one_interval_pairs: 11, longer_gaps: 0, accepted_on_confirmation: false },
        BBB: { bars: 4, one_interval_pairs: 0, longer_gaps: 3, accepted_on_confirmation: false },
      },
      instrument_count: 2,
      one_interval_pairs: 11,
      longer_gaps: 3,
      accepted_on_confirmation: [],
      ambiguous: ["BBB"],
      missing_observations_confirmed_for: [],
    },
  };

  async function declareFifteenMinutes(container: HTMLElement) {
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    await userEvent.upload(input, new File(["Timestamp,Close\n"], "multi.csv", { type: "text/csv" }));
    await screen.findByRole("button", { name: "Import and run quality checks" });
    const [convention] = screen.getAllByLabelText("Timestamp convention") as HTMLSelectElement[];
    if (!convention) throw new Error("no timestamp convention control");
    await userEvent.selectOptions(convention, "interval_start");
    const [interval] = screen.getAllByLabelText("Bar interval") as HTMLSelectElement[];
    if (!interval) throw new Error("no bar interval control");
    await userEvent.selectOptions(interval, "15m");
    return interval;
  }

  it("names each ambiguous instrument, says what ticking it asserts, and sends only those named", async () => {
    const uploads = stubApi(AWARE, [], AMBIGUOUS);
    const { container } = render(
      <MemoryRouter>
        <DatasetsPage />
      </MemoryRouter>,
    );
    await declareFifteenMinutes(container);
    // No dataset-wide confirmation exists any more.
    expect(screen.queryByLabelText("Gaps are missing observations")).not.toBeInTheDocument();
    await checkTheInterval();

    const box = (await screen.findByLabelText("Missing observations: BBB")) as HTMLInputElement;
    expect(box.checked).toBe(false);
    expect(screen.queryByLabelText("Missing observations: AAA")).not.toBeInTheDocument();
    const warning = screen.getByText(/is your declared nominal bar interval/).closest(".banner")?.textContent ?? "";
    expect(warning).toContain("15m is your declared nominal bar interval");
    expect(warning).toContain("bars aggregated over a longer interval equally well");
    expect(warning).toContain("asserts that its observations are genuinely missing at 15m");
    expect(warning).toContain("only to that instrument in this file");
    expect(warning).toContain("never treated as verified");
    expect(warning).toContain("backtests see closes before they happened");
    expect(warning).toContain("4 bars, 3 longer gaps, none one interval apart");

    const importButton = screen.getByRole("button", { name: "Import and run quality checks" });
    expect(importButton).toBeDisabled(); // BBB undecided
    await userEvent.click(box);
    expect(importButton).toBeEnabled();
    await userEvent.click(importButton);
    await waitFor(() => expect(uploads).toHaveLength(1));
    expect(uploads[0]?.getAll("missing_observations_confirmed_for")).toEqual(["BBB"]);
    expect(uploads[0]?.get("missing_observations_confirmed")).toBeNull();
    expect(uploads.checks[0]?.get("bar_interval")).toBe("15m");
    const digest = await sha256Hex(uploads[0]?.get("file") as File);
    expect(uploads[0]?.get("preview_id")).toBe(`preview:${digest}:interval_start|15m|`);
  });

  it("voids the check and the declarations when the declared interval changes", async () => {
    stubApi(AWARE, [], AMBIGUOUS);
    const { container } = render(
      <MemoryRouter>
        <DatasetsPage />
      </MemoryRouter>,
    );
    const interval = await declareFifteenMinutes(container);
    await checkTheInterval();
    await userEvent.click(await screen.findByLabelText("Missing observations: BBB"));
    await userEvent.selectOptions(interval, "1h");
    expect(screen.queryByLabelText("Missing observations: BBB")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Import and run quality checks" })).toBeDisabled();
  });

  async function choose(container: HTMLElement, content: string, name = "bars.csv") {
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    await userEvent.upload(input, new File([content], name, { type: "text/csv" }));
    await screen.findByRole("button", { name: "Import and run quality checks" });
  }

  async function declare(intervalCode = "15m") {
    const [convention] = screen.getAllByLabelText("Timestamp convention") as HTMLSelectElement[];
    if (!convention) throw new Error("no timestamp convention control");
    await userEvent.selectOptions(convention, "interval_start");
    const [interval] = screen.getAllByLabelText("Bar interval") as HTMLSelectElement[];
    if (!interval) throw new Error("no bar interval control");
    await userEvent.selectOptions(interval, intervalCode);
    return interval;
  }

  const importButton = () => screen.getByRole("button", { name: "Import and run quality checks" });

  it("never carries file A's check or BBB tick over to file B (the reviewed failure)", async () => {
    const uploads = stubApi(AWARE, [], AMBIGUOUS);
    const { container } = render(<MemoryRouter><DatasetsPage /></MemoryRouter>);
    await choose(container, "Timestamp,Close\nA\n");
    await declare();
    await checkTheInterval();
    await userEvent.click(await screen.findByLabelText("Missing observations: BBB"));
    expect(importButton()).toBeEnabled();

    // File B: same file name, different bytes.
    await choose(container, "Timestamp,Close\nB\n");
    expect(screen.queryByLabelText("Missing observations: BBB")).not.toBeInTheDocument();
    expect(importButton()).toBeDisabled();
    await declare();
    expect(screen.queryByLabelText("Missing observations: BBB")).not.toBeInTheDocument();
    expect(importButton()).toBeDisabled(); // not until B itself is checked

    await checkTheInterval();
    const box = (await screen.findByLabelText("Missing observations: BBB")) as HTMLInputElement;
    expect(box.checked).toBe(false); // B's own decision, not A's
    await userEvent.click(box);
    await userEvent.click(importButton());
    await waitFor(() => expect(uploads).toHaveLength(1));
    const sent = uploads[0];
    const fileB = sent?.get("file") as File;
    const digestB = await sha256Hex(fileB);
    expect(await sha256Hex(uploads.checks[1]?.get("file") as File)).toBe(digestB);
    expect(digestB).not.toBe(await sha256Hex(uploads.checks[0]?.get("file") as File));
    expect(sent?.get("preview_id")).toBe(`preview:${digestB}:interval_start|15m|`);
    expect(sent?.getAll("missing_observations_confirmed_for")).toEqual(["BBB"]);
    expect(sent?.get("missing_observations_confirmed")).toBeNull();
  });

  it("asks for a new check when the user switches away and back to the same file", async () => {
    stubApi(AWARE, [], AMBIGUOUS);
    const { container } = render(<MemoryRouter><DatasetsPage /></MemoryRouter>);
    await choose(container, "Timestamp,Close\nA\n");
    await declare();
    await checkTheInterval();
    await userEvent.click(await screen.findByLabelText("Missing observations: BBB"));
    await choose(container, "Timestamp,Close\nB\n");
    await choose(container, "Timestamp,Close\nA\n"); // the same bytes, chosen again
    await declare();
    expect(screen.queryByLabelText("Missing observations: BBB")).not.toBeInTheDocument();
    expect(importButton()).toBeDisabled();
  });

  it("binds the check to the declaration: another interval is another preview", async () => {
    const uploads = stubApi(AWARE, [], AMBIGUOUS);
    const { container } = render(<MemoryRouter><DatasetsPage /></MemoryRouter>);
    await choose(container, "Timestamp,Close\nA\n");
    const interval = await declare("15m");
    await checkTheInterval();
    await screen.findByLabelText("Missing observations: BBB");
    await userEvent.selectOptions(interval, "30m");
    expect(screen.queryByLabelText("Missing observations: BBB")).not.toBeInTheDocument();
    await checkTheInterval();
    await userEvent.click(await screen.findByLabelText("Missing observations: BBB"));
    await userEvent.click(importButton());
    await waitFor(() => expect(uploads).toHaveLength(1));
    expect(uploads[0]?.get("bar_interval")).toBe("30m");
    expect(String(uploads[0]?.get("preview_id"))).toMatch(/:interval_start\|30m\|$/);
  });

  it("drops a slower check response for an earlier file", async () => {
    let releaseA: () => void = () => undefined;
    const heldA = new Promise<void>((resolve) => {
      releaseA = resolve;
    });
    const uploads = stubApi(
      AWARE,
      [],
      (form: FormData | undefined) => ((form?.get("file") as File | null)?.name === "a.csv" ? AMBIGUOUS : CONSISTENT),
      {
        hold: (url, form) =>
          url.endsWith("/datasets/inspect") && form?.get("timestamp_convention") && (form.get("file") as File).name === "a.csv"
            ? heldA
            : undefined,
      },
    );
    const { container } = render(<MemoryRouter><DatasetsPage /></MemoryRouter>);
    await choose(container, "Timestamp,Close\nA\n", "a.csv");
    await declare();
    await userEvent.click(screen.getByRole("button", { name: "Check the declared interval against this file" }));
    // A's check is still in flight when B is chosen and checked.
    await choose(container, "Timestamp,Close\nB\n", "b.csv");
    await declare();
    await checkTheInterval();
    await waitFor(() => expect(importButton()).toBeEnabled()); // B: nothing ambiguous
    releaseA();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByLabelText("Missing observations: BBB")).not.toBeInTheDocument();
    await userEvent.click(importButton());
    await waitFor(() => expect(uploads).toHaveLength(1));
    expect((uploads[0]?.get("file") as File).name).toBe("b.csv");
    expect(uploads[0]?.getAll("missing_observations_confirmed_for")).toEqual([]);
  });

  it("drops a slower file inspection for an earlier file", async () => {
    let releaseA: () => void = () => undefined;
    const heldA = new Promise<void>((resolve) => {
      releaseA = resolve;
    });
    const uploads = stubApi(AWARE, [], CONSISTENT, {
      hold: (url, form) =>
        url.endsWith("/datasets/inspect") && !form?.get("timestamp_convention") && (form?.get("file") as File).name === "a.csv"
          ? heldA
          : undefined,
    });
    const { container } = render(<MemoryRouter><DatasetsPage /></MemoryRouter>);
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    // A's inspection is slow; B is chosen and inspected before it returns.
    await userEvent.upload(input, new File(["Timestamp,Close\nA\n"], "a.csv", { type: "text/csv" }));
    await choose(container, "Timestamp,Close\nB\n", "b.csv");
    releaseA();
    await new Promise((resolve) => setTimeout(resolve, 20));
    await declare();
    await checkTheInterval();
    await waitFor(() => expect(importButton()).toBeEnabled());
    await userEvent.click(importButton());
    await waitFor(() => expect(uploads).toHaveLength(1));
    expect((uploads[0]?.get("file") as File).name).toBe("b.csv");
    expect((uploads.checks[0]?.get("file") as File).name).toBe("b.csv");
  });

  it("ignores a check that returns after the declaration changed", async () => {
    let releaseFirst: () => void = () => undefined;
    const heldFirst = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });
    const uploads = stubApi(AWARE, [], AMBIGUOUS, {
      hold: (url, form) =>
        url.endsWith("/datasets/inspect") && form?.get("bar_interval") === "15m" ? heldFirst : undefined,
    });
    const { container } = render(<MemoryRouter><DatasetsPage /></MemoryRouter>);
    await choose(container, "Timestamp,Close\nA\n");
    const interval = await declare("15m");
    await userEvent.click(screen.getByRole("button", { name: "Check the declared interval against this file" }));
    // While the 15m check is in flight, the user changes the interval.
    await userEvent.selectOptions(interval, "30m");
    releaseFirst();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByLabelText("Missing observations: BBB")).not.toBeInTheDocument();
    expect(importButton()).toBeDisabled(); // the 15m result does not count for 30m
    await checkTheInterval();
    await userEvent.click(await screen.findByLabelText("Missing observations: BBB"));
    await userEvent.click(importButton());
    await waitFor(() => expect(uploads).toHaveLength(1));
    expect(String(uploads[0]?.get("preview_id"))).toMatch(/\|30m\|$/);
  });

  it("enables import after a check that finds nothing ambiguous, for that file only", async () => {
    const uploads = stubApi(AWARE, [], CONSISTENT);
    const { container } = render(<MemoryRouter><DatasetsPage /></MemoryRouter>);
    await choose(container, "Timestamp,Close\nA\n");
    await declare();
    await checkTheInterval();
    await waitFor(() => expect(importButton()).toBeEnabled());
    await choose(container, "Timestamp,Close\nB\n");
    await declare();
    expect(importButton()).toBeDisabled(); // A's clean result is not B's
    expect(uploads).toHaveLength(0);
  });

  it("refuses locally when the selected file's digest is not the checked one", async () => {
    const uploads = stubApi(AWARE, [], AMBIGUOUS, { reportedDigest: "0".repeat(64) });
    const { container } = render(<MemoryRouter><DatasetsPage /></MemoryRouter>);
    await choose(container, "Timestamp,Close\nA\n");
    await declare();
    await checkTheInterval();
    await userEvent.click(await screen.findByLabelText("Missing observations: BBB"));
    await userEvent.click(importButton());
    expect(await screen.findByText(/not the one that was checked/)).toBeInTheDocument();
    expect(uploads).toHaveLength(0);
    expect(screen.queryByLabelText("Missing observations: BBB")).not.toBeInTheDocument();
  });

  it("shows the server's refusal when the content does not match the preview", async () => {
    const uploads = stubApi(AWARE, [], AMBIGUOUS, {
      uploadError: "The preview_id does not match this file and these settings.",
    });
    const { container } = render(<MemoryRouter><DatasetsPage /></MemoryRouter>);
    await choose(container, "Timestamp,Close\nA\n");
    await declare();
    await checkTheInterval();
    await userEvent.click(await screen.findByLabelText("Missing observations: BBB"));
    await userEvent.click(importButton());
    expect(await screen.findByText(/does not match this file and these settings/)).toBeInTheDocument();
    expect(uploads).toHaveLength(1);
    expect(screen.queryByText(/awaiting your approval/)).not.toBeInTheDocument();
  });

  it("binds a URL check to the URL, and shows a refusal when the resource changed", async () => {
    const uploads = stubApi(AWARE, [], AMBIGUOUS, {
      fetchError: "The preview_id does not match this file and these settings: the file's bytes differ.",
    });
    render(<MemoryRouter><DatasetsPage /></MemoryRouter>);
    const urlInput = screen.getByPlaceholderText("https://…");
    await userEvent.type(urlInput, "https://data.example.com/bars.csv");
    await declare();
    const fetchButton = screen.getByRole("button", { name: "Fetch" });
    expect(fetchButton).toBeDisabled(); // not before the URL is checked
    await userEvent.click(screen.getByRole("button", { name: "Check the declared interval against this URL" }));
    await userEvent.click(await screen.findByLabelText("Missing observations (URL): BBB"));
    expect(fetchButton).toBeEnabled();
    await userEvent.click(fetchButton);
    expect(await screen.findByText(/the file's bytes differ/)).toBeInTheDocument();
    expect(uploads.fetches[0]).toMatchObject({
      url: "https://data.example.com/bars.csv",
      preview_id: "preview:remote:https://data.example.com/bars.csv",
      missing_observations_confirmed_for: ["BBB"],
    });
    expect(uploads.fetches[0]).not.toHaveProperty("missing_observations_confirmed");

    await userEvent.type(urlInput, "?v=2"); // another resource
    expect(screen.queryByLabelText("Missing observations (URL): BBB")).not.toBeInTheDocument();
    expect(fetchButton).toBeDisabled();
  });

  it("shows a refused declaration and allows no import", async () => {
    stubApi(AWARE, [], { refused: "Two BBB bars are 5m apart, less than the declared 15m", spacing: null, ambiguous: [] });
    const { container } = render(
      <MemoryRouter>
        <DatasetsPage />
      </MemoryRouter>,
    );
    await declareFifteenMinutes(container);
    await checkTheInterval();
    expect(await screen.findByText(/Two BBB bars are 5m apart/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Import and run quality checks" })).toBeDisabled();
  });
});

describe("the dataset list", () => {
  it("shows the declared interval, never the observed spacing", async () => {
    stubApi(DETECTED, [
      {
        id: "d",
        slug: "bars",
        name: "bars",
        description: "",
        created_at: "2026-10-11T00:00:00Z",
        versions: [
          version({ id: "a", version: 1, bar_interval: "15m", interval_status: "validated" }),
          version({
            id: "b",
            version: 2,
            bar_interval: "15m",
            interval_status: "missing_observations_confirmed",
            accepted_on_confirmation: ["BBB"],
          }),
          version({ id: "c", version: 3, interval_status: "unvalidated" }),
          version({
            id: "e",
            version: 4,
            status: "failed",
            interval_status: "none",
            failure_reason: "The declared bar interval does not fit the data: BBB: no two consecutive bars",
          }),
        ],
      },
    ]);
    render(
      <MemoryRouter>
        <DatasetsPage />
      </MemoryRouter>,
    );
    expect(await screen.findByText("Bar interval", { selector: "th" })).toBeInTheDocument();
    const headings = screen.getAllByRole("columnheader").map((cell) => cell.textContent);
    const column = headings.indexOf("Bar interval");
    const rows = screen.getAllByRole("row").slice(1);
    const interval = (index: number) => rows[index]?.querySelectorAll("td")[column]?.textContent ?? "";
    expect(interval(0)).toBe("15m");
    expect(interval(1)).toBe("15m user-declared for 1, not verified");
    expect(interval(2)).toBe("not validated");
    expect(interval(3)).toBe("—");
    // Every version's observed spacing is 1h; it is not shown anywhere in the list.
    for (const row of rows) expect(row.textContent).not.toContain("1h");
  });
});

describe("execution assumptions on a result", () => {
  it("shows the fill timing and every optimistic assumption AlphaLab recorded", () => {
    render(
      <ExecutionPanel
        requested="same_event"
        assumptions={{
          fill_timing: "same_event",
          routing: "SIMULATED",
          fill_policy: "ImmediateFill",
          costs: "PercentageCommission(0.0003)",
          latency: "none",
          frictionless: false,
          unlimited_liquidity: true,
          optimistic: [
            "orders fill at the price of the event that decided them (FillTiming.SAME_EVENT)",
            "liquidity is unlimited: every order fills in full (ImmediateFill)",
          ],
        }}
      />,
    );
    expect(screen.getByText(/Same bar \(optimistic\)/)).toBeInTheDocument();
    expect(screen.getByText(/FillTiming.SAME_EVENT/)).toBeInTheDocument();
    expect(screen.getByText(/liquidity is unlimited/)).toBeInTheDocument();
  });

  it("says so when a result predates recorded assumptions", () => {
    render(<ExecutionPanel requested={undefined} assumptions={undefined} />);
    expect(screen.getByText(/stored before execution assumptions were recorded/)).toBeInTheDocument();
    expect(screen.getByText(/Same bar \(optimistic\)/)).toBeInTheDocument();
  });
});
