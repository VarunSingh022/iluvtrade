"""Trading sessions: create, control, and read what they produced."""

from __future__ import annotations

import json
from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from iluvtrade.api.deps import current_principal, db_session, rate_limit, require_trader
from iluvtrade.api.v1.schemas import (
    CreateSessionRequest,
    KillSwitchRequest,
    SessionEventResponse,
    SessionFillResponse,
    SessionOrderResponse,
    SessionPositionResponse,
    TradingSessionResponse,
)
from iluvtrade.db.base import utcnow
from iluvtrade.db.models.trading import (
    SessionEvent,
    SessionFill,
    SessionOrder,
    SessionPosition,
    SessionStatus,
    TradingMode,
    TradingSession,
)
from iluvtrade.platform.accounts import Principal
from iluvtrade.trading import sessions
from iluvtrade.trading.runner import RUNNER

router = APIRouter(prefix="/trading", tags=["trading"])

#: A running session whose last advance is older than this is not showing live
#: state. PHASE 14 forbids painting stale state as live, so the flag is computed
#: rather than assumed from the status alone.
STALE_AFTER = timedelta(seconds=30)


def _session(row: TradingSession) -> TradingSessionResponse:
    is_live_state = True
    if row.status is SessionStatus.RUNNING:
        is_live_state = (
            row.last_advanced_at is not None and (utcnow() - row.last_advanced_at) < STALE_AFTER
        )
    return TradingSessionResponse(
        id=row.id,
        name=row.name,
        mode=row.mode.value,
        status=row.status.value,
        strategy_version_id=row.strategy_version_id,
        dataset_version_id=row.dataset_version_id,
        broker_account_id=row.broker_account_id,
        entitlement_id=row.entitlement_id,
        starting_cash=row.starting_cash,
        base_currency=row.base_currency,
        seed=row.seed,
        records_processed=row.records_processed,
        cash=row.cash,
        equity=row.equity,
        realized_pnl=row.realized_pnl,
        unrealized_pnl=row.unrealized_pnl,
        commission_paid=row.commission_paid,
        kill_switch_engaged=row.kill_switch_engaged,
        kill_switch_reason=row.kill_switch_reason,
        started_at=row.started_at,
        stopped_at=row.stopped_at,
        last_advanced_at=row.last_advanced_at,
        failure_reason=row.failure_reason,
        risk_config=json.loads(row.risk_config_json or "{}"),
        is_live_state=is_live_state,
    )


@router.get("/sessions", response_model=list[TradingSessionResponse])
def list_sessions(
    mode: str | None = None,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> list[TradingSessionResponse]:
    parsed = TradingMode(mode) if mode else None
    return [_session(row) for row in sessions.list_sessions(session, principal, mode=parsed)]


@router.post(
    "/sessions",
    response_model=TradingSessionResponse,
    status_code=201,
    dependencies=[Depends(rate_limit("session"))],
)
def create_session(
    payload: CreateSessionRequest,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> TradingSessionResponse:
    """Deploy a strategy version to paper or live.

    A live session is refused unless the deployment, the user and this request
    all permit it. See :func:`iluvtrade.trading.sessions.create`.
    """

    spec = sessions.SessionSpec(
        name=payload.name,
        mode=TradingMode(payload.mode),
        strategy_version_id=payload.strategy_version_id,
        dataset_version_id=payload.dataset_version_id,
        broker_account_id=payload.broker_account_id,
        parameters=payload.parameters,
        starting_cash=payload.starting_cash,
        base_currency=payload.base_currency,
        risk_profile=payload.risk_profile,
        seed=payload.seed,
        live_confirmed=payload.live_confirmed,
    )
    return _session(sessions.create(session, principal, spec))


@router.get("/sessions/{session_id}", response_model=TradingSessionResponse)
def get_session(
    session_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> TradingSessionResponse:
    return _session(sessions.get(session, principal, session_id))


@router.post(
    "/sessions/{session_id}/start",
    response_model=TradingSessionResponse,
    dependencies=[Depends(rate_limit("session"))],
)
def start(
    session_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> TradingSessionResponse:
    row = sessions.start(session, principal, session_id)
    response = _session(row)
    # Committed before the runner is launched, so the background thread reads a
    # session that exists in STARTING rather than racing this transaction.
    session.commit()
    RUNNER.launch(session_id)
    return response


@router.post("/sessions/{session_id}/pause", response_model=TradingSessionResponse)
def pause(
    session_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> TradingSessionResponse:
    return _session(sessions.pause(session, principal, session_id))


@router.post("/sessions/{session_id}/resume", response_model=TradingSessionResponse)
def resume(
    session_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> TradingSessionResponse:
    row = sessions.resume(session, principal, session_id)
    response = _session(row)
    session.commit()
    RUNNER.launch(session_id)
    return response


@router.post("/sessions/{session_id}/stop", response_model=TradingSessionResponse)
def stop(
    session_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> TradingSessionResponse:
    return _session(sessions.request_stop(session, principal, session_id))


@router.post("/sessions/{session_id}/kill", response_model=TradingSessionResponse)
def kill(
    session_id: str,
    payload: KillSwitchRequest,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> TradingSessionResponse:
    """Halt immediately and permanently. A halted session cannot be resumed."""

    return _session(
        sessions.engage_kill_switch(session, principal, session_id, reason=payload.reason)
    )


@router.get("/sessions/{session_id}/orders", response_model=list[SessionOrderResponse])
def orders(
    session_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> list[SessionOrderResponse]:
    sessions.get(session, principal, session_id)
    rows = session.execute(
        select(SessionOrder)
        .where(SessionOrder.session_id == session_id)
        .order_by(SessionOrder.sequence)
    ).scalars()
    return [
        SessionOrderResponse(
            sequence=row.sequence,
            engine_order_id=row.engine_order_id,
            instrument=row.instrument,
            side=row.side,
            quantity=row.quantity,
            filled_quantity=row.filled_quantity,
            average_fill_price=row.average_fill_price,
            status=row.status,
            order_type=row.order_type,
            submitted_timestamp=row.submitted_timestamp,
        )
        for row in rows
    ]


@router.get("/sessions/{session_id}/fills", response_model=list[SessionFillResponse])
def fills(
    session_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> list[SessionFillResponse]:
    sessions.get(session, principal, session_id)
    rows = session.execute(
        select(SessionFill)
        .where(SessionFill.session_id == session_id)
        .order_by(SessionFill.sequence)
    ).scalars()
    return [
        SessionFillResponse(
            sequence=row.sequence,
            engine_fill_id=row.engine_fill_id,
            engine_order_id=row.engine_order_id,
            instrument=row.instrument,
            side=row.side,
            quantity=row.quantity,
            price=row.price,
            commission=row.commission,
            fill_timestamp=row.fill_timestamp,
        )
        for row in rows
    ]


@router.get("/sessions/{session_id}/positions", response_model=list[SessionPositionResponse])
def positions(
    session_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> list[SessionPositionResponse]:
    sessions.get(session, principal, session_id)
    rows = session.execute(
        select(SessionPosition)
        .where(SessionPosition.session_id == session_id)
        .order_by(SessionPosition.instrument)
    ).scalars()
    return [
        SessionPositionResponse(
            instrument=row.instrument,
            quantity=row.quantity,
            average_price=row.average_price,
            market_price=row.market_price,
            unrealized_pnl=row.unrealized_pnl,
            realized_pnl=row.realized_pnl,
        )
        for row in rows
    ]


@router.get("/sessions/{session_id}/events", response_model=list[SessionEventResponse])
def events(
    session_id: str,
    limit: int = 200,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> list[SessionEventResponse]:
    """The session's operational log, including every refusal."""

    sessions.get(session, principal, session_id)
    rows = session.execute(
        select(SessionEvent)
        .where(SessionEvent.session_id == session_id)
        .order_by(SessionEvent.sequence.desc())
        .limit(min(limit, 1000))
    ).scalars()
    return [
        SessionEventResponse(
            sequence=row.sequence,
            created_at=row.created_at,
            severity=row.severity,
            kind=row.kind,
            message=row.message,
        )
        for row in rows
    ]
