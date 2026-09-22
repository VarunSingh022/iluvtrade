# syntax=docker/dockerfile:1
#
# iluvtrade, as one image: the API and the built frontend it serves.
#
# One image rather than two because the application already serves the SPA from
# ``frontend/dist`` when that directory exists (see ``iluvtrade/main.py``).
# Splitting them would add a reverse proxy, a second deployment unit and a CORS
# configuration to solve a problem this application does not have.
#
# ---------------------------------------------------------------------------
# BEFORE YOU BUILD: the AlphaLab wheel
# ---------------------------------------------------------------------------
#
# ``alphalab==3.5.0`` is NOT on PyPI. It is built from its own repository and
# installed from a local wheel, which means this image cannot be built from a
# clean checkout alone. Put the wheel in ``deploy/wheels/`` first:
#
#     mkdir -p deploy/wheels
#     cp /path/to/AlphaLab/dist/alphalab-3.5.0-py3-none-any.whl deploy/wheels/
#
# Stating this here rather than letting the build fail at the pip step, and
# pinning the version rather than taking whatever wheel is present, is the
# difference between a reproducible image and one that silently picks up a
# different engine build.

# --- stage 1: the frontend -------------------------------------------------
FROM node:22-bookworm-slim AS frontend

WORKDIR /build
# Dependencies first, so a source change does not re-resolve the tree.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


# --- stage 2: the application ----------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# argon2-cffi and cryptography ship wheels for this platform; build tools are
# only needed if a wheel is missing for the target architecture.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The engine, from the wheel that must be present in the build context.
COPY deploy/wheels/ /tmp/wheels/
RUN test -f /tmp/wheels/alphalab-3.5.0-py3-none-any.whl \
      || (echo "ERROR: deploy/wheels/alphalab-3.5.0-py3-none-any.whl is missing. See the header of this Dockerfile." >&2; exit 1) \
 && pip install /tmp/wheels/alphalab-3.5.0-py3-none-any.whl

COPY backend/pyproject.toml backend/alembic.ini ./backend/
COPY backend/iluvtrade/ ./backend/iluvtrade/
COPY backend/migrations/ ./backend/migrations/
# One command, one source of truth: the PostgreSQL driver comes from the
# package's own `postgres` extra rather than a version pinned separately here.
# Two pins for one dependency drift apart, and the one in a Dockerfile nobody
# has built drifts silently.
RUN pip install "./backend[postgres]"

COPY --from=frontend /build/dist/ ./frontend/dist/
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Not root. A process that never needs to write outside its storage root has no
# reason to be able to.
RUN useradd --system --create-home --uid 10001 iluvtrade \
 && mkdir -p /var/lib/iluvtrade/storage \
 && chown -R iluvtrade:iluvtrade /var/lib/iluvtrade /app
USER iluvtrade

ENV ILUVTRADE_STORAGE_ROOT=/var/lib/iluvtrade/storage \
    ILUVTRADE_ENVIRONMENT=production \
    ILUVTRADE_AUTO_CREATE_TABLES=false

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["serve"]
