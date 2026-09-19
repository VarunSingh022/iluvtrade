"""Payment providers, behind an interface.

PHASE 15 is explicit: *do not lock the codebase to one payment vendor; create
payment-provider interfaces first.* This package is that interface, plus the one
implementation a deployment without a merchant account can honestly offer.

**No money moves here.** :class:`ManualProvider` records a purchase as settled
without contacting anyone, which is the truthful thing for a local or internal
deployment to do. What it does *not* do is pretend: the provider name is written
on every purchase row, so a ledger entry always says how it was settled, and a
report can separate manually recorded purchases from processed ones.

Integrating a real provider means implementing :class:`PaymentProvider` and
registering it. Nothing above this package changes — ``reddesk.marketplace``
already writes the amount, the fee, the creator's net, the currency and the
licence hash correctly regardless of who settled it, so switching providers does
not restate history.
"""

from iluvtrade.billing.provider import (
    Charge,
    ChargeResult,
    ChargeStatus,
    ManualProvider,
    PaymentError,
    PaymentProvider,
    Refund,
    get_provider,
    register_provider,
)

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
