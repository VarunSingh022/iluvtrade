"""Application configuration → AlphaLab's ``RunConfig``.

Everything a run needs is assembled here so that a backtest, a paper session and
a live session are configured by one function with one mode argument. The
alternative — three call sites each building their own pipeline config — is how
paper and live drift apart in risk limits without anyone noticing.

**Risk limits are mandatory.** AlphaLab's ``RiskLimits`` has no defaults worth
inheriting for a real deployment, and a run configured with permissive limits
looks identical to one configured with real ones until something goes wrong.
:class:`RiskProfile` therefore has no "unlimited" constructor; the loosest thing
available is :meth:`RiskProfile.research`, which is named for what it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from alphalab.allocation.budget import CapitalBudget
from alphalab.allocation.constraints import AllocationConstraints
from alphalab.backtesting import ExecutionMode, RunConfig
from alphalab.execution.commission import PercentageCommission, PerShareCommission
from alphalab.execution.simulator import ExecutionSimulator
from alphalab.instrument.registry import InstrumentRegistry
from alphalab.portfolio.account import Account
from alphalab.risk.limits import (
    DailyLossLimit,
    DrawdownLimit,
    ExposureLimit,
    LeverageLimit,
    MarginLimit,
    OrderSizeLimit,
    PositionLimit,
    RiskLimits,
)
from alphalab.runtime.execution_pipeline import ExecutionPipelineConfig

__all__ = ["ExecutionMode", "RiskProfile", "build_run_config"]


@dataclass(frozen=True, slots=True)
class RiskProfile:
    """The hard limits a run may not exceed, in application vocabulary.

    These become AlphaLab's ``RiskLimits``, which the execution pipeline
    evaluates *before* the OMS sees an order. Nothing in iluvtrade — not the UI,
    not an API caller, not a marketplace strategy — can route around them,
    because the refusal happens inside the engine rather than in a check this
    application performs first.
    """

    max_order_notional: Decimal
    max_order_quantity: Decimal
    max_position_notional: Decimal
    max_position_quantity: Decimal
    max_gross_exposure: Decimal
    max_net_exposure: Decimal
    max_leverage: Decimal
    #: Fraction of equity that must remain as margin, 0-1.
    margin_requirement: Decimal
    max_daily_loss: Decimal
    #: Fraction, 0-1.
    max_drawdown: Decimal
    allow_shorting: bool = False
    enforce_integer_quantities: bool = True

    @classmethod
    def research(cls, capital: Decimal) -> RiskProfile:
        """Limits proportionate to the capital being simulated.

        Not "no limits": a research run with genuinely unbounded risk hides the
        strategies that only work because they were allowed to lever 300x.
        """

        return cls(
            max_order_notional=capital,
            max_order_quantity=capital,
            max_position_notional=capital * Decimal("2"),
            max_position_quantity=capital,
            max_gross_exposure=capital * Decimal("2"),
            max_net_exposure=capital * Decimal("2"),
            max_leverage=Decimal("2"),
            margin_requirement=Decimal("0.25"),
            max_daily_loss=capital * Decimal("0.25"),
            max_drawdown=Decimal("0.50"),
            allow_shorting=True,
            enforce_integer_quantities=False,
        )

    @classmethod
    def conservative(cls, capital: Decimal) -> RiskProfile:
        """The default for anything touching a real venue."""

        return cls(
            max_order_notional=capital * Decimal("0.10"),
            max_order_quantity=capital,
            max_position_notional=capital * Decimal("0.25"),
            max_position_quantity=capital,
            max_gross_exposure=capital,
            max_net_exposure=capital,
            max_leverage=Decimal("1"),
            margin_requirement=Decimal("1.00"),
            max_daily_loss=capital * Decimal("0.02"),
            max_drawdown=Decimal("0.10"),
            allow_shorting=False,
            enforce_integer_quantities=True,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_order_notional": str(self.max_order_notional),
            "max_order_quantity": str(self.max_order_quantity),
            "max_position_notional": str(self.max_position_notional),
            "max_position_quantity": str(self.max_position_quantity),
            "max_gross_exposure": str(self.max_gross_exposure),
            "max_net_exposure": str(self.max_net_exposure),
            "max_leverage": str(self.max_leverage),
            "margin_requirement": str(self.margin_requirement),
            "max_daily_loss": str(self.max_daily_loss),
            "max_drawdown": str(self.max_drawdown),
            "allow_shorting": self.allow_shorting,
            "enforce_integer_quantities": self.enforce_integer_quantities,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RiskProfile:
        return cls(
            max_order_notional=Decimal(str(payload["max_order_notional"])),
            max_order_quantity=Decimal(str(payload["max_order_quantity"])),
            max_position_notional=Decimal(str(payload["max_position_notional"])),
            max_position_quantity=Decimal(str(payload["max_position_quantity"])),
            max_gross_exposure=Decimal(str(payload["max_gross_exposure"])),
            max_net_exposure=Decimal(str(payload["max_net_exposure"])),
            max_leverage=Decimal(str(payload["max_leverage"])),
            margin_requirement=Decimal(str(payload["margin_requirement"])),
            max_daily_loss=Decimal(str(payload["max_daily_loss"])),
            max_drawdown=Decimal(str(payload["max_drawdown"])),
            allow_shorting=bool(payload.get("allow_shorting", False)),
            enforce_integer_quantities=bool(payload.get("enforce_integer_quantities", True)),
        )

    def to_limits(self) -> RiskLimits:
        """The AlphaLab object the engine actually enforces."""

        return RiskLimits(
            order_size=OrderSizeLimit(self.max_order_notional, self.max_order_quantity),
            position=PositionLimit(self.max_position_notional, self.max_position_quantity),
            exposure=ExposureLimit(self.max_gross_exposure, self.max_net_exposure),
            leverage=LeverageLimit(self.max_leverage),
            margin=MarginLimit(self.margin_requirement),
            daily_loss=DailyLossLimit(self.max_daily_loss),
            drawdown=DrawdownLimit(self.max_drawdown),
        )


def _commission_model(kind: str, rate: Decimal) -> PerShareCommission | PercentageCommission:
    """The cost model a simulated venue charges.

    Named explicitly rather than defaulted: a backtest run with zero costs is a
    different claim from one run with real ones, and which was used has to be
    recorded on the run.
    """

    if kind == "percentage":
        return PercentageCommission(rate)
    return PerShareCommission(rate)


def build_run_config(
    *,
    mode: ExecutionMode,
    account_id: str,
    account_name: str,
    currency: str,
    starting_cash: Decimal,
    risk: RiskProfile,
    strategy_ids: tuple[str, ...],
    instruments: InstrumentRegistry | None = None,
    seed: int,
    start_timestamp: float,
    venue: str = "SIM",
    commission_kind: str = "percentage",
    commission_rate: Decimal = Decimal("0.0003"),
    years_elapsed: float = 1.0,
    risk_free_rate: float = 0.0,
    max_market_data_age_seconds: float | None = None,
) -> RunConfig:
    """Assemble the config every environment runs under."""

    # Currency is passed explicitly at all three places that take one. AlphaLab
    # defaults ``ExecutionPipelineConfig.currency`` to USD and ``CapitalBudget``
    # to the empty string, and this product's first venue settles in INR — a
    # default inherited here would value an Indian book in dollars and reconcile
    # against nothing.
    budget = CapitalBudget(
        global_capital=starting_cash,
        maximum_exposure=risk.max_gross_exposure,
        cash_buffer=Decimal("0"),
        strategy_budgets=dict.fromkeys(strategy_ids, starting_cash),
        currency=currency,
    )
    pipeline = ExecutionPipelineConfig(
        account=Account(account_id, currency, account_name, start_timestamp),
        starting_cash=starting_cash,
        budget=budget,
        allocation_constraints=AllocationConstraints(
            allow_shorting=risk.allow_shorting,
            enforce_integer_quantities=risk.enforce_integer_quantities,
        ),
        risk_limits=risk.to_limits(),
        simulator=ExecutionSimulator(
            commission_model=_commission_model(commission_kind, commission_rate)
        ),
        currency=currency,
        venue=venue,
        instruments=instruments,
    )
    return RunConfig(
        pipeline=pipeline,
        mode=mode,
        seed=seed,
        start_timestamp=start_timestamp,
        years_elapsed=years_elapsed,
        risk_free_rate=risk_free_rate,
        max_market_data_age_seconds=max_market_data_age_seconds,
        compile_analytics=True,
    )
