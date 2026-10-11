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

from alphalab.allocation import AllocationConstraints, CapitalBudget
from alphalab.backtesting import ExecutionMode, RunConfig
from alphalab.execution import (
    ExecutionSimulator,
    FillTiming,
    PercentageCommission,
    PerShareCommission,
)
from alphalab.instrument import InstrumentRegistry
from alphalab.portfolio import Account
from alphalab.risk import (
    DailyLossLimit,
    DrawdownLimit,
    ExposureLimit,
    LeverageLimit,
    MarginLimit,
    OrderSizeLimit,
    PositionLimit,
    RiskLimits,
)
from alphalab.runtime import ExecutionPipelineConfig

__all__ = [
    "DEFAULT_FILL_TIMING",
    "FILL_TIMINGS",
    "LEGACY_FILL_TIMING",
    "TRADING_DAY_ZONES",
    "ExecutionMode",
    "RiskProfile",
    "UnsupportedExchangeError",
    "UnsupportedFillTimingError",
    "build_run_config",
    "fill_timing_for",
    "trading_day_zone",
]

#: The IANA time zone each supported exchange keeps its trading day in.
#:
#: AlphaLab's daily-loss limit is "the most equity the book may lose within one
#: trading day", and since AlphaLab 3.10 it requires the zone that day is kept
#: in — "a day's boundary is a place's, and no place is assumed". The exchange a
#: run trades on is already stated (it is part of every instrument's identity;
#: see :mod:`.instruments`), so the zone is derived from it rather than asked for
#: twice. Both Indian exchanges keep India Standard Time.
TRADING_DAY_ZONES: dict[str, str] = {
    "XNSE": "Asia/Kolkata",
    "XBOM": "Asia/Kolkata",
}


#: When a simulated order fills, by the name this application stores and shows,
#: and the AlphaLab setting it selects (``alphalab.execution.FillTiming``,
#: ledger EXE-001). The meaning is AlphaLab's, quoted for the user:
#:
#: * ``next_event`` -- the order works until its asset's next market event and
#:   fills at that event's price and instant (for bars, the next bar's close).
#:   The strategy decides on one observation and trades on the next: no
#:   look-ahead, at the cost of one event's delay.
#: * ``same_event`` -- the order fills at the price and instant of the event
#:   that produced it: the close the strategy has just seen. No real order trades
#:   at the price that triggered it, so AlphaLab calls this optimistic by
#:   construction.
FILL_TIMINGS: dict[str, FillTiming] = {
    "next_event": FillTiming.NEXT_EVENT,
    "same_event": FillTiming.SAME_EVENT,
}

#: What a run gets when nobody chose: the conservative one. ``same_event`` is
#: available only by asking for it.
DEFAULT_FILL_TIMING = "next_event"

#: What every run stored before fill timing was recorded actually used. A stored
#: request or session that names no timing is read as this -- never as the
#: current default, which would reproduce it under different execution.
LEGACY_FILL_TIMING = "same_event"


class UnsupportedFillTimingError(ValueError):
    """The run names a fill timing AlphaLab does not offer."""


def fill_timing_for(name: str) -> FillTiming:
    """AlphaLab's setting for ``name``, or a refusal -- never a fallback."""

    try:
        return FILL_TIMINGS[name]
    except KeyError:
        raise UnsupportedFillTimingError(
            f"Fill timing {name!r} is not one AlphaLab offers. "
            f"Choose one of: {', '.join(sorted(FILL_TIMINGS))}."
        ) from None


class UnsupportedExchangeError(ValueError):
    """The run names an exchange whose trading day this deployment cannot place."""


def trading_day_zone(exchange: str) -> str:
    """The zone a run on ``exchange`` keeps its trading day in, or a refusal.

    Refused rather than defaulted to UTC: a daily-loss limit that resets at UTC
    midnight on an Indian book resets at 05:30 local, mid-way through nobody's
    trading day, and the run would look correctly limited while not being so.
    """

    try:
        return TRADING_DAY_ZONES[exchange]
    except KeyError:
        raise UnsupportedExchangeError(
            f"Exchange {exchange!r} has no declared trading-day time zone, which the "
            f"daily-loss limit requires. Supported: {', '.join(sorted(TRADING_DAY_ZONES))}."
        ) from None


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

    def to_limits(self, *, trading_day_zone: str) -> RiskLimits:
        """The AlphaLab object the engine actually enforces.

        ``trading_day_zone`` is where the daily-loss limit's day begins and ends
        (:func:`trading_day_zone`). It is not part of the profile: the same
        limits apply on any exchange, and a profile stored on a session before
        the zone was required must still read back.
        """

        return RiskLimits(
            order_size=OrderSizeLimit(self.max_order_notional, self.max_order_quantity),
            position=PositionLimit(self.max_position_notional, self.max_position_quantity),
            exposure=ExposureLimit(self.max_gross_exposure, self.max_net_exposure),
            leverage=LeverageLimit(self.max_leverage),
            margin=MarginLimit(self.margin_requirement),
            daily_loss=DailyLossLimit(self.max_daily_loss, zone=trading_day_zone),
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
    exchange: str,
    fill_timing: str,
    strategy_ids: tuple[str, ...],
    instruments: InstrumentRegistry | None = None,
    seed: int,
    start_timestamp: float,
    venue: str = "SIM",
    commission_kind: str = "percentage",
    commission_rate: Decimal = Decimal("0.0003"),
    # ``None``: AlphaLab derives the span from the equity curve's first and last
    # instants. This used to be 1.0, copying AlphaLab's own old default, which
    # AlphaLab 3.10 removed as a defect — every run's CAGR and Calmar ratio were
    # compounded as if it had lasted exactly a year, and paper sessions here
    # still inherited it.
    years_elapsed: float | None = None,
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
        risk_limits=risk.to_limits(trading_day_zone=trading_day_zone(exchange)),
        simulator=ExecutionSimulator(
            commission_model=_commission_model(commission_kind, commission_rate)
        ),
        currency=currency,
        venue=venue,
        instruments=instruments,
        # Stated on every run, never inherited: AlphaLab's own default is the
        # optimistic SAME_EVENT, kept there so that old runs keep their meaning.
        fill_timing=fill_timing_for(fill_timing),
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
