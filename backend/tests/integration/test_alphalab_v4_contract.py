"""What AlphaLab 4.x requires of this application, held against the real engine.

Each test pins one place where AlphaLab between 3.5 and 4.1 changed what an
application must do, and where getting it wrong is silent rather than loud:

* a strategy that raises is isolated and the run **continues without it**
  (EXE-006), so a result has to be refused, not stored, when one did;
* the daily-loss limit needs the zone its day is kept in (3.10), which must be
  the exchange's, not a guessed UTC;
* ``years_elapsed`` is derived by the engine (3.10) — the old ``1.0`` default
  compounded every CAGR as if the run had lasted exactly a year;
* a result carries AlphaLab's own ``result_id`` / ``configuration_id``, which
  the integration guide asks the application to record (step 10).

Nothing here is mocked: the strategies run inside AlphaLab's pipeline, and the
product tests go through the HTTP API and the real worker.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from alphalab.strategy import BaseStrategy, Intent

from iluvtrade.alphalab_bridge import runconfig
from iluvtrade.alphalab_bridge import strategies as implementations
from iluvtrade.backtests.requests import BacktestRequest
from iluvtrade.backtests.runner import StrategyFailedDuringRun, execute
from tests.conftest import make_csv, register
from tests.support import DECLARED

pytestmark = pytest.mark.integration

FAILING_KEY = "test_fails_after_n_bars"


class _FailsAfterNBars(implementations.BuyAndHold):
    """Buys on the first bar like the benchmark, then raises on bar ``n``.

    Buying first matters: the failure must happen to a strategy that has
    already traded, so the run it leaves behind has real fills and a real
    valuation — exactly the result that would be mistaken for a valid one.
    """

    def __init__(self, strategy_id: str, *, after: int) -> None:
        super().__init__(strategy_id, quantity=Decimal("10"))
        self._after = after
        self._seen = 0

    def on_bar(self, context: Any, event: Any) -> Iterable[Any]:
        self._seen += 1
        if self._seen >= self._after:
            raise RuntimeError(f"deliberate failure on bar {self._seen}")
        return super().on_bar(context, event)


@pytest.fixture
def failing_implementation(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Register the failing strategy for this test only."""

    monkeypatch.setitem(
        implementations.IMPLEMENTATIONS,
        FAILING_KEY,
        implementations.StrategyImplementation(
            key=FAILING_KEY,
            name="Fails after N bars (test only)",
            description="Raises from on_bar after N bars.",
            parameters=(implementations.ParameterSpec("after", "int", 30, 1, 1000),),
            factory=lambda sid, p: _FailsAfterNBars(sid, after=p["after"]),
        ),
    )
    yield FAILING_KEY


def _rows(count: int = 80) -> list[dict[str, Any]]:
    rows = []
    price = Decimal("100")
    for index in range(count):
        price += Decimal("0.5") if index % 7 else Decimal("-1.25")
        rows.append(
            {
                "symbol": "ACME",
                "timestamp": 1_704_067_200 + index * 86_400,
                "open": str(price),
                "high": str(price + 1),
                "low": str(price - 1),
                "close": str(price),
                "volume": "10000",
                "vwap": str(price),
                "trade_count": 10,
            }
        )
    return rows


def _execute(key: str, parameters: dict[str, Any], **overrides: Any) -> Any:
    request = BacktestRequest(
        dataset_version_id="dsv", strategy_version_id="sv", seed=11, **overrides
    )
    return execute(
        request,
        rows=_rows(),
        implementation_key=key,
        version_defaults=parameters,
        dataset_identity="dsv-contract",
        frequency="1d",
        account_name="contract",
    )


# --- a strategy that failed ---------------------------------------------------


def test_a_backtest_whose_strategy_failed_is_refused_not_stored(failing_implementation) -> None:
    with pytest.raises(StrategyFailedDuringRun) as caught:
        _execute(failing_implementation, {"after": 20})

    failures = caught.value.failures
    assert len(failures) == 1
    assert failures[0]["strategy_id"] == "strategy-sv"
    assert "deliberate failure on bar 20" in failures[0]["error"]
    assert "deliberate failure on bar 20" in str(caught.value)


def test_a_clean_backtest_reports_no_strategy_failures() -> None:
    executed = _execute("buy_and_hold", {"quantity": 10})
    assert executed.document["strategy_failures"] == []


def test_a_failed_strategy_fails_the_job_through_the_product(
    client, headers, failing_implementation
) -> None:
    from iluvtrade.backtests.worker import BacktestWorkerPool

    register(client, "failing-backtest@example.com")
    dataset_version_id = _approved_dataset(client, headers)
    strategy_version_id = _published_strategy(
        client, headers, key=failing_implementation, params={"after": 25}
    )
    job = client.post(
        "/api/v1/backtests",
        json={
            "dataset_version_id": dataset_version_id,
            "strategy_version_id": strategy_version_id,
            "seed": 5,
        },
        headers=headers,
    ).json()
    BacktestWorkerPool(size=1).drain(timeout=120)

    finished = client.get(f"/api/v1/backtests/{job['id']}").json()
    assert finished["status"] == "failed", finished
    assert finished["error_code"] == "StrategyFailedDuringRun"
    assert "deliberate failure on bar 25" in finished["error_message"]
    # No result was stored for a run of a strategy that was no longer running.
    assert client.get(f"/api/v1/backtests/{job['id']}/result").status_code == 400


def test_a_paper_session_whose_strategy_failed_ends_failed(
    client, headers, failing_implementation
) -> None:
    from iluvtrade.trading.runner import RUNNER

    register(client, "failing-paper@example.com")
    dataset_version_id = _approved_dataset(client, headers)
    strategy_version_id = _published_strategy(
        client, headers, key=failing_implementation, params={"after": 40}
    )
    session = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "Fails mid-run",
            "mode": "paper",
            "strategy_version_id": strategy_version_id,
            "dataset_version_id": dataset_version_id,
            "starting_cash": "1000000.00",
            "seed": 5,
        },
        headers=headers,
    ).json()
    client.post(f"/api/v1/trading/sessions/{session['id']}/start", headers=headers)
    RUNNER.join(session["id"], timeout=180)

    state = client.get(f"/api/v1/trading/sessions/{session['id']}").json()
    assert state["status"] == "failed", state
    assert "deliberate failure on bar 40" in state["failure_reason"]
    # It stopped at the failure rather than replaying the rest without a strategy.
    assert state["records_processed"] == 40

    events = client.get(f"/api/v1/trading/sessions/{session['id']}/events").json()
    failed = [event for event in events if event["kind"] == "strategy_failed"]
    assert len(failed) == 1
    assert failed[0]["severity"] == "error"


# --- the trading day's zone -----------------------------------------------------


def _config_for(exchange: str) -> Any:
    capital = Decimal("100000")
    return runconfig.build_run_config(
        mode=runconfig.ExecutionMode.BACKTEST,
        account_id="a",
        account_name="A",
        currency="INR",
        starting_cash=capital,
        risk=runconfig.RiskProfile.research(capital),
        exchange=exchange,
        fill_timing="next_event",
        strategy_ids=("s",),
        seed=1,
        start_timestamp=0.0,
    )


def test_the_daily_loss_limit_keeps_the_exchanges_day() -> None:
    limit = _config_for("XNSE").pipeline.risk_limits.daily_loss
    assert limit.zone == "Asia/Kolkata"
    assert limit.max_daily_loss == Decimal("25000.0000")


def test_an_exchange_without_a_declared_zone_is_refused_not_defaulted() -> None:
    with pytest.raises(runconfig.UnsupportedExchangeError, match="XNYS"):
        _config_for("XNYS")


def test_a_backtest_on_an_unplaceable_exchange_is_refused_at_submission(client, headers) -> None:
    register(client, "exchange@example.com")
    dataset_version_id = _approved_dataset(client, headers)
    strategy_version_id = _published_strategy(client, headers)
    refused = client.post(
        "/api/v1/backtests",
        json={
            "dataset_version_id": dataset_version_id,
            "strategy_version_id": strategy_version_id,
            "exchange": "XNYS",
        },
        headers=headers,
    )
    assert refused.status_code == 400, refused.text
    assert "trading-day time zone" in refused.json()["error"]["message"]


# --- what the engine derives and records -----------------------------------------


def test_the_span_is_derived_by_the_engine_not_assumed() -> None:
    assert _config_for("XNSE").years_elapsed is None

    executed = _execute("buy_and_hold", {"quantity": 10})
    returns = executed.document["report"]["returns"]
    # 80 daily bars, plus the funding instant one second before the first.
    assert returns["years_elapsed"] == pytest.approx(79 / 365.25, rel=1e-3)
    assert returns["periodicity"]
    assert returns["periods_per_year"] > 0
    assert executed.document["run_configuration"]["years_elapsed"] == returns["years_elapsed"]


def test_a_result_records_alphalabs_own_identities() -> None:
    first = _execute("moving_average_crossover", {"fast": 3, "slow": 9, "quantity": 5})
    second = _execute("moving_average_crossover", {"fast": 3, "slow": 9, "quantity": 5})
    other = _execute("moving_average_crossover", {"fast": 3, "slow": 10, "quantity": 5})

    identity = first.document["engine_identity"]
    assert re.fullmatch(r"[0-9a-f]{64}", identity["result_id"])
    assert re.fullmatch(r"[0-9a-f]{64}", identity["configuration_id"])
    assert identity["seed"] == 11
    assert identity["dataset_id"] == "dsv-contract"
    assert identity["strategy_ids"] == ["strategy-sv"]

    # The same inputs are the same run, by the engine's own record.
    assert second.document["engine_identity"] == identity
    # Different parameters are a different run, and the engine says so.
    assert other.document["engine_identity"]["result_id"] != identity["result_id"]


def test_the_engine_identity_is_served_with_the_result(client, headers) -> None:
    from iluvtrade.backtests.worker import BacktestWorkerPool

    register(client, "identity@example.com")
    dataset_version_id = _approved_dataset(client, headers)
    strategy_version_id = _published_strategy(client, headers)
    job = client.post(
        "/api/v1/backtests",
        json={
            "dataset_version_id": dataset_version_id,
            "strategy_version_id": strategy_version_id,
            "seed": 77,
        },
        headers=headers,
    ).json()
    BacktestWorkerPool(size=1).drain(timeout=120)

    result = client.get(f"/api/v1/backtests/{job['id']}/result").json()
    engine_identity = result["reproducibility"]["engine_identity"]
    assert engine_identity == result["result"]["engine_identity"]
    assert re.fullmatch(r"[0-9a-f]{64}", engine_identity["result_id"])
    assert engine_identity["seed"] == 77
    assert result["result"]["strategy_failures"] == []
    assert result["reproducibility"]["configuration"]["trading_day_zone"] == "Asia/Kolkata"


# --- helpers (the product's own paths) --------------------------------------------


def _approved_dataset(client: Any, headers: dict[str, str]) -> str:
    created = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=120), "text/csv")},
        data={**DECLARED, "dataset_name": "Contract data"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    version_id = str(created.json()["id"])
    approved = client.post(f"/api/v1/datasets/versions/{version_id}/approve", headers=headers)
    assert approved.status_code == 200, approved.text
    return version_id


def _published_strategy(
    client: Any,
    headers: dict[str, str],
    *,
    key: str = "moving_average_crossover",
    params: dict[str, Any] | None = None,
) -> str:
    strategy = client.post("/api/v1/strategies", json={"name": "Contract"}, headers=headers).json()
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
    assert published.status_code == 200, published.text
    return str(published.json()["id"])


# --- the drivers are AlphaLab's drivers, record for record --------------------------


class _SliceTrader(BaseStrategy):
    """Trades only on ``on_slice``: a cross-sectional decision per complete instant.

    The deployment's own strategies subscribe to bars alone, and for them
    whether a driver closes each instant changes nothing in the record. This
    one makes the driver's ``close_slice`` calls (AlphaLab EXE-004) observable:
    a driver that skipped them would deliver it no slices and trade nothing.
    """

    def __init__(self, strategy_id: str) -> None:
        self._strategy_id = strategy_id
        self._slices = 0

    def on_slice(self, context: Any, event: Any) -> Iterable[Any]:
        self._slices += 1
        if self._slices % 10:
            return ()
        target = Decimal("10") if (self._slices // 10) % 2 else Decimal("0")
        return (
            Intent(
                strategy_id=self._strategy_id,
                instrument=event.assets[0],
                target=target,
                timestamp=event.timestamp,
            ),
        )


_STRATEGIES = {
    "bars": (
        frozenset({"bars"}),
        lambda: implementations.build(
            "moving_average_crossover", "s", {"fast": 5, "slow": 20, "quantity": 25}
        ),
    ),
    "slices": (frozenset({"bars", "slices"}), lambda: _SliceTrader("s")),
}


def _inputs(kind: str, mode: Any) -> tuple[Any, ...]:
    from iluvtrade.alphalab_bridge import engine, instruments, market

    rows = []
    for symbol, base in (("ACME", Decimal("100")), ("BETA", Decimal("50"))):
        price = base
        for index in range(90):
            price += Decimal("0.8") if (index // 15) % 2 == 0 else Decimal("-0.7")
            rows.append(
                {
                    "symbol": symbol,
                    "timestamp": 1_704_067_200 + index * 86_400,
                    "open": str(price),
                    "high": str(price + 1),
                    "low": str(price - 1),
                    "close": str(price),
                    "volume": "1000",
                    "vwap": str(price),
                    "trade_count": 1,
                }
            )
    rows.sort(key=lambda row: (row["timestamp"], row["symbol"]))
    universe = instruments.universe_for(["ACME", "BETA"], currency="INR")
    source = market.source_from_rows("session-x", rows, universe=universe, frequency="1d")
    dataset = market.dataset_from_rows("dsv-x", rows, universe=universe, frequency="1d")
    start = rows[0]["timestamp"] - 1.0
    capital = Decimal("1000000")
    config = runconfig.build_run_config(
        mode=mode,
        account_id="x",
        account_name="X",
        currency="INR",
        starting_cash=capital,
        risk=runconfig.RiskProfile.research(capital),
        exchange=universe.exchange,
        fill_timing="next_event",
        strategy_ids=("s",),
        instruments=universe.registry,
        seed=31,
        start_timestamp=start,
    )
    subscriptions, build = _STRATEGIES[kind]

    def runtime() -> Any:
        return engine.runtime_for(
            {"s": build()}, subscriptions=subscriptions, start_timestamp=start
        )

    factory = engine.context_factory_for({}, [], start)
    return config, source, dataset, runtime, factory


def _drive_like_the_paper_runner(config: Any, source: Any, runtime: Any, factory: Any) -> Any:
    """The engine calls ``trading/runner.py`` makes, in its order, minus the database."""

    from iluvtrade.alphalab_bridge import engine

    with engine.run_scope(config.seed):
        state = engine.initialize_session(config, runtime, source_id=source.source_id)
        for record in source.records():
            state, _ = engine.advance(state, record, factory)
        state = engine.close_final_slice(state, factory)
    # Unfinished, as the runner projects it: a paper session is never finalized,
    # and finalizing outside the scope would mint the report's ids from uuid4.
    return engine.result_of(state)


@pytest.mark.parametrize("kind", sorted(_STRATEGIES))
def test_a_paper_run_is_alphalabs_own_session_byte_for_byte(kind: str) -> None:
    """The record-by-record loop must be ``TradingSession.run`` split open, not a variant.

    Compared by AlphaLab's ``result_id`` -- the digest of the run's complete
    record -- not by figures, which a run can match while its record differs.
    Before this was checked, the runner funded the session outside the seeded
    identifier scope, so the record held random ids and this digest differed on
    every run while every figure agreed.
    """

    from alphalab.backtesting import BacktestResult
    from alphalab.runtime import TradingSession

    from iluvtrade.alphalab_bridge import results

    config, source, _, runtime, factory = _inputs(kind, runconfig.ExecutionMode.PAPER)
    ours = _drive_like_the_paper_runner(config, source, runtime(), factory)
    again = _drive_like_the_paper_runner(config, source, runtime(), factory)
    reference = BacktestResult(TradingSession.run(config, source, runtime(), factory))

    assert ours.fills, "the comparison is only worth something if the strategy traded"
    assert results.identity_of(ours)["result_id"] == results.identity_of(again)["result_id"]
    assert results.identity_of(ours) == results.identity_of(reference)


@pytest.mark.parametrize("kind", sorted(_STRATEGIES))
def test_the_observed_backtest_is_alphalabs_own_backtest_byte_for_byte(kind: str) -> None:
    """``run_backtest_observed`` re-drives ``BacktestEngine.run`` to see each step.

    It must stay that driver exactly, including closing every instant before the
    next record (EXE-004), which AlphaLab's driver does since 3.x and this copy
    did not until 4.1 was integrated.
    """

    from iluvtrade.alphalab_bridge import engine, results

    config, _, dataset, runtime, factory = _inputs(kind, runconfig.ExecutionMode.BACKTEST)
    observed = engine.run_backtest_observed(config, dataset, runtime(), factory)
    reference = engine.run_backtest(config, dataset, runtime(), factory)

    assert observed.fills, "the comparison is only worth something if the strategy traded"
    assert results.identity_of(observed) == results.identity_of(reference)


def test_the_paper_runner_funds_inside_the_seeded_scope() -> None:
    """Structural guard for the property above, on the runner itself.

    The test above drives the bridge as the runner does; this holds the runner
    to doing so — ``initialize_session`` lexically inside ``with
    engine.run_scope(...)``, and the last instant closed when the data runs out.
    """

    import ast
    import pathlib

    import iluvtrade.trading.runner as runner_module

    tree = ast.parse(pathlib.Path(runner_module.__file__).read_text(encoding="utf-8"))

    def calls(node: ast.AST, name: str) -> list[ast.Call]:
        return [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == name
        ]

    scopes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.With)
        and any(calls(item.context_expr, "run_scope") for item in node.items)
    ]
    assert len(scopes) == 1
    assert len(calls(tree, "initialize_session")) == 1
    assert len(calls(scopes[0], "initialize_session")) == 1, "funding outside the seeded scope"
    assert len(calls(scopes[0], "close_final_slice")) == 1


# --- replay pause and resume (AlphaLab 4.1, FUT-001): the engine half ------------
#
# The application half — persisting a payload per session and resuming from it —
# is not built (docs/TRADING.md). What is held here is the part that lives in the
# bridge: every strategy this deployment runs declares its memory, so a replay
# paused through a serialized payload and resumed with freshly built objects is
# the uninterrupted replay, byte for byte; and what AlphaLab refuses, it refuses.

_IMPLEMENTATIONS = {
    "moving_average_crossover": {"fast": 5, "slow": 20, "quantity": 25},
    "buy_and_hold": {"quantity": 10},
    "mean_reversion_bands": {"window": 10, "entry_sigma": 1.0, "quantity": 10},
}


def _replay_inputs(key: str, *, commission: str = "0.0003", rows: Any = None) -> tuple[Any, ...]:
    from alphalab.runtime.run_snapshot import RunObjects
    from alphalab.runtime.snapshot import RuntimeObjects

    from iluvtrade.alphalab_bridge import engine, instruments, market

    rows = rows if rows is not None else _cycle_rows()
    universe = instruments.universe_for(["ACME"], currency="INR")
    start = rows[0]["timestamp"] - 1.0
    capital = Decimal("1000000")
    config = runconfig.build_run_config(
        mode=runconfig.ExecutionMode.REPLAY,
        account_id="replay",
        account_name="Replay",
        currency="INR",
        starting_cash=capital,
        risk=runconfig.RiskProfile.research(capital),
        exchange=universe.exchange,
        fill_timing="next_event",
        strategy_ids=("s",),
        instruments=universe.registry,
        seed=17,
        start_timestamp=start,
        commission_rate=Decimal(commission),
    )
    parameters = implementations.validate_parameters(key, _IMPLEMENTATIONS[key])

    def strategy(**override: Any) -> Any:
        return implementations.build(key, "s", {**parameters, **override})

    def objects(**override: Any) -> Any:
        """What a new process builds again: a payload records these, it does not carry them."""

        return RunObjects(
            pipeline=RuntimeObjects(
                sizing_model=config.pipeline.sizing_model,
                simulator=config.pipeline.simulator,
                strategies={"s": strategy(**override)},
                instruments=universe.registry,
            ),
            fill_policy=config.fill_policy,
        )

    dataset = market.dataset_from_rows("dsv-replay", rows, universe=universe, frequency="1d")
    runtime = engine.runtime_for({"s": strategy()}, start_timestamp=start)
    return config, dataset, runtime, objects, engine.context_factory_for(parameters, [], start)


def _cycle_rows() -> list[dict[str, Any]]:
    rows = []
    price = Decimal("100")
    for index in range(160):
        price += Decimal("0.8") if (index // 20) % 2 == 0 else Decimal("-0.7")
        rows.append(
            {
                "symbol": "ACME",
                "timestamp": 1_704_067_200 + index * 86_400,
                "open": str(price),
                "high": str(price + 1),
                "low": str(price - 1),
                "close": str(price),
                "volume": "1000",
                "vwap": str(price),
                "trade_count": 1,
            }
        )
    return rows


@pytest.mark.parametrize("key", sorted(_IMPLEMENTATIONS))
def test_a_paused_replay_resumes_to_the_uninterrupted_result(key: str) -> None:
    from alphalab.backtesting import PausedReplay, ReplayBacktest, replay_snapshot
    from alphalab.persistence import deserialize, serialize

    from iluvtrade.alphalab_bridge import results

    config, dataset, runtime, objects, factory = _replay_inputs(key)
    whole = ReplayBacktest.run(config, dataset, runtime, factory)
    assert whole.backtest.fills, "the comparison is only worth something if the strategy traded"

    def every_37(progress: Any) -> bool:
        return progress.records_replayed % 37 == 0

    _, _, runtime, _, _ = _replay_inputs(key)
    chain = ReplayBacktest.run(config, dataset, runtime, factory, pause_when=every_37)
    pauses = 0
    while isinstance(chain, PausedReplay):
        pauses += 1
        # Through the payload every time, into objects built afresh: what another
        # process would have, and nothing the paused one held.
        payload = serialize(replay_snapshot.capture(chain))
        restored = replay_snapshot.restore(
            replay_snapshot.from_primitives(deserialize(payload)), objects(), dataset
        )
        chain = ReplayBacktest.resume(restored, factory, pause_when=every_37)

    # Before the first record, then after 37, 74, 111 and 148 of the 160.
    assert pauses == 5
    assert results.identity_of(chain.backtest) == results.identity_of(whole.backtest)


def test_a_paused_replay_refuses_altered_inputs() -> None:
    from alphalab.backtesting import PausedReplay, ReplayBacktest, replay_snapshot
    from alphalab.persistence import StateDecodeError, deserialize, serialize

    key = "moving_average_crossover"
    config, dataset, runtime, objects, factory = _replay_inputs(key)
    paused = ReplayBacktest.run(
        config, dataset, runtime, factory, pause_when=lambda p: p.records_replayed == 60
    )
    assert isinstance(paused, PausedReplay)
    snapshot = replay_snapshot.from_primitives(
        deserialize(serialize(replay_snapshot.capture(paused)))
    )

    altered_rows = _cycle_rows()
    altered_rows[100]["close"] = "1"
    _, altered_dataset, _, _, _ = _replay_inputs(key, rows=altered_rows)
    _, _, _, other_cost, _ = _replay_inputs(key, commission="0.01")

    with pytest.raises(StateDecodeError, match="not the ones it read"):
        replay_snapshot.restore(snapshot, objects(), altered_dataset)
    with pytest.raises(StateDecodeError, match="simulator"):
        replay_snapshot.restore(snapshot, other_cost(), dataset)
    # AlphaLab records a strategy by type, not by its constructor arguments; the
    # strategy's own declared state is what refuses another version's settings.
    with pytest.raises(StateDecodeError, match="captured with parameters"):
        replay_snapshot.restore(snapshot, objects(fast=6), dataset)
    # The same values spelled another way are the same parameters.
    replay_snapshot.restore(snapshot, objects(quantity=Decimal("25.000")), dataset)
