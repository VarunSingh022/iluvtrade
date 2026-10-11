"""Shared helpers with a **single module identity**.

``conftest.py`` at the tests root is imported by pytest as ``conftest`` and by
test modules as ``tests.conftest`` — two module objects from one file. Sharing
a *function* across that boundary is harmless; sharing a **class** is not, and
``pytest.raises(SomeError)`` silently stops matching because the exception
raised and the one caught are different objects.

Anything class-shaped that tests and fixtures both need therefore lives here,
which is imported once under one name.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any


class RealDatabaseAccess(RuntimeError):
    """The test suite tried to open the developer's own application database."""


def real_database_path() -> Path:
    """The database a developer's own ``iluvtrade serve`` would use."""

    from iluvtrade.config import REPO_ROOT

    return REPO_ROOT / "var" / "iluvtrade.db"


#: Thread-name prefixes this application owns. Anything else alive at teardown
#: belongs to pytest, anyio or the TestClient's portal and is not ours to join.
APPLICATION_THREAD_PREFIXES = ("session-", "backtest-worker-")


def live_application_threads() -> list[str]:
    """Names of this application's background threads that are still running."""

    return sorted(
        thread.name
        for thread in threading.enumerate()
        if thread.is_alive() and thread.name.startswith(APPLICATION_THREAD_PREFIXES)
    )


#: What the synthetic test CSVs' timestamps mean. ``make_csv`` writes one bare
#: date per bar for NSE-style daily data, so: the trading session of that date,
#: in India Standard Time. Every import must declare this
#: (``iluvtrade.data.temporal``); tests that upload a file send it.
DECLARED: dict[str, str] = {"timestamp_convention": "session_date", "timezone": "Asia/Kolkata"}


def session_date_ist() -> object:
    """The same declaration as an object, for tests that call ``ingest`` directly."""

    from iluvtrade.data.temporal import TemporalDeclaration, TimestampConvention

    return TemporalDeclaration(TimestampConvention.SESSION_DATE, "Asia/Kolkata")


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------
#
# The suite runs on SQLite by default: a temporary file per test. Setting
# ILUVTRADE_TEST_DATABASE_URL to a PostgreSQL server URL (one whose user may
# create databases) runs it on PostgreSQL instead -- every test against its own
# database, cloned from a template that `alembic upgrade head` built at the start
# of the run, and dropped afterwards. There is no fallback in either direction: a
# non-PostgreSQL value is refused, and PostgreSQL tests requested without one
# are refused rather than run on SQLite.

POSTGRES_ENV = "ILUVTRADE_TEST_DATABASE_URL"

#: Every database the suite creates starts with this, and it drops nothing else.
POSTGRES_PREFIX = "iluvtrade_test_"


def postgres_server_url() -> str | None:
    """The PostgreSQL server URL the suite was asked to use, or ``None`` for SQLite."""

    import os

    value = os.environ.get(POSTGRES_ENV, "").strip()
    return value or None


def postgres_url_for(database: str) -> str:
    """The SQLAlchemy URL of ``database`` on the requested server."""

    from sqlalchemy.engine import make_url

    server = postgres_server_url()
    assert server is not None
    return make_url(server).set(database=database).render_as_string(hide_password=False)


def postgres_admin_connection() -> Any:
    """An autocommit psycopg connection to the server's maintenance database."""

    import psycopg
    from sqlalchemy.engine import make_url

    server = postgres_server_url()
    assert server is not None
    url = make_url(server).set(drivername="postgresql", database="postgres")
    return psycopg.connect(url.render_as_string(hide_password=False), autocommit=True)


def create_postgres_database(name: str, *, template: str | None = None) -> None:
    if not name.startswith(POSTGRES_PREFIX):
        raise ValueError(f"Refusing to create {name!r}: not a test database name.")
    clause = f' TEMPLATE "{template}"' if template else ""
    with postgres_admin_connection() as connection:
        connection.execute(f'CREATE DATABASE "{name}"{clause}')


def drop_postgres_database(name: str) -> None:
    # The prefix check is the one thing standing between a typo and dropping a
    # real database on a shared server; it is not optional.
    if not name.startswith(POSTGRES_PREFIX):
        raise ValueError(f"Refusing to drop {name!r}: not a test database name.")
    with postgres_admin_connection() as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


# ---------------------------------------------------------------------------
# A populated schema, for migration tests
# ---------------------------------------------------------------------------

#: The largest seed a pre-0006 (32-bit INTEGER) column can hold.
OLD_SEED_MAX = 2**31 - 1


def seed_every_table(connection: Any) -> dict[str, str]:
    """Insert one row into every table, parents first, filling required columns.

    Reflects whatever schema the connection's database has *now* (any revision),
    so a migration test can populate the schema as it stood before a revision
    and check what the revision does to real rows -- not just to empty tables.
    Returns the id inserted per table.
    """

    import datetime as dt
    import uuid

    from sqlalchemy import (
        BigInteger,
        Boolean,
        DateTime,
        Float,
        Integer,
        LargeBinary,
        MetaData,
        Numeric,
    )

    metadata = MetaData()
    metadata.reflect(bind=connection)
    ids: dict[str, str] = {}
    for table in metadata.sorted_tables:
        if table.name == "alembic_version":
            continue
        row: dict[str, Any] = {}
        for column in table.columns:
            foreign = next(iter(column.foreign_keys), None)
            if foreign is not None and foreign.column.table.name in ids:
                row[column.name] = ids[foreign.column.table.name]
                continue
            if column.nullable or column.server_default is not None:
                continue
            if column.primary_key and column.name == "id":
                row[column.name] = str(uuid.uuid4())
            elif column.name == "seed":
                row[column.name] = OLD_SEED_MAX
            elif isinstance(column.type, Boolean):
                row[column.name] = False
            elif isinstance(column.type, Integer | BigInteger):
                row[column.name] = 1
            elif isinstance(column.type, Float | Numeric):
                row[column.name] = 0.0
            elif isinstance(column.type, DateTime):
                row[column.name] = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
            elif isinstance(column.type, LargeBinary):
                row[column.name] = b""
            elif column.name.endswith("_json"):
                row[column.name] = "{}"
            else:
                row[column.name] = f"{table.name}-{column.name}"[
                    : getattr(column.type, "length", None) or 64
                ]
        connection.execute(table.insert().values(**row))
        if "id" in row:
            ids[table.name] = row["id"]
    return ids
