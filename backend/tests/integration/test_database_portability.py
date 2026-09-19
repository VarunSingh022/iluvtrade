"""The schema's portability, pinned where it can be pinned without PostgreSQL.

**No PostgreSQL server or driver exists on this machine**, so nothing here
claims PostgreSQL works. What these tests do is narrower and still worth
having: they pin the *choices* that make the schema portable, so a column type
that would only work on SQLite cannot arrive unnoticed between now and the day
someone actually runs it against PostgreSQL.

Every assertion below corresponds to a line in the compatibility table in
``docs/DEPLOYMENT.md``. When one fails, that table is what needs updating too.
"""

from __future__ import annotations

import sqlalchemy as sa

from iluvtrade.db.models import Base

#: Column types that behave differently enough across backends to be worth
#: refusing outright, with what to use instead.
PORTABILITY_HAZARDS = {
    sa.JSON: "store JSON as Text; the application parses it, and no query indexes into it",
    sa.ARRAY: "PostgreSQL-only",
}

#: ``Float`` is a subclass of ``Numeric`` in SQLAlchemy, and it is fine: REAL on
#: SQLite, DOUBLE PRECISION on PostgreSQL, and used here only for metrics and
#: epoch timestamps. A *true* ``Numeric`` is the hazard — SQLite has no decimal
#: type, so it round-trips through a float and silently loses precision, which
#: is exactly why money is a string.
FLOAT_COLUMNS_ARE_METRICS_NOT_MONEY = True


def _columns():
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            yield table.name, column


def test_no_column_uses_a_backend_specific_type() -> None:
    offenders = []
    for table_name, column in _columns():
        for hazard, reason in PORTABILITY_HAZARDS.items():
            if isinstance(column.type, hazard):
                offenders.append(f"{table_name}.{column.name} is {hazard.__name__} — {reason}")
    assert not offenders, offenders


def test_no_column_is_a_true_decimal() -> None:
    """SQLite has no decimal type; a Numeric column loses precision silently."""

    offenders = [
        f"{table_name}.{column.name}"
        for table_name, column in _columns()
        if isinstance(column.type, sa.Numeric) and not isinstance(column.type, sa.Float)
    ]
    assert not offenders, (
        f"These columns are Numeric: {offenders}. On SQLite the value round-trips "
        "through a float. Money is String(40) here for exactly that reason."
    )


def test_float_columns_are_only_ever_metrics_or_timestamps() -> None:
    """Float is portable and imprecise; the question is what it holds."""

    allowed = {
        ("creator_payouts", "rating_snapshot"),
        ("dataset_versions", "start_timestamp"),
        ("dataset_versions", "end_timestamp"),
        ("dataset_versions", "quality_score"),
        ("backtest_jobs", "progress"),
        ("backtest_runs", "total_return"),
        ("backtest_runs", "cagr"),
        ("backtest_runs", "volatility"),
        ("backtest_runs", "sharpe_ratio"),
        ("backtest_runs", "max_drawdown"),
        ("session_fills", "fill_timestamp"),
        ("session_orders", "submitted_timestamp"),
    }
    found = {
        (table_name, column.name)
        for table_name, column in _columns()
        if isinstance(column.type, sa.Float)
    }
    unexpected = sorted(found - allowed)
    assert not unexpected, (
        f"New Float column(s): {unexpected}. If any of them holds money, it must "
        "be a string — a float cannot represent 0.1, and a position is not "
        "approximately right."
    )


def test_money_is_stored_as_text_not_as_a_float() -> None:
    """A float cannot hold 0.1 exactly, and a position is not approximately right."""

    money_columns = {
        ("trading_sessions", "starting_cash"),
        ("trading_sessions", "cash"),
        ("trading_sessions", "equity"),
        ("trading_sessions", "realized_pnl"),
        ("session_fills", "price"),
        ("session_fills", "commission"),
        ("listings", "price_amount"),
    }
    seen = 0
    for table_name, column in _columns():
        if (table_name, column.name) in money_columns:
            seen += 1
            assert isinstance(column.type, sa.String), (
                f"{table_name}.{column.name} is {column.type!r}, not a string"
            )
    assert seen == len(money_columns), "a money column was renamed; update this list"


def test_every_enum_is_stored_as_text_rather_than_a_native_type() -> None:
    """A native PostgreSQL enum needs its own migration to add a value.

    Storing the value as text makes adding one an application change, which is
    what it actually is — and keeps the two backends' schemas the same shape.
    """

    native = [
        f"{table_name}.{column.name}"
        for table_name, column in _columns()
        if isinstance(column.type, sa.Enum) and column.type.native_enum
    ]
    assert not native, f"These columns would become a native enum: {native}"


def test_every_primary_key_is_an_application_generated_uuid() -> None:
    """No autoincrement, so nothing depends on a backend's sequence behaviour."""

    wrong = []
    for table in Base.metadata.sorted_tables:
        if table.name == "alembic_version":
            continue
        keys = list(table.primary_key.columns)
        if len(keys) != 1 or not isinstance(keys[0].type, sa.String):
            wrong.append(f"{table.name}: {[c.name for c in keys]}")
        elif keys[0].autoincrement is True:
            wrong.append(f"{table.name}.{keys[0].name} autoincrements")
    assert not wrong, wrong


def test_every_timestamp_column_is_the_timezone_aware_decorator() -> None:
    """SQLite has no timezone storage; PostgreSQL does. One type hides both."""

    from iluvtrade.db.base import UtcDateTime

    plain = [
        f"{table_name}.{column.name}"
        for table_name, column in _columns()
        if isinstance(column.type, sa.DateTime) and not isinstance(column.type, UtcDateTime)
    ]
    assert not plain, (
        f"These columns use a bare DateTime: {plain}. Use UtcDateTime, which "
        "refuses a naive value and returns UTC-aware on every backend."
    )


def test_cascade_deletes_are_declared_on_the_constraint() -> None:
    """Not emulated in the application, so PostgreSQL enforces them natively.

    SQLite needs ``PRAGMA foreign_keys=ON``, which ``db/session.py`` sets for
    SQLite only. Declaring the cascade on the constraint means the behaviour is
    the database's on both, rather than application code that one backend runs
    and the other does not.
    """

    declared = [
        fk for table in Base.metadata.sorted_tables for fk in table.foreign_keys if fk.ondelete
    ]
    assert len(declared) > 20, "the schema should rely on database-level cascades"
    assert all(fk.ondelete.upper() == "CASCADE" for fk in declared)


def test_sqlite_pragmas_are_applied_only_to_sqlite() -> None:
    """A PRAGMA sent to PostgreSQL is a syntax error at connect time."""

    import inspect

    from iluvtrade.db import session as db_session

    source = inspect.getsource(db_session.get_engine)
    assert 'url.startswith("sqlite")' in source
    pragma_source = inspect.getsource(db_session._configure_sqlite)
    assert "PRAGMA" in pragma_source
    assert "_configure_sqlite" not in source.split("else:")[-1], (
        "the PRAGMA hook must not be attached on the non-SQLite branch"
    )


def test_the_postgresql_driver_is_installable_from_this_package() -> None:
    """It was not: the Dockerfile installed psycopg and pyproject did not.

    ``pip install -e . && point at PostgreSQL`` then failed with
    ModuleNotFoundError — an error about Python packaging, for what is
    documented as the production database.
    """

    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())
    extras = pyproject["project"]["optional-dependencies"]
    assert "postgres" in extras
    assert any("psycopg" in entry for entry in extras["postgres"])
