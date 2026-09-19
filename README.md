# iluvtrade

The application platform around **AlphaLab**, with the **RedDesk** strategy
marketplace.

Upload market data, see exactly what the importer made of it, run a reproducible
backtest on a real quantitative engine, publish the strategy for others to
license, and deploy an exact version to paper trading.

> **Not production-ready.** Live trading is disabled, the Zerodha connector has
> never touched the venue, no money moves, and seller code cannot be executed.
> [docs/STATUS.md](docs/STATUS.md) states the status of every capability in one
> table — read it before believing anything else here.

---

## The one architectural rule

**AlphaLab is the engine. iluvtrade orchestrates it and never reimplements it.**

```
                              Browser
                        (React + TypeScript)
                                 │  HttpOnly cookie + X-Requested-With
                                 ▼
                          iluvtrade API
                     (FastAPI, /api/v1, typed)
             auth · tenancy · rate limit · CSRF · audit chain
                                 │
        ┌────────────────────────┼────────────────────────┐
        ▼                        ▼                        ▼
  Application services     Background work           RedDesk
  ───────────────────      ───────────────           ───────
  identity / tenancy       backtest workers          listings
  datasets / research      session runners           entitlements
  strategies / versions    (in-process pools)        purchases · refunds
  brokers / trading                                  payouts (recorded)
        │                        │                        │
        └────────────────────────┼────────────────────────┘
                                 ▼
                         AlphaLab bridge
              (the ONLY place `import alphalab` appears)
                instruments · market · runconfig
                engine · results · broker vocab
                                 │
                                 ▼
                        AlphaLab v3.0.0
        market data · strategy dispatch · allocation · risk
        OMS · execution · portfolio accounting · analytics
                                 │
                         Broker boundary
                   (alphalab.broker.BrokerProtocol)
                                 │
                   ┌─────────────┴─────────────┐
                 Paper                     Zerodha
          (AlphaLab simulator)        (Kite Connect v3)
              IMPLEMENTED            INTEGRATION-READY —
                                     never run against the venue
```

**AlphaLab is the canonical quantitative authority.** Every number this product
displays was computed by it.

Three authorities, and nothing crosses them:

| Authority | Owns | Lives in |
|---|---|---|
| **AlphaLab** | every quantitative and trading semantic — market data, strategy dispatch, allocation, risk, OMS, execution, portfolio accounting, analytics | installed package `alphalab==3.0.0` |
| **RedDesk** | marketplace and licensing: what an organization is *permitted* to run | `backend/iluvtrade/reddesk/` |
| **Platform** | identity, tenancy, authorization, audit: *who is asking* | `backend/iluvtrade/platform/` |

Two rules are **enforced by tests**, not by convention:

- Every `import alphalab` lives in `backend/iluvtrade/alphalab_bridge/`.
  `tests/unit/test_engine_boundary.py` walks the source tree and fails otherwise.
- Every table holding user data carries `organization_id`, and
  `platform.tenancy.scoped()` *refuses* to build a query for a model that does
  not. `tests/security/test_tenant_isolation.py` enumerates the schema to prove
  it.

The tables named `session_orders` / `session_fills` / `session_positions` are
**projections** of AlphaLab's `RunState`, written only by
`trading/projection.py`. They are not a second book.

---

## Quick start

```bash
# 1. Backend
cd backend
python3 -m venv .venv
./.venv/bin/pip install /path/to/alphalab-3.0.0-py3-none-any.whl
./.venv/bin/pip install -e ".[dev]"

# 2. Configuration
cp ../.env.example ../.env    # then set ILUVTRADE_SECRET_KEY

# 3. Schema
./.venv/bin/alembic upgrade head

# 4. Frontend
cd ../frontend && npm install && npm run build

# 5. Run
cd ../backend && ./.venv/bin/python -m iluvtrade.cli serve
```

Then open http://127.0.0.1:8000. The built frontend is served by the API, so
there is one origin and the session cookie is first-party.

For frontend development with hot reload, run the API and Vite side by side —
Vite proxies `/api` so the origin stays single. See
[GETTING_STARTED.md](docs/GETTING_STARTED.md).

### See it work end to end

```bash
cd backend && PYTHONPATH=$PWD ./.venv/bin/python -m iluvtrade.demo
```

Twenty-six steps against the real HTTP API: two workspaces, a deliberately
imperfect CSV, the quality report, a reproducible backtest on AlphaLab, a
marketplace listing with real evidence, a purchase, an entitlement, a paper
session, and the audit trail — with a check that no credential reached it.
Nothing is stubbed.

---

## What is real, and what is not

Stated plainly, because a trading platform that overstates itself is dangerous.

### Real, and exercised

- **The AlphaLab integration.** Backtests and paper sessions run the actual
  engine. Every number displayed was computed by it. The accounting identity
  `equity == cash + realized + unrealized − commission` holds exactly over
  `Decimal`, and a test asserts it.
- **Reproducibility.** A run records the dataset version *and its canonical
  hash*, the strategy version *and its content hash*, the parameters, the seed
  and the engine version. Replaying a run reproduces it order id for order id —
  asserted in `tests/integration/test_backtest_reproducibility.py`.
- **Backtest/paper parity.** Given one dataset, one strategy version and one
  seed, a paper session and a backtest produce the same book. Asserted end to
  end through the API.
- **The data workspace.** Schema detection, validation, cleaning, the quality
  report and the approval gate. Every rejected row carries its line number and
  reason; every changed value carries its before and after.
- **Security controls.** Tenant isolation (a 32-operation cross-tenant matrix),
  role-based authorization, TOTP two-factor authentication, credential
  encryption bound to its connection with working key rotation, SSRF defence
  with an explicit address deny-list, CSRF on every mutating route,
  path-traversal refusal, strategy-version immutability, order idempotency,
  per-principal rate limiting, and a per-tenant audit hash chain.
- **Traceability.** One correlation id spans an HTTP request, the job it
  queued, the AlphaLab run that executed it and the row that stored the result.
- **Accounts as a product.** TOTP two-factor with a real enrolment screen,
  organization invitations, workspace switching, and password reset whose token
  infrastructure is complete and whose *delivery* refuses rather than
  pretending.
- **Configuration that fails closed.** A production deployment with an unset
  secret key, `create_all` left on, a plaintext CORS origin or rate limiting
  disabled does not start. `iluvtrade check-config` reports the same assessment
  without starting anything.
- **498 backend tests and 55 frontend tests.**

### Real boundary, not yet run against the outside world

- **Zerodha.** The connector is written to the published Kite Connect v3
  contracts — verified against the documentation, not guessed — and implements
  AlphaLab's `BrokerProtocol` in full. It is tested against a local server
  speaking the same protocol. **It has never executed against Zerodha**, which
  needs a Kite Connect subscription, a registered app and a funded account. The
  UI says so rather than implying otherwise. See [docs/BROKERS.md](docs/BROKERS.md).
- **Payments.** `iluvtrade.billing` defines the provider interface; the only
  implementation records a settled purchase without moving money, and *raises*
  rather than recording a payout. The marketplace UI states which posture is
  live before the button is clicked. See [docs/PAYMENTS.md](docs/PAYMENTS.md).
- **Email and push.** `NotificationChannel` is the seam for notifications and
  `DeliveryProvider` for password reset; nothing is registered, and both say so
  at the surface. Password reset answers **503**, not a `202` for a message that
  will never arrive. An invitation hands its token to the *inviter* to pass on.
- **Containers.** `Dockerfile`, `docker-compose.yml` and an entrypoint that runs
  migrations explicitly. **Never built** — Docker is not installed on the
  machine this was developed on, and claiming otherwise would be a guess. The
  local path in [DEPLOYMENT.md](docs/DEPLOYMENT.md) *has* been exercised end to
  end.
- **Multi-instance rate limiting.** Counters are per-process, so N workers means
  N x the limit. `GET /api/health` reports the real scope.

### Deliberately not built

- **Live trading is not enabled and the live loop is not wired.** Paper runs
  through `TradingSession`; live needs AlphaLab's `LiveSession` plus venue
  routing and reconciliation. Three independent gates refuse a live session
  today, and the paper runner refuses outright rather than executing a live
  session against the simulator. See [docs/TRADING.md](docs/TRADING.md).
- **Arbitrary seller code.** A listing names a strategy implementation that
  exists in this repository. Uploading Python is not a feature, because running
  untrusted code needs the isolation boundary described in
  [docs/SECURITY.md](docs/SECURITY.md) — and half a sandbox reads as safety
  while providing none.

---

## Documentation

| Document | What it covers |
|---|---|
| [STATUS.md](docs/STATUS.md) | **What is implemented, tested, disabled or absent — read this first** |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | The three authorities, the AlphaLab boundary, data flow, persistence |
| [GETTING_STARTED.md](docs/GETTING_STARTED.md) | Install, configure, run, develop |
| [API.md](docs/API.md) | Every endpoint, the error envelope, authentication |
| [RESEARCH.md](docs/RESEARCH.md) | Data ingestion, backtests, reproducibility |
| [REDDESK.md](docs/REDDESK.md) | Listings, licensing, entitlements, payouts |
| [BROKERS.md](docs/BROKERS.md) | The broker boundary, Zerodha, what is unverified |
| [TRADING.md](docs/TRADING.md) | Paper sessions, the live gap, risk controls, the kill switch |
| [SECURITY.md](docs/SECURITY.md) | Controls, threat model, and what is deferred |
| [OBSERVABILITY.md](docs/OBSERVABILITY.md) | Correlation, structured logs, and where metrics would attach |
| [PAYMENTS.md](docs/PAYMENTS.md) | The provider boundary, the state machine, and why no money moves |
| [SANDBOX_CONTRACT.md](docs/SANDBOX_CONTRACT.md) | The contract a seller-code sandbox must satisfy — unbuilt, specified |
| [COMPLIANCE.md](docs/COMPLIANCE.md) | Compliance boundaries and external dependencies |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) | Running it somewhere real, and what has to change first |

---

## Quality gates

```bash
cd backend
./.venv/bin/ruff check iluvtrade tests
./.venv/bin/ruff format --check iluvtrade tests
./.venv/bin/mypy
PYTHONPATH=$PWD ./.venv/bin/python -m pytest -q          # 498 tests
./.venv/bin/alembic check                                 # models vs migrations
./.venv/bin/python -m iluvtrade.cli check-config          # unsafe settings

cd ../frontend
npm run typecheck
npm test                                                  # 55 tests
npm run build

cd ..
./scripts/audit-dependencies.sh                           # npm advisories vs assessed
```

Targeted suites:

```bash
cd backend
PYTHONPATH=$PWD ./.venv/bin/python -m pytest -m security -q
PYTHONPATH=$PWD ./.venv/bin/python -m pytest -m integration -q
PYTHONPATH=$PWD ./.venv/bin/python -m pytest tests/unit/test_engine_boundary.py -q
PYTHONPATH=$PWD ./.venv/bin/python -m pytest tests/security/test_api_surface.py -q
PYTHONPATH=$PWD ./.venv/bin/python -m pytest tests/integration/test_concurrency_and_failure.py -q
PYTHONPATH=$PWD ./.venv/bin/python -m pytest tests/test_isolation.py -q
```

## Licence

Proprietary. AlphaLab is MIT-licensed and used as a dependency.
