"""The strategies this deployment can run, and the runtime that hosts them.

Every strategy here implements AlphaLab's
:class:`~alphalab.strategy.protocol.StrategyProtocol` and emits
:class:`~alphalab.strategy.events.Intent` objects. **They decide nothing else.**
Sizing, risk, order construction, execution and accounting all happen downstream
in AlphaLab's pipeline, which is why a strategy in this file is thirty lines and
not three hundred.

Why the implementations live in this repository
-----------------------------------------------

PHASE 10 requires that marketplace strategies be treated as untrusted code and
run behind a sandbox. That sandbox does not exist in this deployment. Rather
than build half of it — which would read as safety while providing none — the
registry maps a listing to a **declared, in-repository implementation**. A
creator lists a strategy by choosing one of these and supplying parameters;
uploading arbitrary Python is not a feature that exists, so there is no path for
seller code to reach this process. ``docs/SECURITY.md`` records the isolated
worker as the deferred dependency it is.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from alphalab.strategy.context import StrategyContext
from alphalab.strategy.events import Intent
from alphalab.strategy.protocol import BaseStrategy, StrategyProtocol

__all__ = [
    "IMPLEMENTATIONS",
    "ParameterSpec",
    "StrategyImplementation",
    "UnknownImplementationError",
    "build",
    "describe",
    "validate_parameters",
]


class UnknownImplementationError(LookupError):
    """The requested implementation key is not registered in this deployment."""


class ParameterError(ValueError):
    """The supplied parameters do not satisfy the implementation's schema."""


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """One tunable parameter, with the bounds the UI renders and the API enforces."""

    name: str
    kind: str  # "int" | "number" | "string"
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "default": self.default,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "description": self.description,
        }

    def coerce(self, value: Any) -> Any:
        try:
            match self.kind:
                case "int":
                    coerced: Any = int(value)
                case "number":
                    coerced = float(value)
                case _:
                    coerced = str(value)
        except (TypeError, ValueError) as exc:
            raise ParameterError(f"{self.name!r} is not a valid {self.kind}.") from exc
        if self.kind in {"int", "number"}:
            if self.minimum is not None and coerced < self.minimum:
                raise ParameterError(f"{self.name!r} must be at least {self.minimum}.")
            if self.maximum is not None and coerced > self.maximum:
                raise ParameterError(f"{self.name!r} must be at most {self.maximum}.")
        return coerced


# ---------------------------------------------------------------------------
# Strategy implementations
# ---------------------------------------------------------------------------


class MovingAverageCrossover(BaseStrategy):
    """Long when a fast mean crosses above a slow mean, flat when it crosses below.

    The canonical worked example. It holds a rolling window per instrument, which
    makes it the right shape to show what a stateful strategy looks like without
    the state being interesting enough to distract.
    """

    def __init__(self, strategy_id: str, *, fast: int, slow: int, quantity: Decimal) -> None:
        if fast >= slow:
            raise ParameterError("fast must be strictly less than slow.")
        self._strategy_id = strategy_id
        self._fast, self._slow = fast, slow
        self._quantity = quantity
        self._closes: dict[str, list[Decimal]] = {}
        self._long: dict[str, bool] = {}

    def on_bar(self, context: StrategyContext, event: Any) -> Iterable[Intent]:
        bar = event.bar
        window = self._closes.setdefault(bar.asset_id, [])
        window.append(bar.close)
        if len(window) > self._slow:
            del window[0 : len(window) - self._slow]
        if len(window) < self._slow:
            return ()

        fast_mean = sum(window[-self._fast :]) / Decimal(self._fast)
        slow_mean = sum(window) / Decimal(len(window))
        currently_long = self._long.get(bar.asset_id, False)

        if fast_mean > slow_mean and not currently_long:
            self._long[bar.asset_id] = True
            return (self._intent(bar, self._quantity),)
        if fast_mean <= slow_mean and currently_long:
            self._long[bar.asset_id] = False
            return (self._intent(bar, -self._quantity),)
        return ()

    def _intent(self, bar: Any, target: Decimal) -> Intent:
        return Intent(
            strategy_id=self._strategy_id,
            instrument=bar.asset_id,
            target=target,
            timestamp=bar.timestamp,
        )


class BuyAndHold(BaseStrategy):
    """Buy once per instrument on its first bar, then do nothing.

    The benchmark every other result should be read against.
    """

    def __init__(self, strategy_id: str, *, quantity: Decimal) -> None:
        self._strategy_id = strategy_id
        self._quantity = quantity
        self._bought: set[str] = set()

    def on_bar(self, context: StrategyContext, event: Any) -> Iterable[Intent]:
        bar = event.bar
        if bar.asset_id in self._bought:
            return ()
        self._bought.add(bar.asset_id)
        return (
            Intent(
                strategy_id=self._strategy_id,
                instrument=bar.asset_id,
                target=self._quantity,
                timestamp=bar.timestamp,
            ),
        )


class MeanReversionBands(BaseStrategy):
    """Buy ``entry_sigma`` below a rolling mean, exit on reversion to it.

    Uses a population standard deviation over the same window as the mean.
    Computed in ``Decimal`` throughout — a float square root would make two runs
    of the same data disagree in the last place, which is exactly the kind of
    irreproducibility a backtest must not have.
    """

    def __init__(
        self, strategy_id: str, *, window: int, entry_sigma: float, quantity: Decimal
    ) -> None:
        self._strategy_id = strategy_id
        self._window = window
        self._entry = Decimal(str(entry_sigma))
        self._quantity = quantity
        self._closes: dict[str, list[Decimal]] = {}
        self._long: dict[str, bool] = {}

    def on_bar(self, context: StrategyContext, event: Any) -> Iterable[Intent]:
        bar = event.bar
        window = self._closes.setdefault(bar.asset_id, [])
        window.append(bar.close)
        if len(window) > self._window:
            del window[0 : len(window) - self._window]
        if len(window) < self._window:
            return ()

        count = Decimal(len(window))
        mean = sum(window) / count
        variance = sum((value - mean) ** 2 for value in window) / count
        deviation = variance.sqrt()
        currently_long = self._long.get(bar.asset_id, False)

        if not currently_long and deviation > 0 and bar.close < mean - self._entry * deviation:
            self._long[bar.asset_id] = True
            return (self._intent(bar, self._quantity),)
        if currently_long and bar.close >= mean:
            self._long[bar.asset_id] = False
            return (self._intent(bar, -self._quantity),)
        return ()

    def _intent(self, bar: Any, target: Decimal) -> Intent:
        return Intent(
            strategy_id=self._strategy_id,
            instrument=bar.asset_id,
            target=target,
            timestamp=bar.timestamp,
        )


@dataclass(frozen=True, slots=True)
class StrategyImplementation:
    """A registered implementation: how to describe it and how to build it."""

    key: str
    name: str
    description: str
    parameters: tuple[ParameterSpec, ...]
    factory: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "parameters": [p.to_dict() for p in self.parameters],
        }


IMPLEMENTATIONS: dict[str, StrategyImplementation] = {
    impl.key: impl
    for impl in (
        StrategyImplementation(
            key="moving_average_crossover",
            name="Moving average crossover",
            description=(
                "Goes long when the fast moving average crosses above the slow one and "
                "flattens when it crosses back below. Trend-following; loses money in "
                "sideways markets by construction."
            ),
            parameters=(
                ParameterSpec("fast", "int", 10, 2, 400, "Fast window, in bars."),
                ParameterSpec("slow", "int", 30, 3, 1000, "Slow window, in bars."),
                ParameterSpec("quantity", "number", 10, 0.000001, 1e9, "Units per signal."),
            ),
            factory=lambda sid, p: MovingAverageCrossover(
                sid, fast=p["fast"], slow=p["slow"], quantity=Decimal(str(p["quantity"]))
            ),
        ),
        StrategyImplementation(
            key="buy_and_hold",
            name="Buy and hold",
            description="Buys each instrument on its first bar and holds. The benchmark.",
            parameters=(
                ParameterSpec("quantity", "number", 10, 0.000001, 1e9, "Units per instrument."),
            ),
            factory=lambda sid, p: BuyAndHold(sid, quantity=Decimal(str(p["quantity"]))),
        ),
        StrategyImplementation(
            key="mean_reversion_bands",
            name="Mean reversion bands",
            description=(
                "Buys when price falls a chosen number of standard deviations below its "
                "rolling mean and exits on reversion to the mean."
            ),
            parameters=(
                ParameterSpec("window", "int", 20, 3, 500, "Lookback, in bars."),
                ParameterSpec("entry_sigma", "number", 2.0, 0.1, 6.0, "Entry threshold, in sigma."),
                ParameterSpec("quantity", "number", 10, 0.000001, 1e9, "Units per signal."),
            ),
            factory=lambda sid, p: MeanReversionBands(
                sid,
                window=p["window"],
                entry_sigma=p["entry_sigma"],
                quantity=Decimal(str(p["quantity"])),
            ),
        ),
    )
}


def describe() -> list[dict[str, Any]]:
    """Every implementation this deployment offers."""

    return [impl.to_dict() for impl in IMPLEMENTATIONS.values()]


def get(key: str) -> StrategyImplementation:
    try:
        return IMPLEMENTATIONS[key]
    except KeyError as exc:
        raise UnknownImplementationError(
            f"{key!r} is not a strategy implementation this deployment registers. "
            f"Known: {', '.join(sorted(IMPLEMENTATIONS))}."
        ) from exc


def validate_parameters(key: str, supplied: dict[str, Any] | None) -> dict[str, Any]:
    """Coerce and bound-check parameters, filling defaults for what is absent.

    Unknown keys are refused rather than ignored: a typo'd parameter that
    silently takes its default produces a backtest of a strategy the user did not
    configure, and nothing in the result reveals it.
    """

    implementation = get(key)
    supplied = dict(supplied or {})
    known = {spec.name for spec in implementation.parameters}
    unknown = sorted(set(supplied) - known)
    if unknown:
        raise ParameterError(
            f"Unknown parameter(s) for {key!r}: {', '.join(unknown)}. "
            f"Accepted: {', '.join(sorted(known))}."
        )
    resolved: dict[str, Any] = {}
    for spec in implementation.parameters:
        resolved[spec.name] = (
            spec.coerce(supplied[spec.name]) if spec.name in supplied else spec.default
        )
    return resolved


def build(key: str, strategy_id: str, parameters: dict[str, Any]) -> StrategyProtocol:
    """Construct one strategy instance. Parameters must already be validated."""

    implementation = get(key)
    instance = implementation.factory(strategy_id, parameters)
    if not isinstance(instance, StrategyProtocol):
        raise UnknownImplementationError(
            f"{key!r} produced {type(instance).__name__}, which does not satisfy "
            "AlphaLab's StrategyProtocol."
        )
    return instance


def parameter_schema(key: str) -> dict[str, Any]:
    """The JSON schema stored on a strategy version."""

    return {"implementation": key, "parameters": [p.to_dict() for p in get(key).parameters]}


def default_parameters(key: str) -> dict[str, Any]:
    return {spec.name: spec.default for spec in get(key).parameters}


def known_keys() -> Sequence[str]:
    return tuple(sorted(IMPLEMENTATIONS))
