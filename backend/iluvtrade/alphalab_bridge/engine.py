"""Driving AlphaLab's engines.

Three entry points, one per environment, each a thin call onto the driver
AlphaLab already provides:

============  =================================================================
backtest      :meth:`alphalab.backtesting.BacktestEngine.run` over a dataset
replay        :meth:`alphalab.backtesting.ReplayBacktest.run`, for parity checks
paper         :meth:`alphalab.runtime.session.TradingSession` advanced record by
              record, so a session can be paused, inspected and stopped
============  =================================================================

The strategy runtime construction below is the part worth reading. AlphaLab
requires a strategy to be walked through its lifecycle — registered, configured,
initialized, subscribed, started — before the pipeline will dispatch to it, and
a strategy left in ``INITIALIZED`` silently receives nothing. :func:`runtime_for`
is the single place that sequence exists, so a paper session and a backtest
cannot start a strategy differently.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Any

from alphalab.backtesting import (
    BacktestEngine,
    BacktestResult,
    MarketDataset,
    ReplayBacktest,
    RunConfig,
    id_scope,
)
from alphalab.market.record import MarketRecord
from alphalab.market.source import MarketDataSource
from alphalab.runtime.execution_pipeline import ExecutionPipelineResult
from alphalab.runtime.run import ExecutionMode, RunEngine, RunState
from alphalab.runtime.session import TradingSession
from alphalab.strategy.context import (
    NoMarket,
    NoOrders,
    NoPortfolio,
    NoRiskView,
    StrategyContext,
)
from alphalab.strategy.protocol import StrategyProtocol
from alphalab.strategy.runtime import create_runtime, register_strategy
from alphalab.strategy.state import RuntimeState
from alphalab.strategy.supervisor import RuntimeSupervisor

__all__ = [
    "BacktestResult",
    "RunState",
    "advance",
    "context_factory_for",
    "finalize",
    "initialize_session",
    "run_backtest",
    "run_backtest_observed",
    "run_replay",
    "run_scope",
    "runtime_for",
]


class _WallClock:
    """The clock AlphaLab hands to a strategy context.

    Its readings come from the run, not from the host: a backtest's "now" is the
    record's timestamp. Feeding a strategy the machine's wall clock during a
    historical run is how look-ahead gets in.
    """

    def __init__(self, now: float) -> None:
        self._now = now

    def now(self) -> float:
        return self._now


@dataclass(slots=True)
class _CollectingLogger:
    """Captures what a strategy logs, so it reaches the session event log.

    AlphaLab's context takes any object with ``info`` and ``error``; discarding
    those messages would mean a strategy's own account of what it did is the one
    thing the operator cannot see.
    """

    records: list[tuple[str, str]]

    def info(self, msg: str) -> None:
        self.records.append(("info", str(msg)[:2000]))

    def error(self, msg: str) -> None:
        self.records.append(("error", str(msg)[:2000]))


def context_factory_for(
    config: Mapping[str, Any], log_sink: list[tuple[str, str]], now: float
) -> Any:
    """Build the context factory AlphaLab calls once per strategy per event.

    The pipeline overlays the portfolio, market, prices and risk view onto
    whatever this returns (ADR-0026), so supplying the ``No*`` placeholders is
    correct rather than lazy: they are what the strategy sees for the fields the
    pipeline does not own, and the pipeline replaces the ones it does.
    """

    logger = _CollectingLogger(log_sink)
    clock = _WallClock(now)

    def factory(strategy_id: str) -> StrategyContext:
        return StrategyContext(
            portfolio=NoPortfolio(),
            market=NoMarket(),
            clock=clock,
            logger=logger,
            risk_view=NoRiskView(),
            config={**dict(config), "strategy_id": strategy_id},
            orders=NoOrders(),
        )

    return factory


def runtime_for(
    strategies: Mapping[str, StrategyProtocol],
    *,
    subscriptions: frozenset[str] = frozenset({"bars"}),
    start_timestamp: float = 0.0,
) -> RuntimeState:
    """Register strategies and walk each to ``RUNNING``.

    AlphaLab's supervisor enforces the transition order and refuses out-of-order
    calls, so this sequence is the contract rather than a convention. A strategy
    that never reaches ``RUNNING`` is not dispatched to, and produces an empty
    result that looks exactly like a strategy that found no signals.
    """

    state = create_runtime()
    for strategy_id, instance in strategies.items():
        state = register_strategy(state, strategy_id, instance)
        strategy_state = state.strategies[strategy_id]
        strategy_state, _ = RuntimeSupervisor.configure(strategy_state, {}, start_timestamp)
        strategy_state, _ = RuntimeSupervisor.initialize(strategy_state, start_timestamp)
        strategy_state, _ = RuntimeSupervisor.subscribe(
            strategy_state, subscriptions, start_timestamp
        )
        strategy_state, _ = RuntimeSupervisor.start(strategy_state, start_timestamp)
        state = replace(state, strategies={**state.strategies, strategy_id: strategy_state})
    return state


def run_backtest(
    config: RunConfig,
    dataset: MarketDataset,
    runtime: RuntimeState,
    context_factory: Any,
) -> BacktestResult:
    """Run one dataset through AlphaLab's backtest driver."""

    return BacktestEngine.run(config, dataset, runtime, context_factory)


def run_backtest_observed(
    config: RunConfig,
    dataset: MarketDataset,
    runtime: RuntimeState,
    context_factory: Any,
    *,
    on_step: Callable[[int, ExecutionPipelineResult], None] | None = None,
) -> BacktestResult:
    """``BacktestEngine.run``, with each record's result handed to ``on_step``.

    Identical to ``BacktestEngine.run`` in what it executes — same
    ``id_scope(seed)``, same ``initialize``, same ``advance``, same
    ``finalize``, in the same order — and a run produced here is byte-for-byte
    the one that driver produces.

    It exists because ``run`` discards each record's
    :class:`~alphalab.runtime.execution_pipeline.ExecutionPipelineResult`, and
    that object is the only place a **risk refusal** appears. ``RunStep`` records
    orders, reports and fills; an order that risk declined never becomes any of
    those, so a run that silently refused twenty orders is indistinguishable
    from a strategy that found no signals. Surfacing that is PHASE 16's rule
    applied to research.

    ``id_scope`` is what makes the run reproducible: it seeds the identifier
    stream so order and fill ids are derived rather than random. Advancing
    outside it produces a run whose numbers match and whose identities do not.
    """

    with id_scope(config.seed):
        state = replace(
            BacktestEngine.initialize(replace(config, mode=ExecutionMode.BACKTEST), runtime),
            source_id=dataset.dataset_id,
        )
        for index, record in enumerate(dataset.records, start=1):
            state, step = BacktestEngine.advance(state, record, context_factory)
            if on_step is not None and step is not None:
                on_step(index, step)
        return BacktestEngine.finalize(state)


def run_scope(seed: int | None) -> Any:
    """The identifier scope a record-by-record run must be driven inside.

    AlphaLab's own drivers wrap their loop in this. A caller that advances
    record by record — which is what a pausable session needs — must do the
    same, or its identifiers fall back to ``uuid4`` and two runs of one dataset
    stop being comparable record by record.
    """

    return id_scope(seed)


def run_replay(
    config: RunConfig,
    dataset: MarketDataset,
    runtime: RuntimeState,
    context_factory: Any,
) -> Any:
    """Replay the same dataset through AlphaLab's replay cursor.

    Used to demonstrate reproducibility: a replay of a run must produce the same
    order ids, the same valuation and the same equity curve, and
    ``tests/integration/test_backtest_reproducibility.py`` asserts it.
    """

    return ReplayBacktest.run(config, dataset, runtime, context_factory)


def initialize_session(config: RunConfig, runtime: RuntimeState) -> RunState:
    """Fund the portfolio and produce the state a paper session starts from."""

    return TradingSession.initialize(config, runtime)


def advance(
    state: RunState, record: MarketRecord, context_factory: Any, now: float | None = None
) -> tuple[RunState, ExecutionPipelineResult | None]:
    """Move one record through the execution path.

    Advancing record by record rather than calling ``TradingSession.run`` is what
    makes a paper session pausable, inspectable and stoppable — the loop lives in
    :mod:`iluvtrade.trading.runner`, which checks for a stop request between
    records.
    """

    return TradingSession.advance(state, record, context_factory, now)


def finalize(state: RunState) -> BacktestResult:
    """Compile a finished run into the same result shape a backtest produces.

    ``RunEngine.finalize`` returns the finished ``RunState``; ``BacktestResult``
    is the read-only projection over it and holds no state of its own. Wrapping
    here means a paper session and a backtest report through one code path, so
    the portfolio screen never needs to know which produced a number.
    """

    return BacktestResult(RunEngine.finalize(state))


def result_of(state: RunState) -> BacktestResult:
    """Project an *unfinished* run, without compiling analytics.

    What a live session screen reads between records: the valuation, orders and
    fills are all current, and ``report`` is simply ``None`` until the run ends.
    """

    return BacktestResult(state)


def records_of(source: MarketDataSource) -> Iterator[MarketRecord]:
    """Iterate a source's records. Named so callers need not import the protocol."""

    return source.records()
