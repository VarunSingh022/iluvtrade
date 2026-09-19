#!/usr/bin/env sh
#
# Container entrypoint.
#
# Migrations run here, explicitly, before the application starts — never via
# ``create_all`` at import time. ``create_all`` creates what is missing and
# ignores what has drifted, so a column added to a model never reaches an
# existing database and the mismatch surfaces later as a query error. The
# application refuses to run ``create_all`` in production at all; this is the
# other half of that decision.
#
# The upgrade is also where a bad deployment should stop. ``set -e`` means a
# failed migration prevents the application from starting against a schema it
# does not match, rather than starting and serving errors.

set -eu

cd /app/backend

case "${1:-serve}" in
  serve)
    echo "==> Validating configuration"
    python -m iluvtrade.cli check-config

    echo "==> Applying migrations"
    alembic upgrade head

    # ILUVTRADE_WEB_CONCURRENCY defaults to 1 on purpose. Rate-limit counters
    # live in each process's memory, so N workers means N x every configured
    # limit. Raising it is a deliberate trade until a shared backend exists;
    # /api/health reports which is actually in force.
    echo "==> Starting API"
    exec uvicorn iluvtrade.main:app \
      --host 0.0.0.0 \
      --port "${PORT:-8000}" \
      --workers "${ILUVTRADE_WEB_CONCURRENCY:-1}" \
      --proxy-headers \
      --forwarded-allow-ips "${ILUVTRADE_FORWARDED_ALLOW_IPS:-127.0.0.1}"
    ;;
  migrate)
    exec alembic upgrade head
    ;;
  check)
    exec python -m iluvtrade.cli check-config
    ;;
  *)
    exec "$@"
    ;;
esac
