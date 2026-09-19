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

---

# The two deployment paths

There are two, and they are **not** equally verified. That distinction is the
most important thing on this page.

| Path | Status |
|---|---|
| Local install, run directly | **TESTED LOCALLY** — exercised end to end during the release-candidate audit |
| Container image and compose stack | **WRITTEN, NEVER BUILT** — Docker is not installed on the development machine |

Nothing below should be read as "this has been deployed to production". It has
not.

## Path 1 — local, and exercised

Every step here was run, in this order, during the audit. The output quoted is
what it produced.

### 1. Install

```bash
cd backend
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

`alphalab==3.0.0` is **not on PyPI**. It comes from the engine's own build:

```bash
.venv/bin/pip install /path/to/AlphaLab/dist/alphalab-3.0.0-py3-none-any.whl
```

`pip install alphalab==3.0.0` fails with "No matching distribution found". This
is the single most common way a fresh setup stalls, which is why it is stated
before the step that needs it rather than after.

### 2. Configure

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # → ILUVTRADE_SECRET_KEY
```

Then check it, without starting anything:

```bash
python -m iluvtrade.cli check-config
```

```
environment:      development
database:         sqlite
secret key:       configured
schema creation:  on
rate limiting:    on
live trading:     disabled
payments:         manual

No unsafe settings for this environment.
```

Run it again with `--environment production` to see what production would
refuse. It exits non-zero when anything is unsafe, so a deployment script can
gate on it.

### 3. Migrate

```bash
cd backend && .venv/bin/alembic upgrade head && .venv/bin/alembic current
```

Migrations are **never** implicit. `create_all` is refused in production
because it creates what is missing and silently ignores what has drifted — a
column added to a model never reaches an existing database, and the mismatch
surfaces later as a query error.

### 4. Run

```bash
cd backend && .venv/bin/python -m uvicorn iluvtrade.main:app --host 127.0.0.1 --port 8000
```

### 5. Frontend

```bash
cd frontend && npm ci && npm run build
```

The API serves `frontend/dist` when it exists, so the built app is same-origin
and needs no CORS. For development, `npm run dev` runs on port 5173 and
`ILUVTRADE_ALLOWED_ORIGINS` already lists it.

### 6. Health check

```bash
curl -s http://127.0.0.1:8000/api/health
```

The response is deliberately more than `{"status": "ok"}`. It reports the
engine version, whether live trading is possible, **whether rate limits are
shared across instances**, which notification channels are attached, and which
payment provider is in play — each one something an operator would otherwise
have to assume:

```json
{
  "status": "ok",
  "engine": {"name": "alphalab", "version": "3.0.0"},
  "live_trading_enabled": false,
  "rate_limiting": {
    "enabled": true,
    "shared_across_instances": false,
    "backend": "InMemoryBackend",
    "caveat": "Counters are per-process. With N API workers the effective limit is N x the configured limit..."
  },
  "notification_channels": [],
  "payment_provider": "manual"
}
```

### 7. Shutdown

`SIGTERM` or Ctrl-C. The worker pool and any running session runners are
stopped by the application's lifespan handler.

**A running session does not survive a restart.** It replays from the start of
its dataset. That is a real limitation, not an oversight to be discovered in
production; see `STATUS.md`.

## Path 2 — containers, written but never built

`Dockerfile`, `docker-compose.yml` and `deploy/entrypoint.sh` exist. The
entrypoint passes `sh -n`. **The image has never been built and the stack has
never been started**, because Docker is not installed on the machine this was
developed on. Treat it as a starting point that needs a first real build.

Before building, place the AlphaLab wheel where the Dockerfile expects it:

```bash
mkdir -p deploy/wheels
cp /path/to/AlphaLab/dist/alphalab-3.0.0-py3-none-any.whl deploy/wheels/
```

Then:

```bash
cp .env.example .env     # set ILUVTRADE_SECRET_KEY and POSTGRES_PASSWORD
docker compose build
docker compose up -d
docker compose exec api python -m iluvtrade.cli check-config
curl -s http://127.0.0.1:8000/api/health
docker compose down
```

Three decisions worth knowing:

* **One image, not two.** The application already serves the built SPA. Splitting
  them would add a reverse proxy, a second deployment unit and a CORS
  configuration to solve a problem that does not exist.
* **Migrations run in the entrypoint**, with `set -e`, so a failed migration
  stops the start rather than serving a schema the code does not match.
* **`ILUVTRADE_WEB_CONCURRENCY` defaults to 1.** Rate-limit counters are
  per-process, so N workers means N × every configured limit. Raising it is a
  deliberate trade until a shared backend exists.

---

# Backup and recovery

## SQLite

```bash
cd backend && .venv/bin/python -m iluvtrade.cli backup /backups/iluvtrade-$(date +%F).db --verify
```

This uses SQLite's **online backup API**, not a file copy. Copying the file of a
live database can capture a torn write or miss the write-ahead log; this takes a
consistent snapshot while the application keeps running. That was verified — the
backup below was taken against a server that was serving requests at the time:

```
Wrote /…/backup.db (610,304 bytes) from /…/app.db.
  integrity_check: ok
  schema revision: 0005_teams_and_reset
  tables:          30
    users                1
    organizations        1
    audit_events         4
    trading_sessions     0
```

`--verify` runs `PRAGMA integrity_check` on the copy and prints its schema
revision, so a backup that is corrupt or from the wrong schema is caught when it
is taken rather than when it is needed.

## PostgreSQL

```bash
pg_dump --format=custom --file=/backups/iluvtrade-$(date +%F).dump "$ILUVTRADE_DATABASE_URL"
pg_restore --clean --if-exists --dbname="$ILUVTRADE_DATABASE_URL" /backups/…dump
```

`iluvtrade backup` prints this rather than running it. Shelling out to `pg_dump`
means depending on a tool that may be absent or version-mismatched with the
server, and passing a URL containing a password as a process argument puts it in
every process listing on the host.

## Restoring

1. Stop the application. A restore under a running process is a torn database.
2. Move the damaged file aside — never delete it. It is evidence, and a
   corrupt database still contains most of the data.
3. Put the backup in place.
4. **Run `alembic upgrade head`.** A backup carries the schema revision it was
   taken at. If the application has since been upgraded, the restored database
   is behind and every query against a newer column fails.
5. `alembic current` — confirm the revision.
6. Start, and check `/api/health`.

The restore path was exercised during the audit: a backup taken from a live
database was opened as the application's database, reported revision
`0005_teams_and_reset`, and its rows read back through the ORM.

## What recovery does and does not give back

| | |
|---|---|
| **Comes back** | Accounts, organizations, memberships, strategies and their frozen versions, dataset metadata, backtest jobs and results, entitlements and purchases, the complete audit chain |
| **Comes back only if the storage root was backed up too** | Raw uploaded files and canonical datasets. They are **files, not rows** — `ILUVTRADE_STORAGE_ROOT` needs its own backup, and a database-only restore leaves dataset rows pointing at objects that are gone |
| **Does not come back** | Running trading sessions. A session that was mid-run is not resumable; it replays from the start of its dataset |
| **Stops working** | Every stored broker credential and TOTP secret, **if the restore is paired with a different `ILUVTRADE_SECRET_KEY`**. They are sealed under it. Restoring a database without the key that encrypted it recovers the rows and loses their contents permanently |

That last row is the one that costs people a weekend. Back up the secret key
with the same seriousness as the database, and separately from it.

## Not implemented

Scheduling, retention, offsite replication, and a rehearsed disaster-recovery
drill. The commands above are correct and tested; nothing runs them for you.
