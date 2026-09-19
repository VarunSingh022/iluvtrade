# Compliance boundaries

## What this document is, and is not

This records the **software boundaries** that a compliance posture would be
built on, and names the external dependencies that software alone cannot
satisfy.

**It is not legal advice and makes no regulatory claim.** Connecting retail
users to broker APIs for automated trading is regulated activity in every
jurisdiction this could operate in. Whether this application may be operated,
by whom, and under what registration is a question for counsel and the relevant
regulator — not for an architecture decision.

## What the software provides

| Boundary | Implementation |
|---|---|
| **User consent** | Registration is explicit. Live trading needs three separate affirmative acts: a deployment setting, a per-user setting, and a per-session confirmation recorded with who made it and when. |
| **Audit records** | Append-only `audit_events`, one writer, credential-shaped values redacted at that writer. Every state change of consequence is recorded. |
| **Strategy provenance** | A strategy has an owner, versions are immutable after publication, and `content_hash` makes a later mutation detectable. |
| **Strategy version identity** | Every run — backtest, paper, live — records the exact version id. Executing "the latest" is structurally impossible. |
| **Order traceability** | Each order carries AlphaLab's own order id; the Zerodha connector tags venue orders with it, so a venue order traces to a session, a strategy version and a user. |
| **Broker connection identity** | `BrokerAccount` records the venue's own account id, learned at connection, never invented. |
| **Risk controls** | Enforced inside AlphaLab before the OMS sees an order. Not bypassable from this application. |
| **Kill switch** | Available to any trader, terminal, reason recorded, audited, notified. |
| **Execution records** | Orders, fills and positions projected from the engine and retained per session. |
| **Performance disclosure** | Every marketplace evidence block carries a disclaimer that a backtest is a simulation, not a prediction. |
| **Marketplace disclosures** | Methodology, risk disclosure and licence terms are required to publish; the licence hash is recorded at purchase. |
| **Data provenance** | Bytes as received are kept with their SHA-256; every transformation and rejection is recorded. |

## External dependencies — not satisfiable in software

| Dependency | Status |
|---|---|
| Broker API terms and app approval | Kite Connect needs a subscription and a registered app. Unobtained. |
| Regulatory registration for automated trading | Unassessed. Depends on jurisdiction and business model. |
| Algo-trading approval where a broker or exchange requires it | Some venues require per-strategy approval or a registered algo id. Not modelled. |
| KYC/AML on marketplace participants | Not implemented. A marketplace paying creators will need it. |
| Tax handling on creator payouts | Not implemented. |
| Payment processing and its regulatory surface | Not implemented; the provider interface exists. |
| Data retention and deletion obligations | No retention policy is enforced. Records are kept indefinitely. |
| Market-data licensing | Users upload their own data. Redistributing licensed vendor data has its own terms this application does not model. |

## Known gaps in the software boundary

- **No independent certification.** In this deployment the listing reviewer is
  an administrator of the creator's own organization.
- **The audit trail is append-only by convention**, not by hash chaining or an
  append-only store. A database administrator could alter it undetectably.
- **No retention or deletion machinery.** There is no way to satisfy an erasure
  request beyond deleting an organization.
- **Reconciliation is unbuilt.** AlphaLab provides `reconcile()`; no scheduled
  loop calls it, because no live session exists to reconcile.

## The honest summary

The software boundaries a compliance posture would need are in place and
testable. The regulatory, contractual and operational work that would make this
lawfully operable has not been done, and no part of this repository should be
read as claiming otherwise.
