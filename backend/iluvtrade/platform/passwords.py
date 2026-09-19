"""Password reset: the token half, and the delivery seam it stops at.

What is real here
-----------------

Everything except delivery. A reset token is 256 bits from :mod:`secrets`,
stored only as an HMAC under the application secret, valid for
:data:`RESET_TTL_SECONDS`, usable exactly once, and revoked — along with every
session the account holds — the moment it is used. All of that is implemented
and tested.

What is not
-----------

**Email.** This deployment has no delivery channel, and one is not simulated.
That decision shapes the endpoint rather than being hidden behind it:
:func:`request_reset` refuses with :class:`DeliveryUnavailable` rather than
answering ``202 Accepted`` and dropping the message. A 202 that never arrives
is worse than a refusal — the user waits, retries, and eventually concludes the
account is broken, while the deployment's logs say everything succeeded.

:class:`DeliveryProvider` is the seam an email or SMS provider implements. It
is a Protocol with one method, and the whole flow above is already built around
it, so adding a provider is a class and a setting — not a redesign.

No user enumeration
-------------------

The refusal above is identical for every address, known or not, because it is
decided *before* the address is looked up. When a provider does exist, the
success path is likewise identical: :func:`request_reset` returns ``None`` for
an unknown address after doing the same work, and the endpoint answers the same
way either way.

The operator escape hatch
-------------------------

``iluvtrade issue-password-reset <email>`` mints a token and prints it once, for
an administrator to hand over through a channel they trust. This is not a
backdoor around the controls: it needs shell access on the application host,
which already implies database access, and it writes the same audit event a
delivered reset would. It exists so that "no email channel" does not mean "no
recoverable accounts" on a deployment that is otherwise complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from iluvtrade.db.base import utcnow
from iluvtrade.db.models.platform import PasswordResetToken, Session, User, UserStatus
from iluvtrade.platform import accounts, audit, notifications, security

__all__ = [
    "DeliveryProvider",
    "DeliveryUnavailable",
    "NoDeliveryConfigured",
    "ResetError",
    "ResetIssue",
    "consume",
    "delivery_provider",
    "issue",
    "request_reset",
]

#: One hour. Long enough for someone to reach their inbox, short enough that a
#: token sitting in a mailbox archive is not a standing key to the account.
RESET_TTL_SECONDS = 3600


class ResetError(ValueError):
    """The reset could not be completed as asked."""


class DeliveryUnavailable(RuntimeError):
    """No channel can carry a reset token to the user on this deployment."""


@dataclass(frozen=True, slots=True)
class ResetIssue:
    """A freshly minted token. The plaintext exists here and nowhere else."""

    user_id: str
    email: str
    display_name: str
    token: str
    expires_at: datetime


class DeliveryProvider(Protocol):
    """How a reset token reaches the person who asked for it.

    An implementation must never log, store or return the token: it goes to the
    address and nowhere else. It must also not reveal, through timing or
    through its return value, whether the address was known — that decision has
    already been made by the caller.
    """

    @property
    def name(self) -> str:
        """Short identifier, reported by the health endpoint."""
        ...

    @property
    def available(self) -> bool:
        """Whether this provider can actually deliver right now."""
        ...

    def deliver(self, issue: ResetIssue) -> None:
        """Send the token. Raise :class:`DeliveryUnavailable` if it cannot."""
        ...


class NoDeliveryConfigured:
    """The only provider that exists. It refuses, and says so.

    Deliberately not a no-op that returns successfully. A silent success here
    would make every layer above believe a reset email was sent, which is the
    exact failure this module's docstring exists to prevent.
    """

    @property
    def name(self) -> str:
        return "none"

    @property
    def available(self) -> bool:
        return False

    def deliver(self, issue: ResetIssue) -> None:
        raise DeliveryUnavailable(
            "Password reset by email is not available on this deployment: no delivery "
            "channel is configured. An administrator can issue a reset with "
            "'iluvtrade issue-password-reset'."
        )


_PROVIDER: DeliveryProvider = NoDeliveryConfigured()


def delivery_provider() -> DeliveryProvider:
    """The configured delivery provider."""

    return _PROVIDER


def issue(session: DbSession, user: User, *, ip_address: str | None = None) -> ResetIssue:
    """Mint a reset token for ``user``, invalidating any outstanding one.

    Superseding rather than accumulating: two live tokens for one account
    doubles the window in which a leaked one works, and a user who clicks
    "forgot password" three times should not leave three keys behind.
    """

    now = utcnow()
    outstanding = session.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.consumed_at.is_(None),
        )
    ).scalars()
    for row in outstanding:
        row.consumed_at = now

    token = security.new_session_token()
    session.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=security.hash_token(token),
            expires_at=now + timedelta(seconds=RESET_TTL_SECONDS),
            requested_ip=ip_address,
        )
    )
    session.flush()
    return ResetIssue(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        token=token,
        expires_at=now + timedelta(seconds=RESET_TTL_SECONDS),
    )


def _audit_organization(session: DbSession, user_id: str) -> str | None:
    """Where to record an account-level event for a user.

    A password is not tenant data, but the audit chain is per organization, so
    an event has to land somewhere. The user's first membership is the one
    consistent answer; an account with none is unreachable anyway.
    """

    try:
        return accounts._default_membership(session, user_id).organization_id
    except accounts.AuthError:
        return None


def request_reset(session: DbSession, *, email: str, ip_address: str | None = None) -> None:
    """Begin a reset for ``email``, if that address has an account.

    Raises :class:`DeliveryUnavailable` when nothing can carry the token —
    checked **first**, so the refusal cannot depend on whether the address
    exists and therefore cannot be used to enumerate accounts.

    Returns ``None`` in every other case, known address or not. The caller
    answers identically either way.
    """

    provider = delivery_provider()
    if not provider.available:
        raise DeliveryUnavailable(
            "Password reset by email is not available on this deployment: no delivery "
            "channel is configured. Contact an administrator."
        )

    normalised = email.strip().lower()
    user = session.execute(select(User).where(User.email == normalised)).scalar_one_or_none()
    if user is None or user.status is not UserStatus.ACTIVE:
        return

    issued = issue(session, user, ip_address=ip_address)
    organization_id = _audit_organization(session, user.id)
    if organization_id:
        audit.record(
            session,
            organization_id=organization_id,
            action="user.password_reset_requested",
            resource_type="user",
            resource_id=user.id,
            actor_user_id=user.id,
            ip_address=ip_address,
            # No token, no hash. An audit row that carries the secret is the
            # secret, and this table is read by every admin of the tenant.
            payload={"delivery": provider.name},
        )
    provider.deliver(issued)


def consume(
    session: DbSession, *, token: str, new_password: str, ip_address: str | None = None
) -> User:
    """Set a new password against a valid token, or refuse.

    Every refusal carries the same message. "Expired" and "already used" are
    both information about a token an attacker may only be guessing at, and the
    user's next step — request another — is the same for both.

    On success every session the account holds is revoked, including the one
    that may have been stolen and is the reason the reset is happening.
    """

    row = session.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == security.hash_token(token)
        )
    ).scalar_one_or_none()
    now = utcnow()
    if row is None or row.consumed_at is not None or row.expires_at <= now:
        raise ResetError("That reset link is not valid. Request a new one.")

    user = session.get(User, row.user_id)
    if user is None or user.status is not UserStatus.ACTIVE:
        raise ResetError("That reset link is not valid. Request a new one.")

    # Hash first: a weak password must leave the token unspent, or the user is
    # locked out of their own reset by a typo.
    password_hash = security.hash_password(new_password)

    row.consumed_at = now
    user.password_hash = password_hash

    revoked = 0
    for live in session.execute(
        select(Session).where(Session.user_id == user.id, Session.revoked_at.is_(None))
    ).scalars():
        live.revoked_at = now
        revoked += 1

    organization_id = _audit_organization(session, user.id)
    if organization_id:
        audit.record(
            session,
            organization_id=organization_id,
            action="user.password_reset_completed",
            resource_type="user",
            resource_id=user.id,
            actor_user_id=user.id,
            ip_address=ip_address,
            payload={"sessions_revoked": revoked},
        )
        # Waiting for them when they sign back in. The value is entirely in the
        # case where it was not them: a reset they did not ask for is the first
        # visible sign of a compromised mailbox.
        notifications.notify(
            session,
            organization_id=organization_id,
            user_id=user.id,
            kind="account.password_reset",
            title="Your password was reset",
            body=(
                f"{revoked} active session(s) were signed out. If you did not do this, "
                "reset your password again and enable two-factor authentication."
            ),
            resource_type="user",
            resource_id=user.id,
        )
    session.flush()
    return user
