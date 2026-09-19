"""Portfolio and order views across sessions, plus platform surfaces.

The portfolio here is an **aggregation of session projections**, not a book of
its own. It sums what AlphaLab produced per session; it does not net positions
across sessions into a single holding, because two sessions are two independent
portfolios in AlphaLab's accounting and merging them would state a position no
engine ever computed.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session as DbSession

from iluvtrade.api.deps import current_principal, db_session
from iluvtrade.api.v1.schemas import (
    AuditEventResponse,
    AuditVerificationResponse,
    NotificationResponse,
)
from iluvtrade.db.models.backtest import BacktestJob, JobStatus
from iluvtrade.db.models.data import DatasetVersion, DatasetVersionStatus
from iluvtrade.db.models.platform import AuditEvent, Role
from iluvtrade.db.models.strategy import Strategy
from iluvtrade.db.models.trading import (
    SessionOrder,
    SessionPosition,
    SessionStatus,
    TradingSession,
)
from iluvtrade.platform import audit, notifications
from iluvtrade.platform.accounts import Principal
from iluvtrade.platform.tenancy import scoped

router = APIRouter(tags=["portfolio"])


def _sum(values: list[str | None]) -> str:
    total = sum((Decimal(v) for v in values if v), Decimal("0"))
    return str(total)


@router.get("/portfolio")
def portfolio(
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> dict:
    """Per-session valuations, and their totals, split by paper and live.

    Paper and live are never summed together. A combined equity figure across
    the two would be meaningless — one is simulated money — and PHASE 14
    requires the modes stay unambiguous.
    """

    rows = list(
        session.execute(
            scoped(TradingSession, principal.organization_id).order_by(
                TradingSession.created_at.desc()
            )
        ).scalars()
    )
    by_mode: dict[str, list[dict]] = {"paper": [], "live": []}
    for row in rows:
        by_mode[row.mode.value].append(
            {
                "session_id": row.id,
                "name": row.name,
                "status": row.status.value,
                "currency": row.base_currency,
                "starting_cash": row.starting_cash,
                "cash": row.cash,
                "equity": row.equity,
                "realized_pnl": row.realized_pnl,
                "unrealized_pnl": row.unrealized_pnl,
                "commission_paid": row.commission_paid,
                "strategy_version_id": row.strategy_version_id,
                "records_processed": row.records_processed,
            }
        )
    return {
        "modes": {
            mode: {
                "sessions": entries,
                "totals": {
                    "equity": _sum([e["equity"] for e in entries]),
                    "realized_pnl": _sum([e["realized_pnl"] for e in entries]),
                    "unrealized_pnl": _sum([e["unrealized_pnl"] for e in entries]),
                    "commission_paid": _sum([e["commission_paid"] for e in entries]),
                },
            }
            for mode, entries in by_mode.items()
        },
        "note": (
            "Totals are the sum of independent per-session portfolios, each computed by "
            "AlphaLab. Positions are not netted across sessions."
        ),
    }


@router.get("/positions")
def positions(
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> list[dict]:
    """Every open position, tagged with the session and mode that holds it."""

    rows = session.execute(
        scoped(SessionPosition, principal.organization_id).order_by(SessionPosition.instrument)
    ).scalars()
    out = []
    for row in rows:
        parent = session.get(TradingSession, row.session_id)
        if parent is None:
            continue
        out.append(
            {
                "session_id": row.session_id,
                "session_name": parent.name,
                "mode": parent.mode.value,
                "instrument": row.instrument,
                "quantity": row.quantity,
                "average_price": row.average_price,
                "market_price": row.market_price,
                "unrealized_pnl": row.unrealized_pnl,
                "realized_pnl": row.realized_pnl,
            }
        )
    return out


@router.get("/orders")
def orders(
    limit: int = 200,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> list[dict]:
    """Orders across every session, newest first."""

    rows = session.execute(
        scoped(SessionOrder, principal.organization_id)
        .order_by(SessionOrder.created_at.desc())
        .limit(min(limit, 1000))
    ).scalars()
    out = []
    for row in rows:
        parent = session.get(TradingSession, row.session_id)
        out.append(
            {
                "session_id": row.session_id,
                "session_name": parent.name if parent else "",
                "mode": parent.mode.value if parent else "",
                "sequence": row.sequence,
                "engine_order_id": row.engine_order_id,
                "instrument": row.instrument,
                "side": row.side,
                "quantity": row.quantity,
                "filled_quantity": row.filled_quantity,
                "average_fill_price": row.average_fill_price,
                "status": row.status,
                "order_type": row.order_type,
            }
        )
    return out


@router.get("/dashboard")
def dashboard(
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> dict:
    """The counts the landing screen shows."""

    def count(model: type[Any], *conditions: Any) -> int:
        query = scoped(model, principal.organization_id)
        for condition in conditions:
            query = query.where(condition)
        return len(list(session.execute(query).scalars()))

    running = count(TradingSession, TradingSession.status == SessionStatus.RUNNING)
    return {
        "datasets": {
            "approved": count(
                DatasetVersion, DatasetVersion.status == DatasetVersionStatus.APPROVED
            ),
            "pending": count(
                DatasetVersion, DatasetVersion.status == DatasetVersionStatus.PENDING_APPROVAL
            ),
        },
        "strategies": count(Strategy),
        "backtests": {
            "queued": count(BacktestJob, BacktestJob.status == JobStatus.QUEUED),
            "running": count(BacktestJob, BacktestJob.status == JobStatus.RUNNING),
            "completed": count(BacktestJob, BacktestJob.status == JobStatus.COMPLETED),
            "failed": count(BacktestJob, BacktestJob.status == JobStatus.FAILED),
        },
        "sessions": {
            "running": running,
            "total": count(TradingSession),
            "halted": count(TradingSession, TradingSession.status == SessionStatus.HALTED),
        },
        "unread_notifications": notifications.unread_count(
            session, principal.organization_id, principal.user_id
        ),
    }


@router.get("/notifications", response_model=list[NotificationResponse])
def list_notifications(
    limit: int = 50,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> list[NotificationResponse]:
    rows = notifications.recent(session, principal.organization_id, principal.user_id, limit=limit)
    return [
        NotificationResponse(
            id=row.id,
            severity=row.severity.value,
            kind=row.kind,
            title=row.title,
            body=row.body,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            created_at=row.created_at,
            read_at=row.read_at,
        )
        for row in rows
    ]


@router.post("/notifications/read-all")
def read_all(
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> dict:
    return {
        "marked": notifications.mark_all_read(session, principal.organization_id, principal.user_id)
    }


@router.get("/audit/verify", response_model=AuditVerificationResponse)
def verify_audit_chain(
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> AuditVerificationResponse:
    """Recompute this organization's audit chain and report any break.

    ADMIN only, like the trail itself. Scoped to the caller's organization —
    the chain is per-tenant, so verifying it never reads another tenant's rows.
    """

    principal.require(Role.ADMIN)
    return AuditVerificationResponse.model_validate(
        audit.verify_chain(session, principal.organization_id).to_dict()
    )


@router.get("/audit", response_model=list[AuditEventResponse])
def audit_log(
    limit: int = 100,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> list[AuditEventResponse]:
    """The organization's audit trail. ADMIN only — it names every actor."""

    principal.require(Role.ADMIN)
    rows = session.execute(
        scoped(AuditEvent, principal.organization_id)
        .order_by(AuditEvent.created_at.desc())
        .limit(min(limit, 500))
    ).scalars()
    return [
        AuditEventResponse(
            id=row.id,
            created_at=row.created_at,
            actor_user_id=row.actor_user_id,
            action=row.action,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            outcome=row.outcome,
            payload=json.loads(row.payload_json or "{}"),
            sequence=row.sequence,
            previous_hash=row.previous_hash,
            event_hash=row.event_hash,
        )
        for row in rows
    ]
