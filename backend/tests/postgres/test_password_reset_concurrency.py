"""PostgreSQL guarantees for concurrent password-reset token consumption."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from iluvtrade.db.models.platform import AuditEvent, PasswordResetToken, Session, User
from iluvtrade.db.session import get_session_factory
from iluvtrade.platform import accounts, passwords, security

pytestmark = pytest.mark.postgres

PASSWORD = "correct-horse-battery-staple"
RESET_PASSWORDS = ("a-new-reset-password-one", "a-new-reset-password-two")


def _issue_token(db, email: str) -> tuple[str, str]:
    accounts.register(db, email=email, password=PASSWORD, display_name="Reset race test")
    user = db.execute(select(User).where(User.email == email)).scalar_one()
    accounts.login(db, email=email, password=PASSWORD)
    issued = passwords.issue(db, user)
    db.commit()
    return user.id, issued.token


def test_concurrent_reset_token_is_consumed_once(db, monkeypatch) -> None:
    email = "password-reset-race@example.com"
    user_id, token = _issue_token(db, email)

    barrier = threading.Barrier(2)
    original_hash_password = security.hash_password

    def synchronized_hash_password(password: str) -> str:
        # Both independent transactions have read the still-valid token and
        # user before either is allowed to attempt the conditional claim.
        barrier.wait(timeout=20)
        return original_hash_password(password)

    monkeypatch.setattr(security, "hash_password", synchronized_hash_password)
    factory = get_session_factory()

    def attempt(new_password: str) -> bool:
        with factory() as session:
            try:
                passwords.consume(session, token=token, new_password=new_password)
                session.commit()
                return True
            except passwords.ResetError:
                session.rollback()
                return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(attempt, password) for password in RESET_PASSWORDS]
        outcomes = [future.result() for future in futures]

    assert sum(outcomes) == 1

    with factory() as session:
        user = session.get(User, user_id)
        reset = session.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.token_hash == security.hash_token(token)
            )
        ).scalar_one()
        login_sessions = (
            session.execute(select(Session).where(Session.user_id == user_id)).scalars().all()
        )
        audit_events = session.execute(
            select(AuditEvent).where(
                AuditEvent.resource_id == user_id,
                AuditEvent.action == "user.password_reset_completed",
            )
        ).scalars()

        assert user is not None
        assert reset.consumed_at is not None
        assert (
            sum(security.verify_password(user.password_hash, value) for value in RESET_PASSWORDS)
            == 1
        )
        assert len(login_sessions) == 1
        assert all(row.revoked_at is not None for row in login_sessions)
        assert len(list(audit_events)) == 1

        with pytest.raises(passwords.ResetError, match="not valid"):
            passwords.consume(
                session,
                token=token,
                new_password="a-third-reset-password-three",
            )
        session.rollback()

    with factory() as session:
        user = session.get(User, user_id)
        assert user is not None
        assert (
            sum(security.verify_password(user.password_hash, value) for value in RESET_PASSWORDS)
            == 1
        )
        assert (
            len(session.execute(select(Session).where(Session.user_id == user_id)).scalars().all())
            == 1
        )
