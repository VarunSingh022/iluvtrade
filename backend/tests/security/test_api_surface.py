"""The frozen API surface.

A release candidate's API is a promise. This module writes that promise down as
a literal list and fails when reality and the list disagree — in either
direction, because a route that quietly *disappears* breaks a client just as
thoroughly as one that quietly appears.

It is not busywork. During this audit two endpoints turned out to be reachable
by nobody — no screen, no demo, no test — while the data one of them produced
was already being rendered. A surface nobody enumerates is a surface nobody
notices things about.

Updating the list is the correct response to an intentional change. Doing it
without also asking "does anything call this?" is the mistake the list exists
to make visible.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.security

#: Every route this release exposes, as "METHOD /path".
FROZEN_SURFACE: frozenset[str] = frozenset(
    [
        "GET /api/health",
        "GET /api/v1/audit",
        "GET /api/v1/audit/verify",
        "GET /api/v1/auth/me",
        "GET /api/v1/auth/mfa",
        "GET /api/v1/auth/organizations",
        "GET /api/v1/auth/password-reset",
        "GET /api/v1/backtests",
        "GET /api/v1/backtests/{job_id}",
        "GET /api/v1/backtests/{job_id}/result",
        "GET /api/v1/brokers",
        "GET /api/v1/brokers/supported",
        "GET /api/v1/brokers/{account_id}",
        "GET /api/v1/dashboard",
        "GET /api/v1/datasets",
        "GET /api/v1/datasets/versions/{version_id}",
        "GET /api/v1/datasets/versions/{version_id}/preview",
        "GET /api/v1/notifications",
        "GET /api/v1/orders",
        "GET /api/v1/organizations/invitations",
        "GET /api/v1/organizations/members",
        "GET /api/v1/portfolio",
        "GET /api/v1/positions",
        "GET /api/v1/reddesk/billing-status",
        "GET /api/v1/reddesk/discover",
        "GET /api/v1/reddesk/entitlements",
        "GET /api/v1/reddesk/listings/{listing_id}",
        "GET /api/v1/reddesk/listings/{listing_id}/validate",
        "GET /api/v1/reddesk/my-listings",
        "GET /api/v1/strategies",
        "GET /api/v1/strategies/implementations",
        "GET /api/v1/strategies/versions/{version_id}",
        "GET /api/v1/strategies/{strategy_id}",
        "GET /api/v1/trading/sessions",
        "GET /api/v1/trading/sessions/{session_id}",
        "GET /api/v1/trading/sessions/{session_id}/events",
        "GET /api/v1/trading/sessions/{session_id}/fills",
        "GET /api/v1/trading/sessions/{session_id}/orders",
        "GET /api/v1/trading/sessions/{session_id}/positions",
        "PATCH /api/v1/auth/me",
        "PATCH /api/v1/strategies/versions/{version_id}",
        "POST /api/v1/auth/login",
        "POST /api/v1/auth/logout",
        "POST /api/v1/auth/mfa/confirm",
        "POST /api/v1/auth/mfa/disable",
        "POST /api/v1/auth/mfa/enrol",
        "POST /api/v1/auth/mfa/recovery-codes",
        "POST /api/v1/auth/password-reset/confirm",
        "POST /api/v1/auth/password-reset/request",
        "POST /api/v1/auth/register",
        "POST /api/v1/auth/switch-organization",
        "POST /api/v1/backtests",
        "POST /api/v1/backtests/{job_id}/cancel",
        "POST /api/v1/brokers",
        "POST /api/v1/brokers/{account_id}/disconnect",
        "POST /api/v1/brokers/{account_id}/zerodha/authorize",
        "POST /api/v1/brokers/{account_id}/zerodha/login-url",
        "POST /api/v1/datasets/fetch",
        "POST /api/v1/datasets/inspect",
        "POST /api/v1/datasets/upload",
        "POST /api/v1/datasets/versions/{version_id}/approve",
        "POST /api/v1/datasets/versions/{version_id}/reject",
        "POST /api/v1/notifications/read-all",
        "POST /api/v1/organizations/invitations",
        "POST /api/v1/organizations/invitations/accept",
        "POST /api/v1/organizations/invitations/{invitation_id}/revoke",
        "POST /api/v1/reddesk/entitlements/{entitlement_id}/advance",
        "POST /api/v1/reddesk/listings",
        "POST /api/v1/reddesk/listings/{listing_id}/publish",
        "POST /api/v1/reddesk/listings/{listing_id}/rate",
        "POST /api/v1/reddesk/listings/{listing_id}/review",
        "POST /api/v1/reddesk/listings/{listing_id}/submit",
        "POST /api/v1/reddesk/listings/{listing_id}/versions",
        "POST /api/v1/reddesk/listings/{listing_id}/withdraw",
        "POST /api/v1/reddesk/purchases",
        "POST /api/v1/strategies",
        "POST /api/v1/strategies/versions/{version_id}/publish",
        "POST /api/v1/strategies/{strategy_id}/versions",
        "POST /api/v1/trading/sessions",
        "POST /api/v1/trading/sessions/{session_id}/kill",
        "POST /api/v1/trading/sessions/{session_id}/pause",
        "POST /api/v1/trading/sessions/{session_id}/resume",
        "POST /api/v1/trading/sessions/{session_id}/start",
        "POST /api/v1/trading/sessions/{session_id}/stop",
    ]
)

#: Routes that answer with something other than a declared response model, and
#: why. Anything not listed here must declare one: a response model is an
#: allowlist, and it is what stops a column added to an ORM object from
#: reaching a client by accident.
UNMODELLED = {
    "GET /api/health",
    # 204 No Content — there is no body to model.
    "POST /api/v1/auth/logout",
    "POST /api/v1/notifications/read-all",
    # 202 Accepted with an empty body, deliberately identical for every
    # address so it cannot be used to enumerate accounts.
    "POST /api/v1/auth/password-reset/request",
    # A small, stable literal: the created review's id and rating.
    "POST /api/v1/reddesk/listings/{listing_id}/rate",
}


def _surface(client) -> set[str]:
    spec = client.get("/api/openapi.json").json()
    return {
        f"{method.upper()} {path}"
        for path, operations in spec["paths"].items()
        for method in operations
        if method.upper() not in {"HEAD", "OPTIONS"}
    }


def test_the_api_surface_matches_the_frozen_list(client) -> None:
    live = _surface(client)
    added = sorted(live - FROZEN_SURFACE)
    removed = sorted(FROZEN_SURFACE - live)

    assert not added, (
        f"These routes are not in the frozen surface: {added}. If the addition is "
        "intended, add them to FROZEN_SURFACE — and check that something actually "
        "calls them."
    )
    assert not removed, (
        f"These routes are in the frozen surface but no longer exist: {removed}. "
        "Removing a route breaks every client that calls it; if that is intended, "
        "remove it from FROZEN_SURFACE in the same change."
    )


def test_the_frozen_surface_is_not_trivially_small() -> None:
    """Guards against the list being emptied to make the test above pass."""

    assert len(FROZEN_SURFACE) >= 80


def test_every_route_declares_a_response_model(client) -> None:
    """A response model is an allowlist, not documentation."""

    spec = client.get("/api/openapi.json").json()
    bare = []
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if method.upper() in {"HEAD", "OPTIONS"}:
                continue
            key = f"{method.upper()} {path}"
            if key in UNMODELLED:
                continue
            for status, response in (operation.get("responses") or {}).items():
                if not status.startswith("2"):
                    continue
                schema = (response.get("content") or {}).get("application/json", {}).get("schema")
                if not schema or schema == {}:
                    bare.append(key)
    assert not bare, (
        f"These routes return an undeclared shape: {sorted(set(bare))}. Declare a "
        "response model, or add the route to UNMODELLED with the reason."
    )


def test_the_unmodelled_allowlist_stays_small() -> None:
    assert len(UNMODELLED) <= 6, (
        "More than a handful of routes returning an undeclared shape means the "
        "response-model rule is not really in force."
    )
