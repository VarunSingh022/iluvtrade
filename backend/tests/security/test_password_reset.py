"""Password reset: real token infrastructure, an honest delivery boundary.

Two things are being asserted, and they are different in kind.

The **token** tests are ordinary security tests: unguessable, hashed at rest,
single-use, short-lived, and revoking every session on use. That half is fully
implemented and behaves like any other credential in this system.

The **delivery** tests assert something less usual — that the application
refuses rather than pretends. With no email channel configured, the request
endpoint answers ``503``, identically for a known and an unknown address. A
``202 Accepted`` would be the easy thing to return and would be a lie: the user
waits for a message that was never sent, and the logs record a success.

A stub provider stands in where delivery *does* exist, so the success path is
covered too and the seam is shown to be a seam rather than an aspiration.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from iluvtrade.platform import passwords
from tests.conftest import register

pytestmark = pytest.mark.security

PASSWORD = "correct-horse-battery-staple"
NEW_PASSWORD = "a-completely-different-passphrase"


class StubProvider:
    """A delivery channel that exists, for the tests that need one to."""

    def __init__(self) -> None:
        self.delivered: list[passwords.ResetIssue] = []

    @property
    def name(self) -> str:
        return "stub"

    @property
    def available(self) -> bool:
        return True

    def deliver(self, issue: passwords.ResetIssue) -> None:
        self.delivered.append(issue)


@pytest.fixture
def delivery(monkeypatch) -> StubProvider:
    provider = StubProvider()
    monkeypatch.setattr(passwords, "_PROVIDER", provider)
    return provider


# --- the honest boundary ----------------------------------------------------


def test_without_a_channel_the_endpoint_refuses_rather_than_pretending(client) -> None:
    """A 202 that never arrives is worse than a refusal."""

    register(client, "noone@example.com")
    response = client.post(
        "/api/v1/auth/password-reset/request", json={"email": "noone@example.com"}
    )
    assert response.status_code == 503
    # The type name, like every other domain error here — more specific than a
    # bare "ServiceUnavailable", so a client can tell "this deployment cannot
    # do resets" from "the database is down".
    assert response.json()["error"]["code"] == "DeliveryUnavailable"
    assert "no delivery channel" in response.json()["error"]["message"]


def test_the_refusal_does_not_depend_on_whether_the_account_exists(client) -> None:
    """Decided before the lookup, so it cannot be an enumeration oracle."""

    register(client, "known@example.com")
    known = client.post("/api/v1/auth/password-reset/request", json={"email": "known@example.com"})
    unknown = client.post(
        "/api/v1/auth/password-reset/request", json={"email": "stranger@example.com"}
    )
    assert known.status_code == unknown.status_code == 503
    assert known.json() == unknown.json()


def test_the_sign_in_screen_can_find_out_what_is_true(client) -> None:
    """So it can say so, instead of offering a link that goes nowhere."""

    body = client.get("/api/v1/auth/password-reset").json()
    assert body["available"] is False
    assert body["channel"] == "none"
    assert "no email delivery" in body["notice"]


def test_with_a_channel_the_answer_is_202_and_still_identical(client, delivery) -> None:
    register(client, "real@example.com")
    known = client.post("/api/v1/auth/password-reset/request", json={"email": "real@example.com"})
    unknown = client.post(
        "/api/v1/auth/password-reset/request", json={"email": "ghost@example.com"}
    )
    assert known.status_code == unknown.status_code == 202
    assert known.text == unknown.text
    # One real address, one delivery — and nothing for the address that has no
    # account, which is what makes the identical response honest rather than
    # merely uniform.
    assert [issue.email for issue in delivery.delivered] == ["real@example.com"]

    assert client.get("/api/v1/auth/password-reset").json()["available"] is True


# --- the token --------------------------------------------------------------


def test_a_reset_token_sets_a_new_password_and_ends_every_session(app, headers, delivery) -> None:
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        register(client, "rotate@example.com")
        assert client.get("/api/v1/auth/me").status_code == 200

        client.post("/api/v1/auth/password-reset/request", json={"email": "rotate@example.com"})
        token = delivery.delivered[-1].token

        done = client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": token, "new_password": NEW_PASSWORD},
        )
        assert done.status_code == 200, done.text
        assert done.json()["email"] == "rotate@example.com"

        # The session that existed before the reset is gone. This is the point
        # of a reset rather than a settings-page password change: the reason
        # someone resets is often that a session is in the wrong hands.
        assert client.get("/api/v1/auth/me").status_code == 401

        assert (
            client.post(
                "/api/v1/auth/login",
                json={"email": "rotate@example.com", "password": PASSWORD},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"email": "rotate@example.com", "password": NEW_PASSWORD},
            ).status_code
            == 200
        )


def test_a_token_works_exactly_once(client, delivery) -> None:
    register(client, "once-reset@example.com")
    client.post("/api/v1/auth/password-reset/request", json={"email": "once-reset@example.com"})
    token = delivery.delivered[-1].token

    assert (
        client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": token, "new_password": NEW_PASSWORD},
        ).status_code
        == 200
    )
    replay = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": token, "new_password": "yet-another-passphrase-here"},
    )
    assert replay.status_code == 400


def test_an_expired_token_is_refused(client, delivery) -> None:
    from iluvtrade.db.base import utcnow
    from iluvtrade.db.models.platform import PasswordResetToken
    from iluvtrade.db.session import session_scope

    register(client, "stale@example.com")
    client.post("/api/v1/auth/password-reset/request", json={"email": "stale@example.com"})
    token = delivery.delivered[-1].token

    with session_scope() as session:
        row = session.query(PasswordResetToken).one()
        row.expires_at = utcnow() - timedelta(seconds=1)

    refused = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": token, "new_password": NEW_PASSWORD},
    )
    assert refused.status_code == 400


def test_expired_used_and_unknown_all_read_the_same(client, delivery) -> None:
    """Each is information about a token the caller may only be guessing at."""

    register(client, "same@example.com")
    client.post("/api/v1/auth/password-reset/request", json={"email": "same@example.com"})
    token = delivery.delivered[-1].token
    client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": token, "new_password": NEW_PASSWORD},
    )

    used = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": token, "new_password": NEW_PASSWORD},
    )
    unknown = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": "z" * 44, "new_password": NEW_PASSWORD},
    )
    assert used.json() == unknown.json()


def test_requesting_again_invalidates_the_previous_token(client, delivery) -> None:
    register(client, "super-reset@example.com")
    for _ in range(2):
        client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": "super-reset@example.com"},
        )
    first, second = delivery.delivered[0].token, delivery.delivered[1].token
    assert first != second

    assert (
        client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": first, "new_password": NEW_PASSWORD},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": second, "new_password": NEW_PASSWORD},
        ).status_code
        == 200
    )


def test_a_weak_password_leaves_the_token_unspent(client, delivery) -> None:
    """Otherwise a typo locks the user out of their own reset."""

    register(client, "weak@example.com")
    client.post("/api/v1/auth/password-reset/request", json={"email": "weak@example.com"})
    token = delivery.delivered[-1].token

    rejected = client.post(
        "/api/v1/auth/password-reset/confirm", json={"token": token, "new_password": "short"}
    )
    assert rejected.status_code == 422

    assert (
        client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": token, "new_password": NEW_PASSWORD},
        ).status_code
        == 200
    )


# --- storage ----------------------------------------------------------------


def test_the_token_is_stored_only_as_a_hash(client, delivery) -> None:
    from iluvtrade.db.models.platform import PasswordResetToken
    from iluvtrade.db.session import session_scope

    register(client, "hash@example.com")
    client.post("/api/v1/auth/password-reset/request", json={"email": "hash@example.com"})
    token = delivery.delivered[-1].token

    with session_scope() as session:
        row = session.query(PasswordResetToken).one()
        assert row.token_hash != token
        assert token not in row.token_hash
        assert len(row.token_hash) == 64


def test_tokens_are_unguessable(client, delivery) -> None:
    register(client, "entropy@example.com")
    seen = set()
    for _ in range(5):
        client.post("/api/v1/auth/password-reset/request", json={"email": "entropy@example.com"})
        seen.add(delivery.delivered[-1].token)
    assert len(seen) == 5
    assert all(len(token) >= 40 for token in seen)


def test_the_token_never_appears_in_a_response_or_the_audit_trail(app, headers, delivery) -> None:
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        register(client, "leak@example.com")
        client.post("/api/v1/auth/password-reset/request", json={"email": "leak@example.com"})
        token = delivery.delivered[-1].token

        assert token not in client.get("/api/v1/audit?limit=100").text
        actions = [e["action"] for e in client.get("/api/v1/audit?limit=100").json()]
        assert "user.password_reset_requested" in actions


def test_completing_a_reset_is_audited(client, delivery) -> None:
    register(client, "audit-reset@example.com")
    client.post("/api/v1/auth/password-reset/request", json={"email": "audit-reset@example.com"})
    client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": delivery.delivered[-1].token, "new_password": NEW_PASSWORD},
    )
    client.post(
        "/api/v1/auth/login",
        json={"email": "audit-reset@example.com", "password": NEW_PASSWORD},
    )
    actions = [e["action"] for e in client.get("/api/v1/audit?limit=100").json()]
    assert "user.password_reset_completed" in actions


def test_the_ttl_is_short(client) -> None:
    """A token sitting in a mailbox archive must not be a standing key."""

    assert passwords.RESET_TTL_SECONDS <= 3600


# --- rate limiting ------------------------------------------------------------


def test_the_request_endpoint_is_rate_limited(app, monkeypatch, delivery) -> None:
    from fastapi.testclient import TestClient

    from iluvtrade.platform.ratelimit import POLICIES, RateLimiter, get_limiter

    monkeypatch.setenv("ILUVTRADE_RATE_LIMIT_ENABLED", "true")
    limiter = RateLimiter(enabled=True)
    monkeypatch.setattr("iluvtrade.platform.ratelimit._LIMITER", limiter)

    policy = POLICIES["password_reset"]
    assert policy.limit <= 5, "resetting is both an oracle and an inbox flood"

    with TestClient(app) as client:
        register(client, "flood@example.com")
        statuses = [
            client.post(
                "/api/v1/auth/password-reset/request", json={"email": "flood@example.com"}
            ).status_code
            for _ in range(policy.limit + 2)
        ]
    assert 429 in statuses
    _ = get_limiter
