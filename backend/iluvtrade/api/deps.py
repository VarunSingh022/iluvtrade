"""Request-scoped dependencies: the database session and the caller's identity.

Authentication is a **bearer token or an HttpOnly cookie**, and the cookie is
what the browser app uses. That choice is what makes the CSRF stance below
coherent, so it is stated here rather than left implicit.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session as DbSession

from iluvtrade.db.models.platform import Role
from iluvtrade.db.session import get_session_factory
from iluvtrade.platform.accounts import AuthError, Principal, resolve_principal
from iluvtrade.platform.ratelimit import POLICIES, get_limiter

__all__ = [
    "SESSION_COOKIE",
    "client_identity",
    "current_principal",
    "db_session",
    "rate_limit",
    "rate_limit_anonymous",
    "require_admin",
    "require_trader",
]

SESSION_COOKIE = "iluvtrade_session"


def db_session() -> Iterator[DbSession]:
    """One transaction per request. Commits on success, rolls back on error."""

    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _token_from(authorization: str | None, cookie: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return (cookie or "").strip()


def current_principal(
    request: Request,
    session: DbSession = Depends(db_session),
    authorization: str | None = Header(default=None),
    iluvtrade_session: str | None = Cookie(default=None),
) -> Principal:
    """Resolve the caller, or 401.

    **CSRF.** Cookie authentication is only honoured for safe methods and for
    requests carrying ``X-Requested-With``, which a cross-origin form post
    cannot set without a preflight the browser will refuse. A bearer token is
    accepted on any method, because a token in a header is not attached
    automatically by the browser and so is not forgeable this way.
    """

    token = _token_from(authorization, iluvtrade_session)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated.")

    used_cookie = not (authorization and authorization.lower().startswith("bearer "))
    unsafe = request.method.upper() not in {"GET", "HEAD", "OPTIONS"}
    if used_cookie and unsafe and request.headers.get("x-requested-with") is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "State-changing requests authenticated by cookie must send the "
                "X-Requested-With header."
            ),
        )

    try:
        return resolve_principal(session, token)
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


def require_trader(principal: Principal = Depends(current_principal)) -> Principal:
    _require(principal, Role.TRADER)
    return principal


def require_admin(principal: Principal = Depends(current_principal)) -> Principal:
    _require(principal, Role.ADMIN)
    return principal


def _require(principal: Principal, role: Role) -> None:
    try:
        principal.require(role)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def client_identity(request: Request) -> str:
    """A best-effort identity for an *unauthenticated* caller.

    Prefers ``X-Forwarded-For``'s first entry, because this runs behind a proxy
    in any real deployment and ``request.client`` would otherwise be the proxy —
    making every anonymous caller share one bucket.

    A forwarded header is client-controlled and therefore spoofable. That is
    accepted here because the alternative is worse: without it, one proxy IP
    means one shared login budget for every user, and a single attacker
    exhausts it for everybody. The header is only ever a rate-limit key, never
    an authorization input. A deployment that needs this to be trustworthy
    configures its proxy to overwrite rather than append the header.
    """

    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


def rate_limit(policy_name: str) -> Any:
    """A dependency limiting an *authenticated* caller, keyed by user.

    Keyed by user rather than organization so one member cannot exhaust a
    colleague's budget, and not by IP so a shared office network is not one
    bucket.
    """

    # Looked up now so a typo is an ImportError at startup, but resolved again
    # at call time so a deployment's override actually takes effect — the
    # policy object captured here is the default, not necessarily the one in
    # force.
    _ = POLICIES[policy_name]

    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        limiter = get_limiter()
        limiter.check(limiter.policy(policy_name), principal.user_id)
        return principal

    return dependency


def rate_limit_anonymous(policy_name: str) -> Any:
    """A dependency limiting an *unauthenticated* caller, keyed by address.

    Used on login and registration, where there is no principal yet — which is
    exactly where credential guessing happens.
    """

    _ = POLICIES[policy_name]

    def dependency(request: Request) -> None:
        limiter = get_limiter()
        limiter.check(limiter.policy(policy_name), client_identity(request))

    return dependency
