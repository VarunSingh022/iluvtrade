"""The product's main paths, through the real HTTP API."""

from __future__ import annotations

import pytest

from tests.conftest import make_csv, register

pytestmark = pytest.mark.integration


def _approved_dataset(client, headers, *, symbols=("ACME",), bars=140) -> str:
    created = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(symbols=symbols, bars=bars), "text/csv")},
        data={"dataset_name": "Test data"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    version_id = created.json()["id"]
    assert (
        client.post(f"/api/v1/datasets/versions/{version_id}/approve", headers=headers).status_code
        == 200
    )
    return version_id


def _published_strategy(client, headers, *, key="moving_average_crossover", params=None) -> str:
    strategy = client.post(
        "/api/v1/strategies", json={"name": "Test strategy"}, headers=headers
    ).json()
    draft = client.post(
        f"/api/v1/strategies/{strategy['id']}/versions",
        json={
            "implementation_key": key,
            "parameters": params or {"fast": 5, "slow": 20, "quantity": 10},
        },
        headers=headers,
    )
    assert draft.status_code == 201, draft.text
    published = client.post(
        f"/api/v1/strategies/versions/{draft.json()['id']}/publish", headers=headers
    )
    assert published.status_code == 200
    return str(published.json()["id"])


# --- authentication ---------------------------------------------------------


def test_an_unauthenticated_request_is_refused(client) -> None:
    for path in ("/api/v1/datasets", "/api/v1/strategies", "/api/v1/trading/sessions"):
        assert client.get(path).status_code == 401


def test_a_cookie_authenticated_write_needs_the_csrf_header(client) -> None:
    """A cross-origin form post cannot set X-Requested-With without a preflight."""

    register(client, "a@example.com")
    without = client.post("/api/v1/strategies", json={"name": "No header"})
    assert without.status_code == 403
    assert "X-Requested-With" in without.json()["error"]["message"]

    with_header = client.post(
        "/api/v1/strategies",
        json={"name": "With header"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert with_header.status_code == 201


def test_a_bearer_token_needs_no_csrf_header(client) -> None:
    """A header token is not attached by the browser, so it is not forgeable."""

    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "b@example.com",
            "password": "correct-horse-battery-staple",
            "display_name": "B",
        },
    )
    token = response.json()["token"]
    client.cookies.clear()
    created = client.post(
        "/api/v1/strategies",
        json={"name": "Bearer"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert created.status_code == 201


def test_logging_out_revokes_the_session(client, headers) -> None:
    register(client, "c@example.com")
    assert client.get("/api/v1/auth/me").status_code == 200
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401


def test_a_weak_password_is_refused(client) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "d@example.com", "password": "short", "display_name": "D"},
    )
    assert response.status_code == 422


# --- data workspace ---------------------------------------------------------


def test_a_dataset_cannot_be_used_before_approval(client, headers) -> None:
    register(client, "e@example.com")
    created = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(), "text/csv")},
        headers=headers,
    ).json()
    strategy_version_id = _published_strategy(client, headers)

    refused = client.post(
        "/api/v1/backtests",
        json={
            "dataset_version_id": created["id"],
            "strategy_version_id": strategy_version_id,
        },
        headers=headers,
    )
    assert refused.status_code == 404
    assert "not approved" in refused.json()["error"]["message"]


def test_the_quality_report_names_every_defect(client, headers) -> None:
    register(client, "f@example.com")
    created = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(flaws=True), "text/csv")},
        headers=headers,
    ).json()

    breakdown = created["quality"]["rejection_breakdown"]
    assert breakdown["duplicate"] >= 1
    assert breakdown["invalid_price"] >= 1
    assert breakdown["inconsistent_ohlc"] >= 1
    assert breakdown["negative_volume"] >= 1
    assert breakdown["missing_timestamp"] >= 1
    assert len(created["rejected_rows"]["logged"]) == created["rejected_row_count"]
    assert created["source"]["content_hash"]
    assert created["canonical_hash"]


def test_inspect_stores_nothing(client, headers) -> None:
    register(client, "g@example.com")
    detected = client.post(
        "/api/v1/datasets/inspect",
        files={"file": ("d.csv", make_csv(), "text/csv")},
        headers=headers,
    )
    assert detected.status_code == 200
    assert detected.json()["ohlc_complete"] is True
    assert client.get("/api/v1/datasets").json() == []


# --- backtests --------------------------------------------------------------


def test_submission_is_idempotent(client, headers) -> None:
    register(client, "h@example.com")
    dataset_version_id = _approved_dataset(client, headers)
    strategy_version_id = _published_strategy(client, headers)
    body = {
        "dataset_version_id": dataset_version_id,
        "strategy_version_id": strategy_version_id,
        "seed": 7,
    }

    first = client.post("/api/v1/backtests", json=body, headers=headers)
    second = client.post("/api/v1/backtests", json=body, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["deduplicated"] is False
    assert second.json()["deduplicated"] is True
    assert len(client.get("/api/v1/backtests").json()) == 1


def test_an_explicit_idempotency_key_is_honoured(client, headers) -> None:
    register(client, "i@example.com")
    dataset_version_id = _approved_dataset(client, headers)
    strategy_version_id = _published_strategy(client, headers)

    ids = set()
    for seed in (1, 2, 3):
        response = client.post(
            "/api/v1/backtests",
            json={
                "dataset_version_id": dataset_version_id,
                "strategy_version_id": strategy_version_id,
                "seed": seed,
                "idempotency_key": "same-key",
            },
            headers=headers,
        )
        ids.add(response.json()["id"])
    assert len(ids) == 1, "one key must produce one job even as the body changes"


def test_a_backtest_runs_and_stores_a_reproducible_result(client, headers) -> None:
    from iluvtrade.backtests.worker import BacktestWorkerPool

    register(client, "j@example.com")
    dataset_version_id = _approved_dataset(client, headers, symbols=("ACME", "BETA"))
    strategy_version_id = _published_strategy(client, headers)

    job = client.post(
        "/api/v1/backtests",
        json={
            "dataset_version_id": dataset_version_id,
            "strategy_version_id": strategy_version_id,
            "starting_cash": "1000000.00",
            "seed": 4242,
        },
        headers=headers,
    ).json()

    BacktestWorkerPool(size=1).drain(timeout=120)

    finished = client.get(f"/api/v1/backtests/{job['id']}").json()
    assert finished["status"] == "completed", finished

    result = client.get(f"/api/v1/backtests/{job['id']}/result").json()
    repro = result["reproducibility"]
    assert repro["seed"] == 4242
    assert repro["engine"].startswith("alphalab ")
    assert repro["dataset_canonical_hash"]
    assert repro["strategy_content_hash"]
    assert repro["universe"] == ["ACME", "BETA"]
    assert result["metrics"]["records_processed"] > 0
    assert "risk_refusals" in result["result"]
    for order in result["result"]["orders"]:
        assert order["symbol"] in {"ACME", "BETA"}, "engine ids must resolve to symbols"


def test_a_queued_job_can_be_cancelled(client, headers) -> None:
    register(client, "k@example.com")
    dataset_version_id = _approved_dataset(client, headers)
    strategy_version_id = _published_strategy(client, headers)
    job = client.post(
        "/api/v1/backtests",
        json={
            "dataset_version_id": dataset_version_id,
            "strategy_version_id": strategy_version_id,
        },
        headers=headers,
    ).json()

    cancelled = client.post(f"/api/v1/backtests/{job['id']}/cancel", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert client.post(f"/api/v1/backtests/{job['id']}/cancel", headers=headers).status_code == 400


def test_a_draft_strategy_cannot_be_backtested(client, headers) -> None:
    register(client, "l@example.com")
    dataset_version_id = _approved_dataset(client, headers)
    strategy = client.post("/api/v1/strategies", json={"name": "Draft"}, headers=headers).json()
    draft = client.post(
        f"/api/v1/strategies/{strategy['id']}/versions",
        json={"implementation_key": "buy_and_hold", "parameters": {"quantity": 1}},
        headers=headers,
    ).json()

    refused = client.post(
        "/api/v1/backtests",
        json={
            "dataset_version_id": dataset_version_id,
            "strategy_version_id": draft["id"],
        },
        headers=headers,
    )
    assert refused.status_code == 400
    assert "draft" in refused.json()["error"]["message"].lower()


def test_an_unknown_parameter_is_refused(client, headers) -> None:
    """A typo'd parameter must not silently take its default."""

    register(client, "m@example.com")
    strategy = client.post("/api/v1/strategies", json={"name": "Typo"}, headers=headers).json()
    refused = client.post(
        f"/api/v1/strategies/{strategy['id']}/versions",
        json={
            "implementation_key": "moving_average_crossover",
            "parameters": {"fast": 5, "slow": 20, "quantitee": 10},
        },
        headers=headers,
    )
    assert refused.status_code == 400
    assert "quantitee" in refused.json()["error"]["message"]


def test_a_parameter_outside_its_bounds_is_refused(client, headers) -> None:
    register(client, "n@example.com")
    strategy = client.post("/api/v1/strategies", json={"name": "Bounds"}, headers=headers).json()
    refused = client.post(
        f"/api/v1/strategies/{strategy['id']}/versions",
        json={
            "implementation_key": "moving_average_crossover",
            "parameters": {"fast": 1, "slow": 20, "quantity": 10},
        },
        headers=headers,
    )
    assert refused.status_code == 400


# --- trading ----------------------------------------------------------------


def test_paper_and_live_are_never_ambiguous(client, headers) -> None:
    register(client, "o@example.com")
    dataset_version_id = _approved_dataset(client, headers)
    strategy_version_id = _published_strategy(client, headers)

    created = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "Paper",
            "mode": "paper",
            "strategy_version_id": strategy_version_id,
            "dataset_version_id": dataset_version_id,
        },
        headers=headers,
    )
    assert created.status_code == 201
    assert created.json()["mode"] == "paper"

    live = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "Live",
            "mode": "live",
            "strategy_version_id": strategy_version_id,
            "broker_account_id": "x",
            "live_confirmed": True,
        },
        headers=headers,
    )
    assert live.status_code == 400
    assert "disabled in this deployment" in live.json()["error"]["message"]


def test_a_paper_session_runs_and_projects_alphalabs_state(client, headers) -> None:
    from iluvtrade.trading.runner import RUNNER

    register(client, "p@example.com")
    dataset_version_id = _approved_dataset(client, headers, bars=160)
    strategy_version_id = _published_strategy(client, headers)

    session = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "Paper run",
            "mode": "paper",
            "strategy_version_id": strategy_version_id,
            "dataset_version_id": dataset_version_id,
            "starting_cash": "1000000.00",
            "risk_profile": "research",
            "seed": 11,
        },
        headers=headers,
    ).json()

    assert (
        client.post(f"/api/v1/trading/sessions/{session['id']}/start", headers=headers).status_code
        == 200
    )
    RUNNER.join(session["id"], timeout=180)

    final = client.get(f"/api/v1/trading/sessions/{session['id']}").json()
    assert final["status"] == "stopped"
    assert final["records_processed"] > 0
    assert final["equity"] is not None

    orders = client.get(f"/api/v1/trading/sessions/{session['id']}/orders").json()
    fills = client.get(f"/api/v1/trading/sessions/{session['id']}/fills").json()
    events = client.get(f"/api/v1/trading/sessions/{session['id']}/events").json()
    assert len(orders) == len(fills) > 0
    assert {e["kind"] for e in events} >= {"session.created", "session.finished"}
    assert all(order["instrument"] == "ACME" for order in orders)


def test_the_kill_switch_is_terminal(client, headers) -> None:
    register(client, "q@example.com")
    dataset_version_id = _approved_dataset(client, headers)
    strategy_version_id = _published_strategy(client, headers)
    session = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "Halt me",
            "mode": "paper",
            "strategy_version_id": strategy_version_id,
            "dataset_version_id": dataset_version_id,
        },
        headers=headers,
    ).json()

    halted = client.post(
        f"/api/v1/trading/sessions/{session['id']}/kill",
        json={"reason": "operator stopped it"},
        headers=headers,
    )
    assert halted.status_code == 200
    assert halted.json()["status"] == "halted"
    assert halted.json()["kill_switch_engaged"] is True

    resumed = client.post(f"/api/v1/trading/sessions/{session['id']}/resume", headers=headers)
    assert resumed.status_code == 400
    assert "cannot be resumed" in resumed.json()["error"]["message"]


def test_paper_and_backtest_agree_on_the_same_inputs(client, headers) -> None:
    """The parity guarantee, asserted end to end through the product.

    Backtest and paper differ only in where records come from and what clock
    judges them — AlphaLab's own parity matrix. Given one dataset, one strategy
    version and one seed, the two must produce the same book.
    """

    from iluvtrade.backtests.worker import BacktestWorkerPool
    from iluvtrade.trading.runner import RUNNER

    register(client, "r@example.com")
    dataset_version_id = _approved_dataset(client, headers, bars=150)
    strategy_version_id = _published_strategy(client, headers)

    job = client.post(
        "/api/v1/backtests",
        json={
            "dataset_version_id": dataset_version_id,
            "strategy_version_id": strategy_version_id,
            "starting_cash": "1000000.00",
            "risk_profile": "research",
            "seed": 909,
        },
        headers=headers,
    ).json()
    BacktestWorkerPool(size=1).drain(timeout=120)
    backtest = client.get(f"/api/v1/backtests/{job['id']}/result").json()

    session = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "Parity",
            "mode": "paper",
            "strategy_version_id": strategy_version_id,
            "dataset_version_id": dataset_version_id,
            "starting_cash": "1000000.00",
            "risk_profile": "research",
            "seed": 909,
        },
        headers=headers,
    ).json()
    client.post(f"/api/v1/trading/sessions/{session['id']}/start", headers=headers)
    RUNNER.join(session["id"], timeout=180)
    paper = client.get(f"/api/v1/trading/sessions/{session['id']}").json()

    assert paper["equity"] == backtest["metrics"]["ending_equity"]
    assert paper["realized_pnl"] == backtest["metrics"]["realized_pnl"]
    assert paper["unrealized_pnl"] == backtest["metrics"]["unrealized_pnl"]
    assert paper["commission_paid"] == backtest["metrics"]["commission_paid"]

    paper_orders = client.get(f"/api/v1/trading/sessions/{session['id']}/orders").json()
    assert len(paper_orders) == backtest["metrics"]["order_count"]
