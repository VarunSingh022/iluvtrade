"""PostgreSQL guarantees for concurrent MFA factor consumption."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pyotp
import pytest
from sqlalchemy import select

from iluvtrade.db.models.platform import Session as LoginSession
from iluvtrade.db.models.platform import User
from iluvtrade.db.session import get_session_factory
from iluvtrade.platform import accounts, mfa

pytestmark = pytest.mark.postgres

PASSWORD = "correct-horse-battery-staple"


def _enabled_user(db, email: str) -> tuple[str, str, str]:
    accounts.register(
        db,
        email=email,
        password=PASSWORD,
        display_name="MFA concurrency test",
    )
    user = db.execute(select(User).where(User.email == email)).scalar_one()
    enrolment = mfa.begin_enrolment(user)
    totp = pyotp.TOTP(enrolment.secret)
    confirmed_code = totp.now()
    challenge_counter = int(time.time()) // totp.interval
    mfa.confirm_enrolment(user, confirmed_code)

    # Model a fresh accepted time step after enrolment confirmation. The
    # challenge code remains valid across a clock-step boundary as the previous
    # step, and both login transactions start with the same persisted value.
    user.mfa_last_counter = challenge_counter - 1
    db.commit()
    return user.id, enrolment.secret, enrolment.recovery_codes[0]


def _compete(
    monkeypatch: pytest.MonkeyPatch,
    user_id: str,
    email: str,
    code: str,
) -> list[str | None]:
    factory = get_session_factory()
    barrier = threading.Barrier(2)
    original_verify = mfa.verify_challenge

    def synchronized_verify(user: User, supplied: str) -> str:
        barrier.wait(timeout=20)
        return original_verify(user, supplied)

    monkeypatch.setattr(mfa, "verify_challenge", synchronized_verify)

    def attempt() -> str | None:
        with factory() as session:
            try:
                _, token = accounts.login(
                    session,
                    email=email,
                    password=PASSWORD,
                    mfa_code=code,
                )
                session.commit()
                return token
            except accounts.AuthError:
                session.rollback()
                return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(attempt) for _ in range(2)]
        outcomes = [future.result() for future in futures]

    monkeypatch.setattr(mfa, "verify_challenge", original_verify)
    assert sum(outcome is not None for outcome in outcomes) == 1, outcomes

    with factory() as session:
        assert (
            len(
                session.execute(select(LoginSession).where(LoginSession.user_id == user_id))
                .scalars()
                .all()
            )
            == 1
        )
    return outcomes


def test_concurrent_login_consumes_a_totp_counter_once(db, monkeypatch) -> None:
    user_id, secret, _ = _enabled_user(db, "mfa-totp-race@example.com")
    totp = pyotp.TOTP(secret)
    code = totp.now()
    expected_counter = mfa._counter(totp, code)
    assert expected_counter is not None

    outcomes = _compete(
        monkeypatch,
        user_id,
        "mfa-totp-race@example.com",
        code,
    )
    assert sum(outcome is not None for outcome in outcomes) == 1

    factory = get_session_factory()
    with factory() as session:
        user = session.get(User, user_id)
        assert user is not None and user.mfa_last_counter == expected_counter
        with pytest.raises(accounts.AuthError, match="verification code"):
            accounts.login(
                session,
                email="mfa-totp-race@example.com",
                password=PASSWORD,
                mfa_code=code,
            )
        session.rollback()

    with factory() as session:
        assert (
            len(
                session.execute(select(LoginSession).where(LoginSession.user_id == user_id))
                .scalars()
                .all()
            )
            == 1
        )


def test_concurrent_login_consumes_a_recovery_code_once(db, monkeypatch) -> None:
    user_id, _, recovery_code = _enabled_user(db, "mfa-recovery-race@example.com")
    outcomes = _compete(
        monkeypatch,
        user_id,
        "mfa-recovery-race@example.com",
        recovery_code,
    )
    assert sum(outcome is not None for outcome in outcomes) == 1

    factory = get_session_factory()
    with factory() as session:
        user = session.get(User, user_id)
        assert user is not None
        assert len(json.loads(user.mfa_recovery_hashes)) == mfa.RECOVERY_CODE_COUNT - 1
        with pytest.raises(accounts.AuthError, match="verification code"):
            accounts.login(
                session,
                email="mfa-recovery-race@example.com",
                password=PASSWORD,
                mfa_code=recovery_code,
            )
        session.rollback()

    with factory() as session:
        assert (
            len(
                session.execute(select(LoginSession).where(LoginSession.user_id == user_id))
                .scalars()
                .all()
            )
            == 1
        )
