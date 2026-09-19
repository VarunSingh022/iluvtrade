# deploy/

Deployment artifacts. Two things live here.

**`entrypoint.sh`** — the container entrypoint. It validates configuration,
runs `alembic upgrade head`, then starts the API. Migrations run here and
nowhere else; the application refuses to create tables in production.

**`wheels/`** — where the AlphaLab wheel must be placed before building the
image. It is git-ignored, and the directory is empty in a fresh checkout.

```bash
mkdir -p deploy/wheels
cp /path/to/AlphaLab/dist/alphalab-3.0.0-py3-none-any.whl deploy/wheels/
```

AlphaLab is not published to PyPI. `pip install alphalab==3.0.0` fails with
"No matching distribution found", so the wheel has to come from the engine's
own build. The Dockerfile checks for it by exact filename and stops with that
message rather than installing whatever else is in the directory.

## Status

The container image and compose file have **not been built or run**. Docker is
not installed on the machine this was developed on, so claiming otherwise would
be a guess. `docs/DEPLOYMENT.md` describes the path that *has* been exercised —
a local install, migrate, run, health-check and shutdown — and marks this one
as untested.
