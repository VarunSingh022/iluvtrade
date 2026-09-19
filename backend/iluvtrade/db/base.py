"""The declarative base and the column conventions every table follows.

Two conventions are load-bearing and are stated here rather than repeated:

* **Identity is a UUID string**, minted by the application. Nothing in the
  product is addressed by a guessable integer, so an insecure direct object
  reference cannot be constructed by counting.
* **Tenant-scoped rows carry ``organization_id``** via :class:`OrgScopedMixin`,
  and every query for them goes through
  :func:`iluvtrade.platform.tenancy.scoped` rather than a bare ``select``.
  A table that holds user data and does not carry the column is a bug, and
  ``tests/security/test_tenant_isolation.py`` enumerates the tables to prove it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, TypeDecorator
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    """A fresh opaque identifier."""

    return str(uuid.uuid4())


def utcnow() -> datetime:
    """Timezone-aware now, in UTC. Naive datetimes are never stored."""

    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """A datetime column that is always UTC-aware on both sides.

    SQLite has no timezone-aware storage: a value written as ``2026-01-01
    12:00+00:00`` reads back naive, and the first comparison against
    :func:`utcnow` raises ``can't compare offset-naive and offset-aware``. The
    usual workarounds — comparing naively, or stripping the tzinfo at every call
    site — make the bug a property of whichever line forgot.

    This type moves the conversion to the boundary instead. Anything written is
    normalised to UTC, and anything read is returned UTC-aware, on every backend.
    A naive value handed in is rejected rather than assumed to be UTC, because
    guessing a timezone is how an audit timestamp ends up hours wrong.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "Refusing to store a naive datetime: its timezone would have to be "
                "guessed. Use iluvtrade.db.base.utcnow() or attach a tzinfo."
            )
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    """Declarative base for every iluvtrade table."""


class IdMixin:
    """A UUID primary key."""

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)


class TimestampMixin:
    """Creation and update stamps, maintained by the database."""

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class OrgScopedMixin:
    """Marks a row as belonging to exactly one organization.

    The presence of this mixin is what
    :func:`iluvtrade.platform.tenancy.scoped` keys on, so tenancy is a type-level
    fact rather than a naming convention a query can forget.
    """

    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
