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

Naive timestamps are read as UTC. That is a *stated* assumption rather than a
guess — the canonical model is epoch seconds, a CSV without an offset carries no
timezone, and inventing the uploader's local zone would shift every bar by an
amount nothing records.

### Present-but-garbage ≠ absent

An empty open price may be filled from the close, and the fill is recorded. A
value of `notanumber` is **rejected**. Filling it from the close would record
the transformation as "the file did not carry this value", which is false for
that row, and would turn a data error into synthetic data that reads as
observed.

### The quality report

Counts, never estimates: duplicates, malformed rows, invalid timestamps,
out-of-order records, invalid prices, negative volumes, missing values, gaps,
symbols, date range, inferred frequency.

Frequency uses the **median** gap, so a weekend or a trading halt does not
decide the answer. A spacing that matches no standard timeframe within 20% is
reported as irregular rather than snapped to the nearest one.

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

### Reproducibility

A `BacktestRun` records:

| Field | Why |
|---|---|
| `dataset_version_id` + `dataset_canonical_hash` | the data, and proof it has not changed |
| `strategy_version_id` + `strategy_content_hash` | the code, and proof it has not changed |
| `parameters_json`, `universe_json` | what it was told to do |
| `configuration_json` | capital, currency, risk limits, commission model |
| `seed` | the identifier stream, so identities match too |
| `engine_name` + `engine_version` | which engine produced it |

Replaying reproduces a run **order id for order id**, not merely in P&L —
asserted in `tests/integration/test_backtest_reproducibility.py`, along with the
accounting identity holding exactly over `Decimal` and money never being
projected as a float.

### Metrics

Every figure comes from AlphaLab's `PerformanceReport`. Where AlphaLab produces
nothing, the API emits `null`. **No metric is computed in this application** — a
plausible-looking figure calculated here would be fabrication, and would be a
second accounting authority nothing reconciles against.
