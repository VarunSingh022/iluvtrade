"""The typed backtest request, and the idempotency key derived from it.

Keeping the request a frozen object with a canonical serialization gives two
things at once: a stable idempotency key, and a record on the job of exactly what
was asked for — which is what makes a failed job diagnosable and a completed one
re-runnable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
from typing import Any

from iluvtrade.common.seeds import canonical_json, derive_seed

__all__ = ["BacktestRequest"]


@dataclass(frozen=True, slots=True)
class BacktestRequest:
    """Everything a backtest needs, stated by identity rather than by name."""

    dataset_version_id: str
    strategy_version_id: str
    parameters: dict[str, Any] = field(default_factory=dict)
    #: Empty means every symbol in the dataset version.
    universe: tuple[str, ...] = ()
    starting_cash: str = "1000000.00"
    currency: str = "INR"
    exchange: str = "XNSE"
    #: ``research`` or ``conservative``; see
    #: :class:`~iluvtrade.alphalab_bridge.runconfig.RiskProfile`.
    risk_profile: str = "research"
    commission_kind: str = "percentage"
    commission_rate: str = "0.0003"
    risk_free_rate: float = 0.0
    #: When a simulated order fills -- ``next_event`` or ``same_event``; see
    #: :data:`~iluvtrade.alphalab_bridge.runconfig.FILL_TIMINGS`. Part of the
    #: canonical request, so two requests that differ only here are two runs,
    #: with two idempotency keys and two derived seeds.
    fill_timing: str = "next_event"
    #: Fixing the seed is what makes a run reproducible field for field rather
    #: than merely equal in P&L. A request that omits it gets a stable one
    #: derived from its own content, so "run it again" means the same run.
    seed: int | None = None

    def canonical(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["parameters"] = dict(sorted(self.parameters.items()))
        payload["universe"] = sorted(self.universe)
        return payload

    def to_json(self) -> str:
        return canonical_json(self.canonical())

    @classmethod
    def from_json(cls, payload: str) -> BacktestRequest:
        data = json.loads(payload)
        data["universe"] = tuple(data.get("universe") or ())
        data["parameters"] = dict(data.get("parameters") or {})
        # A request stored before fill timing was recorded ran with AlphaLab's
        # SAME_EVENT. Reading it back as today's default would reproduce it
        # under different execution and call the result the same run.
        data.setdefault("fill_timing", "same_event")
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def fingerprint(self) -> str:
        """A stable hash of the whole request."""

        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    def idempotency_key(self) -> str:
        """The default key when a caller supplies none.

        Derived from the request, so submitting the same backtest twice — a
        double-clicked button, a retried HTTP call — returns the first job rather
        than queueing a second identical run.
        """

        return self.fingerprint()[:48]

    def effective_seed(self) -> int:
        """The stated seed, or one derived from the request (``common.seeds``).

        ``service.submit`` stores the request with this resolved, so every
        attempt -- and any later reconstruction -- reads the same number back
        rather than deriving it again from a canonical form a later release
        might extend.
        """

        if self.seed is not None:
            return self.seed
        return derive_seed(self.canonical())

    def resolved(self) -> BacktestRequest:
        """This request with its effective seed written in."""

        return replace(self, seed=self.effective_seed())

    def cash(self) -> Decimal:
        return Decimal(self.starting_cash)
