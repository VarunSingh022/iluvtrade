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
| A shared rate-limit store, or a single worker | The application limiter is per-process, so N workers means N x the limit. |
| Rate limiting at the proxy too | The application limits per principal; the proxy should limit per address. |
| `alembic upgrade head` before every start | The schema is the migrations', and production will not create tables. |
| A backup of the storage root | Canonical datasets and run artifacts are files, and losing them makes every run unreproducible. |

## Configuration

Everything is `ILUVTRADE_`-prefixed; see `.env.example`. The ones that matter
most in production:

```bash
ILUVTRADE_ENVIRONMENT=production
ILUVTRADE_SECRET_KEY=<48+ bytes of generated material>
# Only during a rotation. Remove once `iluvtrade rotate-credentials` reports clean.
# ILUVTRADE_RETIRED_SECRET_KEYS=["<the previous key>"]
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

**Alembic owns the schema.** Run migrations before starting the application:

```bash
cd backend && ./.venv/bin/alembic upgrade head
```

`create_all()` still runs at startup in development, because iterating on models
without a migration for every change is the point of development. It is
**forced off in production** by `Settings.should_create_tables` regardless of
configuration — a production database whose schema was created from whatever the
models happened to say is one no revision describes.

Revisions:

| Revision | What |
|---|---|
| `0001_baseline` | the schema as of the first commit |
| `0002_audit_chain` | adds the audit hash chain and **backfills existing rows** |

Bringing an existing pre-migration database forward:

```bash
./.venv/bin/alembic stamp 0001_baseline   # it already has this schema
./.venv/bin/alembic upgrade head          # applies the chain and backfills
```

Both paths — fresh and upgrade — are covered by
`tests/integration/test_migrations.py`, including a downgrade. `alembic check`
asserts the models have not drifted from the migrations, which is the failure
that otherwise surfaces only after deployment.

### After changing a model

```bash
./.venv/bin/alembic revision --autogenerate -m "what changed"
```

Read the generated file before committing it. Autogenerate does not detect
renames (it emits a drop and an add, which loses data) and does not know when a
column needs a backfill.

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

## Key rotation

```bash
# 1. append the current key to ILUVTRADE_RETIRED_SECRET_KEYS, set the new
#    ILUVTRADE_SECRET_KEY, restart
./.venv/bin/python -m iluvtrade.cli rotate-credentials --dry-run
./.venv/bin/python -m iluvtrade.cli rotate-credentials
# 2. only now remove the old key from the retired list
```

Removing the old key before rotating makes every stored broker credential
unreadable. The error names that specific cause, so it is recoverable — but the
order is not optional.

Rotating **does not** re-issue sessions: changing `ILUVTRADE_SECRET_KEY` changes
the session-token HMAC, so every user is signed out. Plan the restart
accordingly.

## Backups

| What | Where | Matters because |
|---|---|---|
| Database | `ILUVTRADE_DATABASE_URL` | all domain state |
| Storage root | `ILUVTRADE_STORAGE_ROOT` | raw uploads, canonical datasets, run artifacts |

A backup of one without the other is not a backup: a `BacktestRun` row whose
artifact is missing is a result nobody can inspect.

## Health

`GET /api/health` reports more than liveness, because each of these is
otherwise something an operator has to assume:

| Field | Why it is there |
|---|---|
| `engine.version` | "Which engine produced these numbers" should be answerable from a health check |
| `live_trading_enabled` | The deployment-level gate |
| `rate_limiting.shared_across_instances` | **`false` here.** With N workers the effective limit is N x the configured one |
| `notification_channels` | Empty, so nothing is delivered outside the app |
| `payment_provider` | `manual` means purchases charge nothing |

## Logs

JSON lines by default (`ILUVTRADE_STRUCTURED_LOGGING=false` for a terminal),
each carrying the correlation id. A request's id is on its response as
`X-Request-ID`, so a user's report leads straight to every line for it. See
[OBSERVABILITY.md](OBSERVABILITY.md).
