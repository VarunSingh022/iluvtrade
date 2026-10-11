# AlphaLab capability map

How the whole of AlphaLab — not only the part the bridge imports today — can
power iluvtrade and RedDesk, what each part really is, and what has to exist
before a product workflow can expose it.

This is **not** an AlphaLab feature catalogue. AlphaLab's own
`docs/ARCHITECTURE.md` and `nowandfuture.md` describe the engine; this page maps
the engine onto this product. Where the two disagree about AlphaLab, AlphaLab's
source and tests win; where they disagree about iluvtrade, `docs/STATUS.md`
wins.

- [1. What was read, and how](#1-what-was-read-and-how)
- [2. The release this map describes](#2-the-release-this-map-describes)
- [3. The engine as built](#3-the-engine-as-built)
- [4. Single-authority rules](#4-single-authority-rules)
- [5. Subsystem map](#5-subsystem-map)
- [6. Where each capability stands for this product](#6-where-each-capability-stands-for-this-product)
- [7. Single-authority audit of iluvtrade against the engine](#7-single-authority-audit-of-iluvtrade-against-the-engine)
- [8. Product workflows, and the earliest incomplete link in each](#8-product-workflows-and-the-earliest-incomplete-link-in-each)
- [9. Next vertical slice: durable paper-session resume](#9-next-vertical-slice-durable-paper-session-resume)
- [10. Roadmap, dependency-ordered](#10-roadmap-dependency-ordered)
- [11. Keeping this page true](#11-keeping-this-page-true)

---

## 1. What was read, and how

| Evidence | Used for |
|---|---|
| The released wheel and sdist, checked against `SHA256SUMS-4.1.0` and GitHub's asset digests | What "released" means |
| The `v4.1.0` tag's source tree (commit `33e61d3`), compared file by file with the wheel's `alphalab/` | Reading source as release source |
| A static pass over all 606 modules: package sizes, `__all__` per package (from `docs/api/public_api.json`), the package import graph, and which AlphaLab tests, examples and benchmarks import each package | Maturity, runtime position, coupling |
| Package and module docstrings, ADR-0001 to ADR-0050, `docs/ARCHITECTURE.md` (Implementation Status), `docs/INTEGRATION.md`, `CHANGELOG.md` | Intent, contracts, history — checked against source where it mattered |
| The release's `tests/consumer_contract.py`, run against this project's environment | What the application may rely on |
| Every `alphalab` import in `backend/iluvtrade/alphalab_bridge/` and every caller of the bridge | What is integrated |

A directory name is not evidence. Where a capability is claimed below, it is
because the source implements it and AlphaLab's tests import it; the counts in
§5 are static counts of test files that import the package, not coverage.

**Status words** used in §5 and §6:

| Maturity | Means |
|---|---|
| **Complete** | Implemented to a stated contract, on or beside a wired path, with substantial tests and examples |
| **Foundation** | Real and tested, but deliberately small in scope: a primitive set, not a platform |
| **Library-scale** | Correct pure-Python implementation with few tests and no example; not sized for production workloads |
| **Boundary** | AlphaLab ships the contract only; the implementation belongs to the host or a vendor |

| Integration | Means |
|---|---|
| **Integrated** | iluvtrade calls it on a product path today |
| **Partially integrated** | Some outputs reach the product; named parts do not |
| **Not integrated** | Available in the installed release; nothing in iluvtrade calls it |

---

## 2. The release this map describes

| | |
|---|---|
| Release | AlphaLab **4.1.0**, tag `v4.1.0` = commit `33e61d3edbd041bcd125d5dea913c00cdc9da613` |
| Wheel | `alphalab-4.1.0-py3-none-any.whl`, SHA-256 `c2c1e8ca916a025b47350b15da9bde6fb3c64e626f2b05736e4152896b7a0a09` |
| Sdist | `alphalab-4.1.0.tar.gz`, SHA-256 `355352eded23708e57a8649c9cb1c4c51eb8c9be717a6759d1cfdd3e5ee4dcaa` |
| Contents | 606 modules: **43 packages** and the `alphalab.api` module. The wheel's `alphalab/` is byte-identical to the tag's |
| Public API | **2,628 names** (`docs/api/public_api.json`): every package's `__all__`, `alphalab.api`'s and the ten snapshot modules'. Stable until 5.0 |
| Runtime dependencies | None (standard library only) |
| Consumer contract | `consumer contract: 9 checks passed` against this project's environment |

**There is no newer, unreleased AlphaLab line.** On 2026-10-11 the AlphaLab
remote's `main` is the `v4.1.0` commit. The only other branches are
`feat/v2.2-backtesting-replay` and `feature/v2-domain-models` (v2-era history,
superseded) and Dependabot action bumps. Every capability below is therefore in
the installed wheel. If a later release adds something, it enters this map only
once it is in a published, hash-pinned artifact.

**Three AlphaLab documentation findings** from reading. None is edited here
(AlphaLab is not modified from this repository):

- AlphaLab's `docs/ARCHITECTURE.md` lists `alphalab.market.stream.StreamingSource`
  as the implemented streaming source, but no `__all__` exports it and the
  manifest does not record it, so it is outside the stable public API an
  application may import (§5.2).

- `alphalab.reinforcement_learning.environment`'s module docstring says
  `PortfolioEngine.apply_fill` double-counts realized P&L and is "not fixed".
  It was fixed in v2.0.0 (CHANGELOG "D1"), the current `apply_fill` has no
  `+ pnl` term, and `tests/regression/test_close_fill_cash_accounting.py`
  guards it. The docstring is stale; the accounting is correct.
- `README.md` ("API stability") says 2,617 public names; the manifest and
  `docs/INTEGRATION.md` say 2,628. The manifest is the authority.

---

## 3. The engine as built

AlphaLab is a library: no process, server, scheduler or event bus. Every engine
is a pure function over an immutable state value, and the caller owns the
process, the loop, persistence locations and concurrency.

### 3.1 Two wired paths and a front door

```text
dataset / provider / stream                                  (host supplies data)
      │   data ─ normalization ─ MarketRecord
      ▼
 mark → strategy → allocation → risk → OMS → fill policy → execution → portfolio → analytics
      └──────────────── runtime.ExecutionPipeline (one step per record) ───────────────┘
                        runtime.RunEngine owns a run over it
 drivers (hold no state):  BacktestEngine · ReplayBacktest · TradingSession · LiveSession
      │                                                     │
      │ snapshots: run_snapshot / replay_snapshot / live_snapshot → persistence.RunStateStore
      ▼                                                     ▼
 lifecycle: candidate → experiment → evidence → model version → strategy version
            → promotion → deployment ledger → authorize_run (refuses a run the ledger does not name)

 alphalab.api: the application's front door — ingestion → dataset version → backtest / replay /
               resume_replay, and run_study (features → diagnostics) — over the same engines
```

| Path | Authority | Answers |
|---|---|---|
| **Execution** | `runtime.ExecutionPipeline`, `runtime.RunEngine`; drivers in `backtesting`, `backtesting.replay`, `runtime.session`, `runtime.live` | What one market event does to the book |
| **Lifecycle** | `lifecycle` (composes `experiment_tracking`, `model_registry`, `deployment_manager`, `research`, `backtesting`) | Which strategy version an environment should run, why, and who decided |
| **Application surface** | `alphalab.api` | How an application goes from bytes to a result with lineage, importing one module |

### 3.2 Reachability — measured, not asserted

From the package import graph of the released source:

| Reached from | Packages |
|---|---|
| Execution path (`runtime`, `backtesting`) | `allocation`, `alt_data`, `analytics`, `backtesting`, `broker`, `common`, `conventions`, `core`, `data`, `execution`, `instrument`, `market`, `marketdata`, `oms`, `options`¹, `persistence`, `portfolio`, `replay`, `risk`, `runtime`, `strategy` |
| Lifecycle path, additionally | `deployment_manager`, `experiment_tracking`, `factor_library`, `lifecycle`, `model_registry`, `research` |
| `alphalab.api`, additionally | `futures`, `factor_library`, `research` |
| **Neither path — standalone** | `brokers`, `cloud_research`, `cluster_scheduler`, `crypto`, `deep_learning`, `distributed`, `feature_store`, `macro`, `ml`, `portfolio_optimizer`, `reinforcement_learning`, `reporting`, `research_assistant`, `scenario`, `scheduler` |

¹ `options` is reachable only because `data.assets` imports two enums from it
(`OptionType`, `ExerciseStyle`). **Option pricing is not on the execution path**;
option *economics* in the portfolio come from `conventions.economics`.

ADR-0009 makes "standalone" a design rule, not a gap: each such engine is
deterministic and tested, and composing it is the caller's job. Chaining
standalone engines into one runtime inside iluvtrade would be building the
cross-engine runtime AlphaLab deliberately declined to build. Compose them per
workflow instead (§10).

### 3.3 What AlphaLab deliberately does not do

Vendor broker or data adapters, credentials, market/reference/FX/classification
data, user accounts or permissions storage, a UI, a running or supervised
process, scheduling of work, concurrency within one process (LIM-006: one run
per process), automated remediation of reconciliation or health findings,
marketplace logic (ADR-0041), and seller-code isolation. Every one of these is
iluvtrade's, RedDesk's or an external provider's — see §6.

---

## 4. Single-authority rules

The rules inside AlphaLab that an application must not fork. Each has one
implementation; iluvtrade calling around it creates a second authority.

| Question | The one authority | Note for iluvtrade |
|---|---|---|
| What one market event does | `runtime.ExecutionPipeline.process_record` | Every driver takes it; never re-sequence steps in the app |
| Cash, positions, realized/unrealized P&L | `portfolio.PortfolioEngine` | `session_*` tables are projections written only by `trading/projection.py` |
| Pre-trade risk | `risk.RiskEngine` on the projected book | Refusals are read from step results, never recomputed |
| Order lifecycle | `core.lifecycle.ORDER_TRANSITIONS` (read by OMS and broker mirror) | A venue adapter translates into `VenueEvent`, never into its own states |
| Simulated fill | `FillPolicy` + `FillTiming` + `ExecutionSimulator` | `FillTiming` is recorded with every run (§7.5) |
| Wire → canonical market record | `market.normalization` (with `data.BarStamp`) | §7.2: iluvtrade builds `market.Bar` directly today |
| Dataset identity | `data.derive_dataset_version` → `MarketDataset.dataset_id` → `ValidationEvidence` | §7.6: iluvtrade mints `dsv-<uuid>` today |
| Instrument identity | `instrument.InstrumentRecord(...).asset_id` (uuid5) | Already used |
| Strategy identity → code | `strategy.StrategyClassRegistry` | §7.7: the bridge keeps an `IMPLEMENTATIONS` dict |
| What is live where, and who put it there | `deployment_manager` ledger via `lifecycle` | Not used yet; RedDesk entitlements answer a different question (§6) |
| Covariance / factor risk | `analytics.risk_model` | Construction and budgets read it; never compute a second one |
| Currency conversion | `portfolio.fx` with supplied, dated rates | No default rate, no triangulation, no inversion |
| A run's identity | `lifecycle.digest_run(result)` (`result_id`, `configuration_id`) | Already recorded with every result |
| Persisted state formats | Each owner's snapshot module + `persistence.upgrade.SchemaHistory` | Store payloads opaque; never decode them in the app |

**Deliberate duplicates in AlphaLab — do not "fix" by merging:** `data.Bar`
(wire, `float`, provider symbol) vs `market.Bar` (canonical, `Decimal`,
`asset_id`); `broker` (one venue's boundary) vs `brokers` (routing across
brokers, reusing `broker`'s types); `research.ResearchEngine` (scores a finished
run) vs `research` study methodology (works before any run);
`walk_forward_analysis` (reads a return series) vs `walk_forward_splits`
(partitions time before a run); `analyze_regimes` vs `classify_regimes`.
`tests/regression/test_shared_names_stay_distinct.py` holds these apart.

---

## 5. Subsystem map

Every significant subsystem of the released engine. **Availability is the same
for all of them**: in the v4.1.0 wheel (§2), with no newer unreleased version.
"Tests/examples" are static counts of AlphaLab test files and examples that
import the package. "Owner" names who owns the *semantics*; iluvtrade owns
orchestration, persistence location and UX for everything it exposes.

### 5.1 Canonical execution engine — the spine

| Field | |
|---|---|
| **Capability** | One deterministic step per market record: mark → strategy → allocation → risk → OMS → fill policy → execution → portfolio → analytics; four drivers (backtest, replay, paper, live) over the same step; seeded identifiers; slices; observations delivered at the instant they became knowable |
| **Authority** | `runtime` (`ExecutionPipeline`, `RunEngine`), `backtesting`, `strategy`, `allocation`, `risk`, `oms`, `execution`, `portfolio`, `analytics`, `core`, `market` |
| **Public API** | `backtesting.{BacktestEngine, ReplayBacktest, RunConfig, MarketDataset, id_scope, advance, finalize, PausedReplay}`, `runtime.{ExecutionPipelineConfig, RunEngine, TradingSession, LiveSession, RetentionPolicy, checkpoint, StrategyFailure}`, `strategy.{BaseStrategy, Intent, start_strategy, create_runtime, StrategyStateProtocol}`, `api.backtest/replay/resume_replay` |
| **Maturity** | Complete. `runtime` 12,190 LOC / 107 test files; `strategy` 97; `portfolio` 73; `execution` 87; `core` 111 |
| **Runtime position** | The execution path itself |
| **Inputs → outputs** | `RunConfig` (capital, budget, limits, simulator, instruments, seed — nothing defaulted) + `MarketDataset`/`MarketDataSource` + started `RuntimeState` → `RunState` per step, `BacktestResult` (fills, orders, valuation, `PerformanceReport`, `strategy_failures`, skipped/unpriced records) |
| **State / persistence** | Immutable values; `runtime.run_snapshot` (schema 5), `runtime.snapshot` (pipeline, 8), `backtesting.replay_snapshot` (1); stored opaque in a `RunStateStore` the caller locates |
| **Tests / examples** | Above; consumer contract checks 1–9; byte-identical stop/resume certified (REP-2) |
| **iluvtrade consumer** | Backtest jobs (`backtests/runner.py`), paper sessions (`trading/runner.py`) |
| **Integration status** | **Integrated** — `BacktestEngine.advance` loop with per-step observation; `TradingSession.advance` record by record; `close_slice` per instant; `start_strategy`; `id_scope` around init/advance/finalize; `strategy_failures` read before any figure; `digest_run` identities stored |
| **Dependencies** | Instrument registry (done), dataset (done), declared strategy state (done) |
| **Owner** | AlphaLab |
| **Follow-up** | Expose `FillTiming` (§7.5); stamp bars correctly (§7.2); durable resume (§9) |

### 5.2 Market data, normalization and the data-source boundary

| Field | |
|---|---|
| **Capability** | Canonical `Bar`/`Quote`/`Tick`/`MarketRecord`; normalization from wire records with a declared bar stamp; `MarketDataSource` with declared ordering; history providers (`BarHistoryProvider`) and a real RFC 6455 streaming source with sequence de-duplication, gap counting and reconnect |
| **Authority** | `market` (domain, normalization, `provider`, `stream`), `data.feed`/`marketdata` (wire records, websocket transport) |
| **Public API** | `market.{Bar, TimeFrame, MarketRecord, SequenceSource, OrderingGuarantee, MarketDataSource, NormalizationPolicy, normalize_wire_bar(s)/quote/trade/book, BarHistoryProvider, ProviderHistorySource}`, `marketdata.WebSocketConnection`. **`StreamingSource` is not public**: it is defined in `alphalab/market/stream.py` and exported by no `__all__`, so under the public-API rule (`docs/api/PUBLIC_API.md`) iluvtrade may not depend on it |
| **Maturity** | Complete for history sources; the streaming source is implemented and tested in AlphaLab (written to protocol, never against a vendor) but outside the stable contract |
| **Runtime position** | Feeds the execution path |
| **Inputs → outputs** | Wire bars/quotes/trades (`float`, provider symbol) → canonical records (`Decimal`, `asset_id`, end-of-interval timestamp) |
| **State / persistence** | Stateless records; stream connection state in-memory |
| **Tests / examples** | `market` 81 test files, 20 examples; `marketdata` 10 |
| **iluvtrade consumer** | Dataset → `MarketDataset`; paper `SequenceSource`; a future live feed |
| **Integration status** | **Partially integrated** — `Bar`, `TimeFrame`, `SequenceSource`, `MarketDataset` used; timestamps interpreted by AlphaLab under a declared convention and start-stamped bars moved by `normalize_wire_bar` (`alphalab_bridge/temporal.py`); `vwap`/`trade_count` `None` when unreported; streaming unused |
| **Dependencies** | A real data vendor for streaming (external); for live streaming, either AlphaLab exporting `StreamingSource` in a 4.x minor release, or iluvtrade writing its own `MarketDataSource` over the public `WebSocketConnection` and `normalize_wire_*` |
| **Owner** | AlphaLab (semantics); iluvtrade (vendor providers) |
| **Follow-up** | Phase 1 (§10); raise the `StreamingSource` export with AlphaLab before Phase 7 |

### 5.3 Universal data engine (ingestion, validation, cleaning, quality, provenance)

| Field | |
|---|---|
| **Capability** | CSV/in-memory ingestion; schema detection; structured validation findings; cleaning under an explicit policy; quality reports; calendars; corporate actions; raw/adjusted basis; a dataset version **derived from content**; transformations recorded; point-in-time ingestion of observations, events and fundamentals |
| **Authority** | `data` (ADR-0036) via `alphalab.api` |
| **Public API** | `api.{inspect_csv, ingest_csv, ingest_rows, validate_dataset, clean_dataset, normalize_records, select, to_market_dataset, ingest_observations, ingest_events, ingest_fundamentals, lift_wire_records}`, `data.{Dataset, CleaningPolicy, QualityReport, BarStamp, MarketCalendar, CorporateAction, derive_dataset_version, dataset_lineage}` |
| **Maturity** | Complete. 6,267 LOC, 65 test files, 27 examples |
| **Runtime position** | Feeds both paths through `alphalab.api` |
| **Inputs → outputs** | Bytes + `IngestionRequest`/policy → `Dataset` with provenance, findings, transformations, `DatasetVersion` → `MarketDataset` whose `dataset_id` *is* that version |
| **State / persistence** | Values; the caller stores bytes and the dataset |
| **Tests / examples** | Above |
| **iluvtrade consumer** | The data workspace (upload/fetch → approve) |
| **Integration status** | **Partially integrated** — `parse_timestamp`, `resolve_zone`, `DateOnlyPolicy`, `BarStamp` used for timestamp interpretation; detection, cleaning, quality and dataset identity are still iluvtrade's (§7.1, §7.6) |
| **Dependencies** | A product decision (§7.1) |
| **Owner** | AlphaLab (canonicalization semantics); iluvtrade (fetch controls, approval gate, storage, UX) |
| **Follow-up** | Phase 1 decision |

### 5.4 Instruments, conventions and global markets

| Field | |
|---|---|
| **Capability** | Instrument identity (uuid5 over a canonical key) and registry with classification provenance; market conventions, instrument economics (multipliers, lots, settlement), calendars and venue sessions inside simulation |
| **Authority** | `instrument`, `conventions` (a leaf over `common`), `runtime.VenueCalendars` |
| **Public API** | `instrument.{InstrumentRecord, InstrumentRegistry, register_instruments, classify_instrument}`, `conventions.*`, `api.convention_from_spec`, `api.sector_classification/currency_classification` |
| **Maturity** | Complete (`instrument` 37 test files; `conventions` 13) |
| **Runtime position** | Read on the fill path |
| **Inputs → outputs** | Symbol, asset type, exchange, currency → `asset_id`; registry → classifications |
| **State / persistence** | `instrument.snapshot` (schema 3) |
| **Tests / examples** | Above; 14 and 8 examples |
| **iluvtrade consumer** | `alphalab_bridge/instruments.py` |
| **Integration status** | **Partially integrated** — identity and registry used; classification (sector) and venue calendars not supplied, so sector attribution is empty by design |
| **Dependencies** | Classification and calendar data (external or user-supplied) |
| **Owner** | AlphaLab; data from external providers |
| **Follow-up** | Phase 8 |

### 5.5 Risk

| Field | |
|---|---|
| **Capability** | Pre-trade limits judged on the projected book: order size, position, exposure, leverage, margin, daily loss (zoned trading day), drawdown, classification-bucket limits; per-strategy capital ceilings (`allocation.ceilings`) |
| **Authority** | `risk` |
| **Public API** | `risk.{RiskLimits, OrderSizeLimit, PositionLimit, ExposureLimit, LeverageLimit, MarginLimit, DailyLossLimit, DrawdownLimit, ClassificationLimit, RiskDecision}` |
| **Maturity** | Complete (25 test files, 19 examples) |
| **Runtime position** | Execution path |
| **Inputs → outputs** | Order requests + marked book → `RiskDecision` (refusal releases the reservation) |
| **State / persistence** | Inside the pipeline snapshot |
| **Tests / examples** | Above |
| **iluvtrade consumer** | `RiskProfile` → `RiskLimits`; refusals surfaced in jobs and session logs |
| **Integration status** | **Integrated** (daily-loss zone from `runconfig.TRADING_DAY_ZONES`); `ClassificationLimit` unused |
| **Dependencies** | Classification data for bucket limits |
| **Owner** | AlphaLab |
| **Follow-up** | None required |

### 5.6 OMS, execution simulation, costs, algorithms, routing and execution analytics

| Field | |
|---|---|
| **Capability** | Canonical order lifecycle; market/limit/stop/auction orders with time in force; fill policies and fill timing; itemized costs (commission, fees, spread, slippage, impact, tax); capacity; TWAP/VWAP/participation/iceberg with urgency; smart route selection; implementation shortfall, slippage, fill and venue quality |
| **Authority** | `oms`, `execution`, `runtime.route_child_order` |
| **Public API** | `execution.{ExecutionSimulator, FillTiming, PercentageCommission, ExecutionCostModel, TWAP, VWAP, Participation, Iceberg, select_route, implementation_shortfall, execution_quality_report, capacity_curve}` |
| **Maturity** | Complete (`execution` 6,834 LOC / 87 test files / 24 examples; `oms` 41) |
| **Runtime position** | Simulation and costs on the path; algorithms/routing are composed by the caller around it |
| **Inputs → outputs** | Orders + market events → `ExecutionReport`s; schedules/routes from supplied quotes, volumes, latencies |
| **State / persistence** | Execution state inside the pipeline snapshot; algorithm state as values |
| **Tests / examples** | Above |
| **iluvtrade consumer** | `runconfig` (commission model); orders/fills listings |
| **Integration status** | **Partially integrated** — simulator, two commission models, explicit `FillTiming` (default `NEXT_EVENT`) and AlphaLab's `execution_assumptions` stored and shown; cost models beyond commission, capacity, algorithms, routing and execution analytics unused |
| **Dependencies** | Liquidity/volume data for capacity and VWAP; venue profiles for routing |
| **Owner** | AlphaLab |
| **Follow-up** | Phase 2 (fill timing, costs), Phase 7 (execution analytics) |

### 5.7 Portfolio, accounting, FX and multi-strategy books

| Field | |
|---|---|
| **Capability** | Exact per-currency accounting at each currency's minor unit; mark-to-market; NAV; settlement in several currencies with conversion only at supplied, dated rates; FX feed boundary; corporate actions; futures/options/perpetual economics; multi-strategy sleeves valued in one reporting currency |
| **Authority** | `portfolio` (`PortfolioEngine`, `fx`, `fx_feed`, `multi_strategy`, `corporate_actions`) |
| **Public API** | `portfolio.{Account, PortfolioEngine, PortfolioValuation, FxRates, …}`, `portfolio.snapshot` (schema 5) |
| **Maturity** | Complete (6,914 LOC / 73 test files / 25 examples / 11 benchmarks) |
| **Runtime position** | Execution path |
| **Inputs → outputs** | Fills, marks, rates → `PortfolioState`, valuations, multi-strategy books |
| **State / persistence** | `portfolio.snapshot`, `portfolio.fx_feed` snapshot |
| **Tests / examples** | Above |
| **iluvtrade consumer** | Valuation/positions in results; session projections |
| **Integration status** | **Integrated** for single-currency books; FX, multi-currency settlement (`also_settles`), corporate actions and multi-strategy books not exposed |
| **Dependencies** | FX rates and corporate-action data (external) |
| **Owner** | AlphaLab; data external |
| **Follow-up** | Phase 8 |

### 5.8 Analytics, attribution and the risk model

| Field | |
|---|---|
| **Capability** | `PerformanceReport` (returns with declared periodicity, risk ratios, drawdowns, exposure, trades, attribution by strategy/asset/sector); benchmark statistics; risk decomposition; the covariance/factor risk model with identities; risk budgets; cross-strategy correlation/overlap/crowding; VaR/CVaR; factor risk incl. FX factors |
| **Authority** | `analytics` (imports only `common` and `core`) |
| **Public API** | `analytics.{PerformanceReport, CovarianceMatrix, FactorLoadings, Classification, evaluate_risk_budget, benchmark_statistics, factor_risk, value_at_risk, …}` |
| **Maturity** | Complete (6,633 LOC / 33 test files) |
| **Runtime position** | Report on the path; risk model and budgets composed by the caller |
| **Inputs → outputs** | Equity curve, trades, positions, supplied covariances/classifications → reports |
| **State / persistence** | Values |
| **Tests / examples** | Above; 8 examples |
| **iluvtrade consumer** | `results.report_of` |
| **Integration status** | **Partially integrated** — the whole `PerformanceReport` including periodicity/annualization metadata; risk model, budgets, cross-strategy and benchmark analytics unused |
| **Dependencies** | Benchmark series; return panels; classifications |
| **Owner** | AlphaLab |
| **Follow-up** | Phase 8 |

### 5.9 Broker boundary, reconciliation and the live driver

| Field | |
|---|---|
| **Capability** | `BrokerProtocol` and canonical broker vocabulary; `PaperBroker`; capability declarations and compatibility checks; normalized venue events with one outcome each; request ledgers for cancel/modify; snapshot reconciliation; durable broker binding; `LiveSession` (settle → advance → route); `live_health` |
| **Authority** | `broker`, `core.capabilities`, `core.lifecycle`, `runtime.live`, `runtime.broker_routing` |
| **Public API** | `broker.{BrokerProtocol, PaperBroker, reconcile, ReconciliationReport, apply_venue_event, issue_cancel, issue_modify, reconcile_snapshot}`, `broker.snapshot`, `runtime.{LiveSession, route_order, apply_broker_execution, live_health}`, `runtime.live_snapshot` |
| **Maturity** | Complete as a contract (46 test files); the HTTP venue adapter is a reference in AlphaLab's tests, not a shipped component (BRK-007) |
| **Runtime position** | Reached from the execution path through `broker_routing` |
| **Inputs → outputs** | Venue messages → `VenueEvent` → mirror/OMS outcomes; snapshots → mismatch reports |
| **State / persistence** | `broker.snapshot` (2), `runtime.live_snapshot` (2) |
| **Tests / examples** | Above; 10 examples |
| **iluvtrade consumer** | `brokers/zerodha.py` (`ZerodhaAdapter` implements `BrokerProtocol`), broker accounts |
| **Integration status** | **Partially integrated** — vocabulary re-exported, Zerodha adapter written to it (never run against the venue); `reconcile`, venue events, capability declarations and `LiveSession` unused; live disabled by three gates |
| **Dependencies** | Kite Connect subscription, registered app, funded account (external); a market-data feed |
| **Owner** | AlphaLab (contract); iluvtrade (adapters, credentials, transport) |
| **Follow-up** | Phases 6–7 |

### 5.10 Persistence, snapshots, checkpoints and retention

| Field | |
|---|---|
| **Capability** | Deterministic serialization; typed decoders that name the failing field; schema histories that upgrade older payloads or refuse; an atomic, digest-checked file store; incremental checkpoints; retention policies |
| **Authority** | `persistence`, each owner's snapshot module, `runtime.checkpoint`, `runtime.retention` |
| **Public API** | `persistence.{serialize, deserialize, FileRunStateStore, MemoryRunStateStore, RunStateRef, StateDecodeError, StorageError}`, `runtime.{checkpoint, read_checkpoints, restore_checkpoints, RetentionPolicy}`, `runtime.run_snapshot.{capture, restore, RunObjects}` |
| **Maturity** | Complete (87 test files) |
| **Runtime position** | Used by every snapshot owner |
| **Inputs → outputs** | State values ↔ JSON payloads; `(run_id, sequence)` ↔ bytes |
| **State / persistence** | Caller chooses the location |
| **Tests / examples** | Above; golden payloads from v3.9.0 onward read by 4.1.0 |
| **iluvtrade consumer** | Durable paper resume (§9) |
| **Integration status** | **Not integrated** (tests in this repository exercise replay capture/restore against the real engine; no product path persists a payload) |
| **Dependencies** | Application checkpoint table and blob storage (exist: `common/storage.py`) |
| **Owner** | AlphaLab (formats); iluvtrade (where and when) |
| **Follow-up** | §9 |

### 5.11 Replay engine and pausable replay

| Field | |
|---|---|
| **Capability** | Replay cursor, clock, session lifecycle and chronological validation; `ReplayBacktest` on the same step; **pause between records and resume to the identical result**, in-process or in another process (v4.1, FUT-001) |
| **Authority** | `replay` (cursor), `backtesting.replay`, `backtesting.replay_snapshot` |
| **Public API** | `api.replay(..., pause_when=…)`, `api.resume_replay`, `backtesting.{ReplayBacktest, PausedReplay, ReplayProgress}`, `backtesting.replay_snapshot.{capture, restore, dataset_digest}` |
| **Maturity** | Complete (driver); `replay` package small (592 LOC, 5 test files) |
| **Runtime position** | A driver of the execution path |
| **Inputs → outputs** | Dataset + config + runtime + pause predicate → `ReplayResult` or `PausedReplay` payload |
| **State / persistence** | `REPLAY_SNAPSHOT_SCHEMA = 1`, envelope around the run snapshot |
| **Tests / examples** | Consumer-contract checks 5–9 |
| **iluvtrade consumer** | `engine.run_replay` (backtest reproducibility check) |
| **Integration status** | **Partially integrated** — replay used to prove parity; pause/resume exercised by tests only. Paper sessions use `TradingSession`, so their resume uses `run_snapshot` + `RunEngine.resume`, not `PausedReplay` (§9) |
| **Dependencies** | None in AlphaLab |
| **Owner** | AlphaLab |
| **Follow-up** | §9 |

### 5.12 Strategy contract, registry, state and adaptive strategies

| Field | |
|---|---|
| **Capability** | `StrategyProtocol`/`BaseStrategy`; target and delta intents; enforced subscriptions, slices, timers, observations; failure isolation; declared strategy state; `StrategyDefinition` (author metadata, parameter bounds); `StrategyClassRegistry` (identity → factory, refusing duplicates and unknowns); adaptive strategies with immutable learned state |
| **Authority** | `strategy` |
| **Public API** | `strategy.{BaseStrategy, Intent, IntentKind, StrategyStateProtocol, start_strategy, StrategyDefinition, StrategyClassRegistry, AdaptiveStrategy, failed_strategies}` |
| **Maturity** | Complete (4,830 LOC / 97 test files) |
| **Runtime position** | Execution path |
| **Inputs → outputs** | Events + context views → intents |
| **State / persistence** | Declared state inside the run snapshot |
| **Tests / examples** | Above; 23 examples |
| **iluvtrade consumer** | `alphalab_bridge/strategies.py` (three implementations), strategy versions |
| **Integration status** | **Partially integrated** — protocol, `start_strategy`, declared state used; `StrategyClassRegistry` and `StrategyDefinition` not (§7.7); adaptive strategies unused |
| **Dependencies** | Seller-code sandbox for anything beyond in-repository implementations |
| **Owner** | AlphaLab (contract); iluvtrade (which implementations a deployment offers) |
| **Follow-up** | Phase 4 |

### 5.13 Research methodology (studies, validation, overfitting, robustness, regimes, events)

| Field | |
|---|---|
| **Capability** | Forward returns, signal diagnostics (IC, decay, quantiles); walk-forward and purged/embargoed cross-validation; walk-forward optimization; parameter sweeps counted for multiple testing; deflated and probabilistic Sharpe; bootstrap and Monte Carlo; perturbation robustness; stress reports; capacity; bias detection; regime detection and conditioning; event studies anchored where news could be traded; run-level research metrics |
| **Authority** | `research` (two layers, §4) |
| **Public API** | `api.run_study`, `research.{ResearchStudy, walk_forward_splits, cross_validation_splits, apply_purge_and_embargo, walk_forward_optimize, walk_forward_analysis, parameter_robustness, deflated_sharpe_ratio, bootstrap_statistics, monte_carlo_simulation, build_overfitting_report, classify_regimes, event_study, research_metrics}` |
| **Maturity** | Complete (6,819 LOC / 28 test files / 11 examples) |
| **Runtime position** | Lifecycle path and `alphalab.api`; independent of execution |
| **Inputs → outputs** | A canonical `Dataset` + study definition → `StudyResult` with sample sizes; or a finished run's returns → reports |
| **State / persistence** | Values with derived ids (`derive_study_id`, `derive_result_id`) |
| **Tests / examples** | Above |
| **iluvtrade consumer** | A research workspace; backtest robustness tabs |
| **Integration status** | **Not integrated** |
| **Dependencies** | An AlphaLab `Dataset` with its derived version (§7.1, §7.6); job orchestration (exists) |
| **Owner** | AlphaLab |
| **Follow-up** | Phases 2–3 |

### 5.14 Factor library and feature framework

| Field | |
|---|---|
| **Capability** | Typed `FeatureDefinition` with derived identity over an `ObservationFrame`; panels; ranking, neutralization, IC, decay, turnover, exposure; six style factors; point-in-time knowledge frames over alternative data and fundamentals; factor loadings for construction |
| **Authority** | `factor_library` |
| **Public API** | `factor_library.{FeatureDefinition, compute_feature, compute_panel, information_coefficient, factor_decay, neutralize_*, observation_frame, fundamental_frame, loadings_from_panels, FACTOR_CATALOG}`, `api.observe/study_panels` |
| **Maturity** | Complete (5,309 LOC / 30 test files / 15 examples) |
| **Runtime position** | Lifecycle path and `alphalab.api` |
| **Inputs → outputs** | `Dataset`/observation sets → feature series and panels with lineage |
| **State / persistence** | Values; ids derived from definition and inputs |
| **Tests / examples** | Above |
| **iluvtrade consumer** | Research workspace |
| **Integration status** | **Not integrated** |
| **Dependencies** | As §5.13 |
| **Owner** | AlphaLab |
| **Follow-up** | Phase 3 |

### 5.15 Feature store

| Field | |
|---|---|
| **Capability** | Registration, versioning, metadata, validation and caching of feature definitions and values; computes nothing |
| **Authority** | `feature_store` |
| **Public API** | `feature_store.*` (42 names) |
| **Maturity** | Foundation (982 LOC / 7 test files / 1 example). In-memory state value; only `ml` imports it |
| **Runtime position** | Standalone |
| **Inputs → outputs** | Feature values through `FeatureValueProtocol` → registered, versioned entries |
| **State / persistence** | In-memory value; no snapshot module |
| **Tests / examples** | Above |
| **iluvtrade consumer** | Possibly a research workspace's feature catalogue |
| **Integration status** | **Not integrated** |
| **Dependencies** | iluvtrade persistence if exposed; `factor_library` already gives features identities, so the store adds caching/registration only |
| **Owner** | AlphaLab (registration semantics); iluvtrade (durable storage) |
| **Follow-up** | Defer until a research workspace shows a need |

### 5.16 Alternative data, point-in-time information and macro events

| Field | |
|---|---|
| **Capability** | Observations, information events and fundamentals with four instants and an availability basis; versioned sets with checked vintages; session placement; TTM/valuation/ratios at an instant; delivery to strategies on the execution path at the instant information became knowable |
| **Authority** | `alt_data` (leaf over `common`), `common.point_in_time` |
| **Public API** | `api.ingest_observations/ingest_events/ingest_fundamentals`, `alt_data.*` (106 names) |
| **Maturity** | Complete (4,378 LOC / 14 test files) |
| **Runtime position** | Research and execution (`process_observation`) |
| **Inputs → outputs** | Rows + declared availability rule → versioned observation sets |
| **State / persistence** | Values |
| **Tests / examples** | Above; 4 examples |
| **iluvtrade consumer** | Research workspace; strategies with external signals |
| **Integration status** | **Not integrated** |
| **Dependencies** | Data vendors or user uploads (external); correct bar stamping (§7.2) so joins are not look-ahead |
| **Owner** | AlphaLab; data external |
| **Follow-up** | Phase 3 |

### 5.17 Portfolio construction and optimization

| Field | |
|---|---|
| **Capability** | Minimum variance, mean-variance, maximum diversification, risk parity, robust mean-variance under bounds/concentration/gross/group/factor/turnover/notional/volatility constraints by one certified solver; Black–Litterman; factor-structured problems at O(n k²); costed construction; lot rounding; v1 closed forms |
| **Authority** | `portfolio_optimizer` (ADR-0005, ADR-0043) |
| **Public API** | `portfolio_optimizer.{construct, black_litterman, round_to_lots, …}` (84 names) |
| **Maturity** | Complete (7,185 LOC / 16 test files / 4 examples) |
| **Runtime position** | Standalone — answers "what to own"; turning targets into orders is the caller's |
| **Inputs → outputs** | Expected returns, `CovarianceMatrix`/`FactorStructure`, constraints → target weights with certified status and diagnostics |
| **State / persistence** | Values |
| **Tests / examples** | Above |
| **iluvtrade consumer** | Portfolio construction tools; target-position strategies |
| **Integration status** | **Not integrated** |
| **Dependencies** | Return/covariance estimation from research (Phase 3) |
| **Owner** | AlphaLab |
| **Follow-up** | Phase 8 |

### 5.18 Scenario and stress analysis

| Field | |
|---|---|
| **Capability** | One shock contract applied to a flat `ScenarioState` any book can produce; synthetic shocks; historical scenarios (2008, 2020, 2022) as contracts that refuse to apply until real observations are supplied |
| **Authority** | `scenario` (imports only `common`) |
| **Public API** | `scenario.*` (30 names) |
| **Maturity** | Complete for its scope (1,218 LOC / 4 test files / 2 examples) |
| **Runtime position** | Standalone |
| **Inputs → outputs** | Positions projection + scenario → shocked state and P&L |
| **State / persistence** | Values |
| **Tests / examples** | Above |
| **iluvtrade consumer** | Backtest/portfolio stress views |
| **Integration status** | **Not integrated** |
| **Dependencies** | Historical observations for historical scenarios (external/user data) |
| **Owner** | AlphaLab |
| **Follow-up** | Phase 2 (synthetic), Phase 8 (historical) |

### 5.19 Options, futures, crypto and fixed income

| Field | |
|---|---|
| **Capability** | **Options**: Black–Scholes–Merton, American binomial lattice, implied volatility, surfaces and term structure, expiry resolution. **Futures**: contract chains, roll policies, continuous contracts. **Crypto**: perpetuals, venue metadata, 24/7 coverage, liquidation price. **Macro**: bond analytics foundation |
| **Authority** | `options`, `futures`, `crypto`, `macro`; economics in `conventions` |
| **Public API** | `options.*` (63), `futures.*` (35), `crypto.*` (32), `macro.*` (34), `api.future_contract_from_spec/option_contract_from_spec` |
| **Maturity** | `options` Complete (2,733 LOC / 11 test files); `futures` Foundation (7); `crypto` Foundation (5); `macro` Foundation (6) |
| **Runtime position** | Standalone pricing/analytics; economics of held contracts are on the path through `conventions` |
| **Inputs → outputs** | Contract specs, market inputs → prices, greeks, chains, rolls |
| **State / persistence** | Values |
| **Tests / examples** | Above; 3, 2, 1, 1 examples |
| **iluvtrade consumer** | Multi-asset research and instruments beyond Indian cash equities |
| **Integration status** | **Not integrated**; iluvtrade supports cash equities on XNSE/XBOM only (`runconfig.TRADING_DAY_ZONES`) |
| **Dependencies** | Derivatives market data and instrument reference data (external); broker support |
| **Owner** | AlphaLab |
| **Follow-up** | Beyond Phase 8; only with a data source and a user need |

### 5.20 Model and strategy lifecycle, governance and evidence

| Field | |
|---|---|
| **Capability** | Typed refs (strategy line, strategy version, model version, deployment); stages and refused moves; evidence-gated promotion; deployment ledger as the only answer to "what is live"; rollback; `Governance(authority, actor_id, approval_required_in)` with separation of duties; `run_plan`/`authorize_run`; **strategy fingerprints, reproducibility manifests, certification (eight properties, four statuses, no score), portability, rerun-from-manifest, lock-file reading**; runtime health; expected/paper/live comparison; book-vs-mirror and multi-account reconciliation |
| **Authority** | `lifecycle` (14,316 LOC / 62 test files / 22 examples), `deployment_manager`, `model_registry`, `experiment_tracking` |
| **Public API** | `lifecycle.{digest_run, build_fingerprint, derive_strategy_fingerprint, manifest_for_run, manifest_for_study, certify_strategy, verify_certification_report, evaluate_portability, rerun_from_manifest, evidence_from_backtest, evidence_from_study, evaluate_policy, promote_strategy_version, deploy_strategy_version, authorize_run, Governance, PermissionAuthority, evaluate_health, compare_expected_paper_live, reconcile_execution_state, reconcile_accounts}`, `lifecycle.snapshot` |
| **Maturity** | Complete |
| **Runtime position** | Lifecycle path; joins execution through `authorize_run` (a query with a refusal, not a runtime) |
| **Inputs → outputs** | Results, studies, definitions, observations → evidence, fingerprints, manifests, certification reports, health and reconciliation reports |
| **State / persistence** | `LifecycleState` (`lifecycle.snapshot`, schema 2); evidence store envelope (`model_registry.evidence`) |
| **Tests / examples** | Above |
| **iluvtrade consumer** | RedDesk evidence and certification; strategy publication; session health; reconciliation screens |
| **Integration status** | **Partially integrated** — `digest_run` only. iluvtrade's `content_hash`, backtest-run evidence and manual certification flag are application-level stand-ins (§7.8) |
| **Dependencies** | Engine-derived dataset identity (§7.6) for evidence to name the bytes; `PermissionAuthority` backed by iluvtrade roles |
| **Owner** | AlphaLab (evidence semantics); RedDesk (listing, licensing, review workflow) |
| **Follow-up** | Phase 4 (evidence), Phase 7 (health, reconciliation, governance) |

### 5.21 Experiment tracking, model registry and deployment manager

| Field | |
|---|---|
| **Capability** | Runs with parameters and metric history; model versions with stages and content-addressed artifacts (`ArtifactStore`: `FileArtifactStore`/`MemoryArtifactStore`); evidence store; checksummed releases with an environment ledger |
| **Authority** | `experiment_tracking` (437 LOC / 12), `model_registry` (1,721 / 23), `deployment_manager` (544 / 6) |
| **Public API** | Their `__all__` (18, 46, 19 names) |
| **Maturity** | `model_registry` Complete; `experiment_tracking` and `deployment_manager` Foundation |
| **Runtime position** | Lifecycle path |
| **Inputs → outputs** | Run records, artifacts → tracked experiments, registered versions, release manifests |
| **State / persistence** | In-memory values; artifact bytes in a store the caller locates |
| **Tests / examples** | Above |
| **iluvtrade consumer** | Research workspace history; strategy publication |
| **Integration status** | **Not integrated** |
| **Dependencies** | iluvtrade persistence (tenant-scoped tables or blobs) — AlphaLab provides values, not a database |
| **Owner** | AlphaLab (semantics); iluvtrade (durable multi-tenant storage) |
| **Follow-up** | Phases 3–4 |

### 5.22 Allocation and capital planning

| Field | |
|---|---|
| **Capability** | Intent → order requests with reservations released exactly once; target intents against each strategy's own position; constraints (shorting, whole units); capital plans across strategies, markets, brokers, accounts and currencies, reconciled exactly and refused rather than scaled; per-strategy ceilings |
| **Authority** | `allocation` (ADR-0015) |
| **Public API** | `allocation.{AllocationConstraints, CapitalBudget, capital plans, ceilings}` (67 names) |
| **Maturity** | Complete (3,378 LOC / 48 test files / 17 examples) |
| **Runtime position** | Execution path (allocation); capital plans composed by the caller |
| **Inputs → outputs** | Intents, prices, budget → order requests, reservations; accounts → capital plans |
| **State / persistence** | `allocation.snapshot` (schema 3) |
| **Tests / examples** | Above |
| **iluvtrade consumer** | `runconfig` (budget and constraints) |
| **Integration status** | **Partially integrated** — budget and constraints; capital plans and ceilings unused (one strategy per run today) |
| **Dependencies** | Multi-strategy sessions |
| **Owner** | AlphaLab |
| **Follow-up** | Phase 8 |

### 5.23 Scheduler

| Field | |
|---|---|
| **Capability** | Deterministic one-shot, interval, market-session and cron timers driven by caller-supplied instants |
| **Authority** | `scheduler` |
| **Public API** | `scheduler.*` (35 names) |
| **Maturity** | Foundation (1,077 LOC / 6 test files) |
| **Runtime position** | Standalone (strategy timers are delivered on the path separately) |
| **Inputs → outputs** | Schedules + instants → fired timer events |
| **State / persistence** | Values |
| **Tests / examples** | Above |
| **iluvtrade consumer** | Possibly a session schedule (open/close) for paper/live |
| **Integration status** | **Not integrated** |
| **Dependencies** | iluvtrade owns the process clock and job scheduling; this computes *when*, never *runs* |
| **Owner** | AlphaLab (timing semantics); iluvtrade (execution of jobs) |
| **Follow-up** | Phase 7, if session scheduling is built |

### 5.24 Reporting

| Field | |
|---|---|
| **Capability** | Report sections and exports (data a presentation is built from); dashboard layouts were removed in v3.12 as presentation |
| **Authority** | `reporting` |
| **Public API** | `reporting.*` (24 names) |
| **Maturity** | Library-scale (552 LOC / 2 test files / no example) |
| **Runtime position** | Standalone |
| **Inputs → outputs** | Sections → report exports |
| **State / persistence** | Values |
| **Tests / examples** | Above |
| **iluvtrade consumer** | Downloadable reports |
| **Integration status** | **Not integrated** |
| **Dependencies** | None |
| **Owner** | AlphaLab (export shape); iluvtrade (presentation) |
| **Follow-up** | Optional; iluvtrade's JSON documents already serve the UI |

### 5.25 Machine learning, deep learning and reinforcement learning

| Field | |
|---|---|
| **Capability** | **ML**: feature pipelines from the feature store, linear and logistic regression, k-fold and walk-forward CV, metrics, a purpose-built linear-algebra module. **DL**: dense networks with real backprop, 1-D convolution, LSTM with BPTT, attention with trained projections, gradient checks. **RL**: a trading environment over the real pipeline, tabular Q-learning, REINFORCE |
| **Authority** | `ml` (770 LOC / 3 test files), `deep_learning` (1,568 / 4), `reinforcement_learning` (767 / 3) |
| **Public API** | 31, 62, 28 names |
| **Maturity** | **Library-scale.** Pure Python, no vectorised backend, no example; correct by its own gradient checks but not sized for production training. `reinforcement_learning`'s docstring carries the stale accounting note (§2) |
| **Runtime position** | Standalone (RL imports the execution path; nothing imports RL) |
| **Inputs → outputs** | Feature matrices / environments → fitted models / policies |
| **State / persistence** | In-memory; models referenced through `model_registry` |
| **Tests / examples** | Above |
| **iluvtrade consumer** | None today |
| **Integration status** | **Not integrated** |
| **Dependencies** | Real compute (external) for anything beyond toy scale; a research workspace first |
| **Owner** | AlphaLab |
| **Follow-up** | Defer; revisit after Phase 3 with a concrete user need |

### 5.26 Distributed research, cloud research, cluster scheduler, research assistant

| Field | |
|---|---|
| **Capability** | **distributed**: job/worker lifecycle state machine. **cloud_research**: `ProcessPoolExecutor` execution and parameter sweeps ("remote" means another OS process). **cluster_scheduler**: aging-aware and affinity assignment, queue position. **research_assistant**: deterministic grid-search driver that generates, evaluates and ranks candidates and lifts one into a `StrategyDefinition` (no LLM) |
| **Authority** | `distributed` (1,005 LOC / 5), `cloud_research` (436 / 2), `cluster_scheduler` (265 / 1), `research_assistant` (572 / 3) |
| **Public API** | 38, 11, 5, 20 names |
| **Maturity** | Library-scale |
| **Runtime position** | Standalone |
| **Inputs → outputs** | Jobs/tasks/parameter grids → assignments, results, ranked candidates |
| **State / persistence** | In-memory values |
| **Tests / examples** | Above; no examples except research_assistant (1) |
| **iluvtrade consumer** | Parameter sweeps inside a research job (research_assistant + experiment tracking) |
| **Integration status** | **Not integrated** |
| **Dependencies** | — |
| **Owner** | AlphaLab (search semantics); **iluvtrade owns job orchestration** — these are not a platform job system and should not replace `backtests/worker.py` |
| **Follow-up** | Use `research_assistant`/`research.parameter_sweep` semantics in Phase 3; do not adopt `distributed`/`cluster_scheduler` as infrastructure (Phase 10 is external) |

### 5.27 Multi-broker router

| Field | |
|---|---|
| **Capability** | Registered broker connections, many brokers and accounts in one value, routing over the canonical broker types |
| **Authority** | `brokers` (952 LOC / 6 test files) |
| **Public API** | `brokers.*` (40 names) |
| **Maturity** | Foundation |
| **Runtime position** | Standalone (above `broker`) |
| **Inputs → outputs** | Connections and accounts → routed orders |
| **State / persistence** | In-memory value |
| **Tests / examples** | Above; 1 example |
| **iluvtrade consumer** | A broker gateway with several venues |
| **Integration status** | **Not integrated**; iluvtrade's `brokers/service.py` owns connection records and credentials |
| **Dependencies** | More than one live venue |
| **Owner** | AlphaLab (routing semantics); iluvtrade (connections, credentials) |
| **Follow-up** | Phase 6, only once a second venue exists |

---

## 6. Where each capability stands for this product

| Bucket | Capabilities |
|---|---|
| **Available and used by iluvtrade** | Execution path and drivers (`BacktestEngine`, `ReplayBacktest`, `TradingSession`), `RunConfig`, risk limits incl. zoned daily loss, simulator + two commission models, explicit `FillTiming` and `execution_assumptions`, timestamp interpretation (`parse_timestamp`, `normalize_wire_bar`, `resolve_zone`), instrument identity/registry, `PerformanceReport`, `strategy_failures`, `start_strategy`, declared strategy state, `close_slice`, `id_scope`, `digest_run`, broker vocabulary (for the Zerodha adapter) |
| **Available, not exposed** | Universal data engine ingestion, cleaning and quality (blocked from full adoption by two contract gaps, §7.1); cost models beyond commission; research methodology; factor library; point-in-time data; analytics risk model/budgets/benchmarks; scenario; portfolio construction; lifecycle evidence/fingerprints/manifests/certification/portability/rerun; health, comparison and reconciliation; durable run/replay snapshots; capital plans |
| **Available, standalone — needs composition** | `portfolio_optimizer`, `scenario`, `options`/`futures`/`crypto`/`macro`, `scheduler`, `feature_store`, `reporting`, `brokers`, ML/DL/RL, distributed/cloud/cluster, research assistant |
| **Partial or thinly tested** | Library-scale: ML, DL, RL, cloud_research, cluster_scheduler, distributed, research_assistant, reporting. Foundation: feature_store, futures, crypto, macro, experiment_tracking, deployment_manager, scheduler, brokers |
| **Only in a newer, unreleased tree** | None (§2) |
| **In the package but not public** | `alphalab.market.stream.StreamingSource` (§5.2) — must not be imported until exported |
| **Owned by iluvtrade** | Users, organizations, roles, tenancy, auth/MFA, audit, notifications; data fetch controls, approval gate, storage; job orchestration, idempotency, cancellation; broker accounts, credentials, vendor adapters and transports; session controls and kill switch; persistence locations for snapshots; health/alert *delivery*; billing configuration |
| **Owned by RedDesk** | Listings, creators, purchases, exact-version entitlements (`resolve_version`), refunds, reviews, payouts ledger, the certification *workflow* (consuming AlphaLab's certification evidence) |
| **External** | Market, FX, corporate-action, classification, alternative and fundamentals data; broker venues and their credentials (Kite Connect); payment provider; email/push; Redis; PostgreSQL in production; container runtime; a seller-code sandbox; compute for ML |

### 6.1 Five levels, kept apart

"Exists" is not "integrated", and "tested here" is not "certified". Each column
is a separate claim; a later one never implies an earlier one was skipped, and an
earlier one never implies a later one. As of 2026-10-11:

| Capability | In the 4.1.0 package | Public API | Integrated in iluvtrade | End-to-end tested here, real engine | Production / infrastructure certified |
|---|---|---|---|---|---|
| Backtest execution path | yes | yes | yes | yes — SQLite and PostgreSQL 16 (local) | **no** — not run in CI since 4.1; never deployed |
| Paper sessions (`TradingSession`) | yes | yes | yes | yes — SQLite and PostgreSQL (local) | **no** |
| Timestamp interpretation | yes | yes | yes | yes | **no** |
| Fill timing + execution assumptions | yes | yes | yes | yes | **no** |
| Strategy-failure isolation, run identities | yes | yes | yes | yes | **no** |
| Pausable replay (`PausedReplay`) | yes | yes | **no** product path (contract tests only) | engine-level only | **no** |
| Run-snapshot resume for paper sessions | yes | yes | **no** (§9) | **no** | **no** |
| Universal data ingestion / cleaning / quality | yes | yes | **no** (§7.1) | **no** | **no** |
| Research methodology, factor library, point-in-time data | yes | yes | **no** | **no** | **no** |
| Lifecycle evidence, fingerprints, certification | yes | yes | `digest_run` only | `digest_run` only | **no** |
| Broker boundary (`BrokerProtocol`) | yes | yes | Zerodha adapter written to it | against a local protocol double only | **no** — never run against a venue |
| Reconciliation, health, expected/paper/live comparison | yes | yes | **no** | **no** | **no** |
| Live driver (`LiveSession`) | yes | yes | **no** — refused by three gates | **no** | **no** |
| Streaming market data (`StreamingSource`) | yes | **no** | **no** | **no** | **no** |

Nothing in the last column is "yes". Certification needs, at minimum: the CI run
on the published commit (both backends), a built and exercised Docker image, and
— for anything touching a venue — external credentials and verification.

---

## 7. Single-authority audit of iluvtrade against the engine

Found while mapping (2026-10-11). The post-integration hardening slice the same
day resolved 7.2–7.5 and corrected 7.10's documentation; the rest stay open and
are assigned to roadmap phases. Severity is about result correctness, not effort.

| # | Finding | Status |
|---|---|---|
| 7.1 | Two canonicalization authorities for data | **OPEN** — narrowed: AlphaLab now interprets every timestamp; cleaning and quality are still iluvtrade's |
| 7.2 | Bars not stamped at the end of their interval | **RESOLVED** |
| 7.3 | Unreported VWAP / trade count invented | **RESOLVED** |
| 7.4 | Unknown spacing labelled daily | **RESOLVED** |
| 7.5 | Fill timing optimistic and unstated | **RESOLVED** |
| 7.6 | Dataset identity application-minted | **OPEN** — the canonical hash now covers the timestamp reading; the AlphaLab `DatasetVersion` is still not used |
| 7.7 | Strategy registry built at the call site | **OPEN** (next milestone, M2) |
| 7.8 | Application stand-ins for engine evidence | **OPEN** (next milestone, M3) |
| 7.9 | Engine capabilities with no caller | **OPEN** (next milestone, M5) |
| 7.10 | Backtest cancellation between records | **Documentation corrected**; the behaviour is **NOT IMPLEMENTED** (next milestone, M1) |
| 7.11 | Defects the hardening slice found | **RESOLVED** — see below |
| 7.12 | Defects the final verification pass found | **RESOLVED** — see below |
| 7.13 | Defects the pre-commit review found (incl. blocker B1) | **RESOLVED** — see below |

### 7.1 Two canonicalization authorities for data — open, narrowed

`iluvtrade.data` (`schema.py`, `cleaning.py`, `quality.py`, `canonical.py`,
`ingest.py`) detects schemas, cleans with a transformation log and scores
quality. AlphaLab's universal data engine (ADR-0036) does the same through
`alphalab.api`. The hardening slice moved **timestamp interpretation** to
AlphaLab (`alphalab_bridge/temporal.py`: `alphalab.data.parse_timestamp`,
`alphalab.market.normalize_wire_bar`) and left cleaning and quality where they
were, because full delegation would currently lose two things the released
public API does not provide:

* **Exact prices.** AlphaLab's ingestion holds bar prices as `float`
  (`alphalab.data.Bar`, the record `ingest_csv` produces) and converts to
  `Decimal` only at normalization. iluvtrade keeps every price as the exact text
  of the CSV. Delegating would route `2500.05` through a binary float.
* **Day-first / month-first dates.** `alphalab.data.TimestampFormat` has
  `EPOCH_SECONDS`, `EPOCH_MILLISECONDS`, `ISO_8601_AWARE`, `ISO_8601_NAIVE`,
  `DATE_ONLY` — no `DD/MM/YYYY` or `MM/DD/YYYY`. iluvtrade detects the order,
  refuses ambiguity, and rewrites to ISO before handing the value to AlphaLab.

**Proposed AlphaLab follow-up (4.x minor, additive):** a `Decimal`-exact bar
record on the ingestion path (or a stated, tested exactness guarantee for the
float route), and an explicit day/month-order date format. With both, Option A
below becomes a pure substitution.

| Option | What changes | Cost / risk |
|---|---|---|
| **A. Adopt AlphaLab's ingestion behind the existing workspace** (recommended once the follow-up ships) | Keep fetch/SSRF controls, upload limits, storage, the approval gate, the timestamp declaration and the UX; replace detect→clean→report with `api.inspect_csv`/`ingest_csv`/`validate_dataset`/`clean_dataset` and store AlphaLab's findings, transformations and `DatasetVersion` | Migration for existing versions; UI mapping of AlphaLab findings; blocked today by the two gaps above |
| B. Keep iluvtrade's cleaning; adopt AlphaLab for interpretation and identity | Already done for interpretation; identity (7.6) remains | Two cleaning authorities remain |
| C. Status quo before the slice | — | Superseded |

### 7.2 Bars stamped at the start of their interval — resolved

Every import declares what its timestamps mean (`session_date`, `interval_start`,
`interval_end`), the IANA zone for bare dates and wall-clock times, and the **bar
interval** (`session_date` implies `1d`). The interval is never inferred and is
validated **per instrument** (§7.13): the observed spacing can refuse it (a gap
shorter than one interval, a gap not a whole number of intervals, an instrument
with no pair one interval apart unless the import names it as having missing
observations — a user declaration, recorded and shown as unverified) but never replaces it; longer whole-interval gaps are accepted and
counted, with no calendar assumed.
AlphaLab turns the timestamps into the end-of-interval instant its `Bar`
expects: a session date becomes 23:59:59.999999 of that civil date in the zone
(`DateOnlyPolicy.END_OF_DAY` — the end of the calendar day, not the exchange's
close); a start-stamped bar moves by its declared interval
(`normalize_wire_bar`), and a month (no fixed length) is refused. The declaration
and spacing evidence are recorded on the version and change the canonical hash.
Versions without this provenance are refused at every gate, including the worker
and the runner. Tests: `tests/integration/test_market_data_temporal.py`;
mutation-checked.

### 7.3 Unreported VWAP / trade count — resolved

Absent stays `None` through the canonical file to AlphaLab's `Bar`; an impossible
reported value (outside the bar's range, not a number) is dropped with a recorded
transformation. `derive_vwap` is gone from the policy and a request naming it is
refused (422).

### 7.4 Unknown spacing labelled daily — resolved

`timeframe_for` refuses an unestablished interval, and approving or running a
version with irregular spacing is refused with the reason.

### 7.5 Fill timing — resolved

Explicit on every backtest and paper session; default `NEXT_EVENT` (AlphaLab's
no-look-ahead choice); `SAME_EVENT` only by name. Part of the request's identity;
read back from AlphaLab's `execution_assumptions` and shown with every optimistic
assumption AlphaLab lists; a result whose recorded timing differs from the
request is not stored. Tests: `tests/integration/test_fill_timing.py` — including
that the same data and strategy fill exactly one bar later under `next_event`.

### 7.6 Dataset identity is application-minted — open

`backtests/worker.py` passes `dataset_identity=f"dsv-{dataset_version_id}"`.
AlphaLab's content-derived `DatasetVersion` is what `ValidationEvidence`,
manifests and `run_study`'s dataset check expect. Lineage is not lost —
iluvtrade's `canonical_hash`, which now also covers the timestamp reading, is
stored with every run — but it is not the identity the engine's evidence chain
verifies. Resolves with 7.1 Option A.

### 7.7 A strategy registry built at the call site — open

`alphalab_bridge/strategies.py` keeps `IMPLEMENTATIONS: dict[str,
StrategyImplementation]` and `ParameterSpec` bounds — the "dictionary built at
the call site" `strategy.StrategyClassRegistry` (ADR-0035) exists to replace,
and parameter bounds `StrategyDefinition` already records. Next milestone M2.

### 7.8 Application stand-ins for engine evidence — open

`StrategyVersion.content_hash`, listing evidence = a `BacktestRun` id,
certification = an admin's flag. AlphaLab provides `StrategyFingerprint`,
`ReproducibilityManifest`, `ValidationEvidence`, `certify_strategy` and
`rerun_from_manifest` (ADR-0041). Next milestone M3.

### 7.9 Engine capabilities with no caller — open

`broker.reconcile`, `broker.reconcile_snapshot`, `lifecycle.evaluate_health`,
`compare_expected_paper_live`, `reconcile_execution_state`. Next milestone M5.

### 7.10 Backtest cancellation — documentation corrected, behaviour not implemented

`docs/STATUS.md`, `docs/API.md` and the cancel button now say what happens: a
queued job is cancelled outright; a running one is checked only when the engine
finishes, runs to the end, and its result is discarded. Honouring cancellation
**between records** is next milestone M1.

### 7.11 Defects the hardening slice found — resolved

Running the whole suite on PostgreSQL for the first time, and comparing two runs'
stored documents for the first time, found six defects; each now has a test that
fails against the old code (mutation-checked):

| Defect | Where | Effect |
|---|---|---|
| Upload route ran blocking database work on the event loop | `api/v1/datasets.py` | Two concurrent uploads in one workspace froze the worker on PostgreSQL |
| Audit chain read-head-then-insert without a lock; then `FOR UPDATE` deadlocking against foreign-key `KEY SHARE` | `platform/audit.py` | Concurrent audited writes failed; now `FOR NO KEY UPDATE` |
| `seed` columns 32-bit | models + migration 0006 | Roughly half of all backtests failed to save on PostgreSQL |
| NUL character in a path/body reached PostgreSQL | `main.py`, `api/v1/schemas.py` | 500 instead of 400/422 |
| Baseline downgrade dropped indexes it never created | `migrations/0001_baseline.py` | Downgrade to base failed on PostgreSQL |
| **Backtest artifact key built from an unassigned id** | `backtests/service.py` | Every run in an organization wrote `backtests/None/result.json`; an older run's page showed the newest run's fills and report beside its own configuration |

Also fixed: an invalid `policy_json` on upload answered 500 instead of 422.

### 7.12 Defects the final verification pass found — resolved

Re-auditing the integration, including the hardening slice's own changes:

| Defect | Where | Effect | Fix |
|---|---|---|---|
| Paper-session seed from Python's `hash()` | `trading/sessions.py` | Randomized per process, blind to dataset and configuration | `common.seeds.derive_seed` over material inputs, stored on the row |
| Bar interval inferred from the median gap (introduced by the hardening slice) | `data/ingest.py` | Interval-start moves, the bar label and the daily check guessed from spacing | Declared interval; spacing only refuses |
| Worker and paper runner did not re-check dataset provenance | `backtests/worker.py`, `trading/runner.py` | A job or session created before an upgrade would run on a legacy version | `ingest.require_runnable` at both |
| Storage writes not atomic | `common/storage.py` | A crash mid-write left a partial file at the real key | temp file + fsync + `os.replace` |
| Result retrieval did not check ownership | `backtests/service.py` | A legacy row served whatever its shared path held | own key + recorded SHA-256 (0007) + embedded run id, or headline-only with the reason |
| Paper runner did not refuse an engine/request fill-timing mismatch | `trading/runner.py` | Would have run and been mislabelled | refused before any record |
| NUL in multipart form fields | `api/v1/datasets.py` | Reached PostgreSQL | 422 |
| SQLite contention matched by message text | `api/errors.py` | Fragile | `sqlite_errorcode` (BUSY/LOCKED), text only as fallback |
| 0006 downgrade lossy | migration 0006 | Down-and-up would rewrite `next_event` sessions as `same_event` | Downgrade refused while such rows exist |


### 7.13 Defects the pre-commit review found — resolved

The read-only review returned REVIEW BLOCKED on B1; the corrections pass fixed it
and the concerns below, each with regression tests (mutation-checked).

| Defect | Where | Effect | Fix |
|---|---|---|---|
| **B1: one instrument's bars validated another's interval** | `data/temporal.py` `check_spacing` | One-interval pairs were counted across the whole file, so a 15-minute instrument vouched for an hourly one declared `15m` `interval_start`; each hourly bar moved 15 minutes forward — its close stamped 45 minutes early (look-ahead) | Per-instrument validation; an instrument with no own one-interval pair is refused unless named in `missing_observations_confirmed_for` (exactly the ambiguous instruments; previewed before import; recorded; shown as user-declared, not verified); record version 4, and records 2 and 3 refused at every gate. The acceptance check found the first fix's single dataset-wide checkbox let the reviewed case through on one unscoped "yes" |
| A blanket hour of slack for daily/weekly bars | `data/temporal.py` | A 23- or 25-hour gap between UTC instants passed as a day | Wall-clock arithmetic for dates and zoned times, exact absolute arithmetic for aware/epoch values; no tolerance |
| Monthly bars accepted on a 27–32-day range | `data/temporal.py` | 27-day gaps passed as months | Calendar-month counting on a consistent day; aware/epoch monthly refused |
| Ambiguous / nonexistent wall-clock times read silently | `data/temporal.py` | The autumn repeated hour was placed on one guess | Refused; start-stamped bars spanning an offset change refused |
| UI showed the median spacing as the frequency | `Datasets.tsx`, `DatasetDetail.tsx`, `Research.tsx` | A sparse 15m version displayed as "1h" | Declared interval with its status; observed spacing labelled separately |
| Out-of-order judged file-wide | `data/cleaning.py` | A symbol-grouped file lost its second instrument's rows | Per-instrument high-water mark |
| Advisory gate matched package names; npm errors read as clean | `scripts/` | A new advisory on `react-router`, or an offline audit, passed | GHSA id + package; fails closed |
| `/datasets/inspect` parsed on the event loop | `api/v1/datasets.py` | A large file stalled the worker | Thread pool; tested |
| User-declared intervals stopped at the dataset | backtests, sessions, `reddesk/marketplace.py` | Results, paper sessions and listing evidence repeated the interval without saying it was only declared for some instruments | Evidence snapshot per run and session (migration 0008), repeated in results, session logs and listing evidence; legacy rows "not recorded" |
| Preview decisions not bound to the file | `Datasets.tsx`, `data/ingest.py` | A confirmation made after checking file A was sent with file B | `preview_id` over the bytes' SHA-256, declaration and policy, required with a confirmation and recomputed on upload/fetch; UI state keyed to each file selection |
| Canonical file written in place, read unverified | `common/storage.py`, worker, runner | A truncated or altered file would feed the engine different bars under the same version id | Atomic `write_jsonl`; `ingest.canonical_rows` verifies the recorded SHA-256 and row count before parsing |


## 8. Product workflows, and the earliest incomplete link in each

| # | Workflow | Built today | Earliest incomplete link | Gap is | AlphaLab capability to consume |
|---|---|---|---|---|---|
| 1 | User → workspace → dataset upload/fetch → quality/provenance → research | Through approval, with provenance and a quality report | The approved dataset carries an application identity and start-stamped bars (§7.1, §7.2, §7.6), so nothing research-grade can consume it | Application | `api.ingest_csv`/`validate_dataset`/`clean_dataset`, `data.BarStamp`, `DatasetVersion` |
| 2 | Dataset/version → features/factors → experiment → reproducible result | Nothing | No study/feature API or job type | Application | `api.run_study`, `factor_library`, `research`, `experiment_tracking` (persisted by iluvtrade) |
| 3 | Strategy/version → configuration → async backtest → metrics/artifacts → saved run | Yes: idempotent jobs, failure refusal, engine identities, full report | Fill-timing assumption not chosen or shown (§7.5); cancellation granularity (§7.10) | Application | `FillTiming`, `ExecutionCostModel` |
| 4 | Backtest → robustness / walk-forward / scenario | Nothing | No analysis endpoints over a stored run | Application | `research.walk_forward_analysis`, `parameter_robustness`, `deflated_sharpe_ratio`, `bootstrap_statistics`, `monte_carlo_simulation`, `scenario` |
| 5 | Research result → publication evidence → RedDesk listing | Listing cites a `BacktestRun` on the exact version | Evidence is not an engine evidence object (§7.8) | Application | `evidence_from_backtest/study`, `manifest_for_run`, `build_fingerprint`, `certify_strategy` |
| 6 | Listing → exact version → entitlement → permitted use | Yes (`resolve_version`) | Seller code cannot run (no sandbox) | Application + external infrastructure | `strategy.StrategyClassRegistry` once code is uploaded; `evaluate_portability` |
| 7 | Strategy/version → paper session → pause/resume/restore → orders/fills/state → audit | Yes, except resume restarts at record 1 | No checkpoint persistence (§9) | Application | `run_snapshot.capture/restore`, `RunEngine.resume`, `FileRunStateStore` semantics, `RetentionPolicy` |
| 8 | Broker account → credentials/OAuth → vendor adapter → broker state and reconciliation | Encrypted credentials; Zerodha adapter to `BrokerProtocol` against a local double | No reconciliation call and no capability declaration; venue never contacted | Application + external credentials | `broker.reconcile_snapshot`, `apply_venue_event`, `core.capabilities` |
| 9 | Strategy/session → broker capability checks → live boundary, risk, kill switch | Live refused by three gates; kill switch | No live loop | Application + external | `LiveSession`, `route_order`, `live_snapshot`, `authorize_run` |
| 10 | Portfolio state → attribution, analytics, multi-currency, risk views | `PerformanceReport` incl. P&L by strategy/asset | Sector needs classification data; FX needs rates; no risk-model views | Application + external data | `analytics.risk_model`, `risk_budget`, `benchmark_statistics`, `portfolio.fx`, `multi_strategy` |
| 11 | Purchase/subscription → entitlement → refunds/revocation | Yes, recording only | No payment provider (money does not move) | External | None — not an engine concern |
| 12 | User/team/workspace administration → permissions → tenant isolation | Yes, asserted over the whole route table | — | — | Later: back `lifecycle.PermissionAuthority` with iluvtrade roles (Phase 7) |
| 13 | Jobs/trading events → notifications, audit, health, failure reporting | In-app notifications, audit chain, strategy-failure events | No structured session health; no external delivery | Application + external infrastructure | `lifecycle.evaluate_health`, `runtime.live_health` |

Every earliest incomplete link is application work or an external dependency.
One AlphaLab-side gap sits further down workflow 9: the streaming market-data
source is not public API (§5.2), so a live feed needs either an AlphaLab export
or an iluvtrade `MarketDataSource` over the public WebSocket transport.

---

## 9. Next vertical slice: durable paper-session resume

A paper session paused today restarts from record 1 when resumed
(`trading/runner.py`, `docs/TRADING.md` item 5). The engine half is complete and
tested; the application half is unbuilt. Kept out of the AlphaLab 4.1
compatibility patch on purpose.

**Which AlphaLab API.** The paper runner drives `TradingSession.advance` record
by record, so the matching contract is the **run snapshot**, not `PausedReplay`
(which belongs to the `ReplayBacktest` driver):

```text
pause:   payload = persistence.serialize(run_snapshot.capture(run_state))   # inside run_scope
         store payload at (session_id, sequence); record records_processed = N
resume:  snapshot = run_snapshot.from_primitives(persistence.deserialize(payload))
         objects = RunObjects(same strategies, simulator, sizing, fill policy — configured identically)
         run_state = run_snapshot.restore(snapshot, objects)          # StateDecodeError on any mismatch
         with RunEngine.resume(run_state):                             # continues the id stream; installs nothing else
             for record in records[N:]:                                # resumes at N+1; nothing replayed
                 run_state = close_slice(...); run_state, step = TradingSession.advance(...)
```

**Application boundaries to build.**

| Piece | Owner | Notes |
|---|---|---|
| `session_checkpoints` table (org-scoped: session id, sequence, records processed, payload key, payload SHA-256, AlphaLab version, dataset version id + canonical hash, configuration digest, strategy version id + content hash, created at) | iluvtrade | `OrgScopedMixin`; Alembic revision written in the nullable → backfill → tighten form (memory: SQLite migration trap) |
| Payload bytes in blob storage via `common/storage.py` | iluvtrade | Mirrors `FileRunStateStore`'s guarantees: atomic write, digest verified before any decode |
| Capture on pause (and optionally every K records under a `RetentionPolicy`) | iluvtrade runner, AlphaLab capture | Inside `run_scope`; never mid-step |
| Validation before restore | iluvtrade | Refuse if AlphaLab version, dataset hash (which covers the timestamp reading), configuration digest, fill timing or strategy content hash differs from the checkpoint's. AlphaLab's `configuration_id` already covers the pipeline's fill timing (verified 2026-10-11), so a resume under another timing is also a different configuration to the engine |
| Restore + `RunEngine.resume` + continue at N+1 | AlphaLab | `StateDecodeError` / `StorageError` → session `FAILED` with the reason; **never** retried with other inputs or silently restarted from record 1 |
| Retention | iluvtrade | Keep the latest K checkpoints per session; delete payloads with their rows |

**Acceptance.** A session paused at record N in one process and resumed in a
fresh process finishes with the same `digest_run(...).result_id` as an
uninterrupted session; a resume against a different dataset version, strategy
parameters, commission or AlphaLab version is refused and the session says why;
a corrupted payload is refused; the cross-tenant matrix covers the new routes;
the runner's background thread is joined in tests before settings reset
(memory: daemon thread outlives its test).

---

## 10. Roadmap, dependency-ordered

Ordered by dependency, not by size. Phases 2/5 and 9/10 can run in parallel with
their neighbours once their prerequisites hold.

**Progress (2026-10-11).** P0: everything local passes on SQLite and PostgreSQL;
CI on the published commit and a Docker build remain. P1: timestamp truth and
honest missing values done; one canonicalization authority (7.1) and engine
dataset identity (7.6) remain, blocked on the AlphaLab follow-up in §7.1. P2:
explicit fill timing done; robustness analysis not started. P10: the PostgreSQL
test path exists and passes locally; it has not run in CI.

### The next milestone, dependency-ordered

None of these is built. Each names the AlphaLab public contract it consumes and
the boundary iluvtrade owns.

| # | Work | Depends on | AlphaLab public API | iluvtrade owns | Done when |
|---|---|---|---|---|---|
| M0 | Publish; certify in CI | — | `tests/consumer_contract.py` | the commit, CI, the Docker build | all four CI jobs green on the commit, both backends; image built and `/api/health` answers from it |
| M1 | Cancellation during long backtests | M0 | none (application behaviour) | check `cancel_requested_at` in `run_backtest_observed`'s per-step callback, every N records; job ends `CANCELLED` with records processed | a job cancelled mid-run stops within N records and stores nothing; STATUS row moves to IMPLEMENTED |
| M2 | Replace the call-site registry (7.7) | M0 | `strategy.StrategyClassRegistry`, `StrategyDefinition`, `instances_for` | which implementations a deployment offers; parameter UX | `IMPLEMENTATIONS` and `ParameterSpec` gone; an unknown or duplicate identity refused by AlphaLab; parameter bounds read from the definition |
| M3 | Engine evidence for RedDesk (7.8) | M2 (identity reaches fingerprints); full dataset evidence also needs 7.6 | `lifecycle.build_fingerprint`, `manifest_for_run`, `evidence_from_backtest`, `certify_strategy`, `verify_*`, `rerun_from_manifest` | listing workflow, review, licensing, what is shown publicly | a listing's evidence verifies and reruns to the same `result_id`; certification shows AlphaLab's properties, never a score; `manifest_gaps` names the dataset gap until 7.6 closes |
| M4 | Durable paper-session checkpoint/resume (§9) | M2 (rebuilding `RunObjects` by identity) | `runtime.run_snapshot.capture/restore/from_primitives`, `RunEngine.resume`, `persistence.serialize/deserialize`, `RetentionPolicy` | checkpoint table, tenant-scoped payload storage, validation of dataset / configuration / strategy / engine identity before restore, retention | §9's acceptance: paused in one process, resumed in another, same `result_id`; any changed input refused, never resumed |
| M5 | Broker health, reconciliation and lifecycle evidence in the product (7.9) | M4 (durable state to reconcile across restarts); P6 capability declarations | `lifecycle.evaluate_health`, `reconcile_execution_state`, `reconcile_accounts`, `compare_expected_paper_live`, `broker.reconcile_snapshot`, `runtime.live_health` | presenting findings, alert delivery, operator actions — never automatic remediation | health and reconciliation reports shown per session from AlphaLab's own categories; nothing mutates state on a finding; live remains DISABLED |

```text
P0 certify 4.1 ──► P1 data truth ──► P2 backtest analysis ──► P3 research workspace ──► P4 lifecycle + RedDesk evidence
        │                 └──────────────────────────────────────────────────────────────┘        │
        ├──► P5 durable paper resume                                                               ▼
        └──► P10 infrastructure track (PostgreSQL suite, workers, sandbox) ─────────► P6 broker gateway ──► P7 paper/live ops
                                                                                    P8 portfolio/risk views (after P3)
                                                                                    P9 billing/creator economics (after P4)
```

### P0 — Certify the AlphaLab 4.1 integration

| | |
|---|---|
| User problem | The platform runs on a verified engine and installs the same way everywhere |
| AlphaLab capability / interface | The whole public API as pinned; `tests/consumer_contract.py` |
| iluvtrade subsystem | `backend/pyproject.toml`, CI, Dockerfile, `scripts/verify-alphalab.sh` |
| New service/UI | None |
| Prerequisites | The owner publishes the working tree |
| Acceptance | All four CI jobs green on the 4.1 commit; the Docker image builds; the consumer contract ends `9 checks passed` in CI |
| Tests | Existing suites; dependency-declaration tests |
| External infrastructure | GitHub Actions; a Docker host |
| Security/compliance | Hash-pinned engine; advisory gate |
| Deferred | Making the PostgreSQL CI step actually run the suite on PostgreSQL (P10) |

### P1 — Data truth: one canonicalization authority, end-stamped bars, engine dataset identity

| | |
|---|---|
| User problem | A dataset means the same thing to the workspace, the backtest and the evidence that cites it |
| AlphaLab capability | Universal data engine; normalization with `BarStamp`; `DatasetVersion` |
| Interface | `api.inspect_csv`, `ingest_csv`, `validate_dataset`, `clean_dataset`, `normalize_records`, `to_market_dataset` |
| iluvtrade subsystem | `iluvtrade/data/`, `alphalab_bridge/market.py`, dataset models |
| New service/UI | "Bars are stamped at the start/end of their interval" declaration at approval; AlphaLab findings in the quality view |
| Prerequisites | Decision §7.1 |
| Acceptance | A dataset version records AlphaLab's `DatasetVersion`; `BacktestResult.dataset_id` equals it; daily bars reach the engine end-stamped; unreported `vwap`/`trade_count` are `None`; older versions still run and are labelled as application-identified |
| Tests | Golden CSVs through both stamps; a migration test with rows present; parity of figures for a start-stamped file before/after (shifted timestamps, same economics) |
| External | None |
| Security | Fetch controls and approval gate unchanged |
| Deferred | Corporate actions, calendars, adjusted basis until a data source supplies them |

### P2 — Reproducible backtests with stated assumptions, and robustness analysis

| | |
|---|---|
| User problem | "How optimistic is this result, and does it survive?" |
| AlphaLab capability | `FillTiming`; cost models; `research` run-evaluation layer; synthetic `scenario` |
| Interface | `ExecutionPipelineConfig.fill_timing`, `ExecutionCostModel`, `walk_forward_analysis`, `parameter_robustness`, `deflated_sharpe_ratio`, `probabilistic_sharpe_ratio`, `bootstrap_statistics`, `monte_carlo_simulation`, `Scenario.apply` |
| iluvtrade subsystem | `BacktestRequest`, `runconfig`, `backtests/`, results UI |
| New service/UI | Fill-timing and cost choices on submission, shown on every result; an analysis job type over a stored run; cancellation between records |
| Prerequisites | P0; P1 for walk-forward splits over a dataset |
| Acceptance | Each result states its fill timing and costs; analysis outputs carry AlphaLab's sample sizes and never a single score; cancelling a running job stops it within one record |
| Tests | Same-event vs next-event on a hand-checked series; analysis determinism by result id |
| External | None |
| Security | Job quotas (rate limits) for analysis jobs |
| Deferred | Historical scenarios (need observations) |

### P3 — Research workspace

| | |
|---|---|
| User problem | Explore features and signals before writing a strategy, reproducibly |
| AlphaLab capability | Study methodology, factor library, point-in-time ingestion, regimes, event studies, experiment tracking, research assistant sweeps |
| Interface | `api.run_study`, `api.study_panels`, `FeatureDefinition`, `walk_forward_splits`, `apply_purge_and_embargo`, `classify_regimes`, `event_study`, `api.ingest_observations/events/fundamentals`, `research.parameter_sweep` |
| iluvtrade subsystem | New `research/` service beside `backtests/`, reusing the worker pool |
| New service/UI | Study definitions, study jobs, result views with diagnostics and sample sizes; tenant-scoped experiment history |
| Prerequisites | P1 (engine dataset identity is checked by `run_study`) |
| Acceptance | A study re-run on the same dataset version gives the same `derive_result_id`; a study handed another dataset is refused |
| Tests | Determinism; refusal on dataset mismatch; cross-tenant matrix extended |
| External | Optional alternative/fundamentals data |
| Security | Uploaded alternative data goes through the same fetch/upload controls |
| Deferred | ML/DL/RL (library-scale; revisit with a user need and compute) |

### P4 — Strategy lifecycle and RedDesk exact-version evidence

| | |
|---|---|
| User problem | A buyer can verify what a listing claims, against the exact version licensed |
| AlphaLab capability | Fingerprints, manifests, evidence, certification, portability, rerun; strategy registry and definitions |
| Interface | `build_fingerprint`, `manifest_for_run`/`manifest_for_study`, `evidence_from_backtest`/`evidence_from_study`, `evaluate_policy`, `certify_strategy`, `verify_certification_report`, `rerun_from_manifest`, `StrategyClassRegistry`, `StrategyDefinition` |
| iluvtrade subsystem | `strategies/service.py`, `reddesk/marketplace.py`, `alphalab_bridge/strategies.py` |
| New service/UI | Evidence panel on listings (fingerprint, manifest gaps, certification properties with statuses); "re-run this evidence" for entitled buyers |
| Prerequisites | P1 (evidence must name the bytes), P2 (stated assumptions) |
| Acceptance | A listing's evidence verifies (`verify_*`) and reruns to the same `result_id`; certification shows AlphaLab's properties, never a score; `resolve_version` unchanged |
| Tests | Tampered evidence refused; rerun equality |
| External | None |
| Security | Evidence is public-by-listing; manifests must not leak tenant data |
| Deferred | Independent certification body; seller-code execution (P10 sandbox) |

### P5 — Durable paper-session resume

As §9. Prerequisites: P0. Independent of P1–P4.

### P6 — Broker capability registry and vendor-adapter gateway

| | |
|---|---|
| User problem | Know before running whether a strategy's orders are supported by the user's broker |
| AlphaLab capability | Capability model; normalized venue events; request ledgers; snapshot reconciliation; multi-broker router |
| Interface | `CapabilityDeclaration`, `check_compatibility`, `lifecycle.unmet_broker_requirements`, `evaluate_portability`, `apply_venue_event`, `issue_cancel`/`issue_modify`, `reconcile_snapshot`, `brokers` |
| iluvtrade subsystem | `brokers/`, `alphalab_bridge/broker.py` |
| New service/UI | A declaration per connector; compatibility shown on session creation; reconciliation reports |
| Prerequisites | P0 |
| Acceptance | Each connector publishes a declaration; incompatible sessions refused with AlphaLab's reasons; Zerodha events translated to `VenueEvent` against the local double |
| Tests | Event-outcome table cases (duplicate, stale, conflict, unknown order) against the double |
| External | Kite Connect credentials for any real verification |
| Security | Credential handling unchanged; webhook signature verification when postbacks arrive |
| Deferred | Any real venue contact |

### P7 — Paper/live controls, reconciliation and operational safety

| | |
|---|---|
| User problem | Operate sessions safely and know when to intervene |
| AlphaLab capability | `LiveSession`, `live_snapshot`, `live_health`, `lifecycle.evaluate_health`, `compare_expected_paper_live`, `reconcile_execution_state`, `reconcile_accounts`, `Governance`/`authorize_run`/deployment ledger |
| iluvtrade subsystem | `trading/`, `platform/notifications.py`, roles |
| New service/UI | Health panel; expected/paper/live comparison; reconciliation screen; deployment approvals with separation of duties backed by iluvtrade roles via `PermissionAuthority` |
| Prerequisites | P5, P6 |
| Acceptance | Health findings carry AlphaLab categories and severities; nothing auto-remediates; a live session is still refused unless all gates *and* an `authorize_run` pass |
| Tests | Governance refusals; reconciliation mismatch classes |
| External | Venue, feed, credentials for live; alert delivery |
| Security | Live remains DISABLED until the venue is verified externally |
| Deferred | Turning live on |

### P8 — Portfolio, risk and attribution views from the engine

| | |
|---|---|
| User problem | Understand risk and construction across strategies and currencies |
| AlphaLab capability | Risk model, budgets, cross-strategy, benchmark, factor risk, multi-strategy books, FX valuation, construction, capital plans, historical scenarios |
| Interface | `analytics.*`, `portfolio.multi_strategy`, `portfolio.fx`/`fx_feed`, `portfolio_optimizer.construct`, `allocation` capital plans, `api.sector_classification` |
| iluvtrade subsystem | Results and a portfolio area |
| New service/UI | Risk and attribution views; construction tool producing targets (never orders) |
| Prerequisites | P3 (return/covariance estimation), classification and FX data |
| Acceptance | Every number is read from an AlphaLab report; conversions list their rates; budgets reported, never enforced |
| Tests | Engine-identity of figures (no recomputation in handlers) |
| External | Classification, FX, benchmark data |
| Security | — |
| Deferred | Derivatives (options/futures/crypto) until data and broker support exist |

### P9 — Billing, subscriptions, creator economics and certification workflow

Owned by iluvtrade/RedDesk; AlphaLab contributes only the certification and
evidence objects from P4. Prerequisites: P4 for evidence-backed certification;
a payment provider contract (external) for money movement. `ManualProvider`
remains until then.

### P10 — Scaling, isolated strategy execution and production infrastructure

| | |
|---|---|
| User problem | Many users, many runs, and creators' own code |
| AlphaLab capability | Determinism per process (LIM-006: one run per process); snapshots for moving work between processes |
| iluvtrade subsystem | `backtests/worker.py`, `trading/runner.py`, deployment |
| New service/UI | Out-of-process workers; PostgreSQL-backed test suite; shared rate limits; the sandbox in `SANDBOX_CONTRACT.md` |
| Prerequisites | P0 |
| Acceptance | The test suite runs against PostgreSQL in CI (today the autouse SQLite fixture prevents it); runs execute one per process; seller code runs only inside the sandbox |
| External | PostgreSQL, Redis, container/VM isolation |
| Security | Sandbox is the precondition for any uploaded code |
| Deferred | AlphaLab's `distributed`/`cloud_research`/`cluster_scheduler` are process-local libraries and are **not** a substitute for this infrastructure |

---

## 11. Keeping this page true

On every AlphaLab upgrade:

1. Re-derive §2 from the new release's `SHA256SUMS`, manifest and tag.
2. Re-run the import-graph pass that produced §3.2 and the counts in §5
   (`docs/api/public_api.json` for public names; the release source for edges).
3. Read the release's CHANGELOG section and migration table, then every ADR it
   adds, and update §4 and the affected §5 blocks.
4. Confirm §6's "only in a newer tree" row against the AlphaLab remote.
5. Re-check §7 items that the release could have changed.

A capability moves to **Integrated** only when a product path calls it and a
test in this repository fails if the call is removed.
