# Research

## Data ingestion

### The principle

**Nothing is changed silently.** Every row dropped is a `RejectedRow` with its
line number, the reason and the raw text. Every value altered is a
`Transformation` with the column, the before and the after. Both are stored with
the dataset version, so "what did the importer do to my data" is answerable from
the database alone.

### Schema detection

Detection is a *proposal*, not a decision. Each field resolves to a `FieldMatch`
carrying the column chosen, how it was chosen, and a confidence — all shown
before anything is imported.

Matching is by header alias first (`close`, `ltp`, `adj_close`, `tradingsymbol`,
`vwap`, …), then by content for timestamp and symbol columns with unrecognised
names. **Aliases shorter than three characters match exactly or not at all**:
`l` is a real spelling of *low*, but prefix-matching it claims every column
starting with l — `ltp` (last traded price, a *close*) was being read as a low
price, producing a bar whose low equalled its close and a backtest quietly wrong
in a way no metric reveals.

Header detection does not trust `csv.Sniffer` alone. Its type-based heuristic is
unreliable on short files — a two-column file headed `candledate,close` is
reported as having no header, after which every column is `column_0` and
detection fails completely, for a perfectly well-formed file. Two stronger
signals are used first: a cell that is a known field name, and a first row that
is entirely non-numeric above a row that is not.

### Ambiguity is refused, not guessed

`03/04/2024` is the third of April or the fourth of March and the file does not
say. When no component in the sample exceeds 12, the importer **refuses** rather
than picking: a silently wrong date order produces a backtest that is wrong in a
way no metric reveals. When one component does exceed 12, the order is
determined and stated.

### What the timestamps mean is declared, never assumed

AlphaLab stamps a bar at the **end** of its interval — the instant its close
became knowable. A CSV does not say which instant its timestamps name, so every
import declares it, and `POST /datasets/inspect` reports what is required:

| Convention | Means | For |
|---|---|---|
| `session_date` | a bare date naming the trading session the bar summarizes | daily bars named by date |
| `interval_end` | the timestamp names the end of the bar's interval | dates or times |
| `interval_start` | the timestamp names the start (“the 09:15 bar”) | times only |

Bare dates and wall-clock times also need the IANA zone they are written in
(`Asia/Kolkata` for NSE/BSE); a zone is refused for values that carry an offset
or are epoch numbers. Every import also declares the **bar interval** (an
AlphaLab interval code — `15m`, `1h`, `1d`, …); `session_date` implies `1d`.

**The interval is never inferred.** A median or most-common gap is distorted by
missing bars, overnight and weekend gaps, holidays and mixed frequencies, so it
decides nothing. And **timestamps alone never show how long a bar is**: 30-minute
bars every 30 minutes and 15-minute bars with every other one missing have
identical timestamps. The declaration states the bar length; the observed
spacing can only *refuse* a declaration it contradicts.

**Each instrument answers for itself.** A file is one bar interval, but the
check runs separately on every instrument's own bars — one instrument's bars
never vouch for another's:

| An instrument's own bars show | Answer |
|---|---|
| two bars closer than one interval | refused, naming the instrument — mixed frequencies, or the wrong interval; a confirmation never excuses it |
| a gap that is not a whole number of intervals | refused — irregular; nothing is rounded, not by a millisecond |
| at least one pair exactly one interval apart, other gaps whole multiples | accepted; longer gaps counted (missing bars, overnight, weekends, holidays — not told apart; no calendar is assumed) |
| **no** pair one interval apart (every gap a multiple, or a single bar) | **ambiguous** — refused, naming the instrument, unless the import names it in `missing_observations_confirmed_for` |

**For an ambiguous instrument the interval is the user's declaration, not
something the data shows.** Its longer gaps fit bars of the declared interval
with observations missing and bars aggregated over a longer interval equally
well. `missing_observations_confirmed_for` lists, by symbol, the instruments for
which the importer asserts the former — that observations are genuinely missing
at the declared interval. It must name **exactly** the ambiguous instruments: a
symbol with no bars in the file, or one whose own bars already show the
interval, is refused, so it can never act as a dataset-wide waiver. In the
upload form, *Check the declared interval against this file* runs the import's
check without storing anything (`POST /datasets/inspect` with the declaration),
lists the ambiguous instruments with their bar and gap counts, explains what
ticking each one asserts, and enables *Import* only once each has been decided.
The fetch form does the same against the URL (`POST /datasets/inspect-url`).

**A confirmation is bound to the bytes and settings it was made about.** Every
interval check returns `content_sha256` (SHA-256 of the raw bytes) and
`preview_id` = SHA-256 of the canonical JSON of {identity version,
`content_sha256`, convention, zone, declared interval as validation normalizes
it, the effective cleaning policy}. An import that names any instrument in
`missing_observations_confirmed_for` must send that `preview_id`; the server
recomputes it from the bytes it is ingesting (for a fetch: the bytes *this*
fetch returned) and refuses a missing or mismatched one before storing anything,
then recomputes the ambiguity itself — the preview's own result is never
trusted. So a decision about file A can never authorize file B, even when an
instrument of the same name is ambiguous in both, and a URL whose content
changed since its check is refused. An import with no confirmation needs no
preview (an ambiguous instrument is refused anyway); a `preview_id` that is sent
is still checked. The identity is a binding, not a credential: ownership, tenant
isolation, size limits and fetch protections apply as before. In the UI every
new file selection clears the check and every tick at once (even the same file
chosen again), a change of declaration voids the check, a response for an
earlier file or an older declaration is dropped, and *Import* recomputes the
file's SHA-256 and refuses locally if it is not the checked file's. The
retired dataset-wide `missing_observations_confirmed` field answers 422.

**The limitation travels with every result.** A backtest run snapshots
`ingest.temporal_evidence` of the exact dataset version it read, when it
executes; a paper session snapshots it when it is created. The evidence states
the convention, zone, declared interval, `status` and what that status means,
and lists `user_declared_instruments`. It is stored on the run row and inside
the run's digested result document, returned in the result's
`reproducibility.temporal_evidence`, on every session, logged as a
`session.temporal_evidence` event on each start (separately from execution
assumptions), and repeated in RedDesk listing evidence from the cited run's
snapshot — never from the listing request. Statuses:

| `status` | Means |
|---|---|
| `validated` | every instrument's timestamps are consistent with the declared interval — consistency, not proof of bar length |
| `missing_observations_confirmed` | the listed instruments' interval is the importer's assertion; nobody verified it, and timestamps cannot |
| `unvalidated` / `none` | no validated provenance / failed import (nothing runs on these) |
| `not_recorded` | a run or session stored before snapshots existed; not reconstructed |
| `unavailable` | a listing cites a run that no longer exists |

A run or session stored before migration 0008 says `not_recorded`; nothing is
backfilled from today's rules, and the paper runner refuses to start such a
session rather than give it evidence it never recorded. Dataset versions are
immutable, so a later re-import or approval creates new versions and never
changes an older run's or session's snapshot.

The declaration is recorded with the version; each instrument accepted on it is
listed in the provenance and the version is shown everywhere as "user-declared
for N, not verified", with a warning beside *Approve*. It is a statement, not
evidence, and nothing describes it as verified: if it is false, those
start-stamped bars are moved by the wrong interval and a backtest sees closes
before they happened. The reviewed case shows the stakes: AAA 15-minute, BBB
hourly, declared `15m interval_start`. Unconfirmed, the import fails and nothing
runs on it. With BBB named, the importer has declared BBB to be 15-minute bars
with three of every four missing; the version is approved, backtested and
paper-run on that declaration, BBB's 09:15 bar stamped 09:30 — 45 minutes early
if BBB is in fact hourly. (Until the acceptance check, one dataset-wide checkbox,
ticked before the user could see which instruments it covered, did the same for
every ambiguous instrument at once; record version 3, never released, is
refused like the others.) One pair one interval apart is the instrument's own evidence that its
bars are not *longer* than the interval; nothing in timestamps shows they are
not shorter, and bars of different lengths in separate stretches of one
instrument's series cannot be detected at all. Those limits are why the
interval is declared.

Until the review-blocker pass the check counted one-interval pairs across the
whole file, so a 15-minute instrument validated an hourly one declared beside
it, and each hourly start-stamped bar was moved 15 minutes forward — its close
stamped 45 minutes before it happened. Versions validated that way (provenance
record version 2) are not accepted as validated provenance; they are refused
like the versions before them.

**Gaps are measured in the units the timestamps carry.**

* *Dates and zoned wall-clock times* are compared on the declared zone's wall
  clock. A daily series across a daylight-saving change is whole days apart;
  16:00 one day and 15:00 the next is not. There is no hour of slack anywhere.
* *Offset-aware and epoch timestamps* are absolute instants and are compared
  exactly, in seconds: a 23- or 25-hour gap is not a day. A series whose UTC
  offset moves with daylight saving must be uploaded as wall-clock times with
  its zone declared; the refusal says so.
* *Monthly bars* (`1M`, `3M`, …) are counted in calendar months. Every label
  must be the same day of the month, or the last day of each month, at one time
  of day; dates 27 or 45 days apart are refused as months. Monthly bars need
  dates or zoned wall-clock times, and are never start-stamped (a month has no
  fixed length to move by). `27d` and `45d` are fixed-length intervals, measured
  as such.
* A *wall-clock time the zone shows twice or never* (the autumn repeated hour,
  the spring skipped hour) names no single instant and is refused, never
  guessed.
* A *start-stamped wall-clock bar whose interval crosses a UTC-offset change*
  (a 4-hour bar starting at midnight on a spring-forward night lasts three real
  hours) is refused: adding the fixed interval would misplace its close. Its
  interval-end form, or offset-aware timestamps, are accepted.

No exchange calendar, session or holiday rule is used. The import is also
refused — before anything is stored — when the declaration is missing or
contradicts the file: mixed forms, a zone where none applies, a date declared
`interval_start`, bare dates with an intraday interval, a `session_date` with a
non-daily interval, or monthly bars with epoch or offset-aware values.

**Row order is per instrument.** A file may list instruments interleaved or
grouped (all of one symbol, then the next); both produce the same canonical
dataset, sorted by time and recorded as a reordering. A row is out of order only
when it goes backwards *within its own instrument*. Until the review-blocker
pass a single file-wide high-water mark rejected every row of a second grouped
instrument up to the first one's last timestamp.

AlphaLab does the reading (`alphalab.data.parse_timestamp`). A session date
becomes **23:59:59.999999 of that civil date in the declared zone**
(`DateOnlyPolicy.END_OF_DAY`). That is the end of the calendar day, **not** the
exchange's closing time; it is at or after the session's end only when the
session ends on that date in that zone, which is why the zone must be the
exchange's. No exchange calendar, session close or holiday rule is used. A
start-stamped bar is moved to the end of its *declared* interval by
`alphalab.market.normalize_wire_bar`, each move recorded as a transformation. The
declaration, the per-instrument spacing evidence, the named missing-observation declarations, the rule applied and the AlphaLab version are
stored on the dataset version, the raw bytes stay with the source, and the
canonical hash covers the resulting instants, so the same file read two ways is
two versions.

Until 2026-10-11 every bare date was read as UTC midnight and every wall-clock
time as UTC: an NSE daily bar reached the engine at 05:30 IST, before the open,
carrying a close not knowable until 15:30. Versions imported that way — and
versions from the first fix, whose interval was still inferred, and versions
whose interval was validated across the whole file rather than per instrument —
have no validated provenance. Approval, backtest submission, the backtest worker,
session creation and the paper runner all refuse them (the worker and runner
re-check, because a job or session created before an upgrade can name such a
version). Re-importing the retained source file with a declaration creates a new
version; it changes nothing about past backtest results.

### Values the source did not report stay absent

VWAP and trade count are kept only when the file reports a valid value — a VWAP
inside the bar's own low–high range, a non-negative whole trade count. Otherwise
they are absent (`None`, which is how AlphaLab's `Bar` represents "not reported").
The typical price or the close is never stored as a VWAP; until 2026-10-11 it
was, and those invented values entered every run's canonical record.

### Present-but-garbage ≠ absent

An empty open price may be filled from the close, and the fill is recorded. A
value of `notanumber` is **rejected**. Filling it from the close would record
the transformation as "the file did not carry this value", which is false for
that row, and would turn a data error into synthetic data that reads as
observed.

### The quality report

Counts, never estimates: duplicates, malformed rows, invalid timestamps,
out-of-order records, invalid prices, negative volumes, missing values, gaps,
symbols, date range, observed spacing.

The observed spacing (`inferred_frequency`) is the **median** gap matched to a
standard timeframe within 20%, otherwise "irregular". It is descriptive only:
it is shown as "observed spacing", separate from the bar interval, and nothing
uses it to place, move or validate a bar.

The score is a weighted penalty over those counts, and is deliberately not a
verdict: it orders datasets for attention, and the findings underneath it are
what a user actually reads. An error floors it at zero, because a dataset that
cannot be built has no quality to grade.

### Approval

A version lands in `PENDING_APPROVAL`. `ingest.require_approved()` refuses
anything else, so a backtest cannot run on data nobody reviewed. The refusal is
a 404 naming the actual status.

## Backtests

### Submission

`POST /api/v1/backtests` returns **202 Accepted**, not 201 — the run has not
happened, and a client treating it as done would read an empty result.

Idempotency has two layers: a lookup on the key catches the ordinary
double-submit, and a unique constraint plus `IntegrityError` handling catches two
concurrent requests that both passed the lookup — the case a check-then-insert
always loses. Without an explicit key, one is derived from the request content,
so "run it again" means the same run.

Everything the job depends on is validated **before** queueing, so a job that
cannot possibly run is refused at the API rather than failing in a worker where
the user has to go looking.

### Fill timing

Every backtest states when its simulated orders fill, AlphaLab's
`FillTiming`:

| `fill_timing` | Meaning |
|---|---|
| `next_event` (default) | the order fills at its instrument's next bar: the strategy decides on one bar and trades on the next — no look-ahead, one bar of delay |
| `same_event` | the order fills at the close of the bar that produced it; AlphaLab calls this optimistic by construction. Only when asked for by name |

It is part of the request's canonical form — two requests that differ only here
are two jobs with two idempotency keys and two derived seeds — and it is read back
from AlphaLab's own record of the run (`execution_assumptions`), which also lists,
in AlphaLab's words, every optimistic assumption in force (the default fill
policy assumes unlimited liquidity, and says so). The worker refuses to store a
result whose recorded timing differs from the request. Requests stored before the
setting existed read back as `same_event`, which is what they ran.

### Execution

Claiming and executing happen in **separate transactions**. A backtest can run
for minutes; holding a write transaction open for its duration would block every
other writer on SQLite. The cost is that a crash mid-run leaves the job
`RUNNING`, which `requeue_orphans()` handles at startup, bounded by
`backtest_max_attempts` so a job that reliably kills its worker is not requeued
forever.

Parameters resolve as **version defaults overlaid with request overrides**.
Validating the request alone would fill the *implementation's* defaults instead,
so a backtest recorded against strategy version X would have run parameters X
does not define — and the reproducibility record would be untrue while looking
correct.

### Seeds

A backtest that states no seed gets one derived from its request by one
documented digest (`iluvtrade.common.seeds`: SHA-256 of the canonical JSON — keys
sorted, compact separators — first 32 bits, unsigned), identical in every
process. It is written into the stored request at submission, so every attempt
reads it back rather than deriving it again. A paper session's default seed is
derived the same way from its material inputs (strategy and dataset versions,
broker account, mode, parameters, capital, currency, risk limits, fill timing)
and stored on the session. AlphaLab reports the seed beside the run's
configuration and dataset identities, so a change to any one is attributable.

### Reproducibility

A `BacktestRun` records:

| Field | Why |
|---|---|
| `dataset_version_id` + `dataset_canonical_hash` | the data, and proof it has not changed |
| `strategy_version_id` + `strategy_content_hash` | the code, and proof it has not changed |
| `parameters_json`, `universe_json` | what it was told to do |
| `configuration_json` | capital, currency, exchange and the trading-day zone its daily-loss limit keeps, risk limits, commission model |
| `seed` | the identifier stream, so identities match too |
| `engine_name` + `engine_version` | which engine produced it |
| `engine_identity` (in the stored result) | AlphaLab's own `result_id` and `configuration_id` (`lifecycle.digest_run`): digests of the run's complete record and configuration, host-independent since AlphaLab 4.1 |

Replaying reproduces a run **order id for order id**, not merely in P&L —
asserted in `tests/integration/test_backtest_reproducibility.py`, along with the
accounting identity holding exactly over `Decimal` and money never being
projected as a float. The same inputs give the same `result_id`, and the
observed driver this application runs gives the `result_id` AlphaLab's own
`BacktestEngine.run` gives (`tests/integration/test_alphalab_v4_contract.py`).

### Each run's result is its own

A run's detailed result (equity curve, orders, fills, report) is stored under the
run's own id, written atomically, names its run, and its SHA-256 is recorded on
the run. It is served only when all three check out; otherwise the response says
why and carries only the headline metrics recorded on the run. Runs stored before
2026-10-11 were all written to one shared file per organization; their detailed
results are never served as verified.

### A strategy that fails

AlphaLab isolates a strategy whose hook raises, or which emits an invalid
intent, and **continues the run without it** (its EXE-006): the valuation, the
report and the equity curve are still produced and look like a strategy that
stopped finding signals. A backtest here runs one strategy, so such a run is
not a result of that strategy. The job is recorded **failed**, with error code
`StrategyFailedDuringRun` and AlphaLab's own account of the failure, and no
result is stored. A paper session stops at the record where it happened and
ends `failed`, with a `strategy_failed` event.

### Metrics

Every figure comes from AlphaLab's `PerformanceReport`. Where AlphaLab produces
nothing, the API emits `null`. **No metric is computed in this application** — a
plausible-looking figure calculated here would be fabrication, and would be a
second accounting authority nothing reconciles against.
