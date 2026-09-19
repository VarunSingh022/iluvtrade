"""Registration, login, sessions and organization membership.

The account lifecycle in one module, because the invariant that matters —
*a user always has exactly one organization they can act in* — is only checkable
where registration and membership are written together.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from iluvtrade.config import get_settings
from iluvtrade.db.base import utcnow
from iluvtrade.db.models.platform import (
    Membership,
    Organization,
    Role,
    Session,
    Subscription,
    User,
    UserStatus,
)
from iluvtrade.platform import audit, mfa, security

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(Exception):
    """Authentication failed. The message is deliberately uninformative."""


class RegistrationError(ValueError):
    """The account could not be created as asked."""


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is making a request, and in which tenant.

    Everything downstream authorizes against this and never re-reads the session
    token, so there is one answer to "who is this" per request.
    """

    user_id: str
    organization_id: str
    role: Role
    email: str
    display_name: str
    session_id: str

    def require(self, minimum: Role) -> None:
        """Raise unless this principal holds at least ``minimum``."""

        if not self.role.satisfies(minimum):
            raise PermissionError(
                f"This action requires the {minimum.value} role; you hold {self.role.value}."
            )


def slugify(value: str, *, fallback: str = "workspace") -> str:
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return slug[:60] or fallback


def _unique_slug(session: DbSession, base: str) -> str:
    slug, suffix = base, 1
    while session.execute(
        select(Organization).where(Organization.slug == slug)
    ).scalar_one_or_none():
        suffix += 1
        slug = f"{base}-{suffix}"
    return slug


def register(
    session: DbSession,
    *,
    email: str,
    password: str,
    display_name: str,
    organization_name: str | None = None,
    ip_address: str | None = None,
) -> tuple[User, Organization]:
    """Create a user and the personal organization they start in."""

    email = email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise RegistrationError("A valid email address is required.")
    if not display_name.strip():
        raise RegistrationError("A display name is required.")
    if session.execute(select(User).where(User.email == email)).scalar_one_or_none():
        # Stated plainly: the address is already an account, and pretending
        # otherwise breaks the signup flow without hiding anything a password
        # reset would not reveal.
        raise RegistrationError("An account already exists for that email address.")

    try:
        password_hash = security.hash_password(password)
    except security.WeakPasswordError as exc:
        raise RegistrationError(str(exc)) from exc

    user = User(email=email, password_hash=password_hash, display_name=display_name.strip())
    session.add(user)
    session.flush()

    org_name = (organization_name or f"{display_name.strip()}'s workspace").strip()
    organization = Organization(
        slug=_unique_slug(session, slugify(org_name)),
        name=org_name,
        personal_for_user_id=user.id,
    )
    session.add(organization)
    session.flush()

    session.add(Membership(organization_id=organization.id, user_id=user.id, role=Role.OWNER))
    session.add(Subscription(organization_id=organization.id, plan="free"))
    audit.record(
        session,
        organization_id=organization.id,
        action="user.registered",
        resource_type="user",
        resource_id=user.id,
        actor_user_id=user.id,
        ip_address=ip_address,
        payload={"email": email},
    )
    return user, organization


def _default_membership(session: DbSession, user_id: str) -> Membership:
    membership = (
        session.execute(
            select(Membership).where(Membership.user_id == user_id).order_by(Membership.created_at)
        )
        .scalars()
        .first()
    )
    if membership is None:
        raise AuthError("This account has no workspace.")
    return membership


def login(
    session: DbSession,
    *,
    email: str,
    password: str,
    organization_id: str | None = None,
    mfa_code: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[Session, str]:
    """Verify credentials and open a session.

    Returns the row and the **plaintext token**, which is the only time the
    token exists outside the client.

    Raises:
        AuthError: The credentials are wrong, or the account cannot sign in.
        mfa.MfaRequired: The password was correct but a second factor is needed.
            Deliberately a different exception, so a caller presents a challenge
            instead of reporting bad credentials.
    """

    email = email.strip().lower()
    user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    # Verifying against a dummy hash for an unknown address keeps the timing of
    # "no such user" and "wrong password" comparable.
    stored = user.password_hash if user else _DUMMY_HASH
    ok = security.verify_password(stored, password)
    if user is None or not ok:
        raise AuthError("Invalid email or password.")
    if user.status is not UserStatus.ACTIVE:
        raise AuthError("This account is not active.")

    if organization_id is not None:
        membership = session.execute(
            select(Membership).where(
                Membership.user_id == user.id, Membership.organization_id == organization_id
            )
        ).scalar_one_or_none()
        if membership is None:
            raise AuthError("Invalid email or password.")
    else:
        membership = _default_membership(session, user.id)

    # The second factor is checked *after* the password and the membership, so a
    # caller learns nothing about whether an account has MFA until they have
    # already proven the password.
    if mfa.is_enabled(user):
        if not mfa_code:
            raise mfa.MfaRequired(
                "This account requires a verification code from your authenticator."
            )
        try:
            mfa.verify_challenge(user, mfa_code)
        except mfa.MfaError as exc:
            audit.record(
                session,
                organization_id=membership.organization_id,
                action="user.mfa_challenge_failed",
                resource_type="user",
                resource_id=user.id,
                actor_user_id=user.id,
                outcome="failure",
                ip_address=ip_address,
            )
            raise AuthError("Invalid email, password, or verification code.") from exc

    if security.needs_rehash(user.password_hash):
        user.password_hash = security.hash_password(password)

    token = security.new_session_token()
    settings = get_settings()
    row = Session(
        user_id=user.id,
        organization_id=membership.organization_id,
        token_hash=security.hash_token(token),
        expires_at=utcnow() + timedelta(seconds=settings.session_ttl_seconds),
        ip_address=ip_address,
        user_agent=(user_agent or "")[:400] or None,
    )
    session.add(row)
    user.last_login_at = utcnow()
    audit.record(
        session,
        organization_id=membership.organization_id,
        action="user.login",
        resource_type="session",
        resource_id=row.id,
        actor_user_id=user.id,
        ip_address=ip_address,
    )
    session.flush()
    return row, token


#: A real Argon2 hash of a value no password equals, used to equalise the cost
#: of an unknown-address login with a known one.
_DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHRzb21lc2FsdA$Zm9vYmFyYmF6cXV1eGNvcmdlZ3JhdWx0Z2FycGx5"
)


def resolve_principal(session: DbSession, token: str) -> Principal:
    """Turn a session token into a principal, or refuse."""

    if not token:
        raise AuthError("Not authenticated.")
    row = session.execute(
        select(Session).where(Session.token_hash == security.hash_token(token))
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        raise AuthError("Not authenticated.")
    if row.expires_at <= utcnow():
        raise AuthError("Session expired.")

    membership = session.execute(
        select(Membership).where(
            Membership.user_id == row.user_id,
            Membership.organization_id == row.organization_id,
        )
    ).scalar_one_or_none()
    if membership is None:
        # Membership was removed while the session lived. The session is dead.
        raise AuthError("Not authenticated.")

    user = session.get(User, row.user_id)
    if user is None or user.status is not UserStatus.ACTIVE:
        raise AuthError("Not authenticated.")

    return Principal(
        user_id=user.id,
        organization_id=row.organization_id,
        role=membership.role,
        email=user.email,
        display_name=user.display_name,
        session_id=row.id,
    )


def logout(session: DbSession, principal: Principal) -> None:
    row = session.get(Session, principal.session_id)
    if row is not None and row.revoked_at is None:
        row.revoked_at = utcnow()
        audit.record(
            session,
            organization_id=principal.organization_id,
            action="user.logout",
            resource_type="session",
            resource_id=row.id,
            actor_user_id=principal.user_id,
        )


def memberships_for(session: DbSession, user_id: str) -> list[Membership]:
    """Every workspace this user belongs to.

    Deliberately **not** scoped through :func:`~iluvtrade.platform.tenancy.scoped`:
    the question spans tenants by definition — "which organizations may this
    person act in?" — and is asked at login and when switching. It is keyed by
    the authenticated user's own id and returns nothing about anyone else.
    """

    return list(
        session.execute(
            select(Membership).where(Membership.user_id == user_id).order_by(Membership.created_at)
        ).scalars()
    )


def switch_organization(
    session: DbSession,
    principal: Principal,
    *,
    organization_id: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[Session, str]:
    """Open a session in another organization the caller already belongs to.

    A session names exactly one organization and every authorization decision
    reads it, so "switching" cannot be a mutation of the current row — that
    would let one token act in two tenants over its lifetime, and any audit
    event already written under the old one would become ambiguous. The old
    session is revoked and a new one issued.

    Membership is re-checked here rather than trusted from the request. The
    organization id is caller-supplied, and this is the only place it is turned
    into authority.
    """

    membership = session.execute(
        select(Membership).where(
            Membership.user_id == principal.user_id,
            Membership.organization_id == organization_id,
        )
    ).scalar_one_or_none()
    if membership is None:
        # Not "forbidden": whether an organization exists is not something a
        # non-member is entitled to learn.
        raise AuthError("No such workspace.")

    logout(session, principal)

    token = security.new_session_token()
    settings = get_settings()
    row = Session(
        user_id=principal.user_id,
        organization_id=organization_id,
        token_hash=security.hash_token(token),
        expires_at=utcnow() + timedelta(seconds=settings.session_ttl_seconds),
        ip_address=ip_address,
        user_agent=(user_agent or "")[:400] or None,
    )
    session.add(row)
    audit.record(
        session,
        organization_id=organization_id,
        action="user.organization_switched",
        resource_type="session",
        resource_id=row.id,
        actor_user_id=principal.user_id,
        ip_address=ip_address,
        payload={"from_organization_id": principal.organization_id},
    )
    session.flush()
    return row, token
