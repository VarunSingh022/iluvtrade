# Implementation status

One table, one vocabulary, no ambiguity. Every claim elsewhere in these
documents should agree with this page; where it does not, this page is correct.

## The vocabulary

| Label | Means |
|---|---|
| **IMPLEMENTED** | Written, and exercised by the automated suite |
| **TESTED LOCALLY** | Exercised end to end on this machine against real dependencies |
| **INTEGRATION-READY** | The boundary is complete and tested against a faithful local double; it has never touched the real external system |
| **REQUIRES EXTERNAL CREDENTIALS** | Cannot be verified without a contract, account or subscription this build does not have |
| **DISABLED** | Present, deliberately switched off, and refused at runtime |
| **NOT IMPLEMENTED** | Absent. No partial version exists |

**This project is not production-ready.** Local tests passing means the code
does what the tests say on one machine. It does not mean the system has been
operated, load-tested, penetration-tested, or run against a real venue.

## Platform

| Capability | Status | Notes |
|---|---|---|
| Registration, login, sessions | IMPLEMENTED | Argon2id; tokens stored as HMAC only |
| Organizations, membership, roles | IMPLEMENTED | Four roles, totally ordered |
| Tenant isolation | IMPLEMENTED | Enforced by `scoped()`; 32-operation cross-tenant matrix test |
| Role-based authorization | IMPLEMENTED | Negative tests per role |
| CSRF protection | IMPLEMENTED | All 33 mutating routes asserted |
| Rate limiting | IMPLEMENTED | In-process, per-principal; **not shared across workers** |
| Audit trail | IMPLEMENTED | Append-only, redacted at the writer |
| Audit tamper-evidence | IMPLEMENTED | Per-tenant hash chain; see the limits in `SECURITY.md` |
| Notifications (in-app) | IMPLEMENTED | |
| Email / push delivery | NOT IMPLEMENTED | No email channel of any kind exists |
| MFA | NOT IMPLEMENTED | |
| Password reset | NOT IMPLEMENTED | Needs an email channel first |
| User invitations | NOT IMPLEMENTED | Registration creates a personal workspace only |
| Secret rotation | NOT IMPLEMENTED | Envelopes carry a key id so it *can* be added; see `SECURITY.md` |

## Data workspace

| Capability | Status | Notes |
|---|---|---|
| CSV upload | IMPLEMENTED | Streamed size limit; traversal-safe storage keys |
| CSV fetch from a URL | IMPLEMENTED | Host + port allowlist, explicit address deny-list, per-hop redirect checks |
| Schema detection | IMPLEMENTED | Refuses ambiguous dates rather than guessing |
| Cleaning with a full transformation log | IMPLEMENTED | Every change carries before/after |
| Quality report | IMPLEMENTED | Counted, never estimated |
| Approval gate | IMPLEMENTED | Nothing may use an unapproved version |
| Provenance | IMPLEMENTED | Original bytes retained and hashed |
| Authenticated data sources | NOT IMPLEMENTED | Needs per-source credential storage |

## Research

| Capability | Status | Notes |
|---|---|---|
| Asynchronous backtest jobs | IMPLEMENTED | In-process worker pool |
| Idempotent submission | IMPLEMENTED | Key lookup plus a unique constraint |
| Reproducibility | IMPLEMENTED | Replay matches order-id for order-id |
| Risk-refusal reporting | IMPLEMENTED | Refusals are never silent |
| Distributed workers | NOT IMPLEMENTED | `claim_next` takes the lock one would need |
| Backtest cancellation mid-run | IMPLEMENTED | Honoured between records |

## Trading

| Capability | Status | Notes |
|---|---|---|
| Paper sessions | TESTED LOCALLY | Real AlphaLab; parity with backtest asserted |
| Session lifecycle + kill switch | IMPLEMENTED | `HALTED` is terminal |
| Risk limits | IMPLEMENTED | Enforced inside AlphaLab, not bypassable |
| **Live trading** | **DISABLED** | Three gates refuse it; the loop is unbuilt |
| Live order routing | NOT IMPLEMENTED | Needs `LiveSession`, a feed, and reconciliation |
| Reconciliation loop | NOT IMPLEMENTED | AlphaLab provides `reconcile()`; nothing calls it |
| Durable mid-run resume | NOT IMPLEMENTED | A restarted paper session replays from the start |

## Brokers

| Capability | Status | Notes |
|---|---|---|
| Paper broker | IMPLEMENTED | AlphaLab's own simulator |
| Credential encryption | IMPLEMENTED | AES-256-GCM, bound to the connection, key-versioned |
| **Zerodha connector** | **INTEGRATION-READY / REQUIRES EXTERNAL CREDENTIALS** | Written to the published Kite Connect v3 contracts and tested against a local server speaking the same protocol. **Never run against Zerodha.** |
| Zerodha postback webhook | NOT IMPLEMENTED | Needs a public endpoint |
| Market-data websocket | NOT IMPLEMENTED | |

## RedDesk

| Capability | Status | Notes |
|---|---|---|
| Listing workflow | IMPLEMENTED | Real validation, not a status flip |
| Backtest evidence | IMPLEMENTED | Must be a run on this platform against that exact version |
| Entitlements | IMPLEMENTED | Resolve to a concrete version; never "latest" |
| Purchases | IMPLEMENTED | Idempotent; licence hash recorded |
| Refunds | IMPLEMENTED | Revokes the entitlement; fails closed if the provider refuses |
| Creator payouts | IMPLEMENTED (recording only) | The ledger is written; **no money moves** |
| Payment provider | DISABLED | Interface exists; `ManualProvider` records without charging and *raises* on payout |
| Real payment integration | REQUIRES EXTERNAL CREDENTIALS | Merchant account, contract, webhooks, tax handling |
| Independent certification | NOT IMPLEMENTED | The reviewer is an admin of the creator's own org |
| **Seller-code execution** | **NOT IMPLEMENTED** | See `SANDBOX_CONTRACT.md`. A listing names an in-repository implementation |

## Persistence

| Capability | Status | Notes |
|---|---|---|
| Alembic migrations | IMPLEMENTED | Fresh and upgrade paths both tested; `alembic check` in CI-able form |
| Startup `create_all` | IMPLEMENTED (development only) | Forced off in production |
| SQLite | TESTED LOCALLY | Fine for one process |
| PostgreSQL | NOT TESTED | The URL is configurable; nothing has run against it |

## Frontend

| Capability | Status | Notes |
|---|---|---|
| 15 screens | IMPLEMENTED | |
| Route guard | IMPLEMENTED | Unauthenticated renders only the login screen |
| No browser storage of secrets | IMPLEMENTED | No `localStorage`/`sessionStorage` use at all |
| Confirmation on destructive actions | IMPLEMENTED | Inline two-step, states the consequence |
| Live-trading visibility | IMPLEMENTED | Shown as disabled; the three gates are named |
| Component/unit tests | IMPLEMENTED | 28 tests |
| End-to-end browser tests | NOT IMPLEMENTED | Verified manually |

## Not addressed at all

Load testing, penetration testing, dependency CVE scanning in CI, log
aggregation, metrics/tracing, alerting, backup automation, disaster recovery,
data retention and erasure, and multi-node deployment.
