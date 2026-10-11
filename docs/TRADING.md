# Trading

## Paper and live

AlphaLab's own parity statement, which this application relies on rather than
reimplements:

| Layer | Backtest | Replay | Paper | Live |
|---|---|---|---|---|
| Market record | canonical | canonical | canonical | canonical |
| Strategy → intents | same | same | same | same |
| Allocation → requests | same | same | same | same |
| Risk → decision | same | same | same | same |
| OMS lifecycle | same | same | same | same |
| **Execution venue** | simulator | simulator | simulator | **broker** |
| Portfolio accounting | same | same | same | same |
| Record source | dataset | cursor | live | live |
| Clock | record | record | wall | wall |

Everything above the execution venue is one code path. That is why a paper
session and a backtest over one dataset version with one seed produce the same
book — asserted in `tests/integration/test_workflow.py`.

## What runs today

**Paper sessions run end to end.** `trading/runner.py` drives
`TradingSession.advance()` one record at a time inside AlphaLab's `id_scope`,
honours stop/pause/kill between records, projects the engine's state every 25
records and on every fill, and logs every refusal. Each session states its
`fill_timing` (`next_event` by default; `same_event` only when asked for — see
`docs/RESEARCH.md`), stored on the row so a restart replays under the same
execution, and a snapshot of its dataset version's **temporal evidence** (what
the bar interval rests on, including any instruments whose interval the
importer only declared), taken at creation, shown on the session and logged as
a `session.temporal_evidence` event on every start — kept apart from the
execution assumptions. A session created before snapshots existed is refused at
start rather than given evidence it never recorded; and its log ends with AlphaLab's own record of the execution
assumptions in force (an `execution_assumptions` event, a warning when any is
optimistic). Sessions created before the setting existed are recorded as
`same_event`, which is what they ran. Before processing any record the runner
reads the timing from AlphaLab's own record and refuses a session whose applied
timing differs from the one it asked for. It also re-checks that the session's
dataset version has validated timestamp provenance, so a session created before
an upgrade cannot run on a version imported under the old UTC reading.

A session's default seed is derived from its material inputs by the documented
digest in `iluvtrade.common.seeds` (identical in every process) and stored on the
row; every run, restart and reconstruction reads that stored value. Until
2026-10-11 it came from Python's per-process `hash()`.

**Live sessions do not run**, and the reason is structural rather than a check
that could be forgotten: `ExecutionMode.LIVE` is **never constructed anywhere in
this repository**, and `routing=` is never assigned. AlphaLab derives routing
from the mode — `LIVE` routes `EXTERNAL`, every other mode routes `SIMULATED` —
and `RunConfig` forces the pipeline's routing to agree with its own mode. So no
run this application configures can reach a venue, whatever a request contains.
`tests/security/test_live_trading_gates.py` asserts both absences against the
source.

On top of that: three gates refuse creation, a fourth refuses a paper broker
backing a live session, no route accepts or mutates `mode`, and the paper runner
refuses a `LIVE` row outright rather than executing it against the simulator —
which would produce simulated fills labelled as live, the worst available
failure.

## The live gap, precisely

AlphaLab's `runtime.session.TradingSession` is explicitly not a live loop. A
live run needs `runtime.live.LiveSession`, which settles venue-reported fills,
advances the run, and routes newly working orders **in that order** — so a fill
already known reaches the portfolio before the strategy is dispatched.

What remains to be built:

1. A live runner driving `LiveSession` with a broker binding.
2. A market-data source implementing `MarketDataSource` against a real feed.
3. A reconciliation loop calling `alphalab.broker.reconcile()` on a schedule and
   raising a `reconciliation_mismatch` event when the books diverge.
4. Wall-clock staleness gating via `RunConfig.max_market_data_age_seconds` —
   already supported by the engine, unused by the paper runner because a
   replayed dataset's clock is the record itself and nothing is ever stale.
5. Durable mid-run resume. A paper session resumed today replays from the
   beginning of its dataset, which is honest for a replay but wrong for a live
   run. What exists: AlphaLab 4.1 can pause a replay and resume it, in this
   process or another, to the byte-identical result (`PausedReplay`,
   `backtesting.replay_snapshot`, `api.resume_replay`); a stopped run continues
   the same way through `runtime.run_snapshot` and `RunEngine.resume`; and every
   strategy here declares its memory, so such a resume continues it rather than
   restarting it empty (asserted against the real engine). What is unbuilt is
   the application half: a per-session checkpoint table and tenant-scoped
   payload store, the runner capturing at a pause and restoring on resume
   against the same dataset version and session configuration, and refusing —
   never retrying with other inputs — when AlphaLab reports `StateDecodeError`
   or `StorageError`.

## The three live gates

Checked in `trading/sessions.create()`, each refused with its own message so a
user is told which one to fix:

1. **Deployment** — `ILUVTRADE_LIVE_TRADING_ENABLED=true`.
2. **Account** — the user's `live_trading_enabled`, off by default.
3. **Session** — `live_confirmed: true` on the request, recorded on the row with
   who set it and when.

No one of them is sufficient. A fourth condition follows: a live session must
name a broker account that is not the paper broker, because that combination
would label simulated fills as live.

## Risk controls

`RiskProfile` becomes AlphaLab's `RiskLimits`, evaluated **before the OMS sees
an order**. Nothing in this application can bypass them — not the UI, not an API
call, not a marketplace strategy — because the refusal happens inside the engine
rather than in a check performed first.

There is deliberately no "unlimited" profile. The loosest is
`RiskProfile.research()`, named for what it is: limits proportionate to the
capital being simulated, because a research run with genuinely unbounded risk
hides the strategies that only work because they were allowed to lever 300×.

`RiskProfile.conservative()` is the default for anything touching a real venue:
no shorting, 1× leverage, 10% position cap, 2% daily loss limit, 10% drawdown
limit.

### Refusals are visible

A refused order never becomes an order, a report or a fill, so a run that
refused every signal is otherwise indistinguishable from a strategy that found
none. Both runners capture `ExecutionPipelineResult.risk_decisions` and surface
refusals — in the session event log as `risk_rejected`, and in a backtest
artifact as `risk_refusals`.

## Session lifecycle

```
CREATED ──start──→ STARTING ──(runner claims)──→ RUNNING
                                                    │
                          ┌─────────────────────────┼──────────────┐
                        pause                     stop           kill
                          │                         │              │
                       PAUSED ──resume──→ STARTING  │              │
                          │                         ▼              ▼
                          └──────stop──────────→ STOPPING       HALTED
                                                    │          (terminal,
                                                    ▼        not resumable)
                                                 STOPPED
```

`RUNNING` means "a runner has claimed this session". Only `run_session()` sets
it, via a conditional `UPDATE` from `STARTING`. That is why `resume` transitions
to `STARTING` rather than `RUNNING`: two runners both believing they own one
session interleave their projections into a book that matches neither run.

`HALTED` is separate from `STOPPED` because the kill switch is not a clean
shutdown. Letting a halted session resume with a click would make the emergency
stop a pause, which is not what the operator pressing it meant.

## Stale state

A session marked `RUNNING` whose last advance is older than 30 seconds is
reported with `is_live_state: false`, and the UI says the figures are the last
known state rather than live state. PHASE 14 forbids painting stale state as
live, and status alone cannot tell you.
