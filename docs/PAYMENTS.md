# Payments

**Status: DISABLED. No money moves in this build.**

`GET /api/v1/reddesk/billing-status` reports which of two very different things
a purchase is, and the marketplace UI shows it before the button is clicked:

| `state` | Means |
|---|---|
| `READY_FOR_PROVIDER_INTEGRATION` | A purchase records the licence and grants the entitlement. **Nothing is charged.** |
| `REAL_PAYMENT_PROCESSING_ENABLED` | A configured provider settles purchases. |

Only the first is reachable today. That distinction is surfaced rather than
buried because a buyer who believes they paid — or a creator who believes they
were paid — when nothing was charged is the worst failure this subsystem has.

## The state machine

```
                    purchase()
                        │
              provider.charge(idempotency_key)
                        │
        ┌───────────────┼───────────────┐
      FAILED         PENDING           PAID
        │               │                │
   ListingError    ListingError   Entitlement ACTIVE
   nothing         no entitlement        │
   written         until settled         │
                                    refund(reason)
                                         │
                              provider.refund(reference)
                                         │
                            ┌────────────┴────────────┐
                          FAILED                   REFUNDED
                            │                         │
                     nothing changes         Entitlement REVOKED
```

Three properties hold at every edge:

- **An entitlement is granted only on `PAID`.** A `PENDING` purchase raises
  rather than granting one — letting someone run a strategy they have not paid
  for and revoking it later is worse than making them wait.
- **A provider refusal changes nothing.** Recording a refund that did not happen
  puts a false entry in a financial ledger.
- **A refund revokes.** A refunded purchase that left a working licence behind
  is a strategy being run for free, and the buyer has no signal their access is
  no longer legitimate.

## Idempotency

| Operation | Key | Behaviour |
|---|---|---|
| Purchase | caller-supplied `idempotency_key` | returns the original purchase and entitlement |
| Charge | the same key, passed through to the provider | the provider must not collect twice |
| Refund | the purchase's own state | a second refund returns the first result |

## The provider interface

`iluvtrade/billing/provider.py`. Four operations and one honesty flag:

```python
class PaymentProvider(Protocol):
    name: str
    moves_money: bool          # False for ManualProvider
    def charge(self, charge: Charge) -> ChargeResult: ...
    def refund(self, refund: Refund) -> ChargeResult: ...
    def payout(self, *, organization_id, amount, currency) -> ChargeResult: ...
```

Deliberately small. An interface that models every vendor's concepts models
none of them; this names the four operations the marketplace performs and leaves
payment methods, customer objects, mandates and 3-D Secure to the
implementation, where they belong.

`get_provider()` **refuses an unknown name** rather than falling back to
`manual` — a deployment that configured a real provider and typo'd its name must
not silently record purchases as settled.

### ManualProvider fails closed

`charge` and `refund` succeed without contacting anyone, which is the truthful
thing a deployment with no merchant account can do — and `moves_money` is
`False`, so nothing downstream can mistake it.

`payout` **raises**. Recording a payout as complete when no money left an
account would put a false entry in a financial ledger, and that is worse than
the feature being unavailable.

## Payouts

`marketplace.record_payout()` totals what a creator organization is owed for a
period from `PAID` purchases only — a refunded one contributes nothing, which is
why it is computed rather than incremented as sales arrive. The row's `status`
stays `pending` until something actually settles it.

| Capability | Status |
|---|---|
| Payout accounting | IMPLEMENTED |
| Payout settlement | REQUIRES EXTERNAL INFRASTRUCTURE |

## Integrating a real provider

1. Implement `PaymentProvider`; `charge` must be idempotent in
   `charge.idempotency_key` at the *provider*, not only here.
2. `register_provider(YourProvider())` at startup.
3. Set `ILUVTRADE_PAYMENT_PROVIDER` to its name.
4. Add a webhook endpoint with signature verification and replay protection —
   **not built**, and the reason `PENDING` exists in the state machine: an
   asynchronous settlement has to arrive somewhere.

External dependencies: a merchant account, a signed contract, a publicly
reachable webhook endpoint, and — for an Indian marketplace paying creators —
tax handling this repository does not attempt.

## What a purchase grants

A licence to run a specific strategy version on iluvtrade for the term. It does
**not** transfer ownership of the strategy or its source. The listing's own
licence text governs, and the buyer's purchase records its SHA-256 — so "what
did I agree to" has an answer that cannot be edited afterwards.
