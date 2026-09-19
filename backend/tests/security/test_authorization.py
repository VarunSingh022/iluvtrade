"""Authentication edges and role-based refusals.

Registration always creates an owner, so the role checks would otherwise never
be exercised. These tests construct lower-privileged memberships directly —
which is what an invite flow will do when it exists — and assert the refusals.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from iluvtrade.db.models.platform import Membership, Role, Session, User
from iluvtrade.platform import accounts
from iluvtrade.platform.accounts import AuthError, Principal
from tests.conftest import make_principal

pytestmark = pytest.mark.security


# --- role ordering ----------------------------------------------------------


def test_roles_are_totally_ordered() -> None:
    assert Role.OWNER.satisfies(Role.ADMIN)
    assert Role.OWNER.satisfies(Role.TRADER)
    assert Role.ADMIN.satisfies(Role.TRADER)
    assert Role.TRADER.satisfies(Role.VIEWER)
    assert not Role.VIEWER.satisfies(Role.TRADER)
    assert not Role.TRADER.satisfies(Role.ADMIN)


def _as(principal: Principal, role: Role) -> Principal:
    from dataclasses import replace

    return replace(principal, role=role)


def test_a_viewer_cannot_create_a_strategy(db) -> None:
    from iluvtrade.strategies import service

    principal = make_principal(db)
    with pytest.raises(PermissionError, match="trader"):
        service.create_strategy(db, _as(principal, Role.VIEWER), name="Nope")


def test_a_viewer_cannot_submit_a_backtest(db) -> None:
    from iluvtrade.backtests import service
    from iluvtrade.backtests.requests import BacktestRequest

    principal = make_principal(db)
    request = BacktestRequest(dataset_version_id="x", strategy_version_id="y")
    with pytest.raises(PermissionError, match="trader"):
        service.submit(db, _as(principal, Role.VIEWER), request)


def test_a_viewer_cannot_create_a_trading_session(db) -> None:
    from iluvtrade.db.models.trading import TradingMode
    from iluvtrade.trading import sessions

    principal = make_principal(db)
    spec = sessions.SessionSpec(name="n", mode=TradingMode.PAPER, strategy_version_id="x")
    with pytest.raises(PermissionError, match="trader"):
        sessions.create(db, _as(principal, Role.VIEWER), spec)


def test_a_viewer_cannot_connect_a_broker(db) -> None:
    from iluvtrade.brokers import service
    from iluvtrade.db.models.broker import BrokerKind

    principal = make_principal(db)
    with pytest.raises(PermissionError, match="trader"):
        service.create_account(db, _as(principal, Role.VIEWER), broker=BrokerKind.PAPER, label="x")


def test_a_trader_cannot_publish_a_marketplace_listing(db) -> None:
    """Publishing is an ADMIN act; creating and submitting are a trader's."""

    from iluvtrade.reddesk import marketplace
    from iluvtrade.strategies import service as strategies

    principal = make_principal(db)
    strategy = strategies.create_strategy(db, principal, name="S")
    listing = marketplace.create_listing(
        db, principal, strategy_id=strategy.id, title="L", price_amount="0"
    )
    with pytest.raises(PermissionError, match="admin"):
        marketplace.publish_listing(db, _as(principal, Role.TRADER), listing.id)
    with pytest.raises(PermissionError, match="admin"):
        marketplace.review_listing(
            db, _as(principal, Role.TRADER), listing_id=listing.id, approve=True
        )


def test_a_trader_cannot_certify_a_strategy_version(db) -> None:
    from iluvtrade.db.models.strategy import CertificationStatus
    from iluvtrade.strategies import service

    principal = make_principal(db)
    strategy = service.create_strategy(db, principal, name="S")
    version = service.create_version(
        db, principal, strategy_id=strategy.id, implementation_key="buy_and_hold"
    )
    with pytest.raises(PermissionError, match="admin"):
        service.certify(
            db,
            _as(principal, Role.TRADER),
            version_id=version.id,
            status=CertificationStatus.CERTIFIED,
        )


def test_a_trader_cannot_read_the_audit_trail(client, headers) -> None:
    """The trail names every actor, so it is an admin surface."""

    from tests.conftest import register

    user = register(client, "t@example.com")
    with client:
        membership = None
        from iluvtrade.db.session import session_scope

        with session_scope() as session:
            membership = session.execute(
                select(Membership).where(Membership.user_id == user["id"])
            ).scalar_one()
            membership.role = Role.TRADER

        response = client.get("/api/v1/audit")

    assert response.status_code == 403
    assert "admin" in response.json()["error"]["message"]


# --- tenant identity cannot be supplied -------------------------------------


def test_logging_in_to_an_organization_you_do_not_belong_to_is_refused(db) -> None:
    """The one client-supplied tenant field, and it is checked."""

    alice = make_principal(db, "alice@example.com")
    make_principal(db, "bob@example.com")

    with pytest.raises(AuthError, match="Invalid email or password"):
        accounts.login(
            db,
            email="bob@example.com",
            password="correct-horse-battery-staple",
            organization_id=alice.organization_id,
        )


def test_the_refusal_does_not_reveal_that_the_organization_exists(db) -> None:
    """A wrong organization and a wrong password must be indistinguishable."""

    alice = make_principal(db, "alice@example.com")
    make_principal(db, "bob@example.com")

    wrong_org: str | None = None
    wrong_password = None
    try:
        accounts.login(
            db,
            email="bob@example.com",
            password="correct-horse-battery-staple",
            organization_id=alice.organization_id,
        )
    except AuthError as exc:
        wrong_org = str(exc)
    try:
        accounts.login(db, email="bob@example.com", password="definitely-wrong-here")
    except AuthError as exc:
        wrong_password = str(exc)

    assert wrong_org == wrong_password


def test_a_session_resolves_to_the_organization_it_was_opened_for(db) -> None:
    principal = make_principal(db, "a@example.com")
    row, token = accounts.login(db, email="a@example.com", password="correct-horse-battery-staple")
    resolved = accounts.resolve_principal(db, token)
    assert resolved.organization_id == row.organization_id == principal.organization_id


def test_removing_a_membership_kills_the_session(db) -> None:
    """A session must not outlive the membership it was granted under."""

    make_principal(db, "a@example.com")
    _, token = accounts.login(db, email="a@example.com", password="correct-horse-battery-staple")
    assert accounts.resolve_principal(db, token)

    membership = db.execute(select(Membership)).scalars().first()
    db.delete(membership)
    db.flush()

    with pytest.raises(AuthError):
        accounts.resolve_principal(db, token)


def test_a_suspended_user_cannot_use_an_existing_session(db) -> None:
    from iluvtrade.db.models.platform import UserStatus

    make_principal(db, "a@example.com")
    _, token = accounts.login(db, email="a@example.com", password="correct-horse-battery-staple")
    user = db.execute(select(User).where(User.email == "a@example.com")).scalar_one()
    user.status = UserStatus.SUSPENDED
    db.flush()

    with pytest.raises(AuthError):
        accounts.resolve_principal(db, token)


def test_a_revoked_session_stops_resolving(db) -> None:
    make_principal(db, "a@example.com")
    _, token = accounts.login(db, email="a@example.com", password="correct-horse-battery-staple")
    principal = accounts.resolve_principal(db, token)
    accounts.logout(db, principal)
    db.flush()

    with pytest.raises(AuthError):
        accounts.resolve_principal(db, token)


def test_an_expired_session_stops_resolving(db) -> None:
    from datetime import timedelta

    from iluvtrade.db.base import utcnow

    make_principal(db, "a@example.com")
    row, token = accounts.login(db, email="a@example.com", password="correct-horse-battery-staple")
    row.expires_at = utcnow() - timedelta(seconds=1)
    db.flush()

    with pytest.raises(AuthError, match="expired"):
        accounts.resolve_principal(db, token)


# --- token handling ---------------------------------------------------------


def test_the_plaintext_token_is_never_stored(db) -> None:
    make_principal(db, "a@example.com")
    _, token = accounts.login(db, email="a@example.com", password="correct-horse-battery-staple")
    stored = db.execute(select(Session)).scalars().all()
    assert all(row.token_hash != token for row in stored)
    assert all(len(row.token_hash) == 64 for row in stored)


def test_a_forged_token_does_not_resolve(db) -> None:
    make_principal(db, "a@example.com")
    for forged in ("", "x", "a" * 43, "../../etc/passwd"):
        with pytest.raises(AuthError):
            accounts.resolve_principal(db, forged)


def test_an_unknown_email_and_a_wrong_password_give_the_same_message(db) -> None:
    make_principal(db, "a@example.com")

    unknown = wrong = None
    try:
        accounts.login(db, email="nobody@example.com", password="long-enough-password")
    except AuthError as exc:
        unknown = str(exc)
    try:
        accounts.login(db, email="a@example.com", password="long-enough-password")
    except AuthError as exc:
        wrong = str(exc)
    assert unknown == wrong == "Invalid email or password."
