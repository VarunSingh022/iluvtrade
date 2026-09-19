"""The loop that advances a paper session through AlphaLab, one record at a time.

Why record-by-record rather than ``TradingSession.run``
-------------------------------------------------------

AlphaLab offers both: ``TradingSession.run`` reads a source to exhaustion, and
``TradingSession.advance`` takes one record. A product needs the second. Between
records is the only place a stop request, a pause or a kill switch can be
honoured *without* leaving the engine's state half-applied, and it is where the
projection is written so a user watching the screen sees the session move.

Live routing is not implemented here
------------------------------------

This runner drives ``PAPER``. AlphaLab's own documentation is explicit that
``TradingSession`` is not a live loop — a live run needs
:class:`alphalab.runtime.live.LiveSession`, which settles venue-reported fills,
advances the run, and routes newly working orders, in that order. Calling this
runner with a ``LIVE`` session is therefore **refused** rather than quietly
executed against the simulator, which would produce simulated fills labelled as
live. ``docs/TRADING.md`` records what remains for live.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import update
from sqlalchemy.engine import CursorResult

from iluvtrade.alphalab_bridge import engine, instruments, market, runconfig
from iluvtrade.alphalab_bridge import strategies as implementations
from iluvtrade.common import storage
from iluvtrade.db.base import utcnow
from iluvtrade.db.models.data import DatasetVersion
from iluvtrade.db.models.strategy import StrategyVersion
from iluvtrade.db.models.trading import SessionStatus, TradingMode, TradingSession
from iluvtrade.db.session import session_scope
from iluvtrade.platform import notifications
from iluvtrade.trading.projection import log_event, project

logger = logging.getLogger("iluvtrade.trading.runner")

__all__ = ["SessionRunner", "run_session"]

#: How often the projection is written, in records. Every record would make the
#: database the bottleneck on a 100k-bar replay; never would make the screen
#: useless. A fill always forces a write regardless.
PROJECTION_INTERVAL = 25


class LiveRoutingUnavailable(RuntimeError):
    """A live session was handed to the paper runner."""


@dataclass(slots=True)
class _Loaded:
    """Everything read from the database before the engine loop starts."""

    rows: list[dict[str, Any]]
    frequency: str | None
    implementation_key: str
    parameters: dict[str, Any]
    starting_cash: Decimal
    currency: str
    risk_profile: dict[str, Any]
    seed: int
    symbols: list[str]


def _load(session_id: str) -> _Loaded:
    import json

    with session_scope() as session:
        trading_session = session.get(TradingSession, session_id)
        if trading_session is None:
            raise LookupError(f"Trading session {session_id} does not exist.")
        if trading_session.mode is TradingMode.LIVE:
            raise LiveRoutingUnavailable(
                "This runner drives paper sessions only. A live session must be driven by "
                "AlphaLab's LiveSession with a broker binding; running it here would "
                "produce simulated fills labelled as live."
            )
        if not trading_session.dataset_version_id:
            raise LookupError("This paper session has no dataset version.")

        version = session.get(DatasetVersion, trading_session.dataset_version_id)
        strategy_version = session.get(StrategyVersion, trading_session.strategy_version_id)
        if version is None or not version.canonical_path:
            raise LookupError("The dataset version has no canonical data.")
        if strategy_version is None:
            raise LookupError("The strategy version no longer exists.")

        rows = list(storage.read_jsonl(version.canonical_path))
        stored_parameters = json.loads(trading_session.parameters_json or "{}")
        # The version's own defaults are the baseline; the session may override
        # within the schema. An unknown key is refused by validate_parameters.
        defaults = json.loads(strategy_version.default_parameters_json or "{}")
        parameters = implementations.validate_parameters(
            strategy_version.implementation_key, {**defaults, **stored_parameters}
        )
        return _Loaded(
            rows=rows,
            frequency=version.inferred_frequency,
            implementation_key=strategy_version.implementation_key,
            parameters=parameters,
            starting_cash=Decimal(trading_session.starting_cash),
            currency=trading_session.base_currency,
            risk_profile=json.loads(trading_session.risk_config_json or "{}"),
            seed=trading_session.seed,
            symbols=sorted({str(row["symbol"]) for row in rows}),
        )


def _should_continue(session_id: str) -> tuple[bool, SessionStatus]:
    """Re-read the session's status. The one place a stop request is noticed."""

    with session_scope() as session:
        trading_session = session.get(TradingSession, session_id)
        if trading_session is None:
            return False, SessionStatus.FAILED
        return trading_session.status is SessionStatus.RUNNING, trading_session.status


def run_session(session_id: str, *, max_records: int | None = None) -> str:
    """Drive one paper session to completion, a stop request, or a pause.

    Returns the terminal status as a string. Safe to call again on a paused
    session: the run restarts from the beginning of the dataset, which is the
    honest behaviour for a replay-backed paper session and is stated in
    ``docs/TRADING.md`` — durable mid-run resume needs AlphaLab's run snapshot,
    which is recorded there as deferred.
    """

    loaded = _load(session_id)

    # Claim the session exclusively. A conditional UPDATE — not a read followed
    # by a write — because two callers can both pass a read-then-check: the API
    # launches a background runner on ``/start``, and a caller may also drive a
    # session synchronously. Two runners over one session interleave their
    # projections and produce a book that matches neither run, which is exactly
    # what this is here to make impossible. ``rowcount == 0`` means somebody
    # else got there first, and this call returns rather than competing.
    with session_scope() as session:
        claimed = cast(
            "CursorResult[Any]",
            session.execute(
                update(TradingSession)
                .where(
                    TradingSession.id == session_id,
                    TradingSession.status == SessionStatus.STARTING,
                )
                .values(status=SessionStatus.RUNNING)
            ),
        )
        if claimed.rowcount == 0:
            existing = session.get(TradingSession, session_id)
            if existing is None:
                raise LookupError(f"Trading session {session_id} does not exist.")
            logger.info(
                "Session %s is already %s; not starting a second runner.",
                session_id,
                existing.status.value,
            )
            return str(existing.status.value)

    with session_scope() as session:
        trading_session = session.get(TradingSession, session_id)
        if trading_session is None:
            raise LookupError(f"Trading session {session_id} does not exist.")
        if trading_session.started_at is None:
            trading_session.started_at = utcnow()
        log_event(
            session,
            trading_session,
            kind="session.running",
            message=f"Replaying {len(loaded.rows)} records for {', '.join(loaded.symbols)}.",
            payload={"records": len(loaded.rows), "symbols": loaded.symbols},
        )
        organization_id = trading_session.organization_id
        created_by = trading_session.created_by_user_id
        session_name = trading_session.name

    universe = instruments.universe_for(loaded.symbols, currency=loaded.currency)
    source = market.source_from_rows(
        f"session-{session_id}", loaded.rows, universe=universe, frequency=loaded.frequency
    )
    records = list(source.records())
    if max_records is not None:
        records = records[:max_records]

    strategy_id = f"session-{session_id}"
    instance = implementations.build(loaded.implementation_key, strategy_id, loaded.parameters)
    risk = runconfig.RiskProfile.from_dict(loaded.risk_profile)
    start_timestamp = (records[0].timestamp - 1.0) if records else 0.0

    config = runconfig.build_run_config(
        mode=runconfig.ExecutionMode.PAPER,
        account_id=f"paper-{session_id}",
        account_name=session_name,
        currency=loaded.currency,
        starting_cash=loaded.starting_cash,
        risk=risk,
        strategy_ids=(strategy_id,),
        instruments=universe.registry,
        seed=loaded.seed,
        start_timestamp=start_timestamp,
        # A replayed paper session's clock is the record's own timestamp, so
        # nothing is stale. A real-time feed would pass a wall clock here and
        # set max_market_data_age_seconds.
        max_market_data_age_seconds=None,
    )

    logs: list[tuple[str, str]] = []
    factory = engine.context_factory_for(loaded.parameters, logs, start_timestamp)
    runtime = engine.runtime_for({strategy_id: instance}, start_timestamp=start_timestamp)
    run_state = engine.initialize_session(config, runtime)

    terminal = SessionStatus.STOPPED
    fills_seen = 0
    processed = 0
    refusals: list[tuple[int, str]] = []

    # Every advance happens inside AlphaLab's identifier scope. Its own drivers
    # wrap their loop in this; advancing outside it drops identifier generation
    # back to ``uuid4``, and a session whose order ids differ on every run cannot
    # be compared to the backtest it was supposed to reproduce.
    with engine.run_scope(loaded.seed):
        for index, record in enumerate(records, start=1):
            keep_going, status = _should_continue(session_id)
            if not keep_going:
                terminal = (
                    SessionStatus.PAUSED
                    if status is SessionStatus.PAUSED
                    else SessionStatus.HALTED
                    if status is SessionStatus.HALTED
                    else SessionStatus.STOPPED
                )
                break

            run_state, step = engine.advance(run_state, record, factory)
            processed = index

            # A refused order never becomes an order, a report or a fill, so
            # without this the session log would show a strategy that simply
            # found no signals. PHASE 16: never a generic success over a refusal.
            if step is not None:
                for decision in step.risk_decisions:
                    if not decision.approved and len(refusals) < 200:
                        refusals.append((index, decision.reason))

            current_fills = len(engine.result_of(run_state).fills)
            if index % PROJECTION_INTERVAL == 0 or current_fills != fills_seen:
                fills_seen = current_fills
                with session_scope() as session:
                    trading_session = session.get(TradingSession, session_id)
                    if trading_session is not None:
                        project(session, trading_session, run_state, universe)
        else:
            terminal = SessionStatus.STOPPED

    # Final projection and any refusals the engine recorded, so the session log
    # says what actually happened rather than only that it finished.
    with session_scope() as session:
        trading_session = session.get(TradingSession, session_id)
        if trading_session is None:
            return terminal.value
        project(session, trading_session, run_state, universe)

        result = engine.result_of(run_state)
        for skipped in result.run.skipped.to_tuple():
            log_event(
                session,
                trading_session,
                kind="record_skipped",
                severity="warning",
                message=f"Record skipped: {getattr(skipped, 'reason', 'unknown')}",
                payload={"timestamp": getattr(skipped, "timestamp", None)},
            )
        for asset in result.unpriced_assets:
            log_event(
                session,
                trading_session,
                kind="unpriced_asset",
                severity="warning",
                message=(
                    f"{universe.symbol_of(str(asset.asset_id))} was never priced, so orders "
                    "for it were dropped."
                ),
            )
        if refusals:
            counts: dict[str, int] = {}
            for _, reason in refusals:
                counts[reason] = counts.get(reason, 0) + 1
            log_event(
                session,
                trading_session,
                kind="risk_rejected",
                severity="warning",
                message=(
                    f"{len(refusals)} order(s) were refused by risk controls and never "
                    f"reached the venue."
                ),
                payload={"reasons": counts, "first": refusals[:10]},
            )
        for level, text in logs[-50:]:
            log_event(session, trading_session, kind="strategy_log", severity=level, message=text)

        if trading_session.status is not SessionStatus.HALTED:
            trading_session.status = terminal
        if terminal in (SessionStatus.STOPPED, SessionStatus.HALTED):
            trading_session.stopped_at = utcnow()
        log_event(
            session,
            trading_session,
            kind="session.finished",
            message=(
                f"Session {trading_session.status.value} after {processed} of "
                f"{len(records)} records."
            ),
            payload={"processed": processed, "total": len(records)},
        )
        notifications.notify(
            session,
            organization_id=organization_id,
            user_id=created_by,
            kind="trading.session.finished",
            severity="info" if terminal is SessionStatus.STOPPED else "warning",
            title=f"Paper session {trading_session.status.value}",
            body=(f"{session_name}: {processed} records, equity {trading_session.equity}."),
            resource_type="trading_session",
            resource_id=session_id,
        )
        return str(trading_session.status.value)


class SessionRunner:
    """Runs sessions in background threads.

    Same shape and same limits as the backtest pool: fine for one node, and the
    place an external worker would attach. ``docs/DEPLOYMENT.md`` says so.
    """

    def __init__(self) -> None:
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def launch(self, session_id: str) -> None:
        with self._lock:
            existing = self._threads.get(session_id)
            if existing is not None and existing.is_alive():
                return

            def _run() -> None:
                try:
                    run_session(session_id)
                except Exception as exc:
                    logger.exception("Trading session %s failed", session_id)
                    with session_scope() as session:
                        trading_session = session.get(TradingSession, session_id)
                        if trading_session is not None:
                            trading_session.status = SessionStatus.FAILED
                            trading_session.failure_reason = str(exc)[:4000]
                            trading_session.stopped_at = utcnow()
                            log_event(
                                session,
                                trading_session,
                                kind="session.failed",
                                severity="error",
                                message=str(exc)[:2000],
                            )

            thread = threading.Thread(target=_run, name=f"session-{session_id[:8]}", daemon=True)
            self._threads[session_id] = thread
            thread.start()

    def join(self, session_id: str, timeout: float = 120.0) -> None:
        thread = self._threads.get(session_id)
        if thread is not None:
            thread.join(timeout=timeout)

    def join_all(self, timeout: float = 120.0) -> None:
        deadline = time.monotonic() + timeout
        for thread in list(self._threads.values()):
            thread.join(timeout=max(0.0, deadline - time.monotonic()))


#: The process-wide runner the API uses.
RUNNER = SessionRunner()
