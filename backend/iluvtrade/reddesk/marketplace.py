"""The creator and buyer workflows.

Creator:  Draft → Validating → Submitted → Approved → Published
Buyer:    Discover → Inspect evidence → Purchase → Entitlement

:func:`validate_listing` is the gate between Draft and Submitted, and it is a
real check rather than a status flip: it refuses a listing whose strategy version
is unpublished, whose price is nonsense, or which claims support for a broker
this deployment has no connector for. A marketplace that lets any of those be
published is one where the listing text and the software disagree.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from iluvtrade.billing import Charge, ChargeStatus, Refund, get_provider
from iluvtrade.db.base import utcnow
from iluvtrade.db.models.backtest import BacktestRun
from iluvtrade.db.models.broker import BrokerKind
from iluvtrade.db.models.platform import Role
from iluvtrade.db.models.reddesk import (
    BillingCadence,
    CreatorPayout,
    Entitlement,
    EntitlementStatus,
    Listing,
    ListingStatus,
    ListingVersion,
    Purchase,
    PurchaseStatus,
    Review,
    VersionAccessPolicy,
)
from iluvtrade.db.models.strategy import Strategy, StrategyVersion, StrategyVersionStatus
from iluvtrade.platform import audit, notifications
from iluvtrade.platform.accounts import Principal, slugify
from iluvtrade.platform.tenancy import NotFoundError, require_owned, scoped
from iluvtrade.reddesk import entitlements

__all__ = [
    "ListingError",
    "ValidationIssue",
    "create_listing",
    "discover",
    "publish_listing",
    "purchase",
    "record_payout",
    "refund",
    "submit_listing",
    "validate_listing",
]

#: The platform's share of a sale. A constant here, and recorded on every
#: purchase row, so a later change does not rewrite history.
PLATFORM_FEE_RATE = Decimal("0.20")


class ListingError(ValueError):
    """The marketplace operation was refused."""


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _listing_or_404(session: DbSession, principal: Principal, listing_id: str) -> Listing:
    return require_owned(session, Listing, listing_id, principal.organization_id)


def create_listing(
    session: DbSession,
    principal: Principal,
    *,
    strategy_id: str,
    title: str,
    summary: str = "",
    description: str = "",
    methodology: str = "",
    risk_disclosure: str = "",
    price_amount: str = "0",
    price_currency: str = "INR",
    billing_cadence: BillingCadence = BillingCadence.ONE_TIME,
    version_access_policy: VersionAccessPolicy = VersionAccessPolicy.PINNED,
    licence_terms: str = "",
    supported_brokers: list[str] | None = None,
    supported_instruments: list[str] | None = None,
    supported_data: list[str] | None = None,
) -> Listing:
    """Create a draft listing for a strategy the caller's organization owns."""

    import json

    principal.require(Role.TRADER)
    require_owned(session, Strategy, strategy_id, principal.organization_id)

    slug = slugify(title, fallback="listing")
    if session.execute(
        scoped(Listing, principal.organization_id).where(Listing.slug == slug)
    ).scalar_one_or_none():
        raise ListingError(f"A listing named {title!r} already exists in this workspace.")

    listing = Listing(
        organization_id=principal.organization_id,
        slug=slug,
        strategy_id=strategy_id,
        title=title.strip(),
        summary=summary[:400],
        description=description,
        methodology=methodology,
        risk_disclosure=risk_disclosure,
        status=ListingStatus.DRAFT,
        price_amount=price_amount,
        price_currency=price_currency,
        billing_cadence=billing_cadence,
        version_access_policy=version_access_policy,
        licence_terms=licence_terms,
        supported_brokers_json=json.dumps(supported_brokers or []),
        supported_instruments_json=json.dumps(supported_instruments or []),
        supported_data_json=json.dumps(supported_data or []),
    )
    session.add(listing)
    session.flush()
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="reddesk.listing.created",
        resource_type="listing",
        resource_id=listing.id,
        actor_user_id=principal.user_id,
        payload={"strategy_id": strategy_id, "title": title},
    )
    return listing


def offer_version(
    session: DbSession,
    principal: Principal,
    *,
    listing_id: str,
    strategy_version_id: str,
    evidence_backtest_run_id: str | None = None,
    release_notes: str = "",
    make_current: bool = True,
) -> ListingVersion:
    """Attach a strategy version to a listing, with its backtest evidence.

    Evidence must be a run **this organization actually performed on this
    platform**, against this exact strategy version. A creator cannot cite a
    number they typed, or another strategy's result.
    """

    listing = _listing_or_404(session, principal, listing_id)
    version = require_owned(
        session, StrategyVersion, strategy_version_id, principal.organization_id
    )
    if version.strategy_id != listing.strategy_id:
        raise ListingError("That strategy version belongs to a different strategy.")
    if version.status is not StrategyVersionStatus.PUBLISHED:
        raise ListingError(
            f"Version {version.version} is {version.status.value}. Only a published, frozen "
            "version can be offered, because a buyer must get something that cannot change."
        )

    if evidence_backtest_run_id is not None:
        run = require_owned(
            session, BacktestRun, evidence_backtest_run_id, principal.organization_id
        )
        if run.strategy_version_id != version.id:
            raise ListingError("The cited backtest was run against a different strategy version.")

    existing = session.execute(
        select(ListingVersion).where(
            ListingVersion.listing_id == listing.id,
            ListingVersion.strategy_version_id == version.id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.evidence_backtest_run_id = evidence_backtest_run_id
        existing.release_notes = release_notes
        offer = existing
    else:
        offer = ListingVersion(
            listing_id=listing.id,
            strategy_version_id=version.id,
            evidence_backtest_run_id=evidence_backtest_run_id,
            release_notes=release_notes,
            is_current=make_current,
        )
        session.add(offer)

    if make_current:
        for other in session.execute(
            select(ListingVersion).where(ListingVersion.listing_id == listing.id)
        ).scalars():
            other.is_current = other is offer
        offer.is_current = True
    session.flush()
    return offer


def validate_listing(session: DbSession, listing: Listing) -> list[ValidationIssue]:
    """Everything wrong with a listing, as a list. Empty means publishable."""

    import json

    issues: list[ValidationIssue] = []

    if not listing.title.strip():
        issues.append(ValidationIssue("title_missing", "A listing needs a title."))
    if len(listing.description.strip()) < 40:
        issues.append(
            ValidationIssue(
                "description_too_short",
                "Describe what the strategy does in at least 40 characters. A buyer cannot "
                "assess a listing with no description.",
            )
        )
    if not listing.methodology.strip():
        issues.append(
            ValidationIssue(
                "methodology_missing",
                "State the methodology. PHASE 9 requires it and a buyer needs it.",
            )
        )
    if not listing.risk_disclosure.strip():
        issues.append(
            ValidationIssue(
                "risk_disclosure_missing",
                "A risk disclosure is required. Past backtest results are not a forecast, "
                "and the listing must say so in the creator's own words.",
            )
        )

    try:
        price = Decimal(listing.price_amount)
        if price < 0:
            issues.append(ValidationIssue("price_negative", "Price cannot be negative."))
    except (InvalidOperation, TypeError):
        issues.append(ValidationIssue("price_invalid", "Price is not a valid amount."))

    offers = list(
        session.execute(
            select(ListingVersion).where(ListingVersion.listing_id == listing.id)
        ).scalars()
    )
    if not offers:
        issues.append(
            ValidationIssue("no_version", "Attach at least one published strategy version.")
        )
    if offers and not any(offer.is_current for offer in offers):
        issues.append(ValidationIssue("no_current_version", "Mark one version as current."))

    for offer in offers:
        version = session.get(StrategyVersion, offer.strategy_version_id)
        if version is None:
            issues.append(
                ValidationIssue("version_missing", "An offered strategy version no longer exists.")
            )
        elif version.status is not StrategyVersionStatus.PUBLISHED:
            issues.append(
                ValidationIssue(
                    "version_unpublished",
                    f"Version {version.version} is {version.status.value}; only published "
                    "versions may be sold.",
                )
            )

    known_brokers = {kind.value for kind in BrokerKind}
    for broker in json.loads(listing.supported_brokers_json or "[]"):
        if str(broker).lower() not in known_brokers:
            issues.append(
                ValidationIssue(
                    "unsupported_broker",
                    f"The listing claims support for {broker!r}, which this platform has no "
                    f"connector for. Known: {', '.join(sorted(known_brokers))}.",
                )
            )

    if Decimal(listing.price_amount or "0") > 0 and not listing.licence_terms.strip():
        issues.append(
            ValidationIssue(
                "licence_missing",
                "A paid listing must state its licence terms. A buyer records the hash of "
                "these at purchase, so they cannot be changed afterwards.",
            )
        )
    return issues


def submit_listing(session: DbSession, principal: Principal, listing_id: str) -> Listing:
    """Run validation and move a draft to Submitted, or refuse with the reasons."""

    principal.require(Role.TRADER)
    listing = _listing_or_404(session, principal, listing_id)
    if listing.status not in (ListingStatus.DRAFT, ListingStatus.REJECTED):
        raise ListingError(f"A {listing.status.value} listing cannot be submitted.")

    listing.status = ListingStatus.VALIDATING
    issues = validate_listing(session, listing)
    if issues:
        listing.status = ListingStatus.DRAFT
        listing.review_notes = "; ".join(issue.message for issue in issues)
        raise ListingError(
            "This listing is not ready: " + " ".join(issue.message for issue in issues)
        )

    listing.status = ListingStatus.SUBMITTED
    listing.review_notes = ""
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="reddesk.listing.submitted",
        resource_type="listing",
        resource_id=listing.id,
        actor_user_id=principal.user_id,
    )
    return listing


def review_listing(
    session: DbSession,
    principal: Principal,
    *,
    listing_id: str,
    approve: bool,
    notes: str = "",
) -> Listing:
    """A reviewer's decision on a submitted listing.

    Requires ADMIN. In this deployment the reviewer is an administrator of the
    creator's own organization, which is honest for a single-tenant install and
    is **not** an independent review; ``docs/REDDESK.md`` says so plainly.
    """

    principal.require(Role.ADMIN)
    listing = _listing_or_404(session, principal, listing_id)
    if listing.status is not ListingStatus.SUBMITTED:
        raise ListingError(f"A {listing.status.value} listing is not awaiting review.")
    listing.status = ListingStatus.APPROVED if approve else ListingStatus.REJECTED
    listing.review_notes = notes
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="reddesk.listing.reviewed",
        resource_type="listing",
        resource_id=listing.id,
        actor_user_id=principal.user_id,
        payload={"approved": approve, "notes": notes},
    )
    return listing


def publish_listing(session: DbSession, principal: Principal, listing_id: str) -> Listing:
    """Make an approved listing visible in discovery."""

    principal.require(Role.ADMIN)
    listing = _listing_or_404(session, principal, listing_id)
    if listing.status is not ListingStatus.APPROVED:
        raise ListingError(
            f"A {listing.status.value} listing cannot be published; it must be approved first."
        )
    issues = validate_listing(session, listing)
    if issues:
        raise ListingError(
            "This listing no longer validates: " + " ".join(i.message for i in issues)
        )
    listing.status = ListingStatus.PUBLISHED
    listing.published_at = utcnow()
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="reddesk.listing.published",
        resource_type="listing",
        resource_id=listing.id,
        actor_user_id=principal.user_id,
    )
    return listing


def withdraw_listing(session: DbSession, principal: Principal, listing_id: str) -> Listing:
    """Remove a listing from discovery. Existing entitlements are untouched.

    Revoking what buyers already hold because a creator delisted would break
    running sessions and rewrite what people paid for.
    """

    principal.require(Role.TRADER)
    listing = _listing_or_404(session, principal, listing_id)
    listing.status = ListingStatus.WITHDRAWN
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="reddesk.listing.withdrawn",
        resource_type="listing",
        resource_id=listing.id,
        actor_user_id=principal.user_id,
    )
    return listing


def discover(
    session: DbSession, *, query: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Published listings, visible to every organization.

    Cross-tenant by design — a marketplace nobody else can see is not one — and
    therefore does **not** use :func:`scoped`. Only published listings are
    returned, and only their public fields.
    """

    statement = (
        select(Listing)
        .where(Listing.status == ListingStatus.PUBLISHED)
        .order_by(Listing.published_at.desc())
        .limit(min(limit, 200))
    )
    if query:
        pattern = f"%{query.strip().lower()}%"
        statement = statement.where(
            func.lower(Listing.title).like(pattern) | func.lower(Listing.summary).like(pattern)
        )
    return [summarise(session, listing) for listing in session.execute(statement).scalars()]


def summarise(session: DbSession, listing: Listing) -> dict[str, Any]:
    """A listing's public view, including its evidence and rating."""

    import json

    offers = list(
        session.execute(
            select(ListingVersion).where(ListingVersion.listing_id == listing.id)
        ).scalars()
    )
    current = next((offer for offer in offers if offer.is_current), None)

    evidence: dict[str, Any] | None = None
    if current is not None and current.evidence_backtest_run_id:
        run = session.get(BacktestRun, current.evidence_backtest_run_id)
        if run is not None:
            evidence = {
                "backtest_run_id": run.id,
                "records_processed": run.records_processed,
                "order_count": run.order_count,
                "starting_cash": run.starting_cash,
                "ending_equity": run.ending_equity,
                "total_return": run.total_return,
                "cagr": run.cagr,
                "sharpe_ratio": run.sharpe_ratio,
                "max_drawdown": run.max_drawdown,
                "volatility": run.volatility,
                "engine": f"{run.engine_name} {run.engine_version}",
                "seed": run.seed,
                # Stated on every piece of evidence the marketplace shows.
                "disclaimer": (
                    "A backtest is a simulation over historical data under stated "
                    "assumptions. It is not a prediction and not a guarantee of future "
                    "performance."
                ),
            }

    ratings = list(
        session.execute(select(Review.rating).where(Review.listing_id == listing.id)).scalars()
    )

    return {
        "id": listing.id,
        "slug": listing.slug,
        "title": listing.title,
        "summary": listing.summary,
        "description": listing.description,
        "methodology": listing.methodology,
        "risk_disclosure": listing.risk_disclosure,
        "status": listing.status.value,
        "creator_organization_id": listing.organization_id,
        "strategy_id": listing.strategy_id,
        "price": {
            "amount": listing.price_amount,
            "currency": listing.price_currency,
            "cadence": listing.billing_cadence.value,
        },
        "version_access_policy": listing.version_access_policy.value,
        "licence_terms": listing.licence_terms,
        "supported_brokers": json.loads(listing.supported_brokers_json or "[]"),
        "supported_instruments": json.loads(listing.supported_instruments_json or "[]"),
        "supported_data": json.loads(listing.supported_data_json or "[]"),
        "current_strategy_version_id": current.strategy_version_id if current else None,
        "version_history": [
            {
                "strategy_version_id": offer.strategy_version_id,
                "release_notes": offer.release_notes,
                "is_current": offer.is_current,
                "has_evidence": bool(offer.evidence_backtest_run_id),
            }
            for offer in offers
        ],
        "evidence": evidence,
        "rating": {
            "average": round(sum(ratings) / len(ratings), 2) if ratings else None,
            "count": len(ratings),
        },
        "published_at": listing.published_at.isoformat() if listing.published_at else None,
    }


def _cadence_term(cadence: BillingCadence) -> timedelta | None:
    match cadence:
        case BillingCadence.MONTHLY:
            return timedelta(days=30)
        case BillingCadence.ANNUAL:
            return timedelta(days=365)
        case _:
            return None


def purchase(
    session: DbSession,
    principal: Principal,
    *,
    listing_id: str,
    idempotency_key: str | None = None,
    provider: str = "manual",
    provider_reference: str | None = None,
) -> tuple[Purchase, Entitlement]:
    """Buy a listing and receive an entitlement to a concrete version.

    **No money moves here.** ``provider='manual'`` records a settled purchase
    without a payment processor, which is what a local deployment can honestly
    do; :mod:`iluvtrade.billing` defines the interface a real provider
    implements. The ledger fields — fee, net, currency, licence hash — are
    written correctly either way, so switching providers does not restate
    history.
    """

    principal.require(Role.TRADER)
    listing = session.get(Listing, listing_id)
    if listing is None or listing.status is not ListingStatus.PUBLISHED:
        raise NotFoundError("That listing is not available.")
    if listing.organization_id == principal.organization_id:
        raise ListingError("You already own this strategy; buying your own listing does nothing.")

    if idempotency_key:
        existing = session.execute(
            scoped(Purchase, principal.organization_id).where(
                Purchase.idempotency_key == idempotency_key
            )
        ).scalar_one_or_none()
        if existing is not None:
            entitlement = session.execute(
                scoped(Entitlement, principal.organization_id).where(
                    Entitlement.source_purchase_id == existing.id
                )
            ).scalar_one_or_none()
            if entitlement is not None:
                return existing, entitlement

    current = (
        session.execute(
            select(ListingVersion).where(
                ListingVersion.listing_id == listing.id, ListingVersion.is_current
            )
        )
        .scalars()
        .first()
    )
    if current is None:
        raise ListingError("That listing has no current version to sell.")

    amount = Decimal(listing.price_amount or "0")
    fee = (amount * PLATFORM_FEE_RATE).quantize(Decimal("0.01"))

    # Settle through the provider rather than assuming settlement. The manual
    # provider records it without contacting anyone — which is the honest thing
    # for a deployment with no merchant account — but the call is real, so
    # swapping in a provider that charges a card changes nothing above this
    # line, and a refusal is handled here rather than discovered later.
    settlement = get_provider(provider).charge(
        Charge(
            amount=amount,
            currency=listing.price_currency,
            organization_id=principal.organization_id,
            description=f"RedDesk listing {listing.slug}",
            idempotency_key=idempotency_key or f"listing:{listing.id}:{principal.organization_id}",
        )
    )
    if settlement.status is ChargeStatus.FAILED:
        raise ListingError(f"Payment was refused: {settlement.message}")

    purchase_row = Purchase(
        organization_id=principal.organization_id,
        listing_id=listing.id,
        strategy_version_id=current.strategy_version_id,
        purchased_by_user_id=principal.user_id,
        status=(PurchaseStatus.PAID if settlement.is_settled else PurchaseStatus.PENDING),
        amount=str(amount),
        currency=listing.price_currency,
        platform_fee=str(fee),
        creator_net=str(amount - fee),
        provider=provider,
        provider_reference=provider_reference or settlement.reference,
        licence_hash=hashlib.sha256((listing.licence_terms or "").encode("utf-8")).hexdigest(),
        idempotency_key=idempotency_key,
    )
    session.add(purchase_row)
    session.flush()

    if not settlement.is_settled:
        # Pending means the provider has accepted but not settled. Granting an
        # entitlement now would let someone run a strategy they have not paid
        # for, and revoking it later is a worse experience than waiting.
        raise ListingError(
            "Payment is still being processed. The entitlement will be granted once the "
            "provider settles it."
        )

    term = _cadence_term(listing.billing_cadence)
    entitlement = entitlements.grant(
        session,
        organization_id=principal.organization_id,
        listing=listing,
        strategy_version_id=current.strategy_version_id,
        purchase_id=purchase_row.id,
        valid_until=utcnow() + term if term else None,
        actor_user_id=principal.user_id,
    )

    audit.record(
        session,
        organization_id=principal.organization_id,
        action="reddesk.purchase.completed",
        resource_type="purchase",
        resource_id=purchase_row.id,
        actor_user_id=principal.user_id,
        payload={
            "listing_id": listing.id,
            "amount": str(amount),
            "currency": listing.price_currency,
            "provider": provider,
            "moves_money": get_provider(provider).moves_money,
            "strategy_version_id": current.strategy_version_id,
        },
    )
    notifications.notify(
        session,
        organization_id=principal.organization_id,
        user_id=principal.user_id,
        kind="reddesk.purchased",
        title=f"Licence acquired: {listing.title}",
        body=(
            f"Your workspace may now run strategy version "
            f"{current.strategy_version_id[:8]} under a "
            f"{listing.version_access_policy.value} licence."
        ),
        resource_type="entitlement",
        resource_id=entitlement.id,
    )
    notifications.notify(
        session,
        organization_id=listing.organization_id,
        kind="reddesk.sale",
        title="Your strategy was purchased",
        body=f"{listing.title} — {listing.price_currency} {amount}.",
        resource_type="listing",
        resource_id=listing.id,
    )
    return purchase_row, entitlement


def review(
    session: DbSession,
    principal: Principal,
    *,
    listing_id: str,
    rating: int,
    body: str = "",
) -> Review:
    """Rate a listing. Only an organization holding an entitlement may review."""

    if not 1 <= rating <= 5:
        raise ListingError("A rating must be between 1 and 5.")
    listing = session.get(Listing, listing_id)
    if listing is None:
        raise NotFoundError("That listing does not exist.")

    held = (
        session.execute(
            scoped(Entitlement, principal.organization_id).where(
                Entitlement.listing_id == listing.id
            )
        )
        .scalars()
        .first()
    )
    if held is None:
        raise ListingError("Only a workspace that has acquired this strategy can review it.")

    existing = session.execute(
        scoped(Review, principal.organization_id).where(Review.listing_id == listing.id)
    ).scalar_one_or_none()
    if existing is not None:
        existing.rating = rating
        existing.body = body
        return existing

    row = Review(
        organization_id=principal.organization_id,
        listing_id=listing.id,
        author_user_id=principal.user_id,
        rating=rating,
        body=body,
    )
    session.add(row)
    session.flush()
    return row


def refund(
    session: DbSession,
    principal: Principal,
    *,
    purchase_id: str,
    reason: str,
) -> tuple[Purchase, list[Entitlement]]:
    """Refund a purchase and revoke every entitlement it granted.

    Revocation is the point. A refunded purchase that left a working licence
    behind is a strategy being run for free — and worse, the buyer has no
    indication their access is no longer legitimate.

    The provider is asked first. If it refuses, nothing is changed: recording a
    refund that did not happen puts a false entry in a financial ledger, which
    is the failure :class:`~iluvtrade.billing.ManualProvider` exists to avoid
    elsewhere.
    """

    principal.require(Role.ADMIN)
    purchase = require_owned(session, Purchase, purchase_id, principal.organization_id)

    if purchase.status is PurchaseStatus.REFUNDED:
        held = list(
            session.execute(
                scoped(Entitlement, principal.organization_id).where(
                    Entitlement.source_purchase_id == purchase.id
                )
            ).scalars()
        )
        return purchase, held
    if purchase.status is not PurchaseStatus.PAID:
        raise ListingError(f"A {purchase.status.value} purchase cannot be refunded.")

    outcome = get_provider(purchase.provider).refund(
        Refund(
            reference=purchase.provider_reference or purchase.id,
            amount=Decimal(purchase.amount),
            reason=reason[:400],
        )
    )
    if outcome.status is ChargeStatus.FAILED:
        raise ListingError(f"The provider refused the refund: {outcome.message}")

    purchase.status = PurchaseStatus.REFUNDED
    revoked: list[Entitlement] = []
    for entitlement in session.execute(
        scoped(Entitlement, principal.organization_id).where(
            Entitlement.source_purchase_id == purchase.id
        )
    ).scalars():
        if entitlement.status is EntitlementStatus.ACTIVE:
            entitlements.revoke(
                session,
                entitlement.id,
                reason=f"Purchase refunded: {reason}",
                actor_user_id=principal.user_id,
            )
        revoked.append(entitlement)

    audit.record(
        session,
        organization_id=principal.organization_id,
        action="reddesk.purchase.refunded",
        resource_type="purchase",
        resource_id=purchase.id,
        actor_user_id=principal.user_id,
        payload={
            "amount": purchase.amount,
            "currency": purchase.currency,
            "provider": purchase.provider,
            "provider_reference": outcome.reference,
            "entitlements_revoked": len(revoked),
            "reason": reason,
        },
    )
    notifications.notify(
        session,
        organization_id=principal.organization_id,
        kind="reddesk.refunded",
        severity="warning",
        title="Purchase refunded",
        body=(
            f"{purchase.currency} {purchase.amount} refunded. "
            f"{len(revoked)} entitlement(s) revoked."
        ),
        resource_type="purchase",
        resource_id=purchase.id,
    )
    session.flush()
    return purchase, revoked


def record_payout(
    session: DbSession,
    principal: Principal,
    *,
    period_start: datetime,
    period_end: datetime,
) -> CreatorPayout:
    """Total what this creator organization is owed for a period.

    **Records what is owed; does not pay it.** Settlement needs a provider that
    moves money, and :class:`~iluvtrade.billing.ManualProvider` raises rather
    than pretending. The row exists so the ledger is auditable either way, and
    its ``status`` stays ``pending`` until something actually settles it.

    Only ``PAID`` purchases count. A refunded one contributes nothing, which is
    why this is computed rather than incremented as sales arrive.
    """

    principal.require(Role.ADMIN)

    listings = [
        row.id for row in session.execute(scoped(Listing, principal.organization_id)).scalars()
    ]
    gross = Decimal("0")
    fees = Decimal("0")
    currency = "INR"
    counted = 0

    if listings:
        purchases = session.execute(
            select(Purchase).where(
                Purchase.listing_id.in_(listings),
                Purchase.status == PurchaseStatus.PAID,
                Purchase.created_at >= period_start,
                Purchase.created_at < period_end,
            )
        ).scalars()
        for purchase in purchases:
            gross += Decimal(purchase.amount)
            fees += Decimal(purchase.platform_fee)
            currency = purchase.currency
            counted += 1

    payout = CreatorPayout(
        organization_id=principal.organization_id,
        period_start=period_start,
        period_end=period_end,
        gross_amount=str(gross),
        platform_fee=str(fees),
        net_amount=str(gross - fees),
        currency=currency,
        purchase_count=counted,
        status="pending",
    )
    session.add(payout)
    session.flush()
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="reddesk.payout.recorded",
        resource_type="creator_payout",
        resource_id=payout.id,
        actor_user_id=principal.user_id,
        payload={
            "gross": str(gross),
            "net": str(gross - fees),
            "currency": currency,
            "purchases": counted,
            "settled": False,
        },
    )
    return payout
