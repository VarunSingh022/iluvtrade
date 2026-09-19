# Getting started

## Requirements

- Python 3.12+
- Node 20+ (for the frontend)
- AlphaLab 3.0.0 — a wheel or an installable source tree

## Install

```bash
git clone <this repository> && cd iluvtrade

# Backend
cd backend
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install /path/to/alphalab-3.0.0-py3-none-any.whl
./.venv/bin/pip install -e ".[dev]"

# Frontend
cd ../frontend && npm install
```

`alphalab` is a normal dependency with no source copied into this repository.
Verify it:

```bash
cd backend && ./.venv/bin/python -c "import alphalab; print(alphalab.__version__)"
# 3.0.0
```

If this prints a different version, another `alphalab` is shadowing it — check
`pip show alphalab` for its location.

## Configure

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # → ILUVTRADE_SECRET_KEY
```

`ILUVTRADE_SECRET_KEY` derives the session-token HMAC and the key that encrypts
broker credentials. Changing it logs everyone out and invalidates every stored
credential. It is required in production.

## Run

### Everything from one origin

```bash
cd frontend && npm run build
cd ../backend && ./.venv/bin/python -m iluvtrade.cli serve
```

http://127.0.0.1:8000 — the API serves the built frontend, so the session cookie
is first-party.

### Frontend development

```bash
# terminal 1
cd backend && ./.venv/bin/python -m iluvtrade.cli serve --reload

# terminal 2
cd frontend && npm run dev
```

http://127.0.0.1:5173. Vite proxies `/api` to the backend so the browser still
sees one origin — without that the HttpOnly cookie would be cross-site and
dropped, and development would need a different auth path from production, which
is how a CSRF hole gets introduced by accident.

Point the proxy elsewhere with `ILUVTRADE_API_ORIGIN=http://127.0.0.1:8099 npm run dev`.

## See it work

```bash
cd backend && PYTHONPATH=$PWD ./.venv/bin/python -m iluvtrade.demo
```

Twenty-six steps against the real HTTP API, ending with a check that no
credential reached the audit trail.

## A first workspace by hand

1. **Create an account.** The first user gets a personal workspace and the
   `owner` role.
2. **Upload a CSV.** Any OHLCV file with a date and a close. The importer shows
   what it detected before storing anything.
3. **Review and approve.** Read the findings, the rejected rows and the
   transformations. Nothing can use the dataset until you approve it.
4. **Create a strategy**, add a version choosing an implementation, and
   **publish** it — a draft cannot be run, because a draft can still change.
5. **Run a backtest** from Research.
6. **Deploy to paper** from Sessions, and watch orders and fills appear.

## Quality gates

```bash
cd backend
./.venv/bin/ruff check iluvtrade tests
./.venv/bin/ruff format --check iluvtrade tests
./.venv/bin/mypy
PYTHONPATH=$PWD ./.venv/bin/python -m pytest -q

cd ../frontend
npm run typecheck && npm run build
```

Run a subset:

```bash
PYTHONPATH=$PWD ./.venv/bin/python -m pytest -m security -q
PYTHONPATH=$PWD ./.venv/bin/python -m pytest -m integration -q
```

## Layout

```
backend/iluvtrade/
  alphalab_bridge/   the ONLY place alphalab is imported
  api/v1/            routers and typed schemas
  backtests/         job submission, worker, engine invocation
  brokers/           accounts, credential crypto, Zerodha connector
  common/            storage
  data/              schema detection, cleaning, quality, fetch, ingest
  db/                models and session management
  platform/          accounts, security, tenancy, audit, notifications
  reddesk/           marketplace and entitlements
  strategies/        strategy identity and immutable versions
  trading/           sessions, runner, projection
frontend/src/
  lib/               typed API client, auth, formatting
  components/        shell, UI primitives, equity curve
  pages/             one per screen
```

## Troubleshooting

**`No module named 'alphalab.backtesting'`** — an older `alphalab` is installed
and shadowing. `pip show alphalab`, uninstall the wrong one.

**`asset_id must be a valid UUID string`** — market data was built without an
instrument universe. Everything must go through
`alphalab_bridge.instruments.universe_for()`.

**A backtest stays `queued`** — the worker pool is not running. It starts with
the app; if you built the app with `start_workers=False`, drain manually with
`BacktestWorkerPool(size=1).drain()`.

**`Refusing to store a naive datetime`** — use `db.base.utcnow()`. The column
type refuses to guess a timezone.
