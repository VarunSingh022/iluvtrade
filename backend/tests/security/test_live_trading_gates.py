"""Live trading must be unreachable, and unreachable for structural reasons.

The strongest form of "paper cannot become live" is not a check that could be
forgotten — it is that the live path is never constructed. These tests assert
that property against the source, and then assert the runtime gates on top of it.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from iluvtrade.db.models.trading import TradingMode
from tests.conftest import make_csv, register

pytestmark = pytest.mark.security

PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "iluvtrade"


# --- structural ------------------------------------------------------------


def test_nothing_in_the_application_constructs_the_live_execution_mode() -> None:
    """``ExecutionMode.LIVE`` appears nowhere. That is the guarantee.

    AlphaLab derives order routing from the mode — ``LIVE`` routes ``EXTERNAL``,
    every other mode routes ``SIMULATED`` — and ``RunConfig`` forces the
    pipeline's routing to agree with its own mode. So as long as this repository
    never names ``LIVE``, no run it configures can route to a venue, regardless
    of what any request contains.
    """

    offenders: dict[str, list[int]] = {}
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        lines = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr == "LIVE"
            and isinstance(node.value, ast.Name | ast.Attribute)
            and "ExecutionMode" in ast.dump(node.value)
        ]
        if lines:
            offenders[str(path.relative_to(PACKAGE))] = lines

    assert not offenders, (
        f"ExecutionMode.LIVE is constructed in {offenders}. Live trading is not "
        "implemented; naming this mode would route orders to a venue."
    )


def test_the_application_never_sets_execution_routing_itself() -> None:
    """Routing is AlphaLab's derivation from the mode, not ours to assign.

    Setting ``routing=`` anywhere would let a config disagree with its own mode —
    a paper run that routes externally, or a live run that silently simulates.
    """

    offenders: list[str] = []
    for path in PACKAGE.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "routing=" in source:
            offenders.append(str(path.relative_to(PACKAGE)))
    assert not offenders, f"These modules assign execution routing: {offenders}"


def test_alphalab_derives_routing_from_mode() -> None:
    """The property the tests above rely on, asserted against the engine."""

    from iluvtrade.alphalab_bridge.runconfig import ExecutionMode

    assert ExecutionMode.PAPER.routing.name == "SIMULATED"
    assert ExecutionMode.BACKTEST.routing.name == "SIMULATED"
    assert ExecutionMode.REPLAY.routing.name == "SIMULATED"
    assert ExecutionMode.LIVE.routing.name == "EXTERNAL"


def test_the_paper_runner_refuses_a_live_session(db) -> None:
    """Refused outright rather than simulated.

    Running a live session through the paper runner would produce simulated
    fills labelled as live — the worst available failure.
    """

    from iluvtrade.db.models.trading import SessionStatus, TradingSession
    from iluvtrade.strategies import service as strategies
    from iluvtrade.trading.runner import LiveRoutingUnavailable, run_session
    from tests.conftest import make_principal

    principal = make_principal(db)
    strategy = strategies.create_strategy(db, principal, name="S")
    version = strategies.publish(
        db,
        principal,
        strategies.create_version(
            db,
            principal,
            strategy_id=strategy.id,
            implementation_key="buy_and_hold",
            parameters={"quantity": 1},
        ).id,
    )

    # Written directly, bypassing ``sessions.create`` and its gates entirely —
    # which is the point. Even a LIVE row that somehow reached the database is
    # refused by the runner rather than executed against the simulator.
    session_row = TradingSession(
        organization_id=principal.organization_id,
        mode=TradingMode.LIVE,
        status=SessionStatus.STARTING,
        name="live",
        created_by_user_id=principal.user_id,
        strategy_version_id=version.id,
        starting_cash="1000",
        seed=1,
    )
    db.add(session_row)
    db.flush()
    session_id = session_row.id
    db.commit()

    with pytest.raises(LiveRoutingUnavailable, match="paper sessions only"):
        run_session(session_id)


# --- the three gates --------------------------------------------------------


def _paper_inputs(client, headers) -> tuple[str, str]:
    version = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=60), "text/csv")},
        headers=headers,
    ).json()
    client.post(f"/api/v1/datasets/versions/{version['id']}/approve", headers=headers)
    strategy = client.post("/api/v1/strategies", json={"name": "S"}, headers=headers).json()
    draft = client.post(
        f"/api/v1/strategies/{strategy['id']}/versions",
        json={"implementation_key": "buy_and_hold", "parameters": {"quantity": 1}},
        headers=headers,
    ).json()
    client.post(f"/api/v1/strategies/versions/{draft['id']}/publish", headers=headers)
    return version["id"], draft["id"]


def test_gate_one_deployment_disabled(client, headers) -> None:
    register(client, "a@example.com")
    _, strategy_version_id = _paper_inputs(client, headers)

    response = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "live",
            "mode": "live",
            "strategy_version_id": strategy_version_id,
            "broker_account_id": "x",
            "live_confirmed": True,
        },
        headers=headers,
    )
    assert response.status_code == 400
    assert "disabled in this deployment" in response.json()["error"]["message"]


def test_gate_two_user_not_enabled(client, headers, monkeypatch) -> None:
    """With the deployment gate open, the user's own setting still refuses."""

    from iluvtrade.config import get_settings

    register(client, "a@example.com")
    _, strategy_version_id = _paper_inputs(client, headers)

    monkeypatch.setenv("ILUVTRADE_LIVE_TRADING_ENABLED", "true")
    get_settings.cache_clear()
    try:
        response = client.post(
            "/api/v1/trading/sessions",
            json={
                "name": "live",
                "mode": "live",
                "strategy_version_id": strategy_version_id,
                "broker_account_id": "x",
                "live_confirmed": True,
            },
            headers=headers,
        )
        assert response.status_code == 400
        assert "not enabled on your account" in response.json()["error"]["message"]
    finally:
        get_settings.cache_clear()


def test_gate_three_session_not_confirmed(client, headers, monkeypatch) -> None:
    """With both other gates open, an unconfirmed session is still refused."""

    from iluvtrade.config import get_settings

    register(client, "a@example.com")
    _, strategy_version_id = _paper_inputs(client, headers)

    monkeypatch.setenv("ILUVTRADE_LIVE_TRADING_ENABLED", "true")
    get_settings.cache_clear()
    try:
        client.patch("/api/v1/auth/me", json={"live_trading_enabled": True}, headers=headers)
        response = client.post(
            "/api/v1/trading/sessions",
            json={
                "name": "live",
                "mode": "live",
                "strategy_version_id": strategy_version_id,
                "broker_account_id": "x",
                "live_confirmed": False,
            },
            headers=headers,
        )
        assert response.status_code == 400
        assert "explicitly confirmed" in response.json()["error"]["message"]
    finally:
        get_settings.cache_clear()


def test_a_paper_broker_cannot_back_a_live_session(client, headers, monkeypatch) -> None:
    """The fourth condition: a simulator must never be labelled a venue.

    All three gates are open here, so this asserts the check that remains.
    """

    from iluvtrade.config import get_settings

    register(client, "a@example.com")
    _, strategy_version_id = _paper_inputs(client, headers)
    broker = client.post(
        "/api/v1/brokers", json={"broker": "paper", "label": "sim"}, headers=headers
    ).json()

    monkeypatch.setenv("ILUVTRADE_LIVE_TRADING_ENABLED", "true")
    get_settings.cache_clear()
    try:
        client.patch("/api/v1/auth/me", json={"live_trading_enabled": True}, headers=headers)
        response = client.post(
            "/api/v1/trading/sessions",
            json={
                "name": "live",
                "mode": "live",
                "strategy_version_id": strategy_version_id,
                "broker_account_id": broker["account_id"],
                "live_confirmed": True,
            },
            headers=headers,
        )
        assert response.status_code == 400
        assert "paper broker cannot be used" in response.json()["error"]["message"]
    finally:
        get_settings.cache_clear()


# --- a paper session cannot become live -------------------------------------


def test_no_endpoint_can_change_a_session_mode(client, headers) -> None:
    """There is no route that mutates ``mode``, and none accepts it.

    A paper session becoming live by editing a request field is the attack this
    forecloses. The only writer of ``mode`` is session creation, which runs the
    gates above.
    """

    register(client, "a@example.com")
    dataset_version_id, strategy_version_id = _paper_inputs(client, headers)

    session = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "paper",
            "mode": "paper",
            "strategy_version_id": strategy_version_id,
            "dataset_version_id": dataset_version_id,
        },
        headers=headers,
    ).json()
    assert session["mode"] == "paper"

    spec = client.get("/api/openapi.json").json()
    session_routes = [p for p in spec["paths"] if "/trading/sessions/{session_id}" in p]
    for path in session_routes:
        for method, operation in spec["paths"][path].items():
            if method.upper() not in {"PATCH", "PUT", "POST"}:
                continue
            body = operation.get("requestBody", {})
            assert "mode" not in str(body), f"{method.upper()} {path} accepts a mode field"

    # And the session is still paper after every control operation.
    for action in ("start", "pause", "stop"):
        client.post(f"/api/v1/trading/sessions/{session['id']}/{action}", headers=headers)
    assert client.get(f"/api/v1/trading/sessions/{session['id']}").json()["mode"] == "paper"


def test_a_session_created_as_paper_reports_simulated_routing(client, headers) -> None:
    """The mode on the row is what the runner reads, and it never says live."""

    register(client, "a@example.com")
    dataset_version_id, strategy_version_id = _paper_inputs(client, headers)
    session = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "paper",
            "mode": "paper",
            "strategy_version_id": strategy_version_id,
            "dataset_version_id": dataset_version_id,
        },
        headers=headers,
    ).json()

    assert session["mode"] == TradingMode.PAPER.value
    assert session["broker_account_id"] is None
    assert session["live_confirmed_at"] is None if "live_confirmed_at" in session else True
