"""Registration, login, logout and the caller's own profile."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session as DbSession

from iluvtrade.api.deps import (
    SESSION_COOKIE,
    current_principal,
    db_session,
    rate_limit_anonymous,
)
from iluvtrade.api.v1.schemas import (
    LoginRequest,
    MembershipSummary,
    MfaCodeRequest,
    MfaEnrolmentResponse,
    MfaStatusResponse,
    PasswordResetAvailability,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    RegisterRequest,
    SessionResponse,
    SwitchOrganizationRequest,
    UpdateSettingsRequest,
    UserResponse,
)
from iluvtrade.config import get_settings
from iluvtrade.db.models.platform import Organization, User
from iluvtrade.platform import accounts, audit, mfa, notifications, passwords

router = APIRouter(prefix="/auth", tags=["auth"])


def _user_response(session: DbSession, principal: accounts.Principal) -> UserResponse:
    organization = session.get(Organization, principal.organization_id)
    user = session.get(User, principal.user_id)
    return UserResponse(
        id=principal.user_id,
        email=principal.email,
        display_name=principal.display_name,
        organization_id=principal.organization_id,
        organization_name=organization.name if organization else "",
        role=principal.role.value,
        live_trading_enabled=bool(user.live_trading_enabled) if user else False,
        mfa_enabled=mfa.is_enabled(user) if user else False,
    )


def _set_cookie(response: Response, token: str) -> None:
    """Set the session cookie.

    ``HttpOnly`` so script cannot read the token even if an XSS lands.
    ``SameSite=Lax`` so it is not sent on cross-site POSTs. ``Secure`` in
    production only, because a development server on plain HTTP would otherwise
    have the cookie silently dropped and look like a broken login.
    """

    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        path="/",
    )


@router.post(
    "/register",
    response_model=SessionResponse,
    status_code=201,
    dependencies=[Depends(rate_limit_anonymous("register"))],
)
def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    session: DbSession = Depends(db_session),
) -> SessionResponse:
    """Create an account and its personal workspace, then log in."""

    client_ip = request.client.host if request.client else None
    accounts.register(
        session,
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
        organization_name=payload.organization_name,
        ip_address=client_ip,
    )
    row, token = accounts.login(
        session,
        email=payload.email,
        password=payload.password,
        ip_address=client_ip,
        user_agent=request.headers.get("user-agent"),
    )
    principal = accounts.resolve_principal(session, token)
    _set_cookie(response, token)
    return SessionResponse(
        token=token, expires_at=row.expires_at, user=_user_response(session, principal)
    )


@router.post(
    "/login",
    response_model=SessionResponse,
    dependencies=[Depends(rate_limit_anonymous("login"))],
)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: DbSession = Depends(db_session),
) -> SessionResponse:
    # ``mfa.MfaRequired`` propagates: it has its own handler, which answers 401
    # with ``error.code == "MfaRequired"`` and the ``X-MFA-Required`` header, so
    # the client shows a code challenge rather than reporting a bad password.
    row, token = accounts.login(
        session,
        email=payload.email,
        password=payload.password,
        organization_id=payload.organization_id,
        mfa_code=payload.mfa_code,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    principal = accounts.resolve_principal(session, token)
    _set_cookie(response, token)
    return SessionResponse(
        token=token, expires_at=row.expires_at, user=_user_response(session, principal)
    )


@router.post("/logout", status_code=204)
def logout(
    response: Response,
    session: DbSession = Depends(db_session),
    principal: accounts.Principal = Depends(current_principal),
) -> Response:
    accounts.logout(session, principal)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.status_code = 204
    return response


@router.get("/me", response_model=UserResponse)
def me(
    session: DbSession = Depends(db_session),
    principal: accounts.Principal = Depends(current_principal),
) -> UserResponse:
    return _user_response(session, principal)


@router.patch("/me", response_model=UserResponse)
def update_me(
    payload: UpdateSettingsRequest,
    session: DbSession = Depends(db_session),
    principal: accounts.Principal = Depends(current_principal),
) -> UserResponse:
    """Update the caller's own settings.

    ``live_trading_enabled`` here is only the *user* gate. The deployment gate
    and the per-session confirmation still apply, so flipping this alone does
    not enable live trading.
    """

    user = session.get(User, principal.user_id)
    if user is None:
        raise LookupError("User not found")
    if payload.display_name is not None:
        user.display_name = payload.display_name.strip()
    if payload.live_trading_enabled is not None:
        user.live_trading_enabled = payload.live_trading_enabled
        audit.record(
            session,
            organization_id=principal.organization_id,
            action="user.live_trading_toggled",
            resource_type="user",
            resource_id=user.id,
            actor_user_id=principal.user_id,
            payload={"enabled": payload.live_trading_enabled},
        )
    session.flush()
    organization = session.get(Organization, principal.organization_id)
    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        organization_id=principal.organization_id,
        organization_name=organization.name if organization else "",
        role=principal.role.value,
        live_trading_enabled=user.live_trading_enabled,
        mfa_enabled=mfa.is_enabled(user),
    )


# ---------------------------------------------------------------------------
# Two-factor authentication
# ---------------------------------------------------------------------------


@router.get("/mfa", response_model=MfaStatusResponse)
def mfa_status(
    session: DbSession = Depends(db_session),
    principal: accounts.Principal = Depends(current_principal),
) -> MfaStatusResponse:
    user = session.get(User, principal.user_id)
    if user is None:
        raise LookupError("User not found")
    return MfaStatusResponse(
        enabled=mfa.is_enabled(user),
        enrolment_pending=bool(user.mfa_secret) and not mfa.is_enabled(user),
        recovery_codes_remaining=mfa.remaining_recovery_codes(user),
    )


@router.post("/mfa/enrol", response_model=MfaEnrolmentResponse, status_code=201)
def begin_mfa_enrolment(
    session: DbSession = Depends(db_session),
    principal: accounts.Principal = Depends(current_principal),
) -> MfaEnrolmentResponse:
    """Issue a secret and recovery codes. Does **not** enable MFA.

    This is the only time the secret and the recovery codes exist outside the
    user's hands — the codes are stored hashed and the secret encrypted, so
    neither can be shown again.
    """

    user = session.get(User, principal.user_id)
    if user is None:
        raise LookupError("User not found")

    enrolment = mfa.begin_enrolment(user)
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="user.mfa_enrolment_started",
        resource_type="user",
        resource_id=user.id,
        actor_user_id=principal.user_id,
    )
    return MfaEnrolmentResponse(
        secret=enrolment.secret,
        provisioning_uri=enrolment.provisioning_uri,
        recovery_codes=list(enrolment.recovery_codes),
    )


@router.post("/mfa/confirm", response_model=MfaStatusResponse)
def confirm_mfa(
    payload: MfaCodeRequest,
    session: DbSession = Depends(db_session),
    principal: accounts.Principal = Depends(current_principal),
) -> MfaStatusResponse:
    """Enable MFA by proving the authenticator holds the secret."""

    user = session.get(User, principal.user_id)
    if user is None:
        raise LookupError("User not found")

    mfa.confirm_enrolment(user, payload.code)
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="user.mfa_enabled",
        resource_type="user",
        resource_id=user.id,
        actor_user_id=principal.user_id,
    )
    notifications.notify(
        session,
        organization_id=principal.organization_id,
        user_id=principal.user_id,
        kind="account.mfa_enabled",
        title="Two-factor authentication enabled",
        body="A verification code is now required to sign in to this account.",
        resource_type="user",
        resource_id=user.id,
    )
    return MfaStatusResponse(
        enabled=True,
        enrolment_pending=False,
        recovery_codes_remaining=mfa.remaining_recovery_codes(user),
    )


@router.post("/mfa/recovery-codes", response_model=MfaEnrolmentResponse)
def regenerate_recovery_codes(
    payload: MfaCodeRequest,
    session: DbSession = Depends(db_session),
    principal: accounts.Principal = Depends(current_principal),
) -> MfaEnrolmentResponse:
    """Replace every recovery code. Requires a current second factor."""

    user = session.get(User, principal.user_id)
    if user is None:
        raise LookupError("User not found")

    codes = mfa.regenerate_recovery_codes(user, payload.code)
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="user.mfa_recovery_codes_regenerated",
        resource_type="user",
        resource_id=user.id,
        actor_user_id=principal.user_id,
    )
    return MfaEnrolmentResponse(secret="", provisioning_uri="", recovery_codes=list(codes))


@router.post("/mfa/disable", response_model=MfaStatusResponse)
def disable_mfa(
    payload: MfaCodeRequest,
    session: DbSession = Depends(db_session),
    principal: accounts.Principal = Depends(current_principal),
) -> MfaStatusResponse:
    """Turn MFA off. Requires a current code — being signed in is not enough."""

    user = session.get(User, principal.user_id)
    if user is None:
        raise LookupError("User not found")

    mfa.disable(user, payload.code)
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="user.mfa_disabled",
        resource_type="user",
        resource_id=user.id,
        actor_user_id=principal.user_id,
    )
    notifications.notify(
        session,
        organization_id=principal.organization_id,
        user_id=principal.user_id,
        kind="account.mfa_disabled",
        title="Two-factor authentication disabled",
        body="This account no longer requires a verification code to sign in.",
        resource_type="user",
        resource_id=user.id,
    )
    return MfaStatusResponse(enabled=False, enrolment_pending=False, recovery_codes_remaining=0)


# ---------------------------------------------------------------------------
# Workspaces the caller belongs to
# ---------------------------------------------------------------------------


@router.get("/organizations", response_model=list[MembershipSummary])
def my_organizations(
    session: DbSession = Depends(db_session),
    principal: accounts.Principal = Depends(current_principal),
) -> list[MembershipSummary]:
    """Every workspace this account may act in."""

    out: list[MembershipSummary] = []
    for membership in accounts.memberships_for(session, principal.user_id):
        organization = session.get(Organization, membership.organization_id)
        out.append(
            MembershipSummary(
                organization_id=membership.organization_id,
                organization_name=organization.name if organization else "",
                role=membership.role.value,
                is_current=membership.organization_id == principal.organization_id,
            )
        )
    return out


@router.post("/switch-organization", response_model=SessionResponse)
def switch_organization(
    payload: SwitchOrganizationRequest,
    request: Request,
    response: Response,
    session: DbSession = Depends(db_session),
    principal: accounts.Principal = Depends(current_principal),
) -> SessionResponse:
    """Act in a different workspace.

    Issues a **new** session and revokes the current one, because a session
    names one organization and every authorization decision reads it. Mutating
    the row instead would let one token act in two tenants across its life.
    """

    row, token = accounts.switch_organization(
        session,
        principal,
        organization_id=payload.organization_id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    switched = accounts.resolve_principal(session, token)
    _set_cookie(response, token)
    return SessionResponse(
        token=token, expires_at=row.expires_at, user=_user_response(session, switched)
    )


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


@router.get("/password-reset", response_model=PasswordResetAvailability)
def password_reset_availability() -> PasswordResetAvailability:
    """Whether a reset can actually reach a user here.

    Public, and it has to be: the sign-in screen reads this to decide whether
    to offer "forgot password" at all, and a link that silently goes nowhere is
    worse than no link. It discloses a property of the deployment, not of any
    account.
    """

    provider = passwords.delivery_provider()
    return PasswordResetAvailability(
        available=provider.available,
        channel=provider.name,
        notice=(
            "Enter your email address and a reset link will be sent."
            if provider.available
            else (
                "This deployment has no email delivery configured, so a reset link "
                "cannot be sent. An administrator can issue one directly."
            )
        ),
    )


@router.post(
    "/password-reset/request",
    status_code=202,
    dependencies=[Depends(rate_limit_anonymous("password_reset"))],
)
def request_password_reset(
    payload: PasswordResetRequest,
    request: Request,
    session: DbSession = Depends(db_session),
) -> Response:
    """Begin a reset.

    Answers 202 for *any* address when a delivery channel exists — a different
    answer for a known address would turn this into an account enumerator. When
    no channel exists it refuses with 503, which is decided before the address
    is looked at and is therefore identical for everyone.
    """

    passwords.request_reset(
        session,
        email=payload.email,
        ip_address=request.client.host if request.client else None,
    )
    return Response(status_code=202)


@router.post(
    "/password-reset/confirm",
    response_model=UserResponse,
    dependencies=[Depends(rate_limit_anonymous("password_reset_confirm"))],
)
def confirm_password_reset(
    payload: PasswordResetConfirmRequest,
    request: Request,
    session: DbSession = Depends(db_session),
) -> UserResponse:
    """Set a new password against a token, and sign every session out.

    Revoking the account's sessions is the point of doing this rather than a
    settings-page password change: the reason someone resets a password is
    often that a session is in the wrong hands.
    """

    user = passwords.consume(
        session,
        token=payload.token,
        new_password=payload.new_password,
        ip_address=request.client.host if request.client else None,
    )
    membership = accounts._default_membership(session, user.id)
    organization = session.get(Organization, membership.organization_id)
    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        organization_id=membership.organization_id,
        organization_name=organization.name if organization else "",
        role=membership.role.value,
        live_trading_enabled=user.live_trading_enabled,
        mfa_enabled=mfa.is_enabled(user),
    )
