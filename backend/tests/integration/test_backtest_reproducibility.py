"""A backtest must be re-derivable, not merely believable."""

from __future__ import annotations

from decimal import Decimal

import pytest

from iluvtrade.alphalab_bridge import engine, instruments, market, results, runconfig
from iluvtrade.alphalab_bridge import strategies as implementations

pytestmark = pytest.mark.integration

CAPITAL = Decimal("1000000.00")
SEED = 20260914


def _rows(count: int = 160) -> list[dict[str, object]]:
    """A deterministic price path. No randomness, so the fixture is stable."""

    rows = []
    start = 1700000000.0
    for index in range(count):
        # A slow rise then a fall, so a crossover strategy actually trades.
        base = 100.0 + (index * 0.6 if index < count * 0.6 else (count * 0.6 - index) * 0.5)
        close = round(base, 2)
        rows.append(
            {
                "symbol": "ACME",
                "timestamp": start + index * 86400,
                "open": str(round(close - 0.3, 2)),
                "high": str(round(close + 0.5, 2)),
                "low": str(round(close - 0.5, 2)),
                "close": str(close),
                "volume": "10000",
                "vwap": str(close),
                "trade_count": 10,
            }
        )
    return rows


def _run(rows, *, seed: int = SEED):
    universe = instruments.universe_for(["ACME"], currency="INR")
    dataset = market.dataset_from_rows("DSV", rows, universe=universe, frequency="1d")
    strategy_id = "s"
    parameters = implementations.validate_parameters(
        "moving_average_crossover", {"fast": 5, "slow": 20, "quantity": 25}
    )
    instance = implementations.build("moving_average_crossover", strategy_id, parameters)
    start = dataset.start_time - 1.0
    config = runconfig.build_run_config(
        mode=runconfig.ExecutionMode.BACKTEST,
        account_id="a",
        account_name="A",
        currency="INR",
        starting_cash=CAPITAL,
        risk=runconfig.RiskProfile.research(CAPITAL),
        strategy_ids=(strategy_id,),
        instruments=universe.registry,
        seed=seed,
        start_timestamp=start,
    )
    runtime = engine.runtime_for({strategy_id: instance}, start_timestamp=start)
    return engine.run_backtest(
        config, dataset, runtime, engine.context_factory_for(parameters, [], start)
    )


def test_the_same_inputs_produce_identical_identities() -> None:
    """Not merely the same P&L — the same order ids, fill for fill."""

    rows = _rows()
    first, second = _run(rows), _run(rows)

    assert [o.order_id.value for o in first.orders] == [o.order_id.value for o in second.orders]
    assert [f.fill_id for f in first.fills] == [f.fill_id for f in second.fills]
    assert first.valuation == second.valuation
    assert first.equity_curve == second.equity_curve


def test_a_different_seed_changes_identities_but_not_the_result() -> None:
    """The seed drives identifiers, not decisions."""

    rows = _rows()
    first, second = _run(rows, seed=1), _run(rows, seed=2)

    assert [o.order_id.value for o in first.orders] != [o.order_id.value for o in second.orders]
    assert first.valuation.equity == second.valuation.equity
    assert len(first.orders) == len(second.orders)


def test_replaying_a_run_reproduces_it_exactly() -> None:
    """AlphaLab's replay path must agree with its backtest path."""

    rows = _rows()
    universe = instruments.universe_for(["ACME"], currency="INR")
    dataset = market.dataset_from_rows("DSV", rows, universe=universe, frequency="1d")
    parameters = implementations.validate_parameters(
        "moving_average_crossover", {"fast": 5, "slow": 20, "quantity": 25}
    )
    start = dataset.start_time - 1.0
    config = runconfig.build_run_config(
        mode=runconfig.ExecutionMode.BACKTEST,
        account_id="a",
        account_name="A",
        currency="INR",
        starting_cash=CAPITAL,
        risk=runconfig.RiskProfile.research(CAPITAL),
        strategy_ids=("s",),
        instruments=universe.registry,
        seed=SEED,
        start_timestamp=start,
    )

    def fresh():
        return engine.runtime_for(
            {"s": implementations.build("moving_average_crossover", "s", parameters)},
            start_timestamp=start,
        )

    backtest = engine.run_backtest(
        config, dataset, fresh(), engine.context_factory_for(parameters, [], start)
    )
    replay = engine.run_replay(
        config, dataset, fresh(), engine.context_factory_for(parameters, [], start)
    )

    assert replay.records_replayed == backtest.records_processed
    assert [o.order_id.value for o in replay.backtest.orders] == [
        o.order_id.value for o in backtest.orders
    ]
    assert replay.backtest.valuation == backtest.valuation
    assert replay.backtest.equity_curve == backtest.equity_curve


def test_the_accounting_identity_holds_exactly() -> None:
    """equity == starting cash + realized + unrealized - commission, over Decimal.

    Exact equality, not a tolerance: AlphaLab computes in ``Decimal`` and this
    application never converts money to float. A tolerance here would hide the
    first place a float crept in.
    """

    result = _run(_rows())
    valuation = result.valuation
    assert valuation.equity == (
        CAPITAL + valuation.realized_pnl + valuation.unrealized_pnl - valuation.commission_paid
    )


def test_money_is_never_projected_as_a_float() -> None:
    """A float in the API payload would silently lose precision."""

    document = results.extract(_run(_rows()))
    for key in ("cash", "equity", "realized_pnl", "unrealized_pnl", "commission_paid"):
        assert isinstance(document["valuation"][key], str), f"{key} must stay a string"
    for order in document["orders"]:
        assert isinstance(order["quantity"], str)
    for fill in document["fills"]:
        assert isinstance(fill["price"], str)


def test_the_observed_driver_matches_alphalabs_own() -> None:
    """``run_backtest_observed`` must not perturb the run it observes."""

    rows = _rows()
    universe = instruments.universe_for(["ACME"], currency="INR")
    dataset = market.dataset_from_rows("DSV", rows, universe=universe, frequency="1d")
    parameters = implementations.validate_parameters(
        "moving_average_crossover", {"fast": 5, "slow": 20, "quantity": 25}
    )
    start = dataset.start_time - 1.0
    config = runconfig.build_run_config(
        mode=runconfig.ExecutionMode.BACKTEST,
        account_id="a",
        account_name="A",
        currency="INR",
        starting_cash=CAPITAL,
        risk=runconfig.RiskProfile.research(CAPITAL),
        strategy_ids=("s",),
        instruments=universe.registry,
        seed=SEED,
        start_timestamp=start,
    )

    def fresh():
        return engine.runtime_for(
            {"s": implementations.build("moving_average_crossover", "s", parameters)},
            start_timestamp=start,
        )

    plain = engine.run_backtest(
        config, dataset, fresh(), engine.context_factory_for(parameters, [], start)
    )
    seen: list[int] = []
    observed = engine.run_backtest_observed(
        config,
        dataset,
        fresh(),
        engine.context_factory_for(parameters, [], start),
        on_step=lambda index, _step: seen.append(index),
    )

    assert seen == list(range(1, len(dataset) + 1))
    assert [o.order_id.value for o in plain.orders] == [o.order_id.value for o in observed.orders]
    assert plain.valuation == observed.valuation
    assert plain.equity_curve == observed.equity_curve
