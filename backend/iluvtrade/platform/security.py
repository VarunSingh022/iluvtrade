"""Password hashing and session tokens.

Two rules this module exists to keep:

* A password is hashed with Argon2id and the plaintext is never stored, logged
  or returned.
* A session token is generated with :mod:`secrets`, handed to the client once,
  and stored only as an HMAC-SHA256 under the application secret. A database
  disclosure therefore does not yield usable sessions, and token lookup is still
  a single indexed equality.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from iluvtrade.config import get_settings

#: Long enough that guessing is not a threat model.
_TOKEN_BYTES = 32

MIN_PASSWORD_LENGTH = 12


class WeakPasswordError(ValueError):
    """The supplied password does not meet the minimum policy."""


def _hasher() -> PasswordHasher:
    settings = get_settings()
    return PasswordHasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_cost,
        parallelism=settings.argon2_parallelism,
    )


def validate_password(password: str) -> None:
    """Refuse a password the policy does not accept.

    Length only, deliberately. Composition rules push people toward
    ``Passw0rd!`` and a longer minimum is the control that actually helps.
    """

    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")


def hash_password(password: str) -> str:
    validate_password(password)
    return _hasher().hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Whether ``password`` matches. Never raises for a wrong password."""

    try:
        return _hasher().verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """Whether the stored hash was made with weaker parameters than current."""

    try:
        return _hasher().check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def new_session_token() -> str:
    """A fresh, unguessable session token. Returned to the client once."""

    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(token: str) -> str:
    """The stored form of a session token."""

    settings = get_settings()
    return hmac.new(
        settings.secret_key.encode("utf-8"), token.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def tokens_equal(left: str, right: str) -> bool:
    """Constant-time comparison, for anything token-shaped."""

    return hmac.compare_digest(left, right)
