# RedDesk

The marketplace. It distributes strategies; it does not run them.

## Creator workflow

```
DRAFT ──validate──→ SUBMITTED ──review──→ APPROVED ──publish──→ PUBLISHED
  ▲                      │
  └──────issues──────────┘                          WITHDRAWN (entitlements survive)
```

`validate_listing()` is a real check, not a status flip. It refuses a listing
that has no description of substance, no methodology, no risk disclosure, an
unpublished strategy version, an invalid price, a claim of support for a broker
this platform has no connector for, or a paid offering with no licence terms.
A marketplace that lets any of those be published is one where the listing text
and the software disagree.

Withdrawing a listing does **not** revoke entitlements. Breaking running
sessions and rewriting what people paid for is not an acceptable consequence of
a creator delisting.

## Evidence

A listing version may cite a backtest run. That run must be one **this
organization actually performed on this platform, against this exact strategy
version** — a creator cannot cite a number they typed, or another strategy's
result. A listing with no evidence is publishable and is shown as having none.

Every displayed evidence block carries:

> A backtest is a simulation over historical data under stated assumptions. It
> is not a prediction and not a guarantee of future performance.

## The entitlement

The load-bearing object, and the reason versioning exists.

`entitlements.resolve_version()` answers with a **concrete strategy version id**
or raises. There is no boolean anywhere, because a boolean invites the caller to
then pick a version itself — and picking "the latest" is exactly the defect this
prevents.

Two ways to be permitted:

- **Ownership.** The strategy belongs to the organization, and the version is
  published.
- **Entitlement.** A purchase granted a concrete version.

### Licence policies

| Policy | Meaning |
|---|---|
| `PINNED` | exactly the version purchased, forever |
| `ROLLING` | may be advanced to the listing's current version — by an explicit, audited call, never at read time |

Nothing advances implicitly. A buyer whose entitlement silently followed the
creator's newest publish would be running code they never agreed to, which is
the same defect as executing "latest".

`tests/security/test_entitlements.py` asserts all of it: a pinned entitlement
does not follow a new publication and cannot reach the new version by naming it;
a rolling one advances only when asked; a revoked one stops resolving.

## Purchases

A purchase records the amount, currency, platform fee, creator net, the provider
and the **SHA-256 of the licence terms as they stood at purchase** — so "what
did I agree to" has an answer that cannot be edited afterwards.

Purchases are idempotent on an explicit key.

## What is honest about the commerce

**No money moves.** `provider="manual"` records a settled purchase without a
payment processor, which is what a local deployment can honestly do. The ledger
fields are written correctly either way, so switching providers does not restate
history.

`iluvtrade/billing/` defines the provider interface. Integrating a real one
needs a merchant account, a contract, webhook endpoints with signature
verification, and — for an Indian marketplace paying creators — tax handling
this repository does not attempt.

**Creator payouts are recorded, not paid.** `CreatorPayout` makes the ledger
auditable. Actually moving money is an external dependency.

**Review is not independent.** In this deployment the reviewer is an
administrator of the creator's own organization. That is coherent for a
single-tenant install and is not third-party certification. `CertificationStatus`
exists and is set by a human; nothing in this application earns it automatically.

## What a purchase does not grant

A licence to run the strategy on iluvtrade for the term. It does **not** transfer
ownership of the strategy or its source, and the listing's own licence text —
whose hash the buyer's purchase records — is what actually governs.
