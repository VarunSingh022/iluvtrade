"""RedDesk as a product boundary: ownership, immutability, evidence, entitlement.

Complements ``test_entitlements.py`` (which covers resolution) with the
listing-side invariants — chiefly that evidence cannot be fabricated and that
marketplace metadata cannot reach into a published version.
"""

from __future__ import annotations

import pytest

from iluvtrade.db.models.reddesk import BillingCadence, VersionAccessPolicy
from iluvtrade.reddesk import marketplace
from iluvtrade.strategies import service as strategies
from tests.conftest import make_csv, make_principal, register

pytestmark = pytest.mark.security


def _strategy_with_version(db, principal, *, name: str = "S", quantity: int = 5):
    strategy = strategies.create_strategy(db, principal, name=name)
    version = strategies.publish(
        db,
        principal,
        strategies.create_version(
            db,
            principal,
            strategy_id=strategy.id,
            implementation_key="buy_and_hold",
            parameters={"quantity": quantity},
        ).id,
    )
    return strategy, version


def _listing(db, principal, strategy, *, title: str = "L"):
    return marketplace.create_listing(
        db,
        principal,
        strategy_id=strategy.id,
        title=title,
        description="A strategy described at more than forty characters in length.",
        methodology="Buys once and holds.",
        risk_disclosure="Backtests are simulations, not forecasts.",
        price_amount="100",
        billing_cadence=BillingCadence.ONE_TIME,
        version_access_policy=VersionAccessPolicy.PINNED,
        licence_terms="A licence to run, not ownership of the source.",
    )


# --- ownership --------------------------------------------------------------


def test_a_listing_belongs_to_the_creating_organization(db) -> None:
    principal = make_principal(db)
    strategy, _ = _strategy_with_version(db, principal)
    listing = _listing(db, principal, strategy)
    assert listing.organization_id == principal.organization_id


def test_a_listing_cannot_be_created_for_another_organizations_strategy(db) -> None:
    """The attack: list someone else's work as your own."""

    from iluvtrade.platform.tenancy import NotFoundError

    alice = make_principal(db, "alice@example.com")
    bob = make_principal(db, "bob@example.com")
    alice_strategy, _ = _strategy_with_version(db, alice)

    with pytest.raises(NotFoundError):
        _listing(db, bob, alice_strategy, title="Stolen")


def test_a_version_from_another_strategy_cannot_be_offered(db) -> None:
    principal = make_principal(db)
    first_strategy, _ = _strategy_with_version(db, principal, name="First")
    _, other_version = _strategy_with_version(db, principal, name="Second")
    listing = _listing(db, principal, first_strategy)

    with pytest.raises(marketplace.ListingError, match="different strategy"):
        marketplace.offer_version(
            db, principal, listing_id=listing.id, strategy_version_id=other_version.id
        )


def test_an_unpublished_version_cannot_be_offered(db) -> None:
    """A buyer must receive something that cannot change under them."""

    principal = make_principal(db)
    strategy = strategies.create_strategy(db, principal, name="Draft only")
    draft = strategies.create_version(
        db, principal, strategy_id=strategy.id, implementation_key="buy_and_hold"
    )
    listing = _listing(db, principal, strategy)

    with pytest.raises(marketplace.ListingError, match="published, frozen"):
        marketplace.offer_version(
            db, principal, listing_id=listing.id, strategy_version_id=draft.id
        )


# --- evidence ---------------------------------------------------------------


def test_evidence_must_be_a_run_against_that_exact_version(db) -> None:
    """A creator cannot cite another version's numbers.

    This is the invariant that makes marketplace evidence worth anything.
    """

    from iluvtrade.db.base import utcnow
    from iluvtrade.db.models.backtest import BacktestJob, BacktestRun, JobStatus

    principal = make_principal(db)
    strategy, version = _strategy_with_version(db, principal)
    _, other_version = _strategy_with_version(db, principal, name="Other", quantity=99)
    listing = _listing(db, principal, strategy)

    # A real run, but against the *other* version.
    job = BacktestJob(
        organization_id=principal.organization_id,
        submitted_by_user_id=principal.user_id,
        idempotency_key="k",
        status=JobStatus.COMPLETED,
        request_json="{}",
        dataset_version_id=None,
        strategy_version_id=other_version.id,
        queued_at=utcnow(),
    )
    # dataset_version_id is NOT NULL; use a real one via the ingest path instead.
    from iluvtrade.data import ingest

    outcome = ingest.ingest_upload(
        db, principal, filename="d.csv", payload=make_csv(bars=30), dataset_name="D"
    )
    job.dataset_version_id = outcome.version.id
    db.add(job)
    db.flush()

    run = BacktestRun(
        organization_id=principal.organization_id,
        job_id=job.id,
        dataset_version_id=outcome.version.id,
        strategy_version_id=other_version.id,
        seed=1,
        engine_version="3.5.0",
        starting_cash="1000",
        ending_equity="9999999",
        realized_pnl="0",
        unrealized_pnl="0",
        commission_paid="0",
    )
    db.add(run)
    db.flush()

    with pytest.raises(marketplace.ListingError, match="different strategy version"):
        marketplace.offer_version(
            db,
            principal,
            listing_id=listing.id,
            strategy_version_id=version.id,
            evidence_backtest_run_id=run.id,
        )


def test_another_organizations_run_cannot_be_cited_as_evidence(db) -> None:
    from iluvtrade.platform.tenancy import NotFoundError

    alice = make_principal(db, "alice@example.com")
    bob = make_principal(db, "bob@example.com")
    strategy, version = _strategy_with_version(db, bob)
    listing = _listing(db, bob, strategy)

    with pytest.raises(NotFoundError):
        marketplace.offer_version(
            db,
            bob,
            listing_id=listing.id,
            strategy_version_id=version.id,
            evidence_backtest_run_id="00000000-0000-0000-0000-000000000000",
        )
    _ = alice


# --- publication --------------------------------------------------------------


def test_a_listing_cannot_be_published_without_passing_validation(db) -> None:
    principal = make_principal(db)
    strategy = strategies.create_strategy(db, principal, name="Empty")
    listing = marketplace.create_listing(
        db, principal, strategy_id=strategy.id, title="Bare", price_amount="0"
    )
    with pytest.raises(marketplace.ListingError):
        marketplace.submit_listing(db, principal, listing.id)

    issues = marketplace.validate_listing(db, listing)
    codes = {issue.code for issue in issues}
    assert "description_too_short" in codes
    assert "methodology_missing" in codes
    assert "risk_disclosure_missing" in codes
    assert "no_version" in codes


def test_a_paid_listing_must_state_licence_terms(db) -> None:
    principal = make_principal(db)
    strategy, version = _strategy_with_version(db, principal)
    listing = marketplace.create_listing(
        db,
        principal,
        strategy_id=strategy.id,
        title="Paid",
        description="A strategy described at more than forty characters in length.",
        methodology="m",
        risk_disclosure="r",
        price_amount="999",
        licence_terms="",
    )
    marketplace.offer_version(db, principal, listing_id=listing.id, strategy_version_id=version.id)
    codes = {issue.code for issue in marketplace.validate_listing(db, listing)}
    assert "licence_missing" in codes


def test_a_listing_cannot_claim_an_unknown_broker(db) -> None:
    """The listing text and the software must not disagree."""

    principal = make_principal(db)
    strategy, version = _strategy_with_version(db, principal)
    listing = marketplace.create_listing(
        db,
        principal,
        strategy_id=strategy.id,
        title="Claims",
        description="A strategy described at more than forty characters in length.",
        methodology="m",
        risk_disclosure="r",
        licence_terms="l",
        supported_brokers=["interactive-brokers"],
    )
    marketplace.offer_version(db, principal, listing_id=listing.id, strategy_version_id=version.id)
    codes = {issue.code for issue in marketplace.validate_listing(db, listing)}
    assert "unsupported_broker" in codes


# --- metadata cannot reach the version ---------------------------------------


def test_marketplace_operations_do_not_mutate_a_published_version(db) -> None:
    """Listing, submitting and publishing must leave the version byte-identical."""

    principal = make_principal(db)
    strategy, version = _strategy_with_version(db, principal)
    before = (
        version.implementation_key,
        version.parameters_schema_json,
        version.default_parameters_json,
        version.content_hash,
        version.status,
    )

    listing = _listing(db, principal, strategy)
    marketplace.offer_version(db, principal, listing_id=listing.id, strategy_version_id=version.id)
    marketplace.submit_listing(db, principal, listing.id)
    marketplace.review_listing(db, principal, listing_id=listing.id, approve=True)
    marketplace.publish_listing(db, principal, listing.id)

    db.refresh(version)
    after = (
        version.implementation_key,
        version.parameters_schema_json,
        version.default_parameters_json,
        version.content_hash,
        version.status,
    )
    assert before == after
    assert strategies.verify_integrity(version)


def test_withdrawing_a_listing_leaves_entitlements_intact(db) -> None:
    """Breaking running sessions is not an acceptable consequence of delisting."""

    from iluvtrade.reddesk import entitlements

    creator = make_principal(db, "creator@example.com")
    buyer = make_principal(db, "buyer@example.com")
    strategy, version = _strategy_with_version(db, creator)
    listing = _listing(db, creator, strategy)
    marketplace.offer_version(db, creator, listing_id=listing.id, strategy_version_id=version.id)
    marketplace.submit_listing(db, creator, listing.id)
    marketplace.review_listing(db, creator, listing_id=listing.id, approve=True)
    marketplace.publish_listing(db, creator, listing.id)
    marketplace.purchase(db, buyer, listing_id=listing.id)

    marketplace.withdraw_listing(db, creator, listing.id)

    grant = entitlements.resolve_version(db, buyer.organization_id, strategy_version_id=version.id)
    assert grant.strategy_version_id == version.id


def test_a_purchase_records_the_licence_hash_at_the_time_of_sale(db) -> None:
    """ "What did I agree to" must have an answer that cannot be edited."""

    import hashlib

    creator = make_principal(db, "creator@example.com")
    buyer = make_principal(db, "buyer@example.com")
    strategy, version = _strategy_with_version(db, creator)
    listing = _listing(db, creator, strategy)
    marketplace.offer_version(db, creator, listing_id=listing.id, strategy_version_id=version.id)
    marketplace.submit_listing(db, creator, listing.id)
    marketplace.review_listing(db, creator, listing_id=listing.id, approve=True)
    marketplace.publish_listing(db, creator, listing.id)

    original_terms = listing.licence_terms
    purchase, _ = marketplace.purchase(db, buyer, listing_id=listing.id)
    assert purchase.licence_hash == hashlib.sha256(original_terms.encode()).hexdigest()

    listing.licence_terms = "Completely different terms."
    db.flush()
    assert purchase.licence_hash != hashlib.sha256(listing.licence_terms.encode()).hexdigest(), (
        "the recorded hash must not follow a later edit"
    )


def test_only_an_entitlement_holder_can_review(db) -> None:
    creator = make_principal(db, "creator@example.com")
    stranger = make_principal(db, "stranger@example.com")
    strategy, version = _strategy_with_version(db, creator)
    listing = _listing(db, creator, strategy)
    marketplace.offer_version(db, creator, listing_id=listing.id, strategy_version_id=version.id)
    marketplace.submit_listing(db, creator, listing.id)
    marketplace.review_listing(db, creator, listing_id=listing.id, approve=True)
    marketplace.publish_listing(db, creator, listing.id)

    with pytest.raises(marketplace.ListingError, match="acquired this strategy"):
        marketplace.review(db, stranger, listing_id=listing.id, rating=5, body="great")


# --- recorded identity --------------------------------------------------------


def test_a_backtest_records_the_exact_strategy_version(client, headers) -> None:
    from iluvtrade.backtests.worker import BacktestWorkerPool

    register(client, "a@example.com")
    version = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=60), "text/csv")},
        headers=headers,
    ).json()
    client.post(f"/api/v1/datasets/versions/{version['id']}/approve", headers=headers)
    strategy = client.post("/api/v1/strategies", json={"name": "S"}, headers=headers).json()
    draft = client.post(
        f"/api/v1/strategies/{strategy['id']}/versions",
        json={"implementation_key": "buy_and_hold", "parameters": {"quantity": 2}},
        headers=headers,
    ).json()
    published = client.post(
        f"/api/v1/strategies/versions/{draft['id']}/publish", headers=headers
    ).json()

    job = client.post(
        "/api/v1/backtests",
        json={"dataset_version_id": version["id"], "strategy_version_id": published["id"]},
        headers=headers,
    ).json()
    BacktestWorkerPool(size=1).drain(timeout=120)

    result = client.get(f"/api/v1/backtests/{job['id']}/result").json()
    assert result["reproducibility"]["strategy_version_id"] == published["id"]
    assert result["reproducibility"]["strategy_content_hash"] == published["content_hash"]


def test_a_session_records_the_exact_strategy_version(client, headers) -> None:
    register(client, "a@example.com")
    version = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=40), "text/csv")},
        headers=headers,
    ).json()
    client.post(f"/api/v1/datasets/versions/{version['id']}/approve", headers=headers)
    strategy = client.post("/api/v1/strategies", json={"name": "S"}, headers=headers).json()
    draft = client.post(
        f"/api/v1/strategies/{strategy['id']}/versions",
        json={"implementation_key": "buy_and_hold", "parameters": {"quantity": 2}},
        headers=headers,
    ).json()
    published = client.post(
        f"/api/v1/strategies/versions/{draft['id']}/publish", headers=headers
    ).json()

    session = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "S",
            "mode": "paper",
            "strategy_version_id": published["id"],
            "dataset_version_id": version["id"],
        },
        headers=headers,
    ).json()
    assert session["strategy_version_id"] == published["id"]
