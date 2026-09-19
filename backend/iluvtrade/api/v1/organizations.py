"""The workspace as a team: members, invitations, and switching between them.

Until this router existed an organization could only ever have the one member
who registered it. :class:`~iluvtrade.db.models.platform.Membership` and
:class:`~iluvtrade.db.models.platform.Role` were enforced throughout, but
nothing could create a second row — the roles were real and unreachable.

Authorization here is deliberately split. Reading who is in *your own*
workspace is open to any member, because a team that cannot see itself is not a
team. Creating and revoking invitations requires admin, and the role an
invitation grants can never exceed the inviter's own.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session as DbSession

from iluvtrade.api.deps import current_principal, db_session, rate_limit, require_admin
from iluvtrade.api.v1.schemas import (
    AcceptInvitationRequest,
    InvitationCreatedResponse,
    InvitationCreateRequest,
    InvitationResponse,
    MemberResponse,
)
from iluvtrade.db.models.platform import Invitation, Role, User
from iluvtrade.platform import accounts, invitations, notifications

router = APIRouter(prefix="/organizations", tags=["organizations"])


def _invitation_response(invitation: Invitation) -> InvitationResponse:
    """Project an invitation. Note what is absent: ``token_hash``.

    The response model is an allowlist, so this could not leak even by
    accident — but the projection is written out rather than constructed from
    the ORM object so the omission is visible at review.
    """

    return InvitationResponse(
        id=invitation.id,
        email=invitation.email,
        role=invitation.role.value,
        status=invitations.status_of(invitation),
        invited_by_user_id=invitation.invited_by_user_id,
        expires_at=invitation.expires_at,
        accepted_at=invitation.accepted_at,
        revoked_at=invitation.revoked_at,
        created_at=invitation.created_at,
    )


@router.get("/members", response_model=list[MemberResponse])
def list_members(
    session: DbSession = Depends(db_session),
    principal: accounts.Principal = Depends(current_principal),
) -> list[MemberResponse]:
    """Who is in the caller's own workspace.

    Any member may read this, and only for the organization their session is
    acting in — there is no parameter naming an organization, so there is
    nothing to tamper with.
    """

    return [
        MemberResponse(
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=membership.role.value,
            joined_at=membership.created_at,
        )
        for membership, user in invitations.members_of(session, principal.organization_id)
    ]


@router.get("/invitations", response_model=list[InvitationResponse])
def list_invitations(
    session: DbSession = Depends(db_session),
    principal: accounts.Principal = Depends(require_admin),
) -> list[InvitationResponse]:
    return [
        _invitation_response(invitation)
        for invitation in invitations.list_for(session, principal.organization_id)
    ]


@router.post("/invitations", response_model=InvitationCreatedResponse, status_code=201)
def create_invitation(
    payload: InvitationCreateRequest,
    session: DbSession = Depends(db_session),
    principal: accounts.Principal = Depends(require_admin),
    _limited: accounts.Principal = Depends(rate_limit("invitation")),
) -> InvitationCreatedResponse:
    """Invite someone, and hand the token back to the inviter exactly once."""

    issued = invitations.create(
        session,
        inviter=principal,
        email=payload.email,
        role=Role(payload.role),
    )
    return InvitationCreatedResponse(
        invitation=_invitation_response(issued.invitation),
        token=issued.token,
        share_instructions=(
            "Send this code to the person you invited through a channel you trust. "
            "This deployment has no email delivery, so it is shown here once and "
            "cannot be retrieved again. They sign in with their own account and "
            "enter it under Settings to join this workspace."
        ),
    )


@router.post("/invitations/{invitation_id}/revoke", response_model=InvitationResponse)
def revoke_invitation(
    invitation_id: str,
    session: DbSession = Depends(db_session),
    principal: accounts.Principal = Depends(require_admin),
) -> InvitationResponse:
    invitation = invitations.revoke(session, principal=principal, invitation_id=invitation_id)
    return _invitation_response(invitation)


@router.post("/invitations/accept", response_model=MemberResponse)
def accept_invitation(
    payload: AcceptInvitationRequest,
    session: DbSession = Depends(db_session),
    principal: accounts.Principal = Depends(current_principal),
) -> MemberResponse:
    """Join the workspace an invitation names.

    Requires a signed-in account whose email matches the invitation's. The role
    comes from the invitation and there is no field on this request that could
    carry one.

    The caller's *current* session keeps acting in its existing organization;
    joining is not switching. ``POST /auth/switch-organization`` is the
    deliberate second step, so accepting an invitation never silently moves
    someone out of the workspace they were working in.
    """

    user = session.get(User, principal.user_id)
    if user is None:
        raise LookupError("User not found")

    membership = invitations.accept(session, token=payload.token, user=user)
    # Addressed to the workspace being joined, not to the workspace the caller
    # is currently acting in: it is news for the team that gained a member.
    notifications.notify(
        session,
        organization_id=membership.organization_id,
        user_id=None,
        kind="organization.member_joined",
        title=f"{user.display_name} joined this workspace",
        body=f"{user.email} accepted an invitation and holds the {membership.role.value} role.",
        resource_type="membership",
        resource_id=membership.id,
    )
    return MemberResponse(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=membership.role.value,
        joined_at=membership.created_at,
    )
