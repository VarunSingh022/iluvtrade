"""Adding a member to an organization.

Until now an organization had exactly one member — the person who registered it
— and nothing could add a second. Memberships and roles existed, and were
enforced everywhere, but were unreachable: a workspace could not become a team.

Why a token and not an email
----------------------------

This deployment has no email channel, and :mod:`iluvtrade.platform.passwords`
explains why pretending otherwise is worse than refusing. An invitation does
not need one. The token is returned **to the inviter**, once, and they pass it
on through whatever channel they already trust. That is a real product
capability rather than a placeholder — several products work exactly this way —
and it is the honest shape for a deployment without a mail transport.

What stops a leaked token from being a membership
-------------------------------------------------

Three things, and all three are needed:

* The token is hashed at rest, so reading the database yields nothing usable.
* Accepting requires being **signed in**, so the holder needs an account.
* The invitation names an **email address**, and the signed-in account's
  address must match it. A token forwarded to the wrong person is therefore
  inert.

Role escalation
---------------

The invited role is fixed at creation and is never read from the accepting
request — there is no field for the invitee to send. An inviter also cannot
grant a role above their own, so an admin cannot mint an owner and then be
promoted by them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from iluvtrade.db.base import utcnow
from iluvtrade.db.models.platform import Invitation, Membership, Role, User
from iluvtrade.platform import audit, security
from iluvtrade.platform.accounts import Principal
from iluvtrade.platform.tenancy import NotFoundError, scoped

__all__ = [
    "INVITATION_TTL_SECONDS",
    "InvitationError",
    "InvitationIssue",
    "accept",
    "create",
    "list_for",
    "members_of",
    "revoke",
    "status_of",
]

#: Seven days. An invitation nobody acted on in a week is more likely forgotten
#: than pending, and a standing offer of membership is a standing risk.
INVITATION_TTL_SECONDS = 7 * 24 * 3600


class InvitationError(ValueError):
    """The invitation could not be created or accepted as asked."""


@dataclass(frozen=True, slots=True)
class InvitationIssue:
    """A new invitation. ``token`` exists here and is never stored or re-served."""

    invitation: Invitation
    token: str


def status_of(invitation: Invitation, *, now: datetime | None = None) -> str:
    """One word for what an invitation currently is.

    Computed rather than stored: three timestamps already determine it, and a
    fourth column holding a status is a column that can disagree with them.
    """

    now = now or utcnow()
    if invitation.accepted_at is not None:
        return "accepted"
    if invitation.revoked_at is not None:
        return "revoked"
    if invitation.expires_at <= now:
        return "expired"
    return "pending"


def create(
    session: DbSession,
    *,
    inviter: Principal,
    email: str,
    role: Role,
    ttl_seconds: int = INVITATION_TTL_SECONDS,
) -> InvitationIssue:
    """Invite ``email`` to the inviter's organization at ``role``."""

    normalised = email.strip().lower()
    if "@" not in normalised or normalised.startswith("@") or normalised.endswith("@"):
        raise InvitationError("A valid email address is required.")

    if not inviter.role.satisfies(role):
        raise InvitationError(
            f"You hold {inviter.role.value} and cannot invite someone as {role.value}. "
            "An invitation can never grant more than the inviter holds."
        )

    existing_user = session.execute(
        select(User).where(User.email == normalised)
    ).scalar_one_or_none()
    if existing_user is not None:
        already = session.execute(
            select(Membership).where(
                Membership.organization_id == inviter.organization_id,
                Membership.user_id == existing_user.id,
            )
        ).scalar_one_or_none()
        if already is not None:
            raise InvitationError("That person is already a member of this workspace.")

    now = utcnow()
    # Supersede any outstanding invitation for the same address rather than
    # leaving two live tokens for one seat.
    for outstanding in session.execute(
        scoped(Invitation, inviter.organization_id).where(
            Invitation.email == normalised,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
    ).scalars():
        outstanding.revoked_at = now

    token = security.new_session_token()
    invitation = Invitation(
        organization_id=inviter.organization_id,
        email=normalised,
        role=role,
        invited_by_user_id=inviter.user_id,
        token_hash=security.hash_token(token),
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    session.add(invitation)
    session.flush()

    audit.record(
        session,
        organization_id=inviter.organization_id,
        action="organization.invitation_created",
        resource_type="invitation",
        resource_id=invitation.id,
        actor_user_id=inviter.user_id,
        # The address and the role, never the token.
        payload={"email": normalised, "role": role.value},
    )
    return InvitationIssue(invitation=invitation, token=token)


def accept(session: DbSession, *, token: str, user: User) -> Membership:
    """Join the organization an invitation names, as the role it fixes.

    Every refusal reads the same. Whether a token is unknown, expired, revoked
    or already used is information about somebody else's workspace, and the
    caller's next step — ask for a new invitation — is identical in all four
    cases.
    """

    refusal = InvitationError("That invitation is not valid. Ask for a new one.")

    invitation = session.execute(
        select(Invitation).where(Invitation.token_hash == security.hash_token(token))
    ).scalar_one_or_none()
    if invitation is None or status_of(invitation) != "pending":
        raise refusal
    if invitation.email != user.email.strip().lower():
        # The token alone is not enough: the holder must control the address it
        # was issued to. This is what makes a forwarded token inert.
        raise refusal

    already = session.execute(
        select(Membership).where(
            Membership.organization_id == invitation.organization_id,
            Membership.user_id == user.id,
        )
    ).scalar_one_or_none()
    if already is not None:
        invitation.accepted_at = utcnow()
        invitation.accepted_user_id = user.id
        raise InvitationError("You are already a member of that workspace.")

    membership = Membership(
        organization_id=invitation.organization_id,
        user_id=user.id,
        # Read from the invitation, never from the request. There is no field
        # on the accept call that could carry a role.
        role=invitation.role,
    )
    session.add(membership)
    invitation.accepted_at = utcnow()
    invitation.accepted_user_id = user.id

    audit.record(
        session,
        organization_id=invitation.organization_id,
        action="organization.invitation_accepted",
        resource_type="membership",
        resource_id=invitation.id,
        actor_user_id=user.id,
        payload={"email": invitation.email, "role": invitation.role.value},
    )
    session.flush()
    return membership


def revoke(session: DbSession, *, principal: Principal, invitation_id: str) -> Invitation:
    """Withdraw an outstanding invitation."""

    invitation = session.execute(
        scoped(Invitation, principal.organization_id).where(Invitation.id == invitation_id)
    ).scalar_one_or_none()
    if invitation is None:
        raise NotFoundError("Invitation not found")
    if status_of(invitation) != "pending":
        raise InvitationError(f"That invitation is already {status_of(invitation)}.")

    invitation.revoked_at = utcnow()
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="organization.invitation_revoked",
        resource_type="invitation",
        resource_id=invitation.id,
        actor_user_id=principal.user_id,
        payload={"email": invitation.email},
    )
    session.flush()
    return invitation


def list_for(session: DbSession, organization_id: str) -> list[Invitation]:
    """Every invitation this organization has issued, newest first."""

    return list(
        session.execute(
            scoped(Invitation, organization_id).order_by(Invitation.created_at.desc())
        ).scalars()
    )


def members_of(session: DbSession, organization_id: str) -> list[tuple[Membership, User]]:
    """Who is in this organization.

    Joined rather than lazy-loaded so listing a team is one query, and scoped
    through :func:`~iluvtrade.platform.tenancy.scoped` so it cannot accidentally
    answer for another tenant.
    """

    rows = session.execute(
        scoped(Membership, organization_id).order_by(Membership.created_at)
    ).scalars()
    out: list[tuple[Membership, User]] = []
    for membership in rows:
        user = session.get(User, membership.user_id)
        if user is not None:
            out.append((membership, user))
    return out
