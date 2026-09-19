"""RedDesk: the marketplace layer.

RedDesk distributes strategies; it does not run them. Nothing in this module
touches execution, accounting or risk — a listing points at a
:class:`~iluvtrade.db.models.strategy.StrategyVersion`, and an entitlement says
which versions a buyer may run. What happens when they run is AlphaLab's.

The entitlement is the load-bearing object. It resolves to **exact strategy
versions**, never to "the latest": a buyer who purchased v3 keeps running v3
when the creator publishes v4, and gets v4 only under a plan that says so.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from iluvtrade.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin, UtcDateTime


class ListingStatus(enum.StrEnum):
    """The creator workflow from PHASE 9, as states rather than prose."""

    DRAFT = "draft"
    VALIDATING = "validating"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    PUBLISHED = "published"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class BillingCadence(enum.StrEnum):
    ONE_TIME = "one_time"
    MONTHLY = "monthly"
    ANNUAL = "annual"


class EntitlementStatus(enum.StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class PurchaseStatus(enum.StrEnum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"


class VersionAccessPolicy(enum.StrEnum):
    """Which versions a purchase entitles the buyer to run.

    This is the *licence*, written down. PHASE 15 forbids implying a purchase
    grants more than the licence says, so the licence is a column and not a
    README sentence.
    """

    #: Exactly the version purchased. Nothing else, ever.
    PINNED = "pinned"
    #: The purchased version and later versions published while the subscription
    #: is active. Resolution still returns a concrete version id.
    ROLLING = "rolling"


class Listing(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """A strategy offered on RedDesk by its creator organization.

    ``organization_id`` is the *creator's* org. Buyers are on the other side of
    :class:`Entitlement`, so a listing is never tenant-scoped to its buyers.
    """

    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("organization_id", "slug", name="uq_listing_slug"),)

    slug: Mapped[str] = mapped_column(String(140), nullable=False)
    strategy_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    methodology: Mapped[str] = mapped_column(Text, nullable=False, default="")
    risk_disclosure: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[ListingStatus] = mapped_column(
        Enum(ListingStatus, native_enum=False), default=ListingStatus.DRAFT, nullable=False
    )

    # --- commercial terms -------------------------------------------------
    price_amount: Mapped[str] = mapped_column(String(40), nullable=False, default="0")
    price_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="INR")
    billing_cadence: Mapped[BillingCadence] = mapped_column(
        Enum(BillingCadence, native_enum=False), default=BillingCadence.ONE_TIME, nullable=False
    )
    version_access_policy: Mapped[VersionAccessPolicy] = mapped_column(
        Enum(VersionAccessPolicy, native_enum=False),
        default=VersionAccessPolicy.PINNED,
        nullable=False,
    )
    #: The licence text the buyer accepts. A purchase records the hash of this,
    #: so "what did I agree to" has an answer that cannot be edited afterwards.
    licence_terms: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # --- what it supports, declared rather than inferred -------------------
    supported_brokers_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    supported_instruments_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    supported_data_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    review_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    offers: Mapped[list[ListingVersion]] = relationship(
        back_populates="listing", cascade="all, delete-orphan"
    )


class ListingVersion(Base, IdMixin, TimestampMixin):
    """A strategy version this listing offers, with its backtest evidence.

    Evidence is a reference to a real :class:`~iluvtrade.db.models.backtest.BacktestRun`
    the creator produced on this platform — not a number a creator typed. A
    listing with no evidence run is publishable, and is shown as having none.
    """

    __tablename__ = "listing_versions"
    __table_args__ = (
        UniqueConstraint("listing_id", "strategy_version_id", name="uq_listing_version"),
    )

    listing_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("listings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    strategy_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("strategy_versions.id"), nullable=False
    )
    evidence_backtest_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    release_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    listing: Mapped[Listing] = relationship(back_populates="offers")


class Purchase(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """A buyer organization's acquisition of a listing.

    ``organization_id`` is the **buyer**. ``provider`` names the payment provider
    that settled it; :mod:`iluvtrade.billing` defines the interface so no vendor
    is baked into this table.
    """

    __tablename__ = "purchases"

    listing_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("listings.id"), nullable=False, index=True
    )
    strategy_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("strategy_versions.id"), nullable=False
    )
    purchased_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[PurchaseStatus] = mapped_column(
        Enum(PurchaseStatus, native_enum=False), default=PurchaseStatus.PENDING, nullable=False
    )
    amount: Mapped[str] = mapped_column(String(40), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    platform_fee: Mapped[str] = mapped_column(String(40), nullable=False, default="0")
    creator_net: Mapped[str] = mapped_column(String(40), nullable=False, default="0")
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="manual")
    provider_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: SHA-256 of ``Listing.licence_terms`` as it stood at purchase.
    licence_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    idempotency_key: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)


class Entitlement(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """What a buyer organization may actually run.

    Every deployment path — backtest, paper, live — asks
    :func:`iluvtrade.reddesk.entitlements.resolve` before it runs a strategy an
    organization does not own, and that function answers with a concrete
    strategy version id or refuses.
    """

    __tablename__ = "entitlements"

    listing_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("listings.id"), nullable=False, index=True
    )
    strategy_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    #: The concrete version granted. Under ``ROLLING`` this is advanced by an
    #: explicit service call that writes an audit event — never implicitly at
    #: read time, so "which version did I run" is always answerable.
    granted_strategy_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("strategy_versions.id"), nullable=False
    )
    version_access_policy: Mapped[VersionAccessPolicy] = mapped_column(
        Enum(VersionAccessPolicy, native_enum=False), nullable=False
    )
    status: Mapped[EntitlementStatus] = mapped_column(
        Enum(EntitlementStatus, native_enum=False),
        default=EntitlementStatus.ACTIVE,
        nullable=False,
    )
    source_purchase_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("purchases.id"), nullable=True
    )
    valid_from: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(400), nullable=True)


class Review(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """A buyer's rating of a listing they hold an entitlement for."""

    __tablename__ = "reviews"
    __table_args__ = (UniqueConstraint("listing_id", "organization_id", name="uq_review_once"),)

    listing_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("listings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")


class CreatorPayout(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """Money owed to a creator organization for settled purchases.

    Recorded so the ledger is auditable. Actually moving money needs a payment
    provider under contract; ``docs/SECURITY.md`` and ``docs/REDDESK.md`` record
    that as an external dependency.
    """

    __tablename__ = "creator_payouts"

    period_start: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    gross_amount: Mapped[str] = mapped_column(String(40), nullable=False)
    platform_fee: Mapped[str] = mapped_column(String(40), nullable=False)
    net_amount: Mapped[str] = mapped_column(String(40), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    purchase_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    settled_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    rating_snapshot: Mapped[float | None] = mapped_column(Float, nullable=True)
