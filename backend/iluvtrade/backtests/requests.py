"""The typed backtest request, and the idempotency key derived from it.

Keeping the request a frozen object with a canonical serialization gives two
things at once: a stable idempotency key, and a record on the job of exactly what
was asked for — which is what makes a failed job diagnosable and a completed one
re-runnable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

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
        return json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"), default=str)

    @classmethod
    def from_json(cls, payload: str) -> BacktestRequest:
        data = json.loads(payload)
        data["universe"] = tuple(data.get("universe") or ())
        data["parameters"] = dict(data.get("parameters") or {})
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
        if self.seed is not None:
            return self.seed
        return int(self.fingerprint()[:8], 16)

    def cash(self) -> Decimal:
        return Decimal(self.starting_cash)
