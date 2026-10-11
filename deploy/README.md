# deploy/

Deployment artifacts.

**`entrypoint.sh`** — the container entrypoint. It validates configuration,
runs `alembic upgrade head`, then starts the API. Migrations run here and
nowhere else; the application refuses to create tables in production.

**The AlphaLab engine is not placed here.** It used to be: a wheel had to be
copied into `deploy/wheels/` before the image could be built. Since the
v4.1.0 integration, `backend/pyproject.toml` depends on the published release
wheel by direct reference with its SHA-256, so the image build's
`pip install "./backend[postgres]"` downloads exactly that artifact from the
AlphaLab GitHub release and pip refuses it if its digest differs. The build
needs network access to github.com, and nothing else.

## Status

The container image and compose file have **not been built or run**. Docker is
not installed on the machine this was developed on, so claiming otherwise would
be a guess. `docs/DEPLOYMENT.md` describes the path that *has* been exercised —
a local install, migrate, run, health-check and shutdown — and marks this one
as untested.
