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
| **REQUIRES EXTERNAL INFRASTRUCTURE** | Needs a service to run against (Redis, an SMTP relay, a WORM store) that this build does not assume exists |
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
| Rate limiting (single process) | IMPLEMENTED | Per-principal, injected clock, `429` + `Retry-After` |
| Rate limiting (multi-instance) | REQUIRES EXTERNAL INFRASTRUCTURE | Counters are per-process; `SharedBackend` documents what Redis must provide. `GET /api/health` reports the real scope rather than the configured one |
| Audit trail | IMPLEMENTED | Append-only, redacted at the writer |
| Audit tamper-evidence | IMPLEMENTED | Per-tenant hash chain; see the limits in `SECURITY.md` |
| Notifications (in-app) | IMPLEMENTED | 18-kind catalogue; `notify()` refuses an undeclared kind, and a test asserts nothing declared goes unsent |
| Email / push delivery | REQUIRES EXTERNAL INFRASTRUCTURE | `NotificationChannel` is the seam; nothing is registered, and `GET /api/health` says so |
| **MFA (TOTP)** | **IMPLEMENTED** | RFC 6238 via `pyotp`; two-step enrolment, replay refusal, single-use recovery codes, disable requires proof |
| WebAuthn / passkeys | NOT IMPLEMENTED | TOTP was the one that could be done entirely locally |
| **Credential key rotation** | **IMPLEMENTED** | Multi-key decrypt, re-seal under current, `iluvtrade rotate-credentials` |
| Password reset | NOT IMPLEMENTED | Needs an email channel first |
| User invitations | NOT IMPLEMENTED | Registration creates a personal workspace only |
| **Correlation IDs** | **IMPLEMENTED** | One id spans HTTP → service → job → engine run → stored row |
| **Structured logging** | **IMPLEMENTED** | JSON lines with secret redaction as a backstop |
| Metrics / tracing / alerting | NOT IMPLEMENTED | Attachment points named in `OBSERVABILITY.md` |

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
| Payment state visible to the user | IMPLEMENTED | `GET /reddesk/billing-status`; the UI states which of the two postures is live before the button is clicked |
| Real payment integration | REQUIRES EXTERNAL CREDENTIALS | Merchant account, contract, webhook endpoint with signature verification, tax handling |
| Independent certification | NOT IMPLEMENTED | The reviewer is an admin of the creator's own org |
| **Seller-code execution** | **NOT IMPLEMENTED** | See `SANDBOX_CONTRACT.md`. A listing names an in-repository implementation |

## Persistence

| Capability | Status | Notes |
|---|---|---|
| Alembic migrations | IMPLEMENTED | Four revisions. Fresh, upgrade and downgrade paths tested, **and every revision applied against a populated database** — the test that caught a partially-applying migration an empty-database run could not |
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
| MFA enrolment screen | NOT IMPLEMENTED | The API is complete and tested; no UI yet |
| End-to-end browser tests | NOT IMPLEMENTED | Verified manually |

## Not addressed at all

Load testing, penetration testing, dependency CVE scanning in CI, log
aggregation, metrics/tracing, alerting, backup automation, disaster recovery,
data retention and erasure, and multi-node deployment.

## The four things this build will not do, and why

| | Why not |
|---|---|
| **Execute live orders** | The loop is unbuilt, and `ExecutionMode.LIVE` is constructed nowhere — so no configured run can reach a venue regardless of what a request contains. Three gates refuse a live session on top of that. |
| **Run seller code** | The isolation boundary in `SANDBOX_CONTRACT.md` does not exist. A listing names an in-repository implementation; there is nothing to sandbox. |
| **Move money** | `ManualProvider` records without charging and raises on payout. A false ledger entry is worse than an unavailable feature. |
| **Claim audit immutability** | The hash chain detects row-level tampering. An attacker with write access can recompute it. That needs an external anchor; none exists. |
