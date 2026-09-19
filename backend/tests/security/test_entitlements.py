"""An entitlement must resolve only to the versions it actually grants."""

from __future__ import annotations

import pytest

from iluvtrade.db.models.reddesk import BillingCadence, VersionAccessPolicy
from iluvtrade.reddesk import entitlements, marketplace
from iluvtrade.strategies import service as strategies
from tests.conftest import make_principal

pytestmark = pytest.mark.security


def _listing(db, creator, *, policy=VersionAccessPolicy.PINNED):
    strategy = strategies.create_strategy(db, creator, name="For sale")
    first = strategies.publish(
        db,
        creator,
        strategies.create_version(
            db,
            creator,
            strategy_id=strategy.id,
            implementation_key="buy_and_hold",
            parameters={"quantity": 5},
        ).id,
    )
    listing = marketplace.create_listing(
        db,
        creator,
        strategy_id=strategy.id,
        title="For sale",
        description="A strategy offered for sale, described at sufficient length.",
        methodology="Buys once and holds.",
        risk_disclosure="Backtests are not forecasts.",
        price_amount="100",
        billing_cadence=BillingCadence.ONE_TIME,
        version_access_policy=policy,
        licence_terms="A licence to run, not ownership of the source.",
    )
    marketplace.offer_version(db, creator, listing_id=listing.id, strategy_version_id=first.id)
    marketplace.submit_listing(db, creator, listing.id)
    marketplace.review_listing(db, creator, listing_id=listing.id, approve=True)
    marketplace.publish_listing(db, creator, listing.id)
    return strategy, first, listing


def test_without_an_entitlement_resolution_refuses(db) -> None:
    creator = make_principal(db, "creator@example.com")
    buyer = make_principal(db, "buyer@example.com")
    _, version, _ = _listing(db, creator)

    with pytest.raises(entitlements.EntitlementError):
        entitlements.resolve_version(db, buyer.organization_id, strategy_version_id=version.id)


def test_a_purchase_grants_exactly_the_current_version(db) -> None:
    creator = make_principal(db, "creator@example.com")
    buyer = make_principal(db, "buyer@example.com")
    _, version, listing = _listing(db, creator)

    marketplace.purchase(db, buyer, listing_id=listing.id)
    grant = entitlements.resolve_version(db, buyer.organization_id, strategy_version_id=version.id)
    assert grant.strategy_version_id == version.id
    assert grant.basis == "entitlement"


def test_a_pinned_entitlement_does_not_follow_a_new_publication(db) -> None:
    """The invariant that makes a purchase mean something."""

    creator = make_principal(db, "creator@example.com")
    buyer = make_principal(db, "buyer@example.com")
    strategy, first, listing = _listing(db, creator)
    marketplace.purchase(db, buyer, listing_id=listing.id)

    second = strategies.publish(
        db,
        creator,
        strategies.create_version(
            db,
            creator,
            strategy_id=strategy.id,
            implementation_key="buy_and_hold",
            parameters={"quantity": 500},
        ).id,
    )
    marketplace.offer_version(db, creator, listing_id=listing.id, strategy_version_id=second.id)

    # The buyer still resolves to v1.
    grant = entitlements.resolve_version(db, buyer.organization_id, strategy_id=strategy.id)
    assert grant.strategy_version_id == first.id

    # And cannot reach v2 by naming it.
    with pytest.raises(entitlements.EntitlementError):
        entitlements.resolve_version(db, buyer.organization_id, strategy_version_id=second.id)


def test_a_pinned_entitlement_refuses_to_be_advanced(db) -> None:
    creator = make_principal(db, "creator@example.com")
    buyer = make_principal(db, "buyer@example.com")
    _, _, listing = _listing(db, creator)
    _, entitlement = marketplace.purchase(db, buyer, listing_id=listing.id)

    with pytest.raises(entitlements.EntitlementError, match="pinned"):
        entitlements.advance_rolling(db, entitlement.id)


def test_a_rolling_entitlement_advances_only_when_asked(db) -> None:
    creator = make_principal(db, "creator@example.com")
    buyer = make_principal(db, "buyer@example.com")
    strategy, first, listing = _listing(db, creator, policy=VersionAccessPolicy.ROLLING)
    _, entitlement = marketplace.purchase(db, buyer, listing_id=listing.id)

    second = strategies.publish(
        db,
        creator,
        strategies.create_version(
            db,
            creator,
            strategy_id=strategy.id,
            implementation_key="buy_and_hold",
            parameters={"quantity": 7},
        ).id,
    )
    marketplace.offer_version(db, creator, listing_id=listing.id, strategy_version_id=second.id)

    # Reading does not advance it.
    assert (
        entitlements.resolve_version(
            db, buyer.organization_id, strategy_id=strategy.id
        ).strategy_version_id
        == first.id
    )

    entitlements.advance_rolling(db, entitlement.id)
    assert (
        entitlements.resolve_version(
            db, buyer.organization_id, strategy_id=strategy.id
        ).strategy_version_id
        == second.id
    )


def test_a_revoked_entitlement_stops_resolving(db) -> None:
    creator = make_principal(db, "creator@example.com")
    buyer = make_principal(db, "buyer@example.com")
    _, version, listing = _listing(db, creator)
    _, entitlement = marketplace.purchase(db, buyer, listing_id=listing.id)

    entitlements.revoke(db, entitlement.id, reason="chargeback")
    with pytest.raises(entitlements.EntitlementError):
        entitlements.resolve_version(db, buyer.organization_id, strategy_version_id=version.id)


def test_an_owner_needs_no_entitlement(db) -> None:
    creator = make_principal(db, "creator@example.com")
    _, version, _ = _listing(db, creator)
    grant = entitlements.resolve_version(
        db, creator.organization_id, strategy_version_id=version.id
    )
    assert grant.basis == "owner"
