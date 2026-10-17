"""Projecting AlphaLab's ``RunState`` into the rows a user reads.

**This is the only writer of the session projection tables.** Those tables are
not a book: AlphaLab's ``RunState`` is the book, and everything here is a copy
made for display, audit and restart. Nothing in this module adds, nets, marks or
revalues anything — where a number appears it was produced by AlphaLab and is
being transcribed.

Orders and fills are keyed by ``(session, sequence)``, so a projection run
twice does not duplicate them. Fills are append-only: a fill never changes. An
order does -- under next-bar execution it is projected while still working and
fills at its instrument's next bar -- so an order already written is updated to
the engine's current status, filled quantity and average price. Its engine order
id must still match: a different id at the same sequence means the stored
projection is not of this run, and it is refused rather than overwritten.
Positions are replaced wholesale, because a position that closed must disappear
rather than linger at its last quantity.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from iluvtrade.alphalab_bridge import engine, results
from iluvtrade.alphalab_bridge.instruments import InstrumentUniverse
from iluvtrade.db.base import utcnow
from iluvtrade.db.models.trading import (
    SessionEvent,
    SessionFill,
    SessionOrder,
    SessionPosition,
    TradingSession,
)

__all__ = ["ProjectionMismatch", "log_event", "project"]


class ProjectionMismatch(RuntimeError):
    """The stored projection names a different engine order or fill at a sequence."""


def log_event(
    session: DbSession,
    trading_session: TradingSession,
    *,
    kind: str,
    message: str,
    severity: str = "info",
    payload: dict[str, object] | None = None,
) -> SessionEvent:
    """Append one operational event to the session log."""

    import json

    next_sequence = (
        session.execute(
            select(SessionEvent.sequence)
            .where(SessionEvent.session_id == trading_session.id)
            .order_by(SessionEvent.sequence.desc())
            .limit(1)
        ).scalar_one_or_none()
        or 0
    ) + 1
    event = SessionEvent(
        organization_id=trading_session.organization_id,
        session_id=trading_session.id,
        sequence=next_sequence,
        created_at=utcnow(),
        severity=severity,
        kind=kind,
        message=message[:4000],
        payload_json=json.dumps(payload or {}, default=str),
    )
    session.add(event)
    return event


def project(
    session: DbSession,
    trading_session: TradingSession,
    run_state: object,
    universe: InstrumentUniverse,
) -> None:
    """Write the current engine state into the projection tables."""

    result = engine.result_of(run_state)  # type: ignore[arg-type]

    valuation = results.valuation_of(result)
    trading_session.cash = valuation["cash"]
    trading_session.equity = valuation["equity"]
    trading_session.realized_pnl = valuation["realized_pnl"]
    trading_session.unrealized_pnl = valuation["unrealized_pnl"]
    trading_session.commission_paid = valuation["commission_paid"]
    trading_session.records_processed = result.records_processed
    trading_session.last_advanced_at = utcnow()

    existing_orders = {
        row.sequence: row
        for row in session.execute(
            select(SessionOrder).where(SessionOrder.session_id == trading_session.id)
        ).scalars()
    }
    for row in results.orders_of(result):
        stored = existing_orders.get(row["sequence"])
        if stored is not None:
            if stored.engine_order_id != row["order_id"]:
                raise ProjectionMismatch(
                    f"Order {row['sequence']} of session {trading_session.id} is stored as "
                    f"engine order {stored.engine_order_id}, but the engine reports "
                    f"{row['order_id']} at that sequence; refusing to overwrite it."
                )
            # Its lifecycle has moved on since it was first written (a next-bar
            # order is written working, then fills): transcribe where it is now.
            # Until this was done the row kept its first status for good -- an
            # order written while working read "accepted, 0 filled" beside the
            # fill that completed it.
            stored.status = row["status"]
            stored.quantity = row["quantity"] or "0"
            stored.filled_quantity = row["filled_quantity"] or "0"
            stored.average_fill_price = row["average_fill_price"]
            continue
        session.add(
            SessionOrder(
                organization_id=trading_session.organization_id,
                session_id=trading_session.id,
                sequence=row["sequence"],
                engine_order_id=row["order_id"],
                instrument=universe.symbol_of(row["instrument"]),
                side=row["side"],
                quantity=row["quantity"] or "0",
                filled_quantity=row["filled_quantity"] or "0",
                average_fill_price=row["average_fill_price"],
                status=row["status"],
                order_type=row["order_type"],
                submitted_timestamp=row["created_at"],
            )
        )

    existing_fills = {
        row.sequence: row
        for row in session.execute(
            select(SessionFill).where(SessionFill.session_id == trading_session.id)
        ).scalars()
    }
    for row in results.fills_of(result):
        stored_fill = existing_fills.get(row["sequence"])
        if stored_fill is not None:
            if stored_fill.engine_fill_id != row["fill_id"]:
                raise ProjectionMismatch(
                    f"Fill {row['sequence']} of session {trading_session.id} is stored as "
                    f"engine fill {stored_fill.engine_fill_id}, but the engine reports "
                    f"{row['fill_id']} at that sequence; refusing to overwrite it."
                )
            continue
        session.add(
            SessionFill(
                organization_id=trading_session.organization_id,
                session_id=trading_session.id,
                sequence=row["sequence"],
                engine_fill_id=row["fill_id"],
                engine_order_id=row["order_id"],
                instrument=universe.symbol_of(row["instrument"]),
                side=row["side"],
                quantity=row["quantity"] or "0",
                price=row["price"] or "0",
                commission=row["commission"] or "0",
                fill_timestamp=row["timestamp"],
            )
        )

    # Positions are a snapshot, not a log: replace them so a closed position
    # vanishes instead of persisting at its final quantity.
    session.execute(delete(SessionPosition).where(SessionPosition.session_id == trading_session.id))
    for row in results.positions_of(result):
        session.add(
            SessionPosition(
                organization_id=trading_session.organization_id,
                session_id=trading_session.id,
                instrument=universe.symbol_of(row["instrument"]),
                quantity=row["quantity"] or "0",
                average_price=row["average_price"] or "0",
                market_price=row["market_price"],
                unrealized_pnl=row["unrealized_pnl"],
                realized_pnl=row["realized_pnl"],
            )
        )
