"""Projecting an AlphaLab result into JSON.

**Nothing here computes a metric.** Every figure is read from AlphaLab's
``PerformanceReport``, ``PortfolioValuationSnapshot`` or OMS state and converted
to a JSON-safe type. Where AlphaLab does not produce a number, this module emits
``null`` — PHASE 6 says *do not fabricate metrics*, and a plausible-looking
figure computed here would be exactly that.

``Decimal`` becomes a **string**, never a float. A money value that round-trips
through a float stops satisfying the accounting identity AlphaLab guarantees,
and a UI that then displays ``99999.99999999999`` is reporting an error this
layer introduced.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from alphalab.backtesting import BacktestResult

__all__ = ["extract", "headline", "positions_of", "valuation_of"]


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _float(value: Any) -> float | None:
    """A ratio or rate as a float, or ``None`` when AlphaLab produced none."""

    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError, ArithmeticError):
        return None


def valuation_of(result: BacktestResult) -> dict[str, Any]:
    """The final mark-to-market valuation, verbatim."""

    valuation = result.valuation
    return {
        "timestamp": valuation.timestamp,
        "currency": valuation.currency,
        "cash": _money(valuation.cash),
        "long_value": _money(valuation.long_value),
        "short_value": _money(valuation.short_value),
        "positions_value": _money(valuation.positions_value),
        "unrealized_pnl": _money(valuation.unrealized_pnl),
        "realized_pnl": _money(valuation.realized_pnl),
        "commission_paid": _money(valuation.commission_paid),
        "equity": _money(valuation.equity),
    }


def positions_of(result: BacktestResult) -> list[dict[str, Any]]:
    """Open positions as AlphaLab's portfolio holds them.

    Fields are read by name, not with defensive ``getattr`` fallbacks. A silent
    fallback is what turns a renamed engine field into a blank column that looks
    like real data; an ``AttributeError`` here is a loud, fixable failure.

    ``unrealized_pnl``, ``market_value`` and ``side`` are computed **by
    AlphaLab's own** ``Position`` properties. Recomputing them from quantity and
    price would be a second accounting authority.
    """

    rows: list[dict[str, Any]] = []
    for asset_id, position in dict(result.state.portfolio.positions).items():
        if position.quantity == 0:
            continue
        rows.append(
            {
                "instrument": str(asset_id),
                "quantity": _money(position.quantity),
                "average_price": _money(position.average_cost),
                "market_price": _money(position.market_price),
                "market_value": _money(position.market_value),
                "unrealized_pnl": _money(position.unrealized_pnl),
                "realized_pnl": _money(position.realized_pnl),
                "currency": position.currency,
                "side": getattr(position.side, "value", str(position.side)),
            }
        )
    return sorted(rows, key=lambda row: row["instrument"])


def orders_of(result: BacktestResult) -> list[dict[str, Any]]:
    """Every order, with AlphaLab's own identifiers preserved.

    ``asset_id`` is the engine's instrument identity; the caller maps it back to
    a provider symbol. Carrying both means a results screen is readable *and*
    still joinable to AlphaLab's state.
    """

    return [
        {
            "sequence": index,
            "order_id": str(order.order_id.value),
            "strategy_id": str(order.strategy_id),
            "instrument": str(order.asset_id),
            "side": order.side.value,
            "order_type": order.order_type.value,
            "status": order.status.value,
            "quantity": _money(order.quantity),
            "filled_quantity": _money(order.filled_quantity),
            "remaining_quantity": _money(order.remaining_quantity),
            "average_fill_price": _money(order.average_fill_price),
            "limit_price": _money(order.limit_price),
            "created_at": order.created_at,
            "updated_at": order.updated_at,
        }
        for index, order in enumerate(result.orders)
    ]


def fills_of(result: BacktestResult) -> list[dict[str, Any]]:
    """Every fill, in execution order.

    **A fill's ids are plain strings; an order's is a dataclass.** AlphaLab has
    two distinct ``OrderId`` types kept apart on purpose:
    ``alphalab.oms.ids.OrderId`` is a frozen dataclass wrapping a ``UUID`` and is
    what an OMS ``Order`` carries, while ``alphalab.core.ids.OrderId`` is a
    ``NewType`` over ``str`` and is what a canonical ``Fill`` carries. Reaching
    for ``.value`` on the second raises ``AttributeError`` — so the two are read
    differently here, deliberately, rather than through one lenient accessor.
    """

    return [
        {
            "sequence": index,
            "fill_id": str(fill.fill_id),
            "order_id": str(fill.order_id),
            "instrument": str(fill.asset_id),
            "side": fill.side.value,
            "quantity": _money(fill.quantity),
            "price": _money(fill.price),
            "commission": _money(fill.commission),
            "timestamp": fill.filled_at,
        }
        for index, fill in enumerate(result.fills)
    ]


def equity_curve_of(result: BacktestResult) -> list[dict[str, Any]]:
    return [
        {
            "timestamp": snapshot.timestamp,
            "equity": _money(snapshot.total_equity),
            "cash": _money(snapshot.cash),
            "long_exposure": _money(snapshot.long_exposure),
            "short_exposure": _money(snapshot.short_exposure),
        }
        for snapshot in result.equity_curve
    ]


def report_of(result: BacktestResult) -> dict[str, Any] | None:
    """AlphaLab's performance report, field for field.

    ``None`` when analytics did not run — which is a real state, not an error,
    and must not be replaced with zeros.
    """

    report = result.report
    if report is None:
        return None
    return {
        "report_id": report.report_id,
        "timestamp": report.timestamp,
        "ending_capital": _money(report.ending_capital),
        "returns": {
            "total_return": _float(report.returns.total_return),
            "cagr": _float(report.returns.cagr),
            "arithmetic_return": _float(report.returns.arithmetic_return),
            "geometric_return": _float(report.returns.geometric_return),
            "daily_return_count": len(report.returns.daily_returns or ()),
        },
        "risk": {
            "sharpe_ratio": _float(report.risk.sharpe_ratio),
            "sortino_ratio": _float(report.risk.sortino_ratio),
            "calmar_ratio": _float(report.risk.calmar_ratio),
            "value_at_risk_95": _float(report.risk.value_at_risk_95),
            "cvar_95": _float(report.risk.cvar_95),
            "annualized_volatility": _float(report.risk.annualized_volatility),
        },
        "drawdowns": {
            "max_drawdown": _float(report.drawdowns.max_drawdown),
            "ulcer_index": _float(report.drawdowns.ulcer_index),
            "drawdown_count": len(report.drawdowns.drawdowns or ()),
        },
        "exposure": {
            "gross": _float(report.exposure.gross),
            "net": _float(report.exposure.net),
            "long": _float(report.exposure.long),
            "short": _float(report.exposure.short),
            "cash_pct": _float(report.exposure.cash_pct),
            "leverage": _float(report.exposure.leverage),
        },
        "trades": {
            "win_rate": _float(report.trades.win_rate),
            "loss_rate": _float(report.trades.loss_rate),
            "avg_win": _money(report.trades.avg_win),
            "avg_loss": _money(report.trades.avg_loss),
            "profit_factor": _float(report.trades.profit_factor),
            "expectancy": _money(report.trades.expectancy),
            "avg_holding_period": _float(report.trades.avg_holding_period),
            "turnover": _float(report.trades.turnover),
        },
        "attribution": {
            "pnl_by_strategy": {
                str(k): _money(v) for k, v in dict(report.attribution.pnl_by_strategy or {}).items()
            },
            "pnl_by_asset": {
                str(k): _money(v) for k, v in dict(report.attribution.pnl_by_asset or {}).items()
            },
        },
    }


def skipped_of(result: BacktestResult) -> list[dict[str, Any]]:
    """Records the run declined to act on, and why.

    Surfacing these is PHASE 16's rule applied to research: a run that skipped
    half its data because the records went backwards must not present as a clean
    result.
    """

    rows: list[dict[str, Any]] = []
    for skipped in result.run.skipped.to_tuple():
        rows.append(
            {
                "event_id": getattr(getattr(skipped, "record", None), "event_id", None),
                "timestamp": getattr(skipped, "timestamp", None),
                "reason": getattr(
                    getattr(skipped, "reason", None), "name", str(getattr(skipped, "reason", ""))
                ),
            }
        )
    return rows


def headline(result: BacktestResult) -> dict[str, Any]:
    """The small set of figures a listing row shows."""

    report = result.report
    valuation = result.valuation
    return {
        "records_processed": result.records_processed,
        "order_count": len(result.orders),
        "fill_count": len(result.fills),
        "ending_equity": _money(valuation.equity),
        "realized_pnl": _money(valuation.realized_pnl),
        "unrealized_pnl": _money(valuation.unrealized_pnl),
        "commission_paid": _money(valuation.commission_paid),
        "total_return": _float(report.returns.total_return) if report else None,
        "cagr": _float(report.returns.cagr) if report else None,
        "volatility": _float(report.risk.annualized_volatility) if report else None,
        "sharpe_ratio": _float(report.risk.sharpe_ratio) if report else None,
        "max_drawdown": _float(report.drawdowns.max_drawdown) if report else None,
    }


def extract(result: BacktestResult) -> dict[str, Any]:
    """The complete result document stored as a run's artifact."""

    return {
        "dataset_id": result.dataset_id,
        "seed": result.seed,
        "records_processed": result.records_processed,
        "valuation": valuation_of(result),
        "positions": positions_of(result),
        "orders": orders_of(result),
        "fills": fills_of(result),
        "equity_curve": equity_curve_of(result),
        "report": report_of(result),
        "skipped_records": skipped_of(result),
        "unpriced_assets": [
            {
                "asset_id": str(getattr(asset, "asset_id", "")),
                "reason": getattr(
                    getattr(asset, "reason", None), "name", str(getattr(asset, "reason", ""))
                ),
            }
            for asset in result.unpriced_assets
        ],
    }
