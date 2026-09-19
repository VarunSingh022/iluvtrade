"""Creating, starting, pausing and stopping trading sessions.

The lifecycle is explicit because an ambiguous one is how a "stopped" strategy
keeps trading. States and the transitions allowed out of them:

=============  =====================================================
CREATED        → STARTING (start) · STOPPED (stop)
STARTING       → RUNNING · FAILED
RUNNING        → PAUSED (pause) · STOPPING (stop) · HALTED (kill) · FAILED
PAUSED         → RUNNING (resume) · STOPPING (stop) · HALTED (kill)
STOPPING       → STOPPED
STOPPED/FAILED terminal
HALTED         terminal, and deliberately not resumable
=============  =====================================================

``HALTED`` is separate from ``STOPPED`` because the kill switch is not a clean
shutdown. Letting a halted session resume with a click would make the emergency
stop a pause, which is not what the operator pressing it means.

**Live sessions need three independent gates**, checked in :func:`create`:
the deployment enables live trading, the user's account enables it, and the
session carries an explicit confirmation. No one of them is sufficient.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session as DbSession

from iluvtrade.alphalab_bridge import runconfig
from iluvtrade.config import get_settings
from iluvtrade.data import ingest
from iluvtrade.db.base import utcnow
from iluvtrade.db.models.broker import BrokerAccount, BrokerKind
from iluvtrade.db.models.platform import Role, User
from iluvtrade.db.models.strategy import StrategyVersion
from iluvtrade.db.models.trading import SessionStatus, TradingMode, TradingSession
from iluvtrade.platform import audit, notifications
from iluvtrade.platform.accounts import Principal
from iluvtrade.platform.tenancy import require_owned, scoped
from iluvtrade.reddesk import entitlements
from iluvtrade.trading.projection import log_event

__all__ = [
    "SessionError",
    "SessionSpec",
    "create",
    "engage_kill_switch",
    "get",
    "list_sessions",
    "pause",
    "request_stop",
    "resume",
    "start",
]


class SessionError(ValueError):
    """The session operation was refused."""


class LiveTradingDisabledError(SessionError):
    """Live trading is not permitted. The message names which gate is shut."""


@dataclass(frozen=True, slots=True)
class SessionSpec:
    """What a caller asks for when deploying a strategy."""

    name: str
    mode: TradingMode
    strategy_version_id: str
    dataset_version_id: str | None = None
    broker_account_id: str | None = None
    parameters: dict[str, Any] | None = None
    starting_cash: str = "1000000.00"
    base_currency: str = "INR"
    risk_profile: str = "conservative"
    seed: int | None = None
    #: Must be ``True`` for a live session. Recorded on the row with who set it.
    live_confirmed: bool = False


def _risk_for(profile: str, capital: Decimal) -> runconfig.RiskProfile:
    return (
        runconfig.RiskProfile.research(capital)
        if profile == "research"
        else runconfig.RiskProfile.conservative(capital)
    )


def create(session: DbSession, principal: Principal, spec: SessionSpec) -> TradingSession:
    """Create a session after resolving every permission it depends on."""

    principal.require(Role.TRADER)
    settings = get_settings()

    # --- entitlement: resolves to one concrete version, or refuses ---------
    grant = entitlements.resolve_version(
        session, principal.organization_id, strategy_version_id=spec.strategy_version_id
    )
    version = session.get(StrategyVersion, grant.strategy_version_id)
    if version is None:
        raise SessionError("That strategy version no longer exists.")

    # --- mode-specific inputs ---------------------------------------------
    dataset_version_id: str | None = None
    broker_account_id: str | None = None

    if spec.mode is TradingMode.PAPER:
        if not spec.dataset_version_id:
            raise SessionError("A paper session needs a dataset version to read.")
        dataset_version = ingest.require_approved(
            session, principal.organization_id, spec.dataset_version_id
        )
        dataset_version_id = dataset_version.id
    else:
        # Three gates, each named separately so the user is told which one to fix.
        if not settings.live_trading_enabled:
            raise LiveTradingDisabledError(
                "Live trading is disabled in this deployment. An administrator must set "
                "ILUVTRADE_LIVE_TRADING_ENABLED=true."
            )
        user = session.get(User, principal.user_id)
        if user is None or not user.live_trading_enabled:
            raise LiveTradingDisabledError(
                "Live trading is not enabled on your account. Enable it in Settings; it is "
                "off by default."
            )
        if not spec.live_confirmed:
            raise LiveTradingDisabledError(
                "A live session must be explicitly confirmed. This is a separate, per-session "
                "acknowledgement that real orders will be sent to a real venue."
            )
        if not spec.broker_account_id:
            raise SessionError("A live session needs a broker account to route through.")
        account = require_owned(
            session, BrokerAccount, spec.broker_account_id, principal.organization_id
        )
        if account.broker is BrokerKind.PAPER:
            raise SessionError(
                "A paper broker cannot be used for a live session. That combination would "
                "label simulated fills as live."
            )
        broker_account_id = account.id

    capital = Decimal(spec.starting_cash)
    if capital <= 0:
        raise SessionError("starting_cash must be positive.")
    risk = _risk_for(spec.risk_profile, capital)

    trading_session = TradingSession(
        organization_id=principal.organization_id,
        mode=spec.mode,
        status=SessionStatus.CREATED,
        name=spec.name.strip() or f"{spec.mode.value} session",
        created_by_user_id=principal.user_id,
        strategy_version_id=version.id,
        entitlement_id=grant.entitlement_id,
        dataset_version_id=dataset_version_id,
        broker_account_id=broker_account_id,
        parameters_json=json.dumps(spec.parameters or {}, sort_keys=True),
        risk_config_json=json.dumps(risk.to_dict(), sort_keys=True),
        starting_cash=str(capital),
        base_currency=spec.base_currency,
        seed=spec.seed if spec.seed is not None else abs(hash(version.id)) % (2**31),
        live_confirmed_at=utcnow() if spec.mode is TradingMode.LIVE else None,
        live_confirmed_by_user_id=(principal.user_id if spec.mode is TradingMode.LIVE else None),
    )
    session.add(trading_session)
    session.flush()

    log_event(
        session,
        trading_session,
        kind="session.created",
        message=(
            f"{spec.mode.value.upper()} session created for strategy version {version.version} "
            f"under a {grant.basis} grant."
        ),
        payload={
            "strategy_version_id": version.id,
            "entitlement_basis": grant.basis,
            "risk_profile": spec.risk_profile,
        },
    )
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="trading.session.created",
        resource_type="trading_session",
        resource_id=trading_session.id,
        actor_user_id=principal.user_id,
        payload={
            "mode": spec.mode.value,
            "strategy_version_id": version.id,
            "dataset_version_id": dataset_version_id,
            "broker_account_id": broker_account_id,
            "live_confirmed": spec.live_confirmed,
        },
    )
    return trading_session


def get(session: DbSession, principal: Principal, session_id: str) -> TradingSession:
    return require_owned(session, TradingSession, session_id, principal.organization_id)


def list_sessions(
    session: DbSession, principal: Principal, *, mode: TradingMode | None = None
) -> list[TradingSession]:
    query = scoped(TradingSession, principal.organization_id).order_by(
        TradingSession.created_at.desc()
    )
    if mode is not None:
        query = query.where(TradingSession.mode == mode)
    return list(session.execute(query).scalars())


def _transition(
    session: DbSession,
    principal: Principal,
    session_id: str,
    *,
    allowed_from: set[SessionStatus],
    to: SessionStatus,
    action: str,
    message: str,
) -> TradingSession:
    """Move a session between states, atomically.

    **A conditional UPDATE, not a read-modify-write.** The obvious version —
    read the row, check the status, assign the new one — is wrong under
    concurrency, and was wrong here: two simultaneous ``start`` requests both
    read ``CREATED``, both passed the check, and both wrote ``STARTING``. Two
    runner threads then fed bars into one portfolio, so the position was
    double. A stress test caught it three times in five attempts.

    The status is therefore part of the ``WHERE`` clause. The database decides
    the winner, exactly once, and a caller that matched no row lost the race
    and is refused — the same answer it would get for an invalid transition,
    because from the caller's side those are the same thing: the session was
    not in a state this action is allowed from.

    Ownership is still resolved first, so a foreign id is a 404 rather than a
    refusal that admits the session exists.
    """

    # Ownership and existence, before anything else.
    trading_session = get(session, principal, session_id)

    result = session.execute(
        update(TradingSession)
        .where(
            TradingSession.id == session_id,
            TradingSession.organization_id == principal.organization_id,
            TradingSession.status.in_(sorted(allowed_from, key=lambda s: s.value)),
        )
        .values(status=to)
    )
    # ``rowcount`` is on CursorResult; ``Session.execute`` is typed as the
    # broader Result, which does not declare it. The cast is the narrowing,
    # not a claim that it might be absent.
    claimed = cast("CursorResult[Any]", result).rowcount

    if claimed != 1:
        # Re-read so the message names the state it actually found, which will
        # be the winner's new state when this was a lost race.
        session.expire(trading_session)
        current = get(session, principal, session_id)
        raise SessionError(
            f"A {current.status.value} session cannot be {action}. "
            f"Allowed from: {', '.join(sorted(s.value for s in allowed_from))}."
        )

    # The in-memory object still holds the old status; the UPDATE bypassed it.
    session.expire(trading_session)
    trading_session = get(session, principal, session_id)

    log_event(session, trading_session, kind=f"session.{action}", message=message)
    audit.record(
        session,
        organization_id=principal.organization_id,
        action=f"trading.session.{action}",
        resource_type="trading_session",
        resource_id=trading_session.id,
        actor_user_id=principal.user_id,
        payload={"status": to.value},
    )
    return trading_session


def start(session: DbSession, principal: Principal, session_id: str) -> TradingSession:
    principal.require(Role.TRADER)
    trading_session = _transition(
        session,
        principal,
        session_id,
        allowed_from={SessionStatus.CREATED},
        to=SessionStatus.STARTING,
        action="started",
        message="Session queued to start.",
    )
    trading_session.started_at = utcnow()
    notifications.notify(
        session,
        organization_id=principal.organization_id,
        kind="trading.session.started",
        title=f"{trading_session.mode.value.upper()} session started",
        body=f"{trading_session.name} is queued to run.",
        resource_type="trading_session",
        resource_id=trading_session.id,
    )
    return trading_session


def pause(session: DbSession, principal: Principal, session_id: str) -> TradingSession:
    principal.require(Role.TRADER)
    return _transition(
        session,
        principal,
        session_id,
        allowed_from={SessionStatus.RUNNING},
        to=SessionStatus.PAUSED,
        action="paused",
        message="Session paused; no further records will be processed until it resumes.",
    )


def resume(session: DbSession, principal: Principal, session_id: str) -> TradingSession:
    principal.require(Role.TRADER)
    trading_session = get(session, principal, session_id)
    if trading_session.kill_switch_engaged:
        raise SessionError(
            "This session was halted by the kill switch and cannot be resumed. Create a new "
            "session once the cause has been dealt with."
        )
    # To STARTING, not RUNNING. ``RUNNING`` means "a runner has claimed this
    # session"; only :func:`iluvtrade.trading.runner.run_session` may set it, by
    # the conditional update that makes the claim exclusive. Setting it here
    # would let two runners both believe they owned the session.
    return _transition(
        session,
        principal,
        session_id,
        allowed_from={SessionStatus.PAUSED},
        to=SessionStatus.STARTING,
        action="resumed",
        message="Session resumed; queued to continue.",
    )


def request_stop(session: DbSession, principal: Principal, session_id: str) -> TradingSession:
    """Ask a session to stop. The runner honours it between records."""

    principal.require(Role.TRADER)
    return _transition(
        session,
        principal,
        session_id,
        allowed_from={
            SessionStatus.CREATED,
            SessionStatus.STARTING,
            SessionStatus.RUNNING,
            SessionStatus.PAUSED,
        },
        to=SessionStatus.STOPPING,
        action="stopping",
        message="Stop requested; the session will halt after the current record.",
    )


def engage_kill_switch(
    session: DbSession, principal: Principal, session_id: str, *, reason: str
) -> TradingSession:
    """Halt a session immediately and permanently.

    Deliberately available to any TRADER, not just an ADMIN: an emergency stop
    that needs someone else's permission is not an emergency stop.
    """

    principal.require(Role.TRADER)
    trading_session = get(session, principal, session_id)
    trading_session.kill_switch_engaged = True
    trading_session.kill_switch_reason = reason[:400]
    trading_session.status = SessionStatus.HALTED
    trading_session.stopped_at = utcnow()

    log_event(
        session,
        trading_session,
        kind="kill_switch",
        severity="critical",
        message=f"Kill switch engaged: {reason}",
        payload={"reason": reason},
    )
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="trading.session.killed",
        resource_type="trading_session",
        resource_id=trading_session.id,
        actor_user_id=principal.user_id,
        outcome="success",
        payload={"reason": reason},
    )
    notifications.notify(
        session,
        organization_id=principal.organization_id,
        kind="trading.kill_switch",
        severity="critical",
        title="Trading session halted",
        body=f"{trading_session.name}: {reason}",
        resource_type="trading_session",
        resource_id=trading_session.id,
    )
    return trading_session
