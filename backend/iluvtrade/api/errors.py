"""Turning domain exceptions into HTTP responses, in one place.

Every service raises a domain exception with a message written for a human. The
handlers here map exception *type* to status code, so no route has to remember
whether "not found" is 404 and no service has to import ``HTTPException``.

The error body is always ``{"error": {"code": ..., "message": ...}}`` — a shape
the frontend can branch on without parsing prose.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from iluvtrade.backtests.service import BacktestSubmissionError
from iluvtrade.brokers.crypto import CredentialError
from iluvtrade.brokers.service import BrokerError
from iluvtrade.data.ingest import IngestError
from iluvtrade.platform.accounts import AuthError, RegistrationError
from iluvtrade.platform.invitations import InvitationError
from iluvtrade.platform.mfa import MfaRequired
from iluvtrade.platform.passwords import DeliveryUnavailable, ResetError
from iluvtrade.platform.ratelimit import RateLimitError
from iluvtrade.platform.security import WeakPasswordError
from iluvtrade.platform.tenancy import NotFoundError, TenancyError
from iluvtrade.reddesk.entitlements import EntitlementError
from iluvtrade.reddesk.marketplace import ListingError
from iluvtrade.strategies.service import StrategyError, VersionFrozenError
from iluvtrade.trading.sessions import SessionError

logger = logging.getLogger("iluvtrade.api")

#: Status code → the ``error.code`` a client sees for framework-raised errors.
_HTTP_CODES: dict[int, str] = {
    400: "BadRequest",
    401: "NotAuthenticated",
    403: "Forbidden",
    404: "NotFoundError",
    409: "Conflict",
    413: "PayloadTooLarge",
    422: "ValidationError",
    429: "RateLimited",
    503: "ServiceUnavailable",
}

#: Starlette renamed this constant; read whichever this version defines.
_UNPROCESSABLE: int = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)

_STATUS_MAP: tuple[tuple[type[Exception], int], ...] = (
    (NotFoundError, status.HTTP_404_NOT_FOUND),
    (AuthError, status.HTTP_401_UNAUTHORIZED),
    (EntitlementError, status.HTTP_403_FORBIDDEN),
    (PermissionError, status.HTTP_403_FORBIDDEN),
    (VersionFrozenError, status.HTTP_409_CONFLICT),
    (CredentialError, status.HTTP_409_CONFLICT),
    (RegistrationError, status.HTTP_400_BAD_REQUEST),
    (InvitationError, status.HTTP_400_BAD_REQUEST),
    (ResetError, status.HTTP_400_BAD_REQUEST),
    # Not 400: the request was fine, the deployment cannot carry it. A
    # client distinguishing these is the difference between "try again"
    # and "this will never work here, ask an administrator".
    (DeliveryUnavailable, status.HTTP_503_SERVICE_UNAVAILABLE),
    (WeakPasswordError, status.HTTP_400_BAD_REQUEST),
    (IngestError, status.HTTP_400_BAD_REQUEST),
    (StrategyError, status.HTTP_400_BAD_REQUEST),
    (ListingError, status.HTTP_400_BAD_REQUEST),
    (SessionError, status.HTTP_400_BAD_REQUEST),
    (BrokerError, status.HTTP_400_BAD_REQUEST),
    (BacktestSubmissionError, status.HTTP_400_BAD_REQUEST),
    (TenancyError, status.HTTP_500_INTERNAL_SERVER_ERROR),
)


def _payload(exc: Exception, code: str | None = None) -> dict[str, Any]:
    return {"error": {"code": code or type(exc).__name__, "message": str(exc)}}


def install(app: FastAPI) -> None:
    """Register one handler per mapped exception type."""

    for exception_type, status_code in _STATUS_MAP:

        def _make(status_code: int = status_code) -> Callable[..., Any]:
            async def handler(_request: Request, exc: Exception) -> JSONResponse:
                if status_code >= 500:
                    logger.exception("Unhandled domain error")
                return JSONResponse(status_code=status_code, content=_payload(exc))

            return handler

        app.add_exception_handler(exception_type, _make())

    @app.exception_handler(RateLimitError)
    async def _rate_limited(_request: Request, exc: RateLimitError) -> JSONResponse:
        """429 with ``Retry-After``.

        The header is the point: a client that backs off correctly needs to be
        told how long, and a bare 429 invites an immediate retry loop.
        """

        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "error": {
                    "code": "RateLimited",
                    "message": str(exc),
                    "policy": exc.policy,
                    "retry_after_seconds": exc.retry_after_seconds,
                }
            },
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )

    @app.exception_handler(MfaRequired)
    async def _mfa_required(_request: Request, exc: MfaRequired) -> JSONResponse:
        """401, but not "wrong password".

        The password *was* correct. A client that cannot tell this apart from a
        credential failure shows "invalid email or password" to someone whose
        password is fine, and the account becomes unreachable through the UI.

        Said twice on purpose. The header is the conventional signal; the
        ``error.code`` is the one that survives, because a cross-origin client
        cannot read a response header unless CORS is configured to expose it,
        and a proxy is free to strip one. The body always arrives.
        """

        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content=_payload(exc, "MfaRequired"),
            headers={"X-MFA-Required": "true"},
        )

    @app.exception_handler(OperationalError)
    async def _database_busy(_request: Request, exc: OperationalError) -> JSONResponse:
        """A transient database contention failure, told apart from a real fault.

        SQLite serialises writers, and a long transaction — dataset ingestion
        holds one for the length of the parse — can make a concurrent write
        exceed ``busy_timeout`` and fail with "database is locked". That is
        retryable, and answering it with a generic 500 tells the user the
        opposite: that something is broken and trying again is pointless.

        Only the contention case is remapped. Every other ``OperationalError``
        is a genuine fault and falls through to the 500 handler, where it is
        logged with its detail and the client is told nothing specific.
        """

        text = str(exc.orig if exc.orig is not None else exc).lower()
        if "database is locked" not in text and "database table is locked" not in text:
            raise exc

        logger.warning("Database contention: %s", text)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "DatabaseBusy",
                    "message": (
                        "The database is busy with another write. Nothing was changed; "
                        "try again in a moment."
                    ),
                }
            },
            headers={"Retry-After": "2"},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Give framework-raised errors the same body as domain errors.

        ``HTTPException`` renders as ``{"detail": ...}`` by default, so a 403
        from the CSRF check and a 403 from an entitlement refusal would have
        different shapes — and a client branching on ``error.code`` would break
        on whichever it met second. One envelope, always.
        """

        code = _HTTP_CODES.get(exc.status_code, "HTTPError")
        detail = exc.detail if isinstance(exc.detail, str) else "Request refused."
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": detail}},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        """A malformed request body, in the same envelope, with the offending fields."""

        problems = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ())[1:]) or "body",
                "message": error.get("msg", "invalid"),
            }
            for error in exc.errors()[:20]
        ]
        summary = "; ".join(f"{p['field']}: {p['message']}" for p in problems)
        return JSONResponse(
            status_code=_UNPROCESSABLE,
            content={
                "error": {
                    "code": "ValidationError",
                    "message": summary or "The request body is not valid.",
                    "fields": problems,
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        # The message is deliberately generic: an internal error's text can
        # carry a query, a path or a value that should not reach a client. The
        # detail goes to the log, which is where an operator looks.
        logger.exception("Unhandled error")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "InternalError",
                    "message": "Something went wrong. The failure has been logged.",
                }
            },
        )
