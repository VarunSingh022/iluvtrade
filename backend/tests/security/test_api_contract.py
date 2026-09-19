"""The API contract, asserted against every route rather than a sample.

Each test enumerates the OpenAPI schema or probes every route, so a route added
later is covered without anyone remembering to extend this file. That is the
difference between "one endpoint is protected" and "the application is".
"""

from __future__ import annotations

import pytest

from tests.conftest import register

pytestmark = pytest.mark.security

#: Routes that must be reachable without a session, and why.
PUBLIC = {
    ("GET", "/api/health"),
    ("POST", "/api/v1/auth/register"),
    ("POST", "/api/v1/auth/login"),
}

_PATH_PARAMS = (
    "{version_id}",
    "{job_id}",
    "{listing_id}",
    "{entitlement_id}",
    "{session_id}",
    "{account_id}",
    "{strategy_id}",
)


def _routes(client) -> list[tuple[str, str]]:
    spec = client.get("/api/openapi.json").json()
    return sorted(
        (method.upper(), path)
        for path, operations in spec["paths"].items()
        for method in operations
        if method.upper() not in {"HEAD", "OPTIONS"}
    )


def _concrete(path: str) -> str:
    for token in _PATH_PARAMS:
        path = path.replace(token, "00000000-0000-0000-0000-000000000000")
    return path


# --- authentication ---------------------------------------------------------


def test_every_route_except_the_public_ones_refuses_an_anonymous_caller(app) -> None:
    from fastapi.testclient import TestClient

    with TestClient(app) as anonymous:
        routes = _routes(anonymous)
        reachable = []
        for method, path in routes:
            if (method, path) in PUBLIC:
                continue
            response = anonymous.request(
                method,
                _concrete(path),
                json={} if method in {"POST", "PATCH", "PUT"} else None,
            )
            if response.status_code not in (401, 403, 405):
                reachable.append((method, path, response.status_code))

    assert not reachable, (
        f"These routes answered an unauthenticated caller: {reachable}. "
        "Every route except registration, login and health needs a session."
    )
    assert len(routes) > 50, "the route list looks suspiciously short"


def test_every_state_changing_route_requires_the_csrf_header(client, headers) -> None:
    """One protected endpoint does not prove the application is protected."""

    register(client, "csrf@example.com")
    unprotected = []
    for method, path in _routes(client):
        if method not in {"POST", "PATCH", "PUT", "DELETE"} or (method, path) in PUBLIC:
            continue
        response = client.request(method, _concrete(path), json={})
        if not (response.status_code == 403 and "X-Requested-With" in response.text):
            unprotected.append((method, path, response.status_code))

    assert not unprotected, f"Not CSRF-protected: {unprotected}"
    _ = headers


# --- the error envelope -----------------------------------------------------


def test_every_error_uses_the_documented_envelope(client, headers) -> None:
    """A client branching on ``error.code`` must never meet a different shape."""

    register(client, "env@example.com")
    checked = 0
    wrong = []
    for method, path in _routes(client):
        if (method, path) in PUBLIC:
            continue
        response = client.request(
            method,
            _concrete(path),
            json={} if method in {"POST", "PATCH", "PUT"} else None,
            headers=headers,
        )
        if response.status_code < 400:
            continue
        checked += 1
        body = response.json()
        if not (
            isinstance(body, dict)
            and isinstance(body.get("error"), dict)
            and isinstance(body["error"].get("code"), str)
            and isinstance(body["error"].get("message"), str)
        ):
            wrong.append((method, path, response.status_code, str(body)[:100]))

    assert checked > 20, "too few error responses were exercised to be meaningful"
    assert not wrong, f"These errors did not use the envelope: {wrong}"


def test_an_internal_error_does_not_leak_its_detail(app, monkeypatch) -> None:
    """An exception's text can carry a query, a path or a value.

    Raised from inside a real endpoint's service call rather than a route added
    for the test, so this exercises the handler the application actually uses.
    """

    from fastapi.testclient import TestClient

    from iluvtrade.platform import notifications

    marker = "SENSITIVE-INTERNAL-DETAIL-a1b2c3"

    def explode(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError(f"connection string postgres://user:pw@host/{marker}")

    monkeypatch.setattr(notifications, "unread_count", explode)

    with TestClient(app, raise_server_exceptions=False) as client:
        register(client, "boom@example.com")
        response = client.get("/api/v1/dashboard")

    assert response.status_code == 500
    assert marker not in response.text
    assert "postgres://" not in response.text
    body = response.json()
    assert body["error"]["code"] == "InternalError"
    assert "went wrong" in body["error"]["message"]


def test_validation_errors_name_the_offending_fields(client) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "not-an-email", "password": "short", "display_name": ""},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "ValidationError"
    assert body["error"]["fields"]
    assert {f["field"] for f in body["error"]["fields"]} & {"email", "password"}


# --- request validation -----------------------------------------------------


def test_request_models_reject_unknown_fields(client, headers) -> None:
    """A typo'd parameter must be an error, not a silently ignored setting."""

    register(client, "strict@example.com")
    response = client.post(
        "/api/v1/strategies",
        json={"name": "S", "vizibility": "marketplace"},
        headers=headers,
    )
    assert response.status_code == 422
    assert "vizibility" in response.text


def test_path_parameters_cannot_traverse(client, headers) -> None:
    """A crafted id must not reach the filesystem or another resource."""

    register(client, "trav@example.com")
    for candidate in ("../../etc/passwd", "..%2f..%2fetc%2fpasswd", "%00", "' OR '1'='1"):
        response = client.get(f"/api/v1/datasets/versions/{candidate}")
        assert response.status_code in (400, 404, 422), (
            f"{candidate!r} produced {response.status_code}"
        )
        assert "root:" not in response.text


# --- responses --------------------------------------------------------------


def test_no_response_anywhere_contains_a_credential_field(client, headers) -> None:
    """Swept across every GET, not only the broker endpoints."""

    register(client, "sweep@example.com")
    client.post("/api/v1/brokers", json={"broker": "paper", "label": "b"}, headers=headers)

    forbidden = (
        "password_hash",
        "encrypted_credentials",
        "access_token",
        "api_secret",
        "token_hash",
        "request_token",
    )
    leaks = []
    for method, path in _routes(client):
        if method != "GET":
            continue
        response = client.get(_concrete(path))
        if response.status_code >= 400:
            continue
        for marker in forbidden:
            if marker in response.text:
                leaks.append((path, marker))
    assert not leaks, f"Credential-shaped fields appeared in responses: {leaks}"


def test_no_response_schema_declares_a_credential_field(client) -> None:
    """Checked against the schema too, so an unexercised route is still covered."""

    spec = client.get("/api/openapi.json").json()
    forbidden = {
        "password",
        "password_hash",
        "access_token",
        "api_secret",
        "token_hash",
        "encrypted_credentials",
        "secret",
    }
    offenders = []
    for name, schema in spec.get("components", {}).get("schemas", {}).items():
        # Request models legitimately carry a password; response models must not.
        if name.endswith("Request"):
            continue
        for field in schema.get("properties") or {}:
            if field in forbidden:
                offenders.append(f"{name}.{field}")
    assert not offenders, f"Response schemas declare credential fields: {offenders}"


# --- bounded queries --------------------------------------------------------


def test_list_endpoints_accept_and_respect_a_limit(client, headers) -> None:
    """An unbounded list is a slow denial of service on a large tenant."""

    register(client, "bounded@example.com")
    spec = client.get("/api/openapi.json").json()

    # Endpoints that return a collection and take a caller-supplied limit.
    limited = []
    for path, operations in spec["paths"].items():
        get = operations.get("get")
        if not get:
            continue
        params = {p["name"] for p in get.get("parameters", [])}
        if "limit" in params:
            limited.append(path)

    assert {"/api/v1/backtests", "/api/v1/audit", "/api/v1/orders", "/api/v1/notifications"} <= set(
        limited
    )

    # And an absurd limit must be clamped rather than honoured.
    response = client.get("/api/v1/audit?limit=100000")
    assert response.status_code == 200
    assert len(response.json()) <= 500
