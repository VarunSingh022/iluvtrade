# Architecture

## The problem this shape solves

AlphaLab is a mature quantitative engine: 48 packages, an execution pipeline
that every environment shares, portfolio accounting that satisfies an exact
identity, and a documented parity guarantee between backtest, replay, paper and
live. The temptation when building a product on top of it is to reimplement
small pieces "for convenience" — a P&L sum in an API handler, a position netted
in a view, a strategy resolved by name. Each one creates a second authority for
a question that already has an answer, and the two disagree the first time
something unusual happens.

So the architecture is organised around keeping that from being possible.

## Three authorities

### AlphaLab — every quantitative and trading semantic

An installed dependency (`alphalab==3.0.0`), never vendored. It owns:

| Concern | AlphaLab type |
|---|---|
| Market data | `market.Bar`, `market.Quote`, `market.Tick`, `market.MarketRecord` |
| Instrument identity | `instrument.InstrumentRecord` (uuid5 over a canonical key) |
| Dataset | `backtesting.MarketDataset`, `market.source.SequenceSource` |
| Strategy contract | `strategy.StrategyProtocol`, `strategy.Intent` |
| Run configuration | `runtime.run.RunConfig`, `runtime.ExecutionPipelineConfig` |
| Execution path | `runtime.ExecutionPipeline` (market → strategy → allocation → risk → OMS → execution → portfolio → analytics) |
| Drivers | `backtesting.BacktestEngine`, `backtesting.ReplayBacktest`, `runtime.session.TradingSession`, `runtime.live.LiveSession` |
| Accounting | `portfolio.PortfolioEngine`, `portfolio.PortfolioValuation` |
| Risk | `risk.RiskLimits`, `risk.RiskDecision` |
| Analytics | `analytics.PerformanceReport` |
| Broker boundary | `broker.BrokerProtocol`, `broker.PaperBroker`, `broker.reconcile` |

### RedDesk — what an organization may run

`backend/iluvtrade/reddesk/`. Listings, versions offered, purchases,
entitlements, reviews, payouts. It decides permission and never decides what
running means.

The load-bearing function is `entitlements.resolve_version()`, which answers
with a **concrete strategy version id** or raises. There is no boolean
"allowed?" anywhere, because a boolean invites the caller to then pick a version
itself — and picking "the latest" is the defect the whole versioning design
exists to prevent.

### Platform — who is asking

`backend/iluvtrade/platform/`. Users, organizations, memberships, roles,
sessions, audit, notifications. It knows nothing about markets or money.

## The engine boundary

Every `import alphalab` in this application is inside
`backend/iluvtrade/alphalab_bridge/`:

| Module | Translates |
|---|---|
| `instruments.py` | provider symbols → AlphaLab instrument identity |
| `market.py` | canonical dataset rows → `MarketDataset` / `SequenceSource` |
| `runconfig.py` | application settings → `RunConfig`, including risk limits |
| `strategies.py` | the strategy implementations this deployment offers |
| `engine.py` | drives `BacktestEngine` / `TradingSession`, owns `id_scope` |
| `results.py` | AlphaLab result objects → JSON |
| `broker.py` | re-exports AlphaLab's broker vocabulary for connectors |

`tests/unit/test_engine_boundary.py` walks the source tree with the `ast` module
and fails if `alphalab` is imported anywhere else. A second test fails if this
repository defines a class named `PortfolioEngine`, `BacktestEngine`,
`ExecutionPipeline`, `RiskEngine` or similar.

### Three integration details that are not guessable

These cost real debugging time and are written down so they do not again:

1. **`asset_id` must be a UUID.** A provider symbol like `"RELIANCE"` travels
   the whole execution path and is refused by `alphalab.core.Fill` at the *first
   fill*, far from the cause. AlphaLab already owns the resolution:
   `InstrumentRecord(symbol, asset_type, exchange, currency).asset_id` derives a
   uuid5 over the canonical key. We do not invent a mapping.
2. **Two distinct `OrderId` types, on purpose.** `alphalab.oms.ids.OrderId` is a
   dataclass with `.value: UUID` (what an OMS `Order` carries).
   `alphalab.core.ids.OrderId` is a `NewType` over `str` (what a `Fill`
   carries). `fill.fill_id.value` raises. `results.py` reads each by its own
   shape and says why.
3. **`id_scope(seed)` must wrap a record-by-record loop.** AlphaLab's own
   drivers wrap theirs. Advancing outside it drops identifier generation back to
   `uuid4`, and a run whose numbers match but whose identities differ is not
   reproducible. `engine.run_scope()` exists so a pausable session can hold it.

## Data flow

### Ingestion — one path, two sources

```
upload ─┐
        ├─→ DataSource (bytes + sha256 + origin, kept forever)
fetch ──┘        │
                 ▼
          detect_schema()        proposes a meaning for each column
                 ▼
          clean(policy)          every change logged, every rejection reasoned
                 ▼
          analyse()              the quality report
                 ▼
          DatasetVersion         PENDING_APPROVAL — nothing may use it
                 ▼
          approve()              the human gate
                 ▼
          canonical.jsonl        immutable, hashed
```

`ingest_upload` and `ingest_url` differ only in how bytes are obtained. Both
call one `_canonicalize()`, so there is exactly one implementation of
detect → validate → clean → report → store.

### Research

```
BacktestRequest ─→ idempotency key ─→ BacktestJob (QUEUED)
                                           │
                            worker thread claims it (RUNNING)
                                           │
              canonical.jsonl ─→ MarketDataset ─→ AlphaLab
                                           │
                            BacktestRun + artifact (COMPLETED)
```

The worker drives `run_backtest_observed`, which is `BacktestEngine.run` with
each record's `ExecutionPipelineResult` handed to a callback. That is the only
place a **risk refusal** is visible: a refused order never becomes an order, a
report or a fill, so without it a run that refused every signal would be
indistinguishable from a strategy that found none.

### Paper trading

```
DatasetVersion ─→ SequenceSource ─→ TradingSession.advance() one record at a time
                                              │
                        between records: stop? pause? kill?
                                              │
                        every 25 records or on a fill: project()
```

Record-by-record rather than `TradingSession.run()` because between records is
the only place a stop can be honoured without leaving engine state half-applied.

A session is claimed with a **conditional UPDATE** (`STARTING → RUNNING`), not a
read-then-write: the API launches a runner on `/start` and a caller may also
drive a session synchronously, and two runners over one session interleave their
projections into a book that matches neither run.

## Cross-cutting request concerns

Applied as dependencies rather than middleware, so each is visible on the route
that needs it and absent from the ones that do not:

| Concern | Where | Coverage |
|---|---|---|
| Authentication | `deps.current_principal` | all 67 routes but health, register, login |
| Authorization | `deps.require_trader` / `require_admin` | per route |
| Tenancy | `tenancy.scoped` / `require_owned` | every tenant-scoped read |
| CSRF | `deps.current_principal` | all 33 mutating routes |
| Rate limiting | `deps.rate_limit` / `rate_limit_anonymous` | login, register, ingest, fetch, backtest, broker auth, marketplace, session |
| Error shape | `api.errors` | every exception, including framework-raised ones |

Each is asserted **against the whole route table** rather than a sample, in
`tests/security/test_api_contract.py`. A route added later is covered without
anyone remembering to extend the tests.

## Persistence

| Store | Holds | Why |
|---|---|---|
| Relational DB | application domain state | transactions, constraints, joins |
| Blob storage | raw uploads, canonical datasets, run artifacts | market data does not belong in transactional tables |

SQLite by default with `foreign_keys=ON` and WAL — the pragma matters, because
`ON DELETE CASCADE` silently does nothing without it, which is a tenant-isolation
hole that looks like working code. Postgres for anything beyond one machine.

`UtcDateTime` normalises every timestamp at the column boundary, because SQLite
has no timezone-aware storage and a naive value read back is the first thing to
break a comparison against `utcnow()`.

**Alembic owns the schema**; `create_all` is a development convenience and is
forced off in production. See `DEPLOYMENT.md`.

## Versioned identity

Everything a result depends on is immutable and addressed by identity:

| Entity | Immutable after | Carries |
|---|---|---|
| `DataSource` | always | sha256 of exactly what arrived |
| `DatasetVersion` | approval | canonical hash, schema, quality, transformations |
| `StrategyVersion` | publication | `content_hash` over behaviour-deciding fields |
| `BacktestRun` | always | every input by identity, plus the engine version |

`strategies.service.verify_integrity()` recomputes the hash, so a mutation that
bypassed the service entirely — a stray `UPDATE`, a migration bug — is
detectable rather than invisible.
