"""Two-factor authentication.

The algorithm is pyotp's RFC 6238. What is tested here is the part that is this
application's to get right: the enrolment sequence, replay refusal, single-use
recovery codes, and that disabling requires proof rather than merely a session.
"""

from __future__ import annotations

import json

import pyotp
import pytest

from iluvtrade.platform import accounts, mfa
from tests.conftest import make_principal, register

pytestmark = pytest.mark.security

PASSWORD = "correct-horse-battery-staple"


def _user(db, email: str = "m@example.com"):
    from sqlalchemy import select

    from iluvtrade.db.models.platform import User

    make_principal(db, email)
    return db.execute(select(User).where(User.email == email)).scalar_one()


def _code(secret: str) -> str:
    return pyotp.TOTP(secret).now()


# --- enrolment ---------------------------------------------------------------


def test_a_new_account_has_no_second_factor(db) -> None:
    assert mfa.is_enabled(_user(db)) is False


def test_enrolment_issues_a_secret_and_recovery_codes(db) -> None:
    enrolment = mfa.begin_enrolment(_user(db))

    assert len(enrolment.secret) >= 16
    assert enrolment.provisioning_uri.startswith("otpauth://totp/")
    assert "iluvtrade" in enrolment.provisioning_uri
    assert len(enrolment.recovery_codes) == mfa.RECOVERY_CODE_COUNT
    assert len(set(enrolment.recovery_codes)) == mfa.RECOVERY_CODE_COUNT


def test_enrolment_does_not_enable_mfa(db) -> None:
    """Enabling on issue locks out anyone whose authenticator never got it."""

    user = _user(db)
    mfa.begin_enrolment(user)
    assert mfa.is_enabled(user) is False


def test_confirming_with_a_valid_code_enables_it(db) -> None:
    user = _user(db)
    enrolment = mfa.begin_enrolment(user)
    mfa.confirm_enrolment(user, _code(enrolment.secret))

    assert mfa.is_enabled(user) is True
    assert user.mfa_enabled_at is not None


def test_confirming_with_a_wrong_code_does_not_enable_it(db) -> None:
    user = _user(db)
    mfa.begin_enrolment(user)
    with pytest.raises(mfa.MfaError, match="not valid"):
        mfa.confirm_enrolment(user, "000000")
    assert mfa.is_enabled(user) is False


def test_enrolling_twice_is_refused(db) -> None:
    """Re-enrolling silently would invalidate a working authenticator."""

    user = _user(db)
    enrolment = mfa.begin_enrolment(user)
    mfa.confirm_enrolment(user, _code(enrolment.secret))

    with pytest.raises(mfa.MfaError, match="already enabled"):
        mfa.begin_enrolment(user)


# --- the secret at rest -------------------------------------------------------


def test_the_secret_is_encrypted_at_rest(db) -> None:
    """A database disclosure must not let an attacker mint codes forever."""

    user = _user(db)
    enrolment = mfa.begin_enrolment(user)

    assert user.mfa_secret is not None
    assert enrolment.secret.encode() not in user.mfa_secret


def test_the_secret_is_bound_to_its_user(db) -> None:
    """A secret copied onto another account must not decrypt."""

    from iluvtrade.brokers.crypto import CredentialError, decrypt_credentials

    user = _user(db, "one@example.com")
    other = _user(db, "two@example.com")
    mfa.begin_enrolment(user)

    with pytest.raises(CredentialError):
        decrypt_credentials(user.mfa_secret, connection_id=other.id)


def test_recovery_codes_are_hashed_not_stored(db) -> None:
    user = _user(db)
    enrolment = mfa.begin_enrolment(user)

    stored = user.mfa_recovery_hashes
    for code in enrolment.recovery_codes:
        assert code not in stored
    assert all(h.startswith("$argon2") for h in json.loads(stored))


# --- verification -------------------------------------------------------------


def test_a_valid_code_verifies(db) -> None:
    user = _user(db)
    enrolment = mfa.begin_enrolment(user)
    mfa.confirm_enrolment(user, _code(enrolment.secret))

    # A later step, so it is not the one already consumed at confirmation.
    totp = pyotp.TOTP(enrolment.secret)
    import time

    future = totp.at(int(time.time()) + totp.interval)
    user.mfa_last_counter = None
    assert mfa.verify_challenge(user, future) == "totp"


def test_a_code_cannot_be_replayed(db) -> None:
    """A TOTP code is valid for a window; one observed code must work once."""

    user = _user(db)
    enrolment = mfa.begin_enrolment(user)
    code = _code(enrolment.secret)
    mfa.confirm_enrolment(user, code)

    with pytest.raises(mfa.MfaError, match="already been used"):
        mfa.verify_challenge(user, code)


def test_a_wrong_code_is_refused(db) -> None:
    user = _user(db)
    enrolment = mfa.begin_enrolment(user)
    mfa.confirm_enrolment(user, _code(enrolment.secret))

    with pytest.raises(mfa.MfaError, match="not valid"):
        mfa.verify_challenge(user, "123456")


def test_verification_is_refused_when_mfa_is_not_enabled(db) -> None:
    user = _user(db)
    with pytest.raises(mfa.MfaError, match="not enabled"):
        mfa.verify_challenge(user, "123456")


# --- recovery codes -----------------------------------------------------------


def test_a_recovery_code_works_and_is_consumed(db) -> None:
    user = _user(db)
    enrolment = mfa.begin_enrolment(user)
    mfa.confirm_enrolment(user, _code(enrolment.secret))
    code = enrolment.recovery_codes[0]

    assert mfa.verify_challenge(user, code) == "recovery"
    assert mfa.remaining_recovery_codes(user) == mfa.RECOVERY_CODE_COUNT - 1

    with pytest.raises(mfa.MfaError, match="not valid"):
        mfa.verify_challenge(user, code)


def test_recovery_codes_are_case_and_space_insensitive(db) -> None:
    """They are read off a printout and typed by hand."""

    user = _user(db)
    enrolment = mfa.begin_enrolment(user)
    mfa.confirm_enrolment(user, _code(enrolment.secret))
    code = enrolment.recovery_codes[0]

    assert mfa.verify_challenge(user, f"  {code.lower()} ") == "recovery"


def test_regenerating_replaces_every_code(db) -> None:
    user = _user(db)
    enrolment = mfa.begin_enrolment(user)
    mfa.confirm_enrolment(user, _code(enrolment.secret))
    old = enrolment.recovery_codes[1]

    fresh = mfa.regenerate_recovery_codes(user, enrolment.recovery_codes[0])

    assert len(fresh) == mfa.RECOVERY_CODE_COUNT
    assert set(fresh).isdisjoint(enrolment.recovery_codes)
    with pytest.raises(mfa.MfaError):
        mfa.verify_challenge(user, old)


def test_regenerating_requires_a_current_factor(db) -> None:
    user = _user(db)
    enrolment = mfa.begin_enrolment(user)
    mfa.confirm_enrolment(user, _code(enrolment.secret))

    with pytest.raises(mfa.MfaError):
        mfa.regenerate_recovery_codes(user, "000000")


# --- disabling -----------------------------------------------------------------


def test_disabling_requires_proof_not_merely_a_session(db) -> None:
    """Disabling MFA is the first thing a stolen session would be used for."""

    user = _user(db)
    enrolment = mfa.begin_enrolment(user)
    mfa.confirm_enrolment(user, _code(enrolment.secret))

    with pytest.raises(mfa.MfaError):
        mfa.disable(user, "000000")
    assert mfa.is_enabled(user) is True

    mfa.disable(user, enrolment.recovery_codes[0])
    assert mfa.is_enabled(user) is False
    assert user.mfa_secret is None
    assert mfa.remaining_recovery_codes(user) == 0


# --- login --------------------------------------------------------------------


def test_login_without_a_code_is_refused_once_mfa_is_on(db) -> None:
    """And with a *distinct* exception, so a client shows a challenge."""

    user = _user(db)
    enrolment = mfa.begin_enrolment(user)
    mfa.confirm_enrolment(user, _code(enrolment.secret))
    db.flush()

    with pytest.raises(mfa.MfaRequired):
        accounts.login(db, email="m@example.com", password=PASSWORD)


def test_login_with_a_recovery_code_succeeds(db) -> None:
    user = _user(db)
    enrolment = mfa.begin_enrolment(user)
    mfa.confirm_enrolment(user, _code(enrolment.secret))
    db.flush()

    _, token = accounts.login(
        db, email="m@example.com", password=PASSWORD, mfa_code=enrolment.recovery_codes[0]
    )
    assert accounts.resolve_principal(db, token).email == "m@example.com"


def test_login_with_a_wrong_code_reports_a_plain_auth_failure(db) -> None:
    """The message must not distinguish a bad password from a bad code."""

    user = _user(db)
    enrolment = mfa.begin_enrolment(user)
    mfa.confirm_enrolment(user, _code(enrolment.secret))
    db.flush()

    with pytest.raises(accounts.AuthError) as caught:
        accounts.login(db, email="m@example.com", password=PASSWORD, mfa_code="000000")
    assert "Invalid email, password, or verification code" in str(caught.value)


def test_a_wrong_password_is_refused_before_the_code_is_considered(db) -> None:
    """A caller must not learn whether an account has MFA without the password."""

    user = _user(db)
    enrolment = mfa.begin_enrolment(user)
    mfa.confirm_enrolment(user, _code(enrolment.secret))
    db.flush()

    with pytest.raises(accounts.AuthError, match="Invalid email or password"):
        accounts.login(db, email="m@example.com", password="definitely-wrong-here")


def test_a_failed_code_is_audited(db) -> None:
    from sqlalchemy import select

    from iluvtrade.db.models.platform import AuditEvent

    user = _user(db)
    enrolment = mfa.begin_enrolment(user)
    mfa.confirm_enrolment(user, _code(enrolment.secret))
    db.flush()

    with pytest.raises(accounts.AuthError):
        accounts.login(db, email="m@example.com", password=PASSWORD, mfa_code="000000")

    actions = [row.action for row in db.execute(select(AuditEvent)).scalars()]
    assert "user.mfa_challenge_failed" in actions


# --- through the API ------------------------------------------------------------


def test_the_full_enrolment_flow_over_http(client, headers) -> None:
    register(client, "api@example.com")

    status = client.get("/api/v1/auth/mfa").json()
    assert status == {"enabled": False, "enrolment_pending": False, "recovery_codes_remaining": 0}

    enrolment = client.post("/api/v1/auth/mfa/enrol", headers=headers)
    assert enrolment.status_code == 201
    body = enrolment.json()
    assert body["secret"] and body["provisioning_uri"]
    assert len(body["recovery_codes"]) == mfa.RECOVERY_CODE_COUNT

    pending = client.get("/api/v1/auth/mfa").json()
    assert pending["enabled"] is False and pending["enrolment_pending"] is True

    confirmed = client.post(
        "/api/v1/auth/mfa/confirm", json={"code": _code(body["secret"])}, headers=headers
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["enabled"] is True
    assert client.get("/api/v1/auth/me").json()["mfa_enabled"] is True


def test_login_over_http_signals_that_a_code_is_needed(app) -> None:
    from fastapi.testclient import TestClient

    headers = {"X-Requested-With": "XMLHttpRequest"}
    with TestClient(app) as client:
        register(client, "http@example.com")
        body = client.post("/api/v1/auth/mfa/enrol", headers=headers).json()
        client.post(
            "/api/v1/auth/mfa/confirm", json={"code": _code(body["secret"])}, headers=headers
        )
        client.post("/api/v1/auth/logout", headers=headers)

        refused = client.post(
            "/api/v1/auth/login", json={"email": "http@example.com", "password": PASSWORD}
        )
        assert refused.status_code == 401
        assert refused.headers.get("X-MFA-Required") == "true"
        # Both signals, because a client can only ever see one of them: a
        # cross-origin caller reads the header only if CORS exposes it, and a
        # proxy may strip it. The body always arrives.
        assert refused.json()["error"]["code"] == "MfaRequired"

        # And it must be distinguishable from a genuinely wrong password —
        # otherwise the UI tells a user with a working password that it is
        # wrong, and the account is unreachable through the app.
        wrong = client.post(
            "/api/v1/auth/login", json={"email": "http@example.com", "password": "not-the-one"}
        )
        assert wrong.status_code == 401
        assert wrong.json()["error"]["code"] == "AuthError"
        assert "X-MFA-Required" not in wrong.headers

        accepted = client.post(
            "/api/v1/auth/login",
            json={
                "email": "http@example.com",
                "password": PASSWORD,
                "mfa_code": body["recovery_codes"][0],
            },
        )
        assert accepted.status_code == 200


def test_the_secret_is_never_returned_after_enrolment(client, headers) -> None:
    """It exists outside the server exactly once."""

    register(client, "once@example.com")
    body = client.post("/api/v1/auth/mfa/enrol", headers=headers).json()
    secret = body["secret"]
    client.post("/api/v1/auth/mfa/confirm", json={"code": _code(secret)}, headers=headers)

    for path in ("/api/v1/auth/mfa", "/api/v1/auth/me"):
        assert secret not in client.get(path).text
