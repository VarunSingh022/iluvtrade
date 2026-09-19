"""The RedDesk marketplace API."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session as DbSession

from iluvtrade.api.deps import (
    current_principal,
    db_session,
    rate_limit,
    require_admin,
    require_trader,
)
from iluvtrade.api.v1.schemas import (
    CreateListingRequest,
    EntitlementResponse,
    OfferVersionRequest,
    PurchaseRequest,
    RateListingRequest,
    ReviewListingRequest,
)
from iluvtrade.billing import get_provider
from iluvtrade.config import get_settings
from iluvtrade.db.models.reddesk import (
    BillingCadence,
    Entitlement,
    Listing,
    VersionAccessPolicy,
)
from iluvtrade.platform.accounts import Principal
from iluvtrade.platform.tenancy import NotFoundError, require_owned, scoped
from iluvtrade.reddesk import entitlements, marketplace

router = APIRouter(prefix="/reddesk", tags=["reddesk"])


def _entitlement(row: Entitlement) -> EntitlementResponse:
    return EntitlementResponse(
        id=row.id,
        listing_id=row.listing_id,
        strategy_id=row.strategy_id,
        granted_strategy_version_id=row.granted_strategy_version_id,
        version_access_policy=row.version_access_policy.value,
        status=row.status.value,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
    )


@router.get("/billing-status")
def billing_status(_principal: Principal = Depends(current_principal)) -> dict:
    """Whether acquiring a listing actually moves money.

    Surfaced so the UI can say which of two very different things a purchase is,
    rather than showing one "Acquire" button for both. A buyer who believes they
    paid when nothing was charged — or the reverse — is the failure this exists
    to prevent.
    """

    provider = get_provider(_configured_provider())
    return {
        "provider": provider.name,
        "moves_money": provider.moves_money,
        "state": (
            "REAL_PAYMENT_PROCESSING_ENABLED"
            if provider.moves_money
            else "READY_FOR_PROVIDER_INTEGRATION"
        ),
        "notice": (
            "Purchases are settled by a real payment provider."
            if provider.moves_money
            else (
                "No payment provider is configured. A purchase records the licence and "
                "grants the entitlement, but no money is charged and no payout can be "
                "settled."
            )
        ),
        "payouts_settleable": provider.moves_money,
    }


@router.get("/discover")
def discover(
    q: str | None = None,
    limit: int = 50,
    session: DbSession = Depends(db_session),
    _principal: Principal = Depends(current_principal),
) -> list[dict]:
    """Published listings from every creator. Deliberately cross-tenant."""

    return marketplace.discover(session, query=q, limit=limit)


@router.get("/listings/{listing_id}")
def get_listing(
    listing_id: str,
    session: DbSession = Depends(db_session),
    _principal: Principal = Depends(current_principal),
) -> dict:
    listing = session.get(Listing, listing_id)
    if listing is None:
        raise NotFoundError("That listing does not exist.")
    return marketplace.summarise(session, listing)


@router.get("/my-listings")
def my_listings(
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> list[dict]:
    """Every listing this organization created, at any status."""

    rows = session.execute(
        scoped(Listing, principal.organization_id).order_by(Listing.created_at.desc())
    ).scalars()
    return [marketplace.summarise(session, row) for row in rows]


@router.post("/listings", status_code=201, dependencies=[Depends(rate_limit("marketplace"))])
def create_listing(
    payload: CreateListingRequest,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> dict:
    listing = marketplace.create_listing(
        session,
        principal,
        strategy_id=payload.strategy_id,
        title=payload.title,
        summary=payload.summary,
        description=payload.description,
        methodology=payload.methodology,
        risk_disclosure=payload.risk_disclosure,
        price_amount=payload.price_amount,
        price_currency=payload.price_currency,
        billing_cadence=BillingCadence(payload.billing_cadence),
        version_access_policy=VersionAccessPolicy(payload.version_access_policy),
        licence_terms=payload.licence_terms,
        supported_brokers=payload.supported_brokers,
        supported_instruments=payload.supported_instruments,
        supported_data=payload.supported_data,
    )
    return marketplace.summarise(session, listing)


@router.post("/listings/{listing_id}/versions", status_code=201)
def offer_version(
    listing_id: str,
    payload: OfferVersionRequest,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> dict:
    """Attach a published strategy version, optionally with backtest evidence."""

    marketplace.offer_version(
        session,
        principal,
        listing_id=listing_id,
        strategy_version_id=payload.strategy_version_id,
        evidence_backtest_run_id=payload.evidence_backtest_run_id,
        release_notes=payload.release_notes,
        make_current=payload.make_current,
    )
    return marketplace.summarise(
        session, require_owned(session, Listing, listing_id, principal.organization_id)
    )


@router.get("/listings/{listing_id}/validate")
def validate(
    listing_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> dict:
    """Everything blocking publication. Empty ``issues`` means publishable."""

    listing = require_owned(session, Listing, listing_id, principal.organization_id)
    issues = marketplace.validate_listing(session, listing)
    return {"publishable": not issues, "issues": [issue.to_dict() for issue in issues]}


@router.post("/listings/{listing_id}/submit")
def submit(
    listing_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> dict:
    return marketplace.summarise(
        session, marketplace.submit_listing(session, principal, listing_id)
    )


@router.post("/listings/{listing_id}/review")
def review(
    listing_id: str,
    payload: ReviewListingRequest,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_admin),
) -> dict:
    return marketplace.summarise(
        session,
        marketplace.review_listing(
            session, principal, listing_id=listing_id, approve=payload.approve, notes=payload.notes
        ),
    )


@router.post("/listings/{listing_id}/publish")
def publish(
    listing_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_admin),
) -> dict:
    return marketplace.summarise(
        session, marketplace.publish_listing(session, principal, listing_id)
    )


@router.post("/listings/{listing_id}/withdraw")
def withdraw(
    listing_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> dict:
    return marketplace.summarise(
        session, marketplace.withdraw_listing(session, principal, listing_id)
    )


@router.post("/purchases", status_code=201, dependencies=[Depends(rate_limit("marketplace"))])
def purchase(
    payload: PurchaseRequest,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> dict:
    """Acquire a listing and receive an entitlement to a concrete version."""

    purchase_row, entitlement = marketplace.purchase(
        session,
        principal,
        listing_id=payload.listing_id,
        idempotency_key=payload.idempotency_key,
        provider=_configured_provider(),
    )
    return {
        "billing": {
            "provider": purchase_row.provider,
            "moves_money": get_provider(purchase_row.provider).moves_money,
        },
        "purchase": {
            "id": purchase_row.id,
            "status": purchase_row.status.value,
            "amount": purchase_row.amount,
            "currency": purchase_row.currency,
            "platform_fee": purchase_row.platform_fee,
            "creator_net": purchase_row.creator_net,
            "provider": purchase_row.provider,
            "licence_hash": purchase_row.licence_hash,
        },
        "entitlement": _entitlement(entitlement).model_dump(mode="json"),
    }


@router.get("/entitlements", response_model=list[EntitlementResponse])
def list_entitlements(
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> list[EntitlementResponse]:
    """What this organization may currently run."""

    return [
        _entitlement(row)
        for row in entitlements.active_entitlements(session, principal.organization_id)
    ]


@router.post("/entitlements/{entitlement_id}/advance", response_model=EntitlementResponse)
def advance(
    entitlement_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> EntitlementResponse:
    """Move a rolling entitlement to the listing's current version.

    Explicit and audited. A pinned licence is refused — a buyer's version never
    changes under them.
    """

    require_owned(session, Entitlement, entitlement_id, principal.organization_id)
    return _entitlement(
        entitlements.advance_rolling(session, entitlement_id, actor_user_id=principal.user_id)
    )


@router.post("/listings/{listing_id}/rate", status_code=201)
def rate(
    listing_id: str,
    payload: RateListingRequest,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> dict:
    row = marketplace.review(
        session, principal, listing_id=listing_id, rating=payload.rating, body=payload.body
    )
    return {"id": row.id, "rating": row.rating, "body": row.body}


def _configured_provider() -> str:
    """The provider this deployment is configured to use.

    A single place to read it, so the status endpoint and the purchase route
    cannot disagree about which provider is in play.
    """

    return get_settings().payment_provider
