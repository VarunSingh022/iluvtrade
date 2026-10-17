"""An order's projected row follows the order: written once, then kept current.

The defect: ``project()`` wrote an order row the first time it saw the order's
sequence and skipped it ever after. Under next-bar execution AlphaLab accepts an
order and leaves it working until its instrument's next bar, and the runner
projects every 25 records as well as on each fill -- so an order decided on a
projection bar was stored "accepted, 0 filled" and stayed that way after it
filled, beside the fill row that completed it.

The first test drives a real paper session with a projection after every record,
so the order is genuinely projected while working. The rest call ``project()``
with controlled engine states to pin each rule: updated in place, idempotent,
fills append-only, an engine id mismatch refused, nothing audited.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from tests.conftest import register
from tests.integration.test_fill_timing import _ready


def _paper_session(client: Any, headers: dict[str, str], dataset: str, strategy: str) -> str:
    created = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "Projection",
            "mode": "paper",
            "strategy_version_id": strategy,
            "dataset_version_id": dataset,
            "seed": 11,
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def _order_rows(session_id: str) -> list[Any]:
    from sqlalchemy import select

    from iluvtrade.db.models.trading import SessionOrder
    from iluvtrade.db.session import session_scope

    with session_scope() as session:
        rows = list(
            session.execute(
                select(SessionOrder).where(SessionOrder.session_id == session_id)
            ).scalars()
        )
        for row in rows:
            session.expunge(row)
        return rows


# --- the regression, through the real runner -----------------------------------


def test_an_order_projected_while_working_is_updated_when_it_fills(
    client, headers, monkeypatch
) -> None:
    from iluvtrade.trading import runner
    from iluvtrade.trading.projection import project as real_project
    from iluvtrade.trading.runner import RUNNER

    register(client, "projection-working@example.com")
    dataset, strategy = _ready(client, headers)
    session_id = _paper_session(client, headers, dataset, strategy)

    # Project after every record, so the order -- decided on the first bar,
    # filled on the second under next-bar timing -- is written while working.
    monkeypatch.setattr(runner, "PROJECTION_INTERVAL", 1)
    seen: list[tuple[str, str]] = []

    def recording_project(session: Any, trading_session: Any, *args: Any) -> None:
        real_project(session, trading_session, *args)
        session.flush()
        for row in _order_rows_in(session, trading_session.id):
            seen.append((row.status, row.filled_quantity))

    monkeypatch.setattr(runner, "project", recording_project)
    client.post(f"/api/v1/trading/sessions/{session_id}/start", headers=headers)
    RUNNER.join(session_id, timeout=180)

    # The precondition really happened: first stored unfilled, later filled.
    assert seen, "the order was never projected"
    assert seen[0][1] == "0" and seen[0][0] != "filled", seen[:3]
    assert seen[-1] == ("filled", "1")

    orders = client.get(f"/api/v1/trading/sessions/{session_id}/orders").json()
    fills = client.get(f"/api/v1/trading/sessions/{session_id}/fills").json()
    assert len(orders) == 1 and len(fills) == 1
    assert orders[0]["status"] == "filled"
    assert orders[0]["filled_quantity"] == "1"
    assert orders[0]["average_fill_price"] == fills[0]["price"]
    assert orders[0]["engine_order_id"] == fills[0]["engine_order_id"]


def _order_rows_in(session: Any, session_id: str) -> list[Any]:
    from sqlalchemy import select

    from iluvtrade.db.models.trading import SessionOrder

    return list(
        session.execute(select(SessionOrder).where(SessionOrder.session_id == session_id)).scalars()
    )


# --- project() against controlled engine states --------------------------------


@dataclass
class _Result:
    records_processed: int


ORDER_ID = "6b0f3c1e-0000-4000-8000-000000000001"


def _order(status: str, filled: str, average: str | None) -> dict[str, Any]:
    return {
        "sequence": 0,
        "order_id": ORDER_ID,
        "instrument": "ASSET",
        "side": "buy",
        "order_type": "market",
        "status": status,
        "quantity": "10",
        "filled_quantity": filled,
        "average_fill_price": average,
        "created_at": 1704067200.0,
    }


def _fill(sequence: int, quantity: str, price: str) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "fill_id": f"fill-{sequence}",
        "order_id": ORDER_ID,
        "instrument": "ASSET",
        "side": "buy",
        "quantity": quantity,
        "price": price,
        "commission": "0",
        "timestamp": 1704153600.0 + sequence,
    }


@pytest.fixture
def projected(client, headers, monkeypatch):
    """A real paper session row, and a ``project`` driven by the states it is given."""

    from iluvtrade.alphalab_bridge.instruments import universe_for
    from iluvtrade.db.models.trading import TradingSession
    from iluvtrade.db.session import session_scope
    from iluvtrade.trading import projection

    register(client, "projection-unit@example.com")
    dataset, strategy = _ready(client, headers)
    session_id = _paper_session(client, headers, dataset, strategy)
    universe = universe_for(["ACME"])
    asset = universe.by_symbol["ACME"]
    state: dict[str, Any] = {}
    monkeypatch.setattr(projection.engine, "result_of", lambda run_state: _Result(1))
    monkeypatch.setattr(
        projection.results,
        "valuation_of",
        lambda result: dict.fromkeys(
            ("cash", "equity", "realized_pnl", "unrealized_pnl", "commission_paid"), "0"
        ),
    )
    monkeypatch.setattr(
        projection.results,
        "orders_of",
        lambda result: [dict(o, instrument=asset) for o in state["orders"]],
    )
    monkeypatch.setattr(
        projection.results,
        "fills_of",
        lambda result: [dict(f, instrument=asset) for f in state["fills"]],
    )
    monkeypatch.setattr(projection.results, "positions_of", lambda result: [])

    def run(orders: list[dict[str, Any]], fills: list[dict[str, Any]]) -> None:
        state["orders"], state["fills"] = orders, fills
        with session_scope() as session:
            trading_session = session.get(TradingSession, session_id)
            assert trading_session is not None
            projection.project(session, trading_session, object(), universe)

    return session_id, run


def _counts(session_id: str) -> tuple[int, int, int]:
    from sqlalchemy import func, select

    from iluvtrade.db.models.platform import AuditEvent
    from iluvtrade.db.models.trading import SessionFill, SessionOrder
    from iluvtrade.db.session import session_scope

    with session_scope() as session:

        def count(model: Any, *where: Any) -> int:
            return int(
                session.execute(select(func.count()).select_from(model).where(*where)).scalar_one()
            )

        return (
            count(SessionOrder, SessionOrder.session_id == session_id),
            count(SessionFill, SessionFill.session_id == session_id),
            count(AuditEvent),
        )


def test_a_working_order_is_updated_through_partial_and_full_fills(projected) -> None:
    session_id, run = projected
    run([_order("accepted", "0", None)], [])
    (row,) = _order_rows(session_id)
    first_id = row.id
    assert (row.status, row.filled_quantity, row.average_fill_price) == ("accepted", "0", None)

    run([_order("partially_filled", "4", "100")], [_fill(0, "4", "100")])
    (row,) = _order_rows(session_id)
    assert row.id == first_id  # the same row, updated -- not a second one
    assert (row.status, row.filled_quantity, row.average_fill_price) == (
        "partially_filled",
        "4",
        "100",
    )

    run([_order("filled", "10", "100.6")], [_fill(0, "4", "100"), _fill(1, "6", "101")])
    (row,) = _order_rows(session_id)
    assert row.id == first_id
    assert (row.status, row.filled_quantity, row.average_fill_price) == ("filled", "10", "100.6")
    assert _counts(session_id)[:2] == (1, 2)


def test_projecting_the_same_state_again_changes_nothing(projected) -> None:
    session_id, run = projected
    state = ([_order("filled", "10", "100")], [_fill(0, "10", "100")])
    run(*state)
    before_rows = [(r.id, r.status, r.filled_quantity) for r in _order_rows(session_id)]
    before = _counts(session_id)
    run(*state)
    run(*state)
    assert [(r.id, r.status, r.filled_quantity) for r in _order_rows(session_id)] == before_rows
    assert _counts(session_id) == before  # no new orders, fills or audit events


def test_a_different_engine_order_at_a_stored_sequence_is_refused(projected) -> None:
    from iluvtrade.trading.projection import ProjectionMismatch

    session_id, run = projected
    run([_order("accepted", "0", None)], [])
    other = dict(_order("filled", "10", "100"), order_id="6b0f3c1e-0000-4000-8000-0000000000ff")
    with pytest.raises(ProjectionMismatch, match="refusing to overwrite it"):
        run([other], [])
    (row,) = _order_rows(session_id)
    assert (row.engine_order_id, row.status) == (ORDER_ID, "accepted")


def test_a_different_engine_fill_at_a_stored_sequence_is_refused(projected) -> None:
    from iluvtrade.trading.projection import ProjectionMismatch

    _, run = projected
    run([_order("filled", "10", "100")], [_fill(0, "10", "100")])
    with pytest.raises(ProjectionMismatch, match="engine fill fill-0"):
        run([_order("filled", "10", "100")], [dict(_fill(0, "10", "100"), fill_id="other")])
