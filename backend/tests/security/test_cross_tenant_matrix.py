"""Every tenant-scoped operation, probed with another tenant's resource id.

``test_tenant_isolation.py`` proves the *mechanism* (the schema carries the
column, the helper refuses a model without it). This proves the *coverage*: one
organization builds every kind of resource, and another attempts every
operation on each of them.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_csv, register

pytestmark = pytest.mark.security


@pytest.fixture
def two_tenants(app):
    """Organization A with a full set of resources, and an unrelated B."""

    from fastapi.testclient import TestClient

    from iluvtrade.backtests.worker import BacktestWorkerPool

    headers = {"X-Requested-With": "XMLHttpRequest"}
    with TestClient(app) as a, TestClient(app) as b:
        register(a, "tenant-a@example.com")
        register(b, "tenant-b@example.com")

        dataset = a.post(
            "/api/v1/datasets/upload",
            files={"file": ("a.csv", make_csv(bars=60), "text/csv")},
            data={"dataset_name": "A data"},
            headers=headers,
        ).json()
        a.post(f"/api/v1/datasets/versions/{dataset['id']}/approve", headers=headers)

        strategy = a.post("/api/v1/strategies", json={"name": "A"}, headers=headers).json()
        published = a.post(
            f"/api/v1/strategies/{strategy['id']}/versions",
            json={"implementation_key": "buy_and_hold", "parameters": {"quantity": 3}},
            headers=headers,
        ).json()
        a.post(f"/api/v1/strategies/versions/{published['id']}/publish", headers=headers)
        draft = a.post(
            f"/api/v1/strategies/{strategy['id']}/versions",
            json={"implementation_key": "buy_and_hold", "parameters": {"quantity": 9}},
            headers=headers,
        ).json()

        job = a.post(
            "/api/v1/backtests",
            json={
                "dataset_version_id": dataset["id"],
                "strategy_version_id": published["id"],
            },
            headers=headers,
        ).json()
        BacktestWorkerPool(size=1).drain(timeout=120)

        session = a.post(
            "/api/v1/trading/sessions",
            json={
                "name": "A session",
                "mode": "paper",
                "strategy_version_id": published["id"],
                "dataset_version_id": dataset["id"],
            },
            headers=headers,
        ).json()
        broker = a.post(
            "/api/v1/brokers", json={"broker": "paper", "label": "A"}, headers=headers
        ).json()
        listing = a.post(
            "/api/v1/reddesk/listings",
            json={
                "strategy_id": strategy["id"],
                "title": "A listing",
                "description": "A strategy described at more than forty characters long.",
                "methodology": "m",
                "risk_disclosure": "r",
                "price_amount": "10",
                "licence_terms": "l",
            },
            headers=headers,
        ).json()

        yield {
            "a": a,
            "b": b,
            "headers": headers,
            "dataset_version": dataset["id"],
            "strategy": strategy["id"],
            "strategy_version": published["id"],
            "draft_version": draft["id"],
            "job": job["id"],
            "session": session["id"],
            "broker": broker["account_id"],
            "listing": listing["id"],
        }


def _probes(ids: dict) -> list[tuple[str, str, dict | None]]:
    return [
        ("GET", f"/api/v1/datasets/versions/{ids['dataset_version']}", None),
        ("GET", f"/api/v1/datasets/versions/{ids['dataset_version']}/preview", None),
        ("POST", f"/api/v1/datasets/versions/{ids['dataset_version']}/approve", None),
        ("POST", f"/api/v1/datasets/versions/{ids['dataset_version']}/reject", {"reason": "x"}),
        ("GET", f"/api/v1/strategies/{ids['strategy']}", None),
        (
            "POST",
            f"/api/v1/strategies/{ids['strategy']}/versions",
            {"implementation_key": "buy_and_hold", "parameters": {}},
        ),
        ("GET", f"/api/v1/strategies/versions/{ids['strategy_version']}", None),
        ("PATCH", f"/api/v1/strategies/versions/{ids['draft_version']}", {"changelog": "hijack"}),
        ("POST", f"/api/v1/strategies/versions/{ids['draft_version']}/publish", None),
        ("GET", f"/api/v1/backtests/{ids['job']}", None),
        ("GET", f"/api/v1/backtests/{ids['job']}/result", None),
        ("POST", f"/api/v1/backtests/{ids['job']}/cancel", None),
        ("GET", f"/api/v1/trading/sessions/{ids['session']}", None),
        ("POST", f"/api/v1/trading/sessions/{ids['session']}/start", None),
        ("POST", f"/api/v1/trading/sessions/{ids['session']}/pause", None),
        ("POST", f"/api/v1/trading/sessions/{ids['session']}/resume", None),
        ("POST", f"/api/v1/trading/sessions/{ids['session']}/stop", None),
        ("POST", f"/api/v1/trading/sessions/{ids['session']}/kill", {"reason": "hijack"}),
        ("GET", f"/api/v1/trading/sessions/{ids['session']}/orders", None),
        ("GET", f"/api/v1/trading/sessions/{ids['session']}/fills", None),
        ("GET", f"/api/v1/trading/sessions/{ids['session']}/positions", None),
        ("GET", f"/api/v1/trading/sessions/{ids['session']}/events", None),
        ("GET", f"/api/v1/brokers/{ids['broker']}", None),
        ("POST", f"/api/v1/brokers/{ids['broker']}/disconnect", None),
        ("POST", f"/api/v1/brokers/{ids['broker']}/zerodha/login-url", None),
        ("POST", f"/api/v1/brokers/{ids['broker']}/zerodha/authorize", {"request_token": "t"}),
        ("GET", f"/api/v1/reddesk/listings/{ids['listing']}/validate", None),
        ("POST", f"/api/v1/reddesk/listings/{ids['listing']}/submit", None),
        (
            "POST",
            f"/api/v1/reddesk/listings/{ids['listing']}/review",
            {"approve": True, "notes": ""},
        ),
        ("POST", f"/api/v1/reddesk/listings/{ids['listing']}/publish", None),
        ("POST", f"/api/v1/reddesk/listings/{ids['listing']}/withdraw", None),
        (
            "POST",
            f"/api/v1/reddesk/listings/{ids['listing']}/versions",
            {"strategy_version_id": ids["strategy_version"]},
        ),
    ]


def test_tenant_b_cannot_touch_any_of_tenant_as_resources(two_tenants) -> None:
    """Not readable, not mutable, not executable — for every resource kind."""

    b, headers = two_tenants["b"], two_tenants["headers"]
    leaks = []
    for method, path, body in _probes(two_tenants):
        response = b.request(method, path, json=body, headers=headers)
        if response.status_code not in (400, 403, 404):
            leaks.append((method, path, response.status_code, response.text[:120]))

    assert not leaks, f"Cross-tenant access succeeded: {leaks}"


def test_a_foreign_resource_is_reported_as_not_found(two_tenants) -> None:
    """Never "forbidden" — that would confirm the id exists.

    The broker routes are the reason this is a separate assertion: they once
    answered 400 because a deployment-configuration check ran before the
    ownership check, which told a prober something about a resource they had no
    right to address.
    """

    b, headers = two_tenants["b"], two_tenants["headers"]
    wrong = []
    for method, path, body in _probes(two_tenants):
        response = b.request(method, path, json=body, headers=headers)
        if response.status_code != 404:
            wrong.append((method, path, response.status_code))

    assert not wrong, f"These answered something other than 404 for a foreign id: {wrong}"


def test_tenant_b_sees_none_of_tenant_as_rows_in_any_collection(two_tenants) -> None:
    b = two_tenants["b"]
    populated = []
    for path in (
        "/api/v1/datasets",
        "/api/v1/strategies",
        "/api/v1/backtests",
        "/api/v1/trading/sessions",
        "/api/v1/brokers",
        "/api/v1/reddesk/my-listings",
        "/api/v1/reddesk/entitlements",
        "/api/v1/orders",
        "/api/v1/positions",
    ):
        rows = b.get(path).json()
        if rows:
            populated.append((path, len(rows)))
    assert not populated, f"B's collections contained rows: {populated}"


def test_tenant_bs_dashboard_and_portfolio_count_nothing_of_as(two_tenants) -> None:
    b = two_tenants["b"]
    dashboard = b.get("/api/v1/dashboard").json()
    assert dashboard["datasets"]["approved"] == 0
    assert dashboard["strategies"] == 0
    assert dashboard["backtests"]["completed"] == 0
    assert dashboard["sessions"]["total"] == 0

    portfolio = b.get("/api/v1/portfolio").json()
    for mode in ("paper", "live"):
        assert portfolio["modes"][mode]["sessions"] == []


def test_tenant_bs_audit_trail_contains_only_its_own_events(two_tenants) -> None:
    b = two_tenants["b"]
    actions = [event["action"] for event in b.get("/api/v1/audit").json()]
    assert actions, "B should see its own registration"
    assert all(action.startswith("user.") for action in actions), actions


def test_tenant_a_can_still_use_its_own_resources(two_tenants) -> None:
    """The isolation must not be achieved by breaking the owner's access."""

    a = two_tenants["a"]
    assert a.get(f"/api/v1/datasets/versions/{two_tenants['dataset_version']}").status_code == 200
    assert a.get(f"/api/v1/strategies/{two_tenants['strategy']}").status_code == 200
    assert a.get(f"/api/v1/backtests/{two_tenants['job']}").status_code == 200
    assert a.get(f"/api/v1/trading/sessions/{two_tenants['session']}").status_code == 200
    assert a.get(f"/api/v1/brokers/{two_tenants['broker']}").status_code == 200
    assert len(a.get("/api/v1/datasets").json()) == 1
