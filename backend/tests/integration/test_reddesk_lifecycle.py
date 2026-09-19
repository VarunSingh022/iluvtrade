"""The complete RedDesk lifecycle, over HTTP, as two separate workspaces.

Every other marketplace test drives the service layer. This one drives the API
the frontend actually calls, from "a creator has a strategy" to "a buyer rates
what they bought", because a lifecycle that works service-side and has no
reachable route is not a product.

It exists for a specific reason. An audit of the API surface found two
endpoints — advancing a rolling entitlement, and rating a listing — that no
client, demo or test called, while the *rating* those endpoints produce was
already being rendered on the discovery screen. A displayed field nobody can
produce is a broken slice, so both are exercised here and both are now wired
into the UI.
"""

from __future__ import annotations

import pytest

from tests.conftest import register

pytestmark = pytest.mark.integration

HEADERS = {"X-Requested-With": "XMLHttpRequest"}


def _publish_listing(client, *, title: str, policy: str = "pinned") -> tuple[str, str]:
    """Take a listing from nothing to published. Returns (listing_id, version_id)."""

    strategy = client.post(
        "/api/v1/strategies", json={"name": title, "visibility": "marketplace"}, headers=HEADERS
    )
    assert strategy.status_code == 201, strategy.text
    strategy_id = strategy.json()["id"]

    version = client.post(
        f"/api/v1/strategies/{strategy_id}/versions",
        json={"implementation_key": "buy_and_hold", "parameters": {"quantity": 5}},
        headers=HEADERS,
    )
    assert version.status_code == 201, version.text
    version_id = version.json()["id"]

    published = client.post(f"/api/v1/strategies/versions/{version_id}/publish", headers=HEADERS)
    assert published.status_code == 200, published.text

    listing = client.post(
        "/api/v1/reddesk/listings",
        json={
            "strategy_id": strategy_id,
            "title": title,
            "summary": "A strategy offered for sale.",
            "description": "A strategy offered for sale, described at sufficient length.",
            "methodology": "Buys once and holds.",
            "risk_disclosure": "Backtests are not forecasts.",
            "price_amount": "0",
            "billing_cadence": "one_time",
            "version_access_policy": policy,
            "licence_terms": "A licence to run, not ownership of the source.",
        },
        headers=HEADERS,
    )
    assert listing.status_code == 201, listing.text
    listing_id = listing.json()["id"]

    assert (
        client.post(
            f"/api/v1/reddesk/listings/{listing_id}/versions",
            json={"strategy_version_id": version_id, "release_notes": "First release."},
            headers=HEADERS,
        ).status_code
        == 201
    )
    assert (
        client.post(f"/api/v1/reddesk/listings/{listing_id}/submit", headers=HEADERS).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/reddesk/listings/{listing_id}/review",
            json={"approve": True, "notes": "Looks complete."},
            headers=HEADERS,
        ).status_code
        == 200
    )
    assert (
        client.post(f"/api/v1/reddesk/listings/{listing_id}/publish", headers=HEADERS).status_code
        == 200
    )
    return listing_id, version_id


def test_creator_to_buyer_to_rating_over_http(app) -> None:
    from fastapi.testclient import TestClient

    with TestClient(app) as creator, TestClient(app) as buyer:
        register(creator, "creator-http@example.com")
        register(buyer, "buyer-http@example.com")

        listing_id, version_id = _publish_listing(creator, title="Momentum One")

        # The buyer discovers it, and sees no rating yet.
        discovered = buyer.get("/api/v1/reddesk/discover").json()
        assert [row["id"] for row in discovered] == [listing_id]
        assert discovered[0]["rating"] == {"average": None, "count": 0}

        # Detail and validation are reachable, not only the list.
        assert buyer.get(f"/api/v1/reddesk/listings/{listing_id}").status_code == 200
        assert creator.get(f"/api/v1/reddesk/listings/{listing_id}/validate").status_code == 200

        # Before buying, rating is refused: only a holder may rate.
        refused = buyer.post(
            f"/api/v1/reddesk/listings/{listing_id}/rate", json={"rating": 5}, headers=HEADERS
        )
        assert refused.status_code == 400
        assert "acquired" in refused.json()["error"]["message"]

        purchase = buyer.post(
            "/api/v1/reddesk/purchases",
            json={"listing_id": listing_id, "idempotency_key": "k1"},
            headers=HEADERS,
        )
        assert purchase.status_code == 201, purchase.text
        entitlement = purchase.json()["entitlement"]
        assert entitlement["granted_strategy_version_id"] == version_id

        rated = buyer.post(
            f"/api/v1/reddesk/listings/{listing_id}/rate",
            json={"rating": 4, "body": "Does what it says."},
            headers=HEADERS,
        )
        assert rated.status_code == 201, rated.text
        assert rated.json()["rating"] == 4

        # And the rating reaches the screen that displays it.
        assert buyer.get("/api/v1/reddesk/discover").json()[0]["rating"] == {
            "average": 4.0,
            "count": 1,
        }

        # Rating again amends rather than accumulating.
        buyer.post(
            f"/api/v1/reddesk/listings/{listing_id}/rate", json={"rating": 2}, headers=HEADERS
        )
        assert buyer.get("/api/v1/reddesk/discover").json()[0]["rating"]["count"] == 1

        actions = [event["action"] for event in buyer.get("/api/v1/audit?limit=100").json()]
        assert "reddesk.listing.rated" in actions


def test_a_rolling_entitlement_advances_only_when_the_holder_asks(app) -> None:
    from fastapi.testclient import TestClient

    with TestClient(app) as creator, TestClient(app) as buyer:
        register(creator, "creator-roll@example.com")
        register(buyer, "buyer-roll@example.com")

        listing_id, first_version = _publish_listing(creator, title="Rolling", policy="rolling")
        strategy_id = creator.get("/api/v1/strategies").json()[0]["id"]

        purchase = buyer.post(
            "/api/v1/reddesk/purchases",
            json={"listing_id": listing_id, "idempotency_key": "k2"},
            headers=HEADERS,
        )
        entitlement_id = purchase.json()["entitlement"]["id"]

        # The creator ships a second version.
        second = creator.post(
            f"/api/v1/strategies/{strategy_id}/versions",
            json={"implementation_key": "buy_and_hold", "parameters": {"quantity": 9}},
            headers=HEADERS,
        ).json()["id"]
        creator.post(f"/api/v1/strategies/versions/{second}/publish", headers=HEADERS)
        creator.post(
            f"/api/v1/reddesk/listings/{listing_id}/versions",
            json={"strategy_version_id": second, "release_notes": "Larger size."},
            headers=HEADERS,
        )

        # The buyer's licence has not moved on its own.
        held = buyer.get("/api/v1/reddesk/entitlements").json()[0]
        assert held["granted_strategy_version_id"] == first_version

        advanced = buyer.post(
            f"/api/v1/reddesk/entitlements/{entitlement_id}/advance", headers=HEADERS
        )
        assert advanced.status_code == 200, advanced.text
        assert advanced.json()["granted_strategy_version_id"] == second


def test_a_pinned_entitlement_refuses_to_advance_over_http(app) -> None:
    """A pinned buyer's version never changes under them, including by request."""

    from fastapi.testclient import TestClient

    with TestClient(app) as creator, TestClient(app) as buyer:
        register(creator, "creator-pin@example.com")
        register(buyer, "buyer-pin@example.com")

        listing_id, _ = _publish_listing(creator, title="Pinned", policy="pinned")
        entitlement_id = buyer.post(
            "/api/v1/reddesk/purchases",
            json={"listing_id": listing_id, "idempotency_key": "k3"},
            headers=HEADERS,
        ).json()["entitlement"]["id"]

        refused = buyer.post(
            f"/api/v1/reddesk/entitlements/{entitlement_id}/advance", headers=HEADERS
        )
        assert refused.status_code in (400, 403)


def test_another_workspace_cannot_advance_or_rate_what_it_does_not_hold(app) -> None:
    from fastapi.testclient import TestClient

    with TestClient(app) as creator, TestClient(app) as buyer, TestClient(app) as stranger:
        register(creator, "creator-x@example.com")
        register(buyer, "buyer-x@example.com")
        register(stranger, "stranger-x@example.com")

        listing_id, _ = _publish_listing(creator, title="Guarded", policy="rolling")
        entitlement_id = buyer.post(
            "/api/v1/reddesk/purchases",
            json={"listing_id": listing_id, "idempotency_key": "k4"},
            headers=HEADERS,
        ).json()["entitlement"]["id"]

        # Someone else's entitlement id is indistinguishable from absent.
        assert (
            stranger.post(
                f"/api/v1/reddesk/entitlements/{entitlement_id}/advance", headers=HEADERS
            ).status_code
            == 404
        )
        assert (
            stranger.post(
                f"/api/v1/reddesk/listings/{listing_id}/rate", json={"rating": 5}, headers=HEADERS
            ).status_code
            == 400
        )


def test_withdrawing_a_listing_leaves_an_existing_entitlement_working(app) -> None:
    """Removing a listing from sale is not revoking what people already bought."""

    from fastapi.testclient import TestClient

    with TestClient(app) as creator, TestClient(app) as buyer:
        register(creator, "creator-w@example.com")
        register(buyer, "buyer-w@example.com")

        listing_id, version_id = _publish_listing(creator, title="Withdrawn")
        buyer.post(
            "/api/v1/reddesk/purchases",
            json={"listing_id": listing_id, "idempotency_key": "k5"},
            headers=HEADERS,
        )

        assert (
            creator.post(
                f"/api/v1/reddesk/listings/{listing_id}/withdraw", headers=HEADERS
            ).status_code
            == 200
        )
        assert buyer.get("/api/v1/reddesk/discover").json() == []

        held = buyer.get("/api/v1/reddesk/entitlements").json()
        assert len(held) == 1
        assert held[0]["status"] == "active"
        assert held[0]["granted_strategy_version_id"] == version_id
