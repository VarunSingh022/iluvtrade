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
    RegisterRequest,
    SessionResponse,
    UpdateSettingsRequest,
    UserResponse,
)
from iluvtrade.config import get_settings
from iluvtrade.db.models.platform import Organization, User
from iluvtrade.platform import accounts

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
    row, token = accounts.login(
        session,
        email=payload.email,
        password=payload.password,
        organization_id=payload.organization_id,
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

    from iluvtrade.platform import audit

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
    )
