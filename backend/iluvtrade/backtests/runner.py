"""Executing one backtest job against AlphaLab.

This is the only place a backtest actually runs. It is deliberately a pure
function of (request, dataset rows, strategy version) → result document, with
the database work either side of it in :mod:`iluvtrade.backtests.service`, so the
engine call is testable without a job queue and the queue is testable without an
engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from iluvtrade.alphalab_bridge import (
    ALPHALAB_VERSION,
    engine,
    instruments,
    market,
    results,
    runconfig,
)
from iluvtrade.alphalab_bridge import (
    strategies as implementations,
)
from iluvtrade.backtests.requests import BacktestRequest

__all__ = ["ExecutedBacktest", "execute"]


@dataclass(frozen=True, slots=True)
class ExecutedBacktest:
    """What one engine run produced, ready to be persisted."""

    document: dict[str, Any]
    headline: dict[str, Any]
    seed: int
    engine_version: str
    universe: dict[str, Any]
    strategy_logs: list[tuple[str, str]]
    records_processed: int


def execute(
    request: BacktestRequest,
    *,
    rows: list[dict[str, Any]],
    implementation_key: str,
    version_defaults: dict[str, Any],
    dataset_identity: str,
    frequency: str | None,
    account_name: str,
) -> ExecutedBacktest:
    """Run one backtest through AlphaLab and project the result."""

    if not rows:
        raise ValueError("The dataset version contains no canonical rows.")

    symbols = sorted({str(row["symbol"]) for row in rows})
    selected = sorted(set(request.universe)) if request.universe else symbols
    unknown = sorted(set(selected) - set(symbols))
    if unknown:
        raise ValueError(
            f"The dataset does not contain: {', '.join(unknown)}. "
            f"It contains: {', '.join(symbols[:50])}."
        )

    universe = instruments.universe_for(
        selected, exchange=request.exchange, currency=request.currency
    )
    dataset = market.dataset_from_rows(
        dataset_identity,
        rows,
        universe=universe,
        frequency=frequency,
        symbols=frozenset(selected),
    )

    # The version's stored parameters are the baseline; the request may override
    # within the schema. Validating the request alone would fill the
    # *implementation's* defaults instead — so a backtest recorded against
    # strategy version X would have run parameters X does not define, and the
    # reproducibility record would be untrue while looking correct.
    parameters = implementations.validate_parameters(
        implementation_key, {**version_defaults, **request.parameters}
    )
    # One strategy per run, identified by the run's own seed so two runs of the
    # same version are distinguishable in attribution without being random.
    strategy_id = f"strategy-{request.strategy_version_id}"
    instance = implementations.build(implementation_key, strategy_id, parameters)

    capital = request.cash()
    risk = (
        runconfig.RiskProfile.conservative(capital)
        if request.risk_profile == "conservative"
        else runconfig.RiskProfile.research(capital)
    )

    # Fund the portfolio strictly before the first record, so the funding
    # snapshot is not confused with the first bar's mark.
    start_timestamp = dataset.start_time - 1.0
    span_seconds = max(dataset.end_time - dataset.start_time, 1.0)
    years_elapsed = max(span_seconds / (365.25 * 86400.0), 1e-9)

    config = runconfig.build_run_config(
        mode=runconfig.ExecutionMode.BACKTEST,
        account_id=f"acct-{request.dataset_version_id}",
        account_name=account_name,
        currency=request.currency,
        starting_cash=capital,
        risk=risk,
        strategy_ids=(strategy_id,),
        instruments=universe.registry,
        seed=request.effective_seed(),
        start_timestamp=start_timestamp,
        commission_kind=request.commission_kind,
        commission_rate=Decimal(request.commission_rate),
        years_elapsed=years_elapsed,
        risk_free_rate=request.risk_free_rate,
    )

    logs: list[tuple[str, str]] = []
    factory = engine.context_factory_for(parameters, logs, start_timestamp)
    runtime = engine.runtime_for({strategy_id: instance}, start_timestamp=start_timestamp)

    # Risk refusals are collected as the run proceeds. They appear nowhere in the
    # finished result — an order risk declined never becomes an order, a report
    # or a fill — so a run that refused every signal would otherwise be reported
    # as a clean result with no trades.
    refusals: list[dict[str, Any]] = []
    unpriced: list[dict[str, Any]] = []

    def observe(index: int, step: Any) -> None:
        for decision in step.risk_decisions:
            if decision.approved:
                continue
            if len(refusals) < 500:
                refusals.append(
                    {
                        "record_index": index,
                        "timestamp": decision.timestamp,
                        "order_id": decision.order_id,
                        "reason": decision.reason,
                        "violations": [
                            {
                                "limit": getattr(v, "limit_name", str(v)),
                                "detail": getattr(v, "message", str(v)),
                            }
                            for v in decision.violations
                        ],
                        "required_margin": str(decision.required_margin),
                        "remaining_buying_power": str(decision.remaining_buying_power),
                    }
                )
        for request in step.unpriced_requests:
            if len(unpriced) < 200:
                unpriced.append(
                    {
                        "record_index": index,
                        "asset_id": str(getattr(request, "asset_id", "")),
                    }
                )

    result = engine.run_backtest_observed(config, dataset, runtime, factory, on_step=observe)

    document = results.extract(result)
    document["risk_refusals"] = {
        "count": len(refusals),
        "logged": refusals,
        "logged_is_truncated": len(refusals) >= 500,
    }
    document["unpriced_order_requests"] = unpriced
    # Translate engine identities back to the symbols the user uploaded, so a
    # results screen never shows a UUID. The engine id is kept alongside, because
    # it is the join key back to AlphaLab's own state.
    document["instrument_universe"] = universe.to_dict()
    for section in ("positions", "orders", "fills"):
        for row in document.get(section, []):
            row["symbol"] = universe.symbol_of(str(row.get("instrument", "")))
    for row in document["unpriced_order_requests"]:
        row["symbol"] = universe.symbol_of(row["asset_id"])
    document["run_configuration"] = {
        "starting_cash": str(capital),
        "currency": request.currency,
        "exchange": request.exchange,
        "risk_profile": request.risk_profile,
        "risk_limits": risk.to_dict(),
        "commission_kind": request.commission_kind,
        "commission_rate": request.commission_rate,
        "risk_free_rate": request.risk_free_rate,
        "years_elapsed": years_elapsed,
        "start_timestamp": start_timestamp,
        "parameters": parameters,
        "universe": selected,
    }

    headline = results.headline(result)
    headline["risk_refusal_count"] = len(refusals)

    return ExecutedBacktest(
        document=document,
        headline=headline,
        seed=request.effective_seed(),
        engine_version=ALPHALAB_VERSION,
        universe=universe.to_dict(),
        strategy_logs=logs[:500],
        records_processed=result.records_processed,
    )
