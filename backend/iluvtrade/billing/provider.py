"""The payment-provider contract.

Deliberately small. A payment interface that models every vendor's concepts
ends up modelling none of them; this names the four operations the marketplace
actually performs and leaves everything else — payment methods, customer
objects, mandates, 3-D Secure — to the implementation, where it belongs.

The money type is ``Decimal`` throughout, for the same reason it is everywhere
else in this application: a float cannot represent a currency amount exactly,
and a fee computed in floating point will eventually not sum to the total.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

__all__ = [
    "Charge",
    "ChargeResult",
    "ChargeStatus",
    "ManualProvider",
    "PaymentError",
    "PaymentProvider",
    "Refund",
    "get_provider",
    "register_provider",
]


class PaymentError(RuntimeError):
    """The provider refused, or could not be reached.

    Implementations must distinguish these two in the message, because a refusal
    must not be retried and an unreachable provider must not be treated as a
    refusal — the second is how a customer gets charged twice.
    """


class ChargeStatus(enum.StrEnum):
    #: Settled. An entitlement may be granted.
    SUCCEEDED = "succeeded"
    #: The provider accepted it but has not settled. No entitlement yet.
    PENDING = "pending"
    #: Refused. The message says why.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Charge:
    """What the marketplace is asking to be collected."""

    amount: Decimal
    currency: str
    #: The buyer organization. Never a person — the licence is held by the org.
    organization_id: str
    #: What is being bought, for the provider's own records.
    description: str
    #: Must make the charge idempotent at the provider. A retried request with
    #: the same key must not collect twice.
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ChargeResult:
    status: ChargeStatus
    #: The provider's own identifier, stored on the purchase row so a ledger
    #: entry can be traced back to the provider's records.
    reference: str
    message: str = ""

    @property
    def is_settled(self) -> bool:
        return self.status is ChargeStatus.SUCCEEDED


@dataclass(frozen=True, slots=True)
class Refund:
    reference: str
    amount: Decimal
    reason: str


@runtime_checkable
class PaymentProvider(Protocol):
    """What the marketplace needs from a payment vendor."""

    @property
    def name(self) -> str:
        """Recorded on every purchase, so the ledger says how it was settled."""
        ...

    @property
    def moves_money(self) -> bool:
        """Whether this provider actually transfers funds.

        ``False`` for :class:`ManualProvider`. Exposed so a deployment can
        report honestly on whether its revenue figures represent collected money
        or recorded intent.
        """
        ...

    def charge(self, charge: Charge) -> ChargeResult:
        """Collect ``charge``. Must be idempotent in ``charge.idempotency_key``."""
        ...

    def refund(self, refund: Refund) -> ChargeResult:
        """Return funds previously collected."""
        ...

    def payout(self, *, organization_id: str, amount: Decimal, currency: str) -> ChargeResult:
        """Pay a creator organization its net proceeds."""
        ...


class ManualProvider:
    """Records a purchase as settled without contacting anyone.

    The right implementation for a deployment with no merchant account, and the
    honest one: it says so through :attr:`moves_money`, and its name appears on
    every purchase it settles.

    :meth:`payout` deliberately **raises**. Recording a payout as complete when
    no money left an account would put a false entry in a financial ledger,
    which is worse than the feature being unavailable.
    """

    @property
    def name(self) -> str:
        return "manual"

    @property
    def moves_money(self) -> bool:
        return False

    def charge(self, charge: Charge) -> ChargeResult:
        return ChargeResult(
            status=ChargeStatus.SUCCEEDED,
            reference=f"manual:{charge.idempotency_key}",
            message="Recorded without a payment processor.",
        )

    def refund(self, refund: Refund) -> ChargeResult:
        return ChargeResult(
            status=ChargeStatus.SUCCEEDED,
            reference=f"manual-refund:{refund.reference}",
            message="Recorded without a payment processor.",
        )

    def payout(self, *, organization_id: str, amount: Decimal, currency: str) -> ChargeResult:
        raise PaymentError(
            "This deployment has no payment provider, so a payout cannot be made. "
            "The amount owed is recorded in creator_payouts; settling it requires a "
            "provider that actually moves money."
        )


_PROVIDERS: dict[str, PaymentProvider] = {"manual": ManualProvider()}


def register_provider(provider: PaymentProvider) -> None:
    """Register an implementation under its own name."""

    if not isinstance(provider, PaymentProvider):
        raise TypeError(f"{type(provider).__name__} does not satisfy PaymentProvider.")
    _PROVIDERS[provider.name] = provider


def get_provider(name: str = "manual") -> PaymentProvider:
    """The provider registered under ``name``, or refuse.

    Refusing an unknown name rather than falling back to ``manual`` is the
    point: a deployment that configured a real provider and typo'd its name must
    not silently record purchases as settled instead.
    """

    try:
        return _PROVIDERS[name]
    except KeyError as exc:
        raise PaymentError(
            f"No payment provider named {name!r} is registered. "
            f"Known: {', '.join(sorted(_PROVIDERS))}."
        ) from exc
