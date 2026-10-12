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
| **Password reset — token infrastructure** | **IMPLEMENTED** | 256-bit token, HMAC at rest, one hour, single use, supersedes any outstanding one, revokes every session on use, identical answer for a known and an unknown address |
| **Password reset — delivery** | **REQUIRES EXTERNAL INFRASTRUCTURE** | `DeliveryProvider` is the seam; the only implementation refuses. `POST /auth/password-reset/request` answers **503**, not a 202 that never arrives. `iluvtrade issue-password-reset` is the operator path, and it works |
| **Organization invitations** | **IMPLEMENTED** | Hashed one-time token, seven-day expiry, revocable, single use, bound to the invited address, role fixed at creation and never read from the accepting request |
| **Organization switching** | **IMPLEMENTED** | A session names one organization; switching issues a new one and revokes the old |
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
| **Timestamp conventions and bar interval** | **IMPLEMENTED** | Every import declares what its timestamps mean (`session_date`, `interval_start`, `interval_end`), the IANA zone for bare dates and wall-clock times, and the **bar interval** (`session_date` implies `1d`); refused before anything is stored otherwise. The interval is **never inferred**, and it is checked against **each instrument's own bars** (one instrument's bars never vouch for another's): a gap shorter than one interval or not a whole number of intervals is refused, exactly — no daylight-saving hour of slack, no rounding; an instrument with no pair one interval apart (or one bar) is ambiguous — for it the interval is only the user's declaration — and is refused unless the import names it in `missing_observations_confirmed_for` (exactly the ambiguous instruments; the upload preview lists them; recorded, and shown as "user-declared, not verified" with a warning at approval); longer whole-interval gaps — missing bars, overnight, weekends, holidays, not told apart — are accepted and counted. Dates and zoned wall-clock times are measured on the zone's wall clock, offset-aware and epoch values as absolute instants, monthly bars in calendar months on a consistent day; wall-clock times a zone shows twice or never, and start-stamped wall-clock bars spanning an offset change, are refused. Timestamps alone cannot show bar length — the declaration states it. AlphaLab reads the timestamps (`alphalab.data.parse_timestamp`: a session date becomes 23:59:59.999999 of that civil date in the zone — not the exchange's closing time; start-stamped bars moved by `alphalab.market.normalize_wire_bar` by the declared interval). The declaration, the spacing evidence and the rule are recorded on the version and change its canonical hash. Versions without this provenance (imported before it, whose interval was inferred, or whose interval was validated across the whole file — record version 2, the reviewed look-ahead blocker) are refused by approval, backtest submission, the backtest worker, session creation and the paper runner (`docs/RESEARCH.md`) |
| **Temporal evidence downstream** | **IMPLEMENTED** | Backtest runs and paper sessions snapshot `ingest.temporal_evidence` of their exact dataset version (migration 0008); results, sessions (and their log) and RedDesk listing evidence show it — including which instruments' interval was the importer's unverified declaration. Older rows show "not recorded"; the runner refuses pre-0008 sessions |
| **Preview bound to content** | **IMPLEMENTED** | A missing-observation confirmation requires the `preview_id` of an interval check of the exact bytes, declaration and cleaning policy, recomputed server-side on upload and fetch (a changed URL resource is refused). The UI clears checks and ticks on every file selection and drops stale responses |
| **Row order per instrument** | **IMPLEMENTED** | Out-of-order is judged within each instrument; a file grouped by symbol produces the same canonical dataset as an interleaved one. Until the review-blocker pass a file-wide high-water mark rejected a grouped file's second instrument as "out of order" |
| **Canonical file integrity** | **IMPLEMENTED** | The canonical file is written atomically (temporary file, fsync, rename) and every path that computes on it — the backtest worker and the paper runner — reads it verified against the SHA-256 recorded at import, refusing a missing, truncated or altered file. The preview endpoint shows the file as stored, unverified |
| **Unreported VWAP / trade count** | **IMPLEMENTED** | Absent stays absent (`None`, as AlphaLab's `Bar` represents it); never the typical price or the close. An impossible reported value is dropped and the drop recorded |
| Authenticated data sources | NOT IMPLEMENTED | Needs per-source credential storage |

## Research

| Capability | Status | Notes |
|---|---|---|
| Asynchronous backtest jobs | IMPLEMENTED | In-process worker pool |
| Idempotent submission | IMPLEMENTED | Key lookup plus a unique constraint |
| Reproducibility | IMPLEMENTED | Replay matches order-id for order-id; AlphaLab's `result_id` / `configuration_id` stored with each result |
| **Run seeds** | **IMPLEMENTED** | A seed the caller does not state is derived by one documented digest (`common.seeds`: SHA-256 of canonical JSON, first 32 bits) — identical in every process — from the run's material inputs, and stored before anything runs: a backtest's in its stored request, a session's on its row. Every attempt and reconstruction reads the stored value. Paper sessions used Python's per-process `hash()` until 2026-10-11. Backtest retries are **off by default** (`backtest_max_attempts = 1`: a lost worker fails the job); when enabled, a retry reuses the stored seed |
| **Fill timing** | **IMPLEMENTED** | Explicit on every backtest and paper session; default `next_event` (the next bar, no look-ahead); `same_event` (optimistic, AlphaLab's words) only when asked for. Part of the request's identity; read back from AlphaLab's own record of the run and shown with every optimistic assumption it lists. Requests stored before it was recorded read back as `same_event`, which is what they ran |
| **Each run keeps its own result document** | **IMPLEMENTED** | Each run's artifact is keyed by its own id, written atomically, names its run and job, and its SHA-256 is recorded on the row (migration 0007). The detailed result is served **only** when all three check out; otherwise the response carries the row's headline metrics and the reason, and `result` is `null`. Runs stored before 2026-10-11 were written to `backtests/None/result.json`, shared by every run in their organization, and are never served as verified |
| Strategy-failure handling | TESTED LOCALLY | A run whose strategy failed is refused, not stored (AlphaLab EXE-006); a paper session ends `failed` |
| Risk-refusal reporting | IMPLEMENTED | Refusals are never silent |
| Distributed workers | NOT IMPLEMENTED | `claim_next` takes the lock one would need |
| Backtest cancellation | IMPLEMENTED (before and after the run only) | A queued job is cancelled outright. A running job is checked only when the engine finishes: it runs to the end and its result is discarded, not stored. **Cancellation between records is NOT IMPLEMENTED** (the per-record callback where it would go exists; see `docs/ALPHALAB_CAPABILITY_MAP.md`) |

## Trading

| Capability | Status | Notes |
|---|---|---|
| Paper sessions | TESTED LOCALLY | Real AlphaLab; parity with backtest asserted |
| Session lifecycle + kill switch | IMPLEMENTED | `HALTED` is terminal |
| Risk limits | IMPLEMENTED | Enforced inside AlphaLab, not bypassable |
| **Live trading** | **DISABLED** | Three gates refuse it; the loop is unbuilt |
| Live order routing | NOT IMPLEMENTED | Needs `LiveSession`, a feed, and reconciliation |
| Reconciliation loop | NOT IMPLEMENTED | AlphaLab provides `reconcile()`; nothing calls it |
| Durable mid-run resume | NOT IMPLEMENTED | A restarted paper session replays from the start. The engine half is in place and tested: every strategy declares its state, and an AlphaLab 4.1 replay paused through a serialized payload resumes to the uninterrupted `result_id`. Persisting payloads and resuming sessions from them is unbuilt (`docs/TRADING.md`) |

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
| Alembic migrations | IMPLEMENTED | Seven revisions. Fresh, upgrade and downgrade paths tested, every revision applied against a populated database, and 0006/0007 applied to a database with a row in every table (SQLite and PostgreSQL) |
| Startup `create_all` | IMPLEMENTED (development only) | Forced off in production |
| (a) SQLite — application suite | TESTED LOCALLY | The default test backend: a temporary file per test. `pytest -q` → **574 passed, 0 failed, 0 skipped, 13 deselected** (the 13 are `tests/postgres`, listed by name as not run). 587 tests collected in all. Fine for one process, and **refused in production** unless `ILUVTRADE_ALLOW_SQLITE_IN_PRODUCTION=true`. Concurrent ingestions can produce `database is locked` (answered as a retryable 503 by SQLite result code), demonstrated by `test_concurrent_dataset_uploads_do_not_write_into_each_other` |
| (b) PostgreSQL — migrations | TESTED LOCALLY | Against PostgreSQL 16.15 on this machine (2026-10-11): `alembic upgrade head` + `current` + `check` on an empty database; and in `tests/postgres`, a database with a row in **every** table at 0005 upgraded to head (sessions backfilled `same_event`, provenance `{}`, no artifact digest, seeds intact), a downgrade refused while a seed exceeds 2**31 − 1 or a session states a non-backfilled fill timing, then down to base and up again. Earlier, `upgrade head` + `check` passed in CI run 35699500951 (commit `e7d730b`, AlphaLab 3.5.0). **Not yet run in CI on this code** |
| (c) PostgreSQL — application suite | TESTED LOCALLY | Against PostgreSQL 16.15 on this machine (2026-10-11), each test on its own database cloned from a template `alembic upgrade head` built, with a per-test assertion that the engine is PostgreSQL and connected to that database (`ILUVTRADE_TEST_DATABASE_URL`; `docs/DEPLOYMENT.md`). `pytest -q -m "not postgres"` → **574 passed, 13 deselected**; `pytest -q tests/postgres` → **13 passed**; `pytest -q` (both) → **587 passed** = 574 + 13. 0 failed, 0 skipped; no test database or connection left afterwards. It found defects SQLite hid (a worker-freezing upload route, colliding audit appends, a 32-bit seed, NUL → 500, a baseline downgrade dropping indexes it did not own). **Not yet run in CI**: the CI step that claimed this before ran on SQLite, because an autouse fixture replaced the URL |
| PostgreSQL driver | IMPLEMENTED | `pip install -e ".[postgres]"`. It was installed by the Dockerfile and absent from `pyproject.toml`, so the documented production path failed with `ModuleNotFoundError` |
| **Backup (SQLite)** | **TESTED LOCALLY** | `iluvtrade backup --verify` uses SQLite's online backup API — taken against a *live, running* deployment and restored from |
| **Backup (PostgreSQL / Compose)** | **TOOLING IMPLEMENTED, NOT DEPLOYED** | `iluvtrade.backup_recovery` coordinates Compose writer quiescence, PostgreSQL dump, storage capture, and Restic/B2 retention. Host credentials, schedule, alerting, real upload, and restore rehearsal are not configured or verified; see `docs/BACKUP_RECOVERY.md` |

## Frontend

| Capability | Status | Notes |
|---|---|---|
| 15 screens | IMPLEMENTED | |
| **MFA enrolment, challenge and disable** | **IMPLEMENTED** | QR plus a manual key, recovery codes shown once, disable requires a current code. Login shows a code challenge instead of reporting a wrong password |
| **Team management** | **IMPLEMENTED** | Members, invitations, join by code, workspace switching |
| **Password reset screen** | **IMPLEMENTED** | Reads whether delivery exists and says so instead of offering a link that goes nowhere |
| **Audit chain verification** | **IMPLEMENTED** | The verify endpoint existed and no screen called it |
| **Session expiry** | **IMPLEMENTED** | A 401 anywhere returns the user to sign-in rather than leaving the shell around a broken screen |
| Route guard | IMPLEMENTED | Unauthenticated renders only the login screen |
| No browser storage of secrets | IMPLEMENTED | No `localStorage`/`sessionStorage` use at all |
| Confirmation on destructive actions | IMPLEMENTED | Inline two-step, states the consequence |
| Live-trading visibility | IMPLEMENTED | Shown as disabled; the three gates are named |
| Component/unit tests | IMPLEMENTED | 65 tests |
| End-to-end browser tests | NOT IMPLEMENTED | Verified manually |

## Deployment

| Capability | Status | Notes |
|---|---|---|
| **Production configuration validation** | **IMPLEMENTED** | `Settings.deployment_problems()` names every unsafe production combination and `get_settings()` refuses to return them, so a misconfigured deployment fails at startup rather than two hours later on the first login. `iluvtrade check-config` reports the same thing without starting |
| **Local install → migrate → run → health → shutdown** | **TESTED LOCALLY** | Now genuinely including the *install*. An earlier claim here was wrong: the server, migrations and demo had all been run from the source tree with the working directory on `sys.path`, and `pip install -e ".[dev]"` had never been executed — it **failed** on an out-of-tree readme. Fixed, then verified in a clean virtual environment: install, console script, import from another directory, and the full suite at 501 |
| **(d) Container image** | **IMPLEMENTED, NOT BUILT** | `Dockerfile`, `docker-compose.yml`, `.dockerignore` and `deploy/entrypoint.sh`; the entrypoint passes `sh -n`. **Docker is not installed on this machine and no CI job builds the image**, so it has never been built or run. Static verification found and fixed two defects in it: no `.dockerignore` (a 366 MB context including `.env`, and the host's `node_modules` overwriting `npm ci`'s), and a `pip install` that would have failed on an out-of-tree readme. Nothing here is a tested deployment |
| Migration execution on deploy | IMPLEMENTED | `deploy/entrypoint.sh` runs `alembic upgrade head` explicitly and fails the start if it fails. `create_all` is refused in production |
| **Dependency advisory gate** | **IMPLEMENTED** | `scripts/audit-dependencies.sh` → `scripts/check_advisories.py` accepts an npm finding only by GHSA id **and** package (currently GHSA-wrjc-x8rr-h8h6 and GHSA-337j-9hxr-rhxg against `react-router`, assessed in `SECURITY.md`), and fails closed on an `npm audit` error, a malformed report or a finding without a GHSA id. Until the review-blocker pass it matched package names (a new advisory against `react-router` would have passed) and read an npm error as zero findings. Pinned by `backend/tests/unit/test_advisory_gate.py` (synthetic reports, no network) |
| **Dependency declaration guard** | **IMPLEMENTED** | Every third-party import must be declared. Two dependencies had already shipped undeclared (`pyotp`, then `email-validator`); this catches the direct-import kind. The transitive kind is only visible from a clean install, which is CI's job |
| **Concurrency and failure behaviour** | **TESTED LOCALLY** | Eleven tests. **Found a real defect**: `_transition` was a read-modify-write, and five simultaneous starts produced three successes — three runner threads feeding one portfolio. Now one conditional `UPDATE`. **This is not a load test** — a handful of threads against SQLite on one machine |
| **Test isolation** | **IMPLEMENTED** | The suite refuses to open the developer's database, joins every background thread before teardown, and hashes the file across the run. Twelve tests, including a reproduction of the original failure sequence. Four consecutive full runs leave the file byte-identical |
| **CI pipeline** | **IMPLEMENTED** | `.github/workflows/ci.yml` — four jobs: frontend, dependency advisories, backend (lint, types, AlphaLab verification, the SQLite suite, migrations, the demo), and PostgreSQL (migrations, `tests/postgres`, then the application suite on PostgreSQL, reported as separate steps). All four passed on GitHub Actions for commit `e7d730b` with AlphaLab 3.5.0 (run 35699500951) — but that run's "suite against PostgreSQL" step ran on SQLite. **Nothing since then has run in CI**: not the AlphaLab 4.1 integration, not the PostgreSQL suite, not this hardening. The backend jobs install AlphaLab 4.1.0 through the hash-pinned reference in `backend/pyproject.toml`; `scripts/verify-alphalab.sh` runs the release's consumer contract |
| TLS termination | NOT IMPLEMENTED | Assumed to be a reverse proxy's job; HSTS is set in production |

## Not addressed at all

Load testing, penetration testing, log aggregation, metrics/tracing, alerting,
host backup scheduling and alert configuration, disaster-recovery rehearsal,
data retention and erasure, and multi-node deployment.

## The five things this build will not do, and why

| | Why not |
|---|---|
| **Send email** | No channel is configured and none is simulated. `DeliveryProvider` is the seam; the only implementation raises. This shapes two features rather than hiding behind them: password reset **refuses with 503** instead of returning a 202 that never arrives, and an invitation hands its token to the *inviter* to pass on. A 202 nobody acts on is worse than a refusal — the user waits, retries, and concludes the account is broken, while the logs say everything succeeded. |
| **Execute live orders** | The loop is unbuilt, and `ExecutionMode.LIVE` is constructed nowhere — so no configured run can reach a venue regardless of what a request contains. Three gates refuse a live session on top of that. |
| **Run seller code** | The isolation boundary in `SANDBOX_CONTRACT.md` does not exist. A listing names an in-repository implementation; there is nothing to sandbox. |
| **Move money** | `ManualProvider` records without charging and raises on payout. A false ledger entry is worse than an unavailable feature. |
| **Claim audit immutability** | The hash chain detects row-level tampering. An attacker with write access can recompute it. That needs an external anchor; none exists. |
