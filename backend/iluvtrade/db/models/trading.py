"""Trading session tables, and the projections a user reads.

**AlphaLab owns the book.** A session's authoritative state is an AlphaLab
``RunState``; this module stores a *projection* of it — snapshots, orders, fills,
positions, events — so the UI, the audit trail and a restart have something
durable to read. Nothing here recomputes P&L, nets a position or decides a fill.
Where a number appears in these tables it was produced by AlphaLab and copied.

The distinction matters because PHASE 25 asks specifically for duplicate
accounting, and a table called ``session_positions`` is exactly what that would
look like if it were authoritative. It is not: it is written only by
:func:`iluvtrade.trading.projection.project`, from a ``RunState``.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from iluvtrade.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin, UtcDateTime


class TradingMode(enum.StrEnum):
    """Paper and live are never ambiguous, in the database or on the screen."""

    PAPER = "paper"
    LIVE = "live"


class SessionStatus(enum.StrEnum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    #: Halted by the kill switch. Distinct from ``STOPPED`` because it is not a
    #: clean shutdown and must not be resumable without an explicit override.
    HALTED = "halted"

    @property
    def is_terminal(self) -> bool:
        return self in {SessionStatus.STOPPED, SessionStatus.FAILED, SessionStatus.HALTED}


class TradingSession(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """One deployment of one strategy version to paper or live."""

    __tablename__ = "trading_sessions"

    mode: Mapped[TradingMode] = mapped_column(
        Enum(TradingMode, native_enum=False), nullable=False, index=True
    )
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus, native_enum=False), default=SessionStatus.CREATED, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    #: The exact version deployed. Never "the latest": PHASE 8 forbids it, and
    #: :func:`iluvtrade.trading.sessions.create` resolves an entitlement to a
    #: concrete id before this row exists.
    strategy_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("strategy_versions.id"), nullable=False
    )
    #: Entitlement under which a strategy the org does not own is being run.
    entitlement_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    #: Paper sessions replay an approved dataset version.
    dataset_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("dataset_versions.id"), nullable=True
    )
    #: Live sessions route through a broker account.
    broker_account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("broker_accounts.id"), nullable=True
    )

    parameters_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    risk_config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    starting_cash: Mapped[str] = mapped_column(String(40), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="INR")
    seed: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- the live gate, recorded rather than assumed ----------------------
    live_confirmed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    live_confirmed_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kill_switch_engaged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    kill_switch_reason: Mapped[str | None] = mapped_column(String(400), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_advanced_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    records_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- latest projection of AlphaLab's valuation ------------------------
    cash: Mapped[str | None] = mapped_column(String(40), nullable=True)
    equity: Mapped[str | None] = mapped_column(String(40), nullable=True)
    realized_pnl: Mapped[str | None] = mapped_column(String(40), nullable=True)
    unrealized_pnl: Mapped[str | None] = mapped_column(String(40), nullable=True)
    commission_paid: Mapped[str | None] = mapped_column(String(40), nullable=True)

    orders: Mapped[list[SessionOrder]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    events: Mapped[list[SessionEvent]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class SessionOrder(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """Projection of one AlphaLab OMS order."""

    __tablename__ = "session_orders"
    __table_args__ = (Index("ix_session_order", "session_id", "sequence"),)

    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trading_sessions.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    #: AlphaLab's own order id. This is the join key back to the authority.
    engine_order_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: The venue's id, once routed. Null in paper.
    broker_order_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    instrument: Mapped[str] = mapped_column(String(120), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[str] = mapped_column(String(40), nullable=False)
    filled_quantity: Mapped[str] = mapped_column(String(40), nullable=False, default="0")
    average_fill_price: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    order_type: Mapped[str] = mapped_column(String(24), nullable=False, default="market")
    submitted_timestamp: Mapped[float | None] = mapped_column(Float, nullable=True)

    session: Mapped[TradingSession] = relationship(back_populates="orders")


class SessionFill(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """Projection of one AlphaLab fill."""

    __tablename__ = "session_fills"
    __table_args__ = (Index("ix_session_fill", "session_id", "sequence"),)

    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trading_sessions.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    engine_fill_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    engine_order_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    instrument: Mapped[str] = mapped_column(String(120), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[str] = mapped_column(String(40), nullable=False)
    price: Mapped[str] = mapped_column(String(40), nullable=False)
    commission: Mapped[str] = mapped_column(String(40), nullable=False, default="0")
    fill_timestamp: Mapped[float | None] = mapped_column(Float, nullable=True)


class SessionPosition(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """Projection of AlphaLab's current position in one instrument.

    Replaced wholesale on each projection. Never incremented here — netting is
    AlphaLab's ``PortfolioEngine``'s job.
    """

    __tablename__ = "session_positions"
    __table_args__ = (Index("ix_session_position", "session_id", "instrument"),)

    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trading_sessions.id", ondelete="CASCADE"), nullable=False
    )
    instrument: Mapped[str] = mapped_column(String(120), nullable=False)
    quantity: Mapped[str] = mapped_column(String(40), nullable=False)
    average_price: Mapped[str] = mapped_column(String(40), nullable=False)
    market_price: Mapped[str | None] = mapped_column(String(40), nullable=True)
    unrealized_pnl: Mapped[str | None] = mapped_column(String(40), nullable=True)
    realized_pnl: Mapped[str | None] = mapped_column(String(40), nullable=True)


class SessionEvent(Base, IdMixin, OrgScopedMixin):
    """The session's operational log: what happened, and when.

    PHASE 16 forbids hiding a failure behind a generic success, so refusals are
    events with their own ``kind`` — ``risk_rejected``, ``record_skipped``,
    ``unpriced_asset``, ``kill_switch`` — and are written whether or not anything
    is watching.
    """

    __tablename__ = "session_events"
    __table_args__ = (Index("ix_session_event", "session_id", "sequence"),)

    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trading_sessions.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    session: Mapped[TradingSession] = relationship(back_populates="events")
