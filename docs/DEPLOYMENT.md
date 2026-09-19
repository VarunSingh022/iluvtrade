# Deployment

## What this repository is ready for

A single-node deployment for research and paper trading, behind TLS, with a
Postgres database and a persistent volume.

## What has to change before it runs somewhere real

| Change | Why |
|---|---|
| `ILUVTRADE_SECRET_KEY` set to real, generated material | It derives session HMACs and the credential encryption key. Refused as empty in production. |
| `ILUVTRADE_ENVIRONMENT=production` | Turns on `Secure` cookies and HSTS, turns off dev CORS. |
| Postgres instead of SQLite | SQLite serialises writers; the worker pool and the API contend. |
| A reverse proxy terminating TLS | The app sets HSTS but does not terminate TLS itself. |
| Rate limiting at the proxy | Nothing in the app throttles login attempts. |
| A backup of the storage root | Canonical datasets and run artifacts are files, and losing them makes every run unreproducible. |

## Configuration

Everything is `ILUVTRADE_`-prefixed; see `.env.example`. The ones that matter
most in production:

```bash
ILUVTRADE_ENVIRONMENT=production
ILUVTRADE_SECRET_KEY=<48+ bytes of generated material>
ILUVTRADE_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/iluvtrade
ILUVTRADE_STORAGE_ROOT=/var/lib/iluvtrade/storage
ILUVTRADE_BACKTEST_WORKER_COUNT=4

# An allowlist. Empty means no host may be fetched — deliberately.
ILUVTRADE_FETCH_ALLOWED_HOSTS=data.example.com

# Off. Read docs/TRADING.md before considering otherwise.
ILUVTRADE_LIVE_TRADING_ENABLED=false
```

## Running

```bash
cd frontend && npm ci && npm run build
cd ../backend && ./.venv/bin/python -m uvicorn iluvtrade.main:app --host 0.0.0.0 --port 8000
```

The API serves the built frontend, so there is one origin and one TLS
certificate. Behind a proxy, forward `X-Forwarded-*` and run uvicorn with
`--proxy-headers`.

## Schema

`create_all()` runs at startup and is right for development and small
deployments. For anything where data matters, generate a migration with Alembic
(`backend/migrations/` is scaffolded) and disable `create_tables`:

```python
app = create_app(create_tables=False)
```

## Scaling, and its limits

The backtest worker pool and the session runner are **in-process thread pools**.
That is fine for one node and has no broker to operate. What it does not survive
is a process restart — `requeue_orphans()` handles jobs left `RUNNING`, but a
paper session restarts from the beginning of its dataset.

The seam for external workers is `backtests.service.claim_next()`, which already
takes the row lock a multi-process worker needs (and degrades to a plain select
on SQLite, which has no `SELECT … FOR UPDATE`). Moving to Celery or RQ is that
function plus a different `run_forever`. Nothing above it changes.

This is stated rather than implied: **the current design does not scale past one
node**, and pretending otherwise would be the kind of claim this project avoids.

## Backups

| What | Where | Matters because |
|---|---|---|
| Database | `ILUVTRADE_DATABASE_URL` | all domain state |
| Storage root | `ILUVTRADE_STORAGE_ROOT` | raw uploads, canonical datasets, run artifacts |

A backup of one without the other is not a backup: a `BacktestRun` row whose
artifact is missing is a result nobody can inspect.

## Health

`GET /api/health` returns the app version, the environment, **the AlphaLab
version actually loaded**, and whether live trading is enabled. The engine
version is there deliberately — "which engine produced these numbers" should be
answerable from a health check.
