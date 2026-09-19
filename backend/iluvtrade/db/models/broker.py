"""Broker account and connection tables.

What is stored here is the *platform's* half of a broker relationship: whose
account it is, which venue, what state the connection is in, and an encrypted
credential blob. The trading half — orders, executions, reconciliation — is
AlphaLab's :class:`~alphalab.broker.protocol.BrokerProtocol`, and this module
does not model any of it.

**Credentials never leave the server.** ``encrypted_credentials`` is an AES-GCM
envelope produced by :mod:`iluvtrade.brokers.crypto`, and no API response in this
application serializes it — ``tests/security/test_broker_secrets.py`` asserts
that against every broker endpoint.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Enum,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from iluvtrade.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin, UtcDateTime


class BrokerKind(enum.StrEnum):
    """Venues this deployment has a connector for.

    ``PAPER`` is AlphaLab's :class:`~alphalab.broker.paper.PaperBroker` and is
    always available. ``ZERODHA`` speaks Kite Connect v3.
    """

    PAPER = "paper"
    ZERODHA = "zerodha"


class ConnectionState(enum.StrEnum):
    DISCONNECTED = "disconnected"
    AUTHORIZING = "authorizing"
    CONNECTED = "connected"
    EXPIRED = "expired"
    ERROR = "error"
    REVOKED = "revoked"


class BrokerAccount(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """An organization's account at one venue."""

    __tablename__ = "broker_accounts"
    __table_args__ = (
        UniqueConstraint("organization_id", "broker", "label", name="uq_broker_acct"),
    )

    broker: Mapped[BrokerKind] = mapped_column(Enum(BrokerKind, native_enum=False), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    #: The venue's own identifier for the account, learned at connection time.
    #: Never invented by this application.
    venue_account_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    venue_user_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    base_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="INR")
    created_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)

    connection: Mapped[BrokerConnection | None] = relationship(
        back_populates="account", cascade="all, delete-orphan", uselist=False
    )


class BrokerConnection(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """The live credential and connectivity state for one broker account."""

    __tablename__ = "broker_connections"

    broker_account_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("broker_accounts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    state: Mapped[ConnectionState] = mapped_column(
        Enum(ConnectionState, native_enum=False),
        default=ConnectionState.DISCONNECTED,
        nullable=False,
    )
    #: AES-GCM envelope. Opaque to everything but :mod:`iluvtrade.brokers.crypto`.
    encrypted_credentials: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    #: Non-secret, displayable hint such as the last four of an API key.
    credential_hint: Mapped[str | None] = mapped_column(String(40), nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Incremented on every authorization attempt, for rate-limit and audit.
    authorization_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    account: Mapped[BrokerAccount] = relationship(back_populates="connection")
