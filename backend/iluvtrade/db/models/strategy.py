"""Strategy identity and immutable versions.

PHASE 8 states the distinction this module exists to keep:

    Strategy identity != Strategy version != marketplace listing != entitlement

``Strategy`` is the identity. ``StrategyVersion`` is what actually runs, and is
frozen on publication: a published version's ``implementation_key``,
``parameters_schema_json`` and ``default_parameters_json`` cannot change, which
is what makes a backtest reproducible and an entitlement meaningful. The
marketplace's half of the distinction lives in :mod:`iluvtrade.db.models.reddesk`.

**What a version is not.** It is not seller code. ``implementation_key`` names a
strategy class registered in this deployment's
:mod:`iluvtrade.strategies.registry` — a declared, in-repository implementation.
Executing uploaded third-party Python needs the isolation boundary PHASE 10
describes, which this deployment does not yet have; ``docs/SECURITY.md`` records
that as an external dependency rather than pretending the seam is closed.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from iluvtrade.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin, UtcDateTime


class StrategyVisibility(enum.StrEnum):
    PRIVATE = "private"
    ORGANIZATION = "organization"
    MARKETPLACE = "marketplace"


class StrategyVersionStatus(enum.StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    #: Withdrawn from new use. Existing entitlements still resolve to it, because
    #: revoking a version someone already ran would rewrite their history.
    DEPRECATED = "deprecated"


class CertificationStatus(enum.StrEnum):
    UNCERTIFIED = "uncertified"
    PENDING = "pending"
    CERTIFIED = "certified"
    REJECTED = "rejected"


class Strategy(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """A strategy's stable identity, owned by one organization."""

    __tablename__ = "strategies"
    __table_args__ = (UniqueConstraint("organization_id", "slug", name="uq_strategy_slug"),)

    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    visibility: Mapped[StrategyVisibility] = mapped_column(
        Enum(StrategyVisibility, native_enum=False),
        default=StrategyVisibility.PRIVATE,
        nullable=False,
    )
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)

    versions: Mapped[list[StrategyVersion]] = relationship(
        back_populates="strategy", cascade="all, delete-orphan", order_by="StrategyVersion.version"
    )


class StrategyVersion(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """One immutable, runnable revision of a strategy."""

    __tablename__ = "strategy_versions"
    __table_args__ = (UniqueConstraint("strategy_id", "version", name="uq_strategy_version"),)

    strategy_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[StrategyVersionStatus] = mapped_column(
        Enum(StrategyVersionStatus, native_enum=False),
        default=StrategyVersionStatus.DRAFT,
        nullable=False,
    )

    #: Key into :mod:`iluvtrade.strategies.registry`. Frozen on publication.
    implementation_key: Mapped[str] = mapped_column(String(120), nullable=False)
    parameters_schema_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    default_parameters_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    changelog: Mapped[str] = mapped_column(Text, nullable=False, default="")

    #: SHA-256 over the frozen fields, computed at publication. A mutation that
    #: bypassed the service layer is detectable by recomputing it — which
    #: ``tests/security/test_strategy_version_immutability.py`` does.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    published_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    certification_status: Mapped[CertificationStatus] = mapped_column(
        Enum(CertificationStatus, native_enum=False),
        default=CertificationStatus.UNCERTIFIED,
        nullable=False,
    )
    certification_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: True once published. Checked by the service layer before any write.
    frozen: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    strategy: Mapped[Strategy] = relationship(back_populates="versions")
