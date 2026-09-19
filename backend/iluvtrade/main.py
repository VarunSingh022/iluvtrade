"""The FastAPI application.

Assembled here rather than at import time so tests can build an app against a
temporary database, and so the worker pool's lifetime is tied to the app's.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from iluvtrade import __version__
from iluvtrade.alphalab_bridge import ALPHALAB_VERSION
from iluvtrade.api import errors
from iluvtrade.api.v1.router import api_v1
from iluvtrade.backtests.worker import BacktestWorkerPool
from iluvtrade.common.observability import (
    configure_logging,
    correlated,
    set_context,
)
from iluvtrade.config import get_settings
from iluvtrade.db.session import create_all

logger = logging.getLogger("iluvtrade")

#: Built frontend, when one exists. Absent in a backend-only deployment.
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Give every request an identifier and put it on the response.

    An inbound ``X-Request-ID`` is adopted rather than replaced, so a trace that
    started at a load balancer or a client stays one trace. It is truncated and
    sanitised first — it is caller-controlled text that ends up in log lines.

    The id is echoed on the response so a user reporting a problem can quote it,
    and an operator can find every line for that request with one query.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        inbound = request.headers.get("x-request-id", "")
        safe = "".join(c for c in inbound if c.isalnum() or c in "-_")[:64] or None

        with correlated(safe) as identifier:
            set_context(method=request.method, path=request.url.path)
            response = await call_next(request)
            response.headers["X-Request-ID"] = identifier
            return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Headers that reduce the blast radius of a mistake elsewhere.

    The CSP has no ``unsafe-inline`` for scripts: the frontend is a built bundle
    with no inline script, so an injected ``<script>`` has nowhere to execute
    even if output encoding were to fail somewhere.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'",
        )
        if get_settings().is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


def create_app(*, start_workers: bool = True, create_tables: bool = True) -> FastAPI:
    """Build the application."""

    settings = get_settings()
    configure_logging(settings.log_level, structured=settings.structured_logging)
    pool = BacktestWorkerPool()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if create_tables and settings.should_create_tables:
            create_all()
            logger.info("Created any missing tables (development mode).")
        elif create_tables:
            # Reached when a production deployment left ``create_tables`` on.
            # Saying so is better than silently doing nothing: the operator
            # needs to know the schema is the migrations' responsibility.
            logger.info(
                "Skipping table creation: this is a production environment, where the "
                "schema is owned by Alembic. Run 'alembic upgrade head' before starting."
            )
        if start_workers:
            pool.start()
        logger.info(
            "iluvtrade %s starting (environment=%s, alphalab=%s)",
            __version__,
            settings.environment,
            ALPHALAB_VERSION,
        )
        try:
            yield
        finally:
            if start_workers:
                pool.stop()

    app = FastAPI(
        title="iluvtrade",
        version=__version__,
        description=(
            "The application platform around AlphaLab, with the RedDesk strategy "
            "marketplace. AlphaLab is the quantitative engine; this API orchestrates it."
        ),
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    # Added last so it runs first: every other middleware's logging should
    # already carry the correlation id.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CorrelationMiddleware)
    if not settings.is_production:
        # The dev frontend runs on its own origin. Credentials are allowed only
        # for that explicit origin list — never with a wildcard, which browsers
        # refuse alongside credentials anyway and which would be wrong here.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    errors.install(app)
    app.include_router(api_v1)

    @app.get("/api/health", tags=["meta"])
    def health() -> JSONResponse:
        """Liveness, and what this deployment actually enforces.

        Deliberately more than "ok": the engine version, whether live trading is
        possible, whether rate limits are shared across instances, and whether
        any external notification channel is attached. Each is something an
        operator would otherwise have to assume.
        """

        from iluvtrade.platform.notifications import registered_channels
        from iluvtrade.platform.ratelimit import describe_enforcement

        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "environment": settings.environment,
                "engine": {"name": "alphalab", "version": ALPHALAB_VERSION},
                "live_trading_enabled": settings.live_trading_enabled,
                "rate_limiting": describe_enforcement().to_dict(),
                "notification_channels": list(registered_channels()),
                "payment_provider": settings.payment_provider,
            }
        )

    if FRONTEND_DIST.is_dir():
        assets = FRONTEND_DIST / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> Response:
            """Serve the built frontend, falling back to index for client routes."""

            if path.startswith("api/"):
                return JSONResponse(
                    {"error": {"code": "NotFound", "message": "No such route."}}, 404
                )
            candidate = FRONTEND_DIST / path
            if path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(FRONTEND_DIST / "index.html")

    return app


app = create_app()
