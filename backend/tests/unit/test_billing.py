"""The payment-provider boundary."""

from __future__ import annotations

from decimal import Decimal

import pytest

from iluvtrade.billing import (
    Charge,
    ChargeResult,
    ChargeStatus,
    ManualProvider,
    PaymentError,
    PaymentProvider,
    Refund,
    get_provider,
    register_provider,
)


def _charge(key: str = "k1") -> Charge:
    return Charge(
        amount=Decimal("4999.00"),
        currency="INR",
        organization_id="org-1",
        description="Listing",
        idempotency_key=key,
    )


def test_the_manual_provider_says_it_does_not_move_money() -> None:
    """A deployment must be able to report honestly on its revenue figures."""

    provider = ManualProvider()
    assert provider.moves_money is False
    assert provider.name == "manual"


def test_the_manual_provider_settles_a_charge() -> None:
    result = ManualProvider().charge(_charge())
    assert result.status is ChargeStatus.SUCCEEDED
    assert result.is_settled
    assert "manual" in result.reference


def test_the_manual_provider_refuses_to_record_a_payout() -> None:
    """Recording a payout as complete when no money moved is a false ledger entry."""

    with pytest.raises(PaymentError, match="no payment provider"):
        ManualProvider().payout(organization_id="org-1", amount=Decimal("100"), currency="INR")


def test_an_unknown_provider_is_refused_rather_than_defaulted() -> None:
    """A typo'd provider name must not silently record purchases as settled."""

    with pytest.raises(PaymentError, match="No payment provider named"):
        get_provider("stripe")


def test_a_custom_provider_can_be_registered() -> None:
    class Fake:
        @property
        def name(self) -> str:
            return "fake"

        @property
        def moves_money(self) -> bool:
            return True

        def charge(self, charge: Charge) -> ChargeResult:
            return ChargeResult(ChargeStatus.SUCCEEDED, f"fake:{charge.idempotency_key}")

        def refund(self, refund: Refund) -> ChargeResult:
            return ChargeResult(ChargeStatus.SUCCEEDED, "fake-refund")

        def payout(self, *, organization_id: str, amount: Decimal, currency: str) -> ChargeResult:
            return ChargeResult(ChargeStatus.SUCCEEDED, "fake-payout")

    provider = Fake()
    assert isinstance(provider, PaymentProvider)
    register_provider(provider)
    assert get_provider("fake").moves_money is True


def test_registering_something_that_is_not_a_provider_is_refused() -> None:
    with pytest.raises(TypeError):
        register_provider(object())  # type: ignore[arg-type]


def test_a_refused_payment_does_not_grant_an_entitlement(db) -> None:
    """The invariant that matters: no payment, no licence."""

    from iluvtrade.db.models.reddesk import BillingCadence, VersionAccessPolicy
    from iluvtrade.reddesk import marketplace
    from iluvtrade.strategies import service as strategies
    from tests.conftest import make_principal

    class Declining:
        @property
        def name(self) -> str:
            return "declining"

        @property
        def moves_money(self) -> bool:
            return True

        def charge(self, charge: Charge) -> ChargeResult:
            return ChargeResult(ChargeStatus.FAILED, "declined", "Insufficient funds.")

        def refund(self, refund: Refund) -> ChargeResult:
            return ChargeResult(ChargeStatus.SUCCEEDED, "r")

        def payout(self, *, organization_id: str, amount: Decimal, currency: str) -> ChargeResult:
            return ChargeResult(ChargeStatus.SUCCEEDED, "p")

    register_provider(Declining())

    creator = make_principal(db, "creator@example.com")
    buyer = make_principal(db, "buyer@example.com")
    strategy = strategies.create_strategy(db, creator, name="Paid")
    version = strategies.publish(
        db,
        creator,
        strategies.create_version(
            db,
            creator,
            strategy_id=strategy.id,
            implementation_key="buy_and_hold",
            parameters={"quantity": 1},
        ).id,
    )
    listing = marketplace.create_listing(
        db,
        creator,
        strategy_id=strategy.id,
        title="Paid strategy",
        description="A strategy offered for sale, described at sufficient length here.",
        methodology="Buys once and holds.",
        risk_disclosure="Backtests are not forecasts.",
        price_amount="4999.00",
        billing_cadence=BillingCadence.ONE_TIME,
        version_access_policy=VersionAccessPolicy.PINNED,
        licence_terms="A licence to run, not ownership.",
    )
    marketplace.offer_version(db, creator, listing_id=listing.id, strategy_version_id=version.id)
    marketplace.submit_listing(db, creator, listing.id)
    marketplace.review_listing(db, creator, listing_id=listing.id, approve=True)
    marketplace.publish_listing(db, creator, listing.id)

    with pytest.raises(marketplace.ListingError, match="refused"):
        marketplace.purchase(db, buyer, listing_id=listing.id, provider="declining")

    from iluvtrade.reddesk import entitlements

    assert entitlements.active_entitlements(db, buyer.organization_id) == []


# --- refunds and payouts -----------------------------------------------------


def _published_listing(db, creator):
    """A published listing with one version, ready to buy."""

    from iluvtrade.db.models.reddesk import BillingCadence, VersionAccessPolicy
    from iluvtrade.reddesk import marketplace
    from iluvtrade.strategies import service as strategies

    strategy = strategies.create_strategy(db, creator, name="Sellable")
    version = strategies.publish(
        db,
        creator,
        strategies.create_version(
            db,
            creator,
            strategy_id=strategy.id,
            implementation_key="buy_and_hold",
            parameters={"quantity": 1},
        ).id,
    )
    listing = marketplace.create_listing(
        db,
        creator,
        strategy_id=strategy.id,
        title="Sellable",
        description="A strategy described at more than forty characters in length.",
        methodology="Buys once and holds.",
        risk_disclosure="Backtests are simulations, not forecasts.",
        price_amount="1000.00",
        billing_cadence=BillingCadence.ONE_TIME,
        version_access_policy=VersionAccessPolicy.PINNED,
        licence_terms="A licence to run, not ownership.",
    )
    marketplace.offer_version(db, creator, listing_id=listing.id, strategy_version_id=version.id)
    marketplace.submit_listing(db, creator, listing.id)
    marketplace.review_listing(db, creator, listing_id=listing.id, approve=True)
    marketplace.publish_listing(db, creator, listing.id)
    return listing, version


def test_a_refund_revokes_the_entitlement(db) -> None:
    """A refunded purchase must not leave a working licence behind."""

    from iluvtrade.reddesk import entitlements, marketplace
    from tests.conftest import make_principal

    creator = make_principal(db, "creator@example.com")
    buyer = make_principal(db, "buyer@example.com")
    listing, version = _published_listing(db, creator)
    purchase, _granted = marketplace.purchase(db, buyer, listing_id=listing.id)

    assert (
        entitlements.resolve_version(
            db, buyer.organization_id, strategy_version_id=version.id
        ).strategy_version_id
        == version.id
    )

    # The buyer's own admin refunds it; the purchase is the buyer org's row.
    refunded, revoked = marketplace.refund(
        db, buyer, purchase_id=purchase.id, reason="changed their mind"
    )

    assert refunded.status.value == "refunded"
    assert len(revoked) == 1
    with pytest.raises(entitlements.EntitlementError):
        entitlements.resolve_version(db, buyer.organization_id, strategy_version_id=version.id)


def test_a_refund_is_idempotent(db) -> None:
    from iluvtrade.reddesk import marketplace
    from tests.conftest import make_principal

    creator = make_principal(db, "creator@example.com")
    buyer = make_principal(db, "buyer@example.com")
    listing, _ = _published_listing(db, creator)
    purchase, _ = marketplace.purchase(db, buyer, listing_id=listing.id)

    first, _ = marketplace.refund(db, buyer, purchase_id=purchase.id, reason="r")
    second, _ = marketplace.refund(db, buyer, purchase_id=purchase.id, reason="r")
    assert first.id == second.id
    assert second.status.value == "refunded"


def test_a_provider_refusing_a_refund_changes_nothing(db) -> None:
    """Recording a refund that did not happen is a false ledger entry."""

    from iluvtrade.reddesk import entitlements, marketplace
    from tests.conftest import make_principal

    class Refusing:
        @property
        def name(self) -> str:
            return "refusing"

        @property
        def moves_money(self) -> bool:
            return True

        def charge(self, charge: Charge) -> ChargeResult:
            return ChargeResult(ChargeStatus.SUCCEEDED, "ok")

        def refund(self, refund: Refund) -> ChargeResult:
            return ChargeResult(ChargeStatus.FAILED, "", "Refund window has closed.")

        def payout(self, *, organization_id: str, amount: Decimal, currency: str) -> ChargeResult:
            return ChargeResult(ChargeStatus.SUCCEEDED, "p")

    register_provider(Refusing())

    creator = make_principal(db, "creator@example.com")
    buyer = make_principal(db, "buyer@example.com")
    listing, version = _published_listing(db, creator)
    purchase, _ = marketplace.purchase(db, buyer, listing_id=listing.id, provider="refusing")

    with pytest.raises(marketplace.ListingError, match="refused the refund"):
        marketplace.refund(db, buyer, purchase_id=purchase.id, reason="r")

    assert purchase.status.value == "paid", "the purchase must be untouched"
    assert entitlements.resolve_version(
        db, buyer.organization_id, strategy_version_id=version.id
    ), "the entitlement must survive a failed refund"


def test_a_payout_records_what_is_owed_without_paying_it(db) -> None:
    from datetime import timedelta

    from iluvtrade.db.base import utcnow
    from iluvtrade.reddesk import marketplace
    from tests.conftest import make_principal

    creator = make_principal(db, "creator@example.com")
    buyer = make_principal(db, "buyer@example.com")
    listing, _ = _published_listing(db, creator)
    marketplace.purchase(db, buyer, listing_id=listing.id)

    payout = marketplace.record_payout(
        db,
        creator,
        period_start=utcnow() - timedelta(days=1),
        period_end=utcnow() + timedelta(days=1),
    )
    assert payout.purchase_count == 1
    assert Decimal(payout.gross_amount) == Decimal("1000.00")
    assert Decimal(payout.net_amount) == Decimal("1000.00") - Decimal(payout.platform_fee)
    assert payout.status == "pending", "recorded, not settled"
    assert payout.settled_at is None


def test_a_refunded_purchase_does_not_count_toward_a_payout(db) -> None:
    from datetime import timedelta

    from iluvtrade.db.base import utcnow
    from iluvtrade.reddesk import marketplace
    from tests.conftest import make_principal

    creator = make_principal(db, "creator@example.com")
    buyer = make_principal(db, "buyer@example.com")
    listing, _ = _published_listing(db, creator)
    purchase, _ = marketplace.purchase(db, buyer, listing_id=listing.id)
    marketplace.refund(db, buyer, purchase_id=purchase.id, reason="r")

    payout = marketplace.record_payout(
        db,
        creator,
        period_start=utcnow() - timedelta(days=1),
        period_end=utcnow() + timedelta(days=1),
    )
    assert payout.purchase_count == 0
    assert Decimal(payout.gross_amount) == Decimal("0")
