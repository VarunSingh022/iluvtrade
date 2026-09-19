"""Two-factor authentication with TOTP.

Nothing cryptographic is invented here. The one-time-password algorithm is
:mod:`pyotp`'s implementation of RFC 6238; the secret is protected by the same
AES-GCM envelope as a broker credential; recovery codes are hashed with Argon2id
exactly as passwords are.

What this module decides — and these are the decisions that matter more than the
algorithm — is:

**Enrolment is two-step.** A secret is issued, and MFA becomes *enabled* only
after the user proves their authenticator has it by entering a code. Enabling on
issue locks out anyone whose authenticator never received the secret, which is a
support burden and, for the last admin of an organization, unrecoverable.

**A code cannot be replayed.** TOTP codes are valid for a window, so the same
code works twice within it. The last accepted counter is stored and a code at or
below it is refused — which is what stops an observed code being reused.

**One step of clock drift is tolerated, and no more.** A wider window multiplies
the codes valid at any instant.

**Recovery codes are single-use and shown once.** They are hashed, so the server
cannot show them again; consuming one removes its hash.

**Disabling requires proof.** Turning MFA off is exactly what an attacker with a
stolen session wants, so it needs a current code or a recovery code — not merely
being signed in.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

import pyotp

from iluvtrade.brokers.crypto import CredentialError, decrypt_credentials, encrypt_credentials
from iluvtrade.db.base import utcnow
from iluvtrade.db.models.platform import User
from iluvtrade.platform import security

__all__ = [
    "Enrolment",
    "MfaError",
    "MfaRequired",
    "begin_enrolment",
    "confirm_enrolment",
    "disable",
    "is_enabled",
    "regenerate_recovery_codes",
    "verify_challenge",
]

#: How many steps of clock drift to accept either side. One step is 30 seconds.
DRIFT_STEPS = 1
RECOVERY_CODE_COUNT = 10
#: Base32-ish, unambiguous alphabet: no O/0 or I/1 to mistype off a printout.
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_RECOVERY_GROUPS = 3
_RECOVERY_GROUP_SIZE = 4

ISSUER = "iluvtrade"


class MfaError(ValueError):
    """The two-factor operation was refused."""


class MfaRequired(Exception):
    """Credentials were correct, but a second factor is needed.

    Distinct from an authentication failure: the password was right, and the
    caller should present a challenge rather than report bad credentials.
    """


@dataclass(frozen=True, slots=True)
class Enrolment:
    """What a user needs to set up an authenticator. Returned once."""

    secret: str
    provisioning_uri: str
    recovery_codes: tuple[str, ...]


def _secret_of(user: User) -> str:
    if not user.mfa_secret:
        raise MfaError("Two-factor authentication is not set up on this account.")
    try:
        return str(decrypt_credentials(user.mfa_secret, connection_id=user.id)["secret"])
    except CredentialError as exc:
        raise MfaError(
            "The stored two-factor secret could not be read. It was encrypted under a "
            "different application key; see the key-rotation procedure."
        ) from exc


def _new_recovery_code() -> str:
    groups = [
        "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_GROUP_SIZE))
        for _ in range(_RECOVERY_GROUPS)
    ]
    return "-".join(groups)


def _normalise(code: str) -> str:
    return code.strip().upper().replace(" ", "")


def is_enabled(user: User) -> bool:
    """Whether this account requires a second factor at login."""

    return user.mfa_enabled_at is not None and bool(user.mfa_secret)


def begin_enrolment(user: User) -> Enrolment:
    """Issue a secret and recovery codes. **Does not enable MFA.**

    The account is unchanged until :func:`confirm_enrolment` proves the
    authenticator holds the secret.
    """

    if is_enabled(user):
        raise MfaError(
            "Two-factor authentication is already enabled. Disable it first to enrol a "
            "new authenticator."
        )

    secret = pyotp.random_base32()
    codes = tuple(_new_recovery_code() for _ in range(RECOVERY_CODE_COUNT))

    user.mfa_secret = encrypt_credentials({"secret": secret}, connection_id=user.id)
    user.mfa_recovery_hashes = json.dumps(
        [security.hash_password(_normalise(code)) for code in codes]
    )
    user.mfa_enabled_at = None
    user.mfa_last_counter = None

    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=ISSUER)
    return Enrolment(secret=secret, provisioning_uri=uri, recovery_codes=codes)


def confirm_enrolment(user: User, code: str) -> None:
    """Enable MFA once a code from the new secret verifies."""

    if is_enabled(user):
        raise MfaError("Two-factor authentication is already enabled.")
    if not user.mfa_secret:
        raise MfaError("Start enrolment before confirming it.")

    totp = pyotp.TOTP(_secret_of(user))
    if not totp.verify(_normalise(code), valid_window=DRIFT_STEPS):
        raise MfaError("That code is not valid. Check your authenticator's clock and try again.")

    user.mfa_enabled_at = utcnow()
    user.mfa_last_counter = _counter(totp, _normalise(code))


def _counter(totp: pyotp.TOTP, code: str) -> int | None:
    """Which time step produced ``code``, if any within the accepted drift."""

    now = int(datetime.now(UTC).timestamp())
    step = totp.interval
    for offset in range(-DRIFT_STEPS, DRIFT_STEPS + 1):
        candidate = now + offset * step
        if totp.at(candidate) == code:
            return candidate // step
    return None


def verify_challenge(user: User, code: str) -> str:
    """Accept a TOTP code or a recovery code. Returns which was used.

    Raises :class:`MfaError` on anything else. A recovery code is consumed.
    """

    if not is_enabled(user):
        raise MfaError("Two-factor authentication is not enabled on this account.")

    supplied = _normalise(code)
    totp = pyotp.TOTP(_secret_of(user))

    if totp.verify(supplied, valid_window=DRIFT_STEPS):
        counter = _counter(totp, supplied)
        if counter is None:
            raise MfaError("That code is not valid.")
        if user.mfa_last_counter is not None and counter <= user.mfa_last_counter:
            # The same code is valid for its whole window; without this, one
            # observed code could be used more than once inside it.
            raise MfaError("That code has already been used. Wait for the next one.")
        user.mfa_last_counter = counter
        return "totp"

    hashes: list[str] = json.loads(user.mfa_recovery_hashes or "[]")
    for index, stored in enumerate(hashes):
        if security.verify_password(stored, supplied):
            # Single use: remove it before returning, so a replayed recovery
            # code fails even if the same request is repeated.
            del hashes[index]
            user.mfa_recovery_hashes = json.dumps(hashes)
            return "recovery"

    raise MfaError("That code is not valid.")


def remaining_recovery_codes(user: User) -> int:
    return len(json.loads(user.mfa_recovery_hashes or "[]"))


def regenerate_recovery_codes(user: User, code: str) -> tuple[str, ...]:
    """Replace every recovery code. Requires a current second factor."""

    verify_challenge(user, code)
    codes = tuple(_new_recovery_code() for _ in range(RECOVERY_CODE_COUNT))
    user.mfa_recovery_hashes = json.dumps(
        [security.hash_password(_normalise(entry)) for entry in codes]
    )
    return codes


def disable(user: User, code: str) -> None:
    """Turn MFA off. Requires a current code or a recovery code.

    Being signed in is not sufficient: disabling is precisely what someone with
    a stolen session would do first.
    """

    verify_challenge(user, code)
    user.mfa_secret = None
    user.mfa_enabled_at = None
    user.mfa_recovery_hashes = "[]"
    user.mfa_last_counter = None
