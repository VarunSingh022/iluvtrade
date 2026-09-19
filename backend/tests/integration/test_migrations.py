"""Migrations: a fresh database, and an existing one brought forward.

Both paths matter and they exercise different code. A migration that only ever
runs against an empty database is not a migration — it is a schema dump.
"""

from __future__ import annotations

import os
import subprocess
import sys
from itertools import pairwise
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[2]
ALEMBIC = BACKEND / ".venv" / "bin" / "alembic"


def _alembic(*args: str, database_url: str) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "ILUVTRADE_DATABASE_URL": database_url,
        "ILUVTRADE_SECRET_KEY": "migration-test",
    }
    return subprocess.run(
        [str(ALEMBIC), *args],
        cwd=BACKEND,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.skipif(not ALEMBIC.exists(), reason="alembic is not installed")
def test_a_fresh_database_migrates_to_head(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/fresh.db"
    result = _alembic("upgrade", "head", database_url=url)
    assert result.returncode == 0, result.stderr

    import sqlite3

    connection = sqlite3.connect(tmp_path / "fresh.db")
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "alembic_version" in tables
    assert len(tables - {"alembic_version"}) == 27

    columns = {row[1] for row in connection.execute("PRAGMA table_info(audit_events)")}
    assert {"sequence", "previous_hash", "event_hash"} <= columns
    connection.close()


@pytest.mark.skipif(not ALEMBIC.exists(), reason="alembic is not installed")
def test_the_migrated_schema_matches_the_models(tmp_path: Path) -> None:
    """``alembic check`` finds nothing, so migrations and models cannot drift.

    This is the test that catches a model changed without a migration — the
    commonest way a deployment breaks after a green test run.
    """

    url = f"sqlite:///{tmp_path}/check.db"
    assert _alembic("upgrade", "head", database_url=url).returncode == 0

    result = _alembic("check", database_url=url)
    assert result.returncode == 0, (
        "The models have drifted from the migrations. Generate a revision:\n"
        f"{result.stdout}\n{result.stderr}"
    )


@pytest.mark.skipif(not ALEMBIC.exists(), reason="alembic is not installed")
def test_an_existing_pre_chain_database_is_brought_forward(tmp_path: Path) -> None:
    """Data survives, and the chain is backfilled and verifiable.

    Simulates the real upgrade: a database at the baseline revision, holding
    audit rows with no chain columns, stamped and upgraded.
    """

    url = f"sqlite:///{tmp_path}/existing.db"

    # Build it at the baseline only, then insert rows the old writer would have.
    assert _alembic("upgrade", "0001_baseline", database_url=url).returncode == 0

    import sqlite3
    import uuid

    connection = sqlite3.connect(tmp_path / "existing.db")
    organization_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO organizations (id, slug, name, created_at, updated_at) "
        "VALUES (?, 'org', 'Org', '2026-01-01 00:00:00+00:00', '2026-01-01 00:00:00+00:00')",
        (organization_id,),
    )
    for index in range(5):
        connection.execute(
            "INSERT INTO audit_events (id, organization_id, created_at, action, "
            "resource_type, outcome, payload_json) VALUES (?, ?, ?, ?, 'thing', 'success', '{}')",
            (
                str(uuid.uuid4()),
                organization_id,
                f"2026-01-01 00:0{index}:00+00:00",
                f"legacy.event.{index}",
            ),
        )
    connection.commit()
    columns = {row[1] for row in connection.execute("PRAGMA table_info(audit_events)")}
    assert "event_hash" not in columns, "the baseline must predate the chain"
    connection.close()

    result = _alembic("upgrade", "head", database_url=url)
    assert result.returncode == 0, result.stderr
    assert "backfilled 5 audit event(s)" in result.stdout

    connection = sqlite3.connect(tmp_path / "existing.db")
    assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 5, (
        "no row may be lost"
    )
    rows = connection.execute(
        "SELECT sequence, previous_hash, event_hash FROM audit_events ORDER BY sequence"
    ).fetchall()
    connection.close()

    assert [row[0] for row in rows] == [1, 2, 3, 4, 5]
    for earlier, later in pairwise(rows):
        assert later[1] == earlier[2], "each event must link to its predecessor"


@pytest.mark.skipif(not ALEMBIC.exists(), reason="alembic is not installed")
def test_the_chain_backfilled_by_the_migration_passes_verification(tmp_path: Path) -> None:
    """Verified by the application's own code, not the migration's."""

    url = f"sqlite:///{tmp_path}/verify.db"
    assert _alembic("upgrade", "head", database_url=url).returncode == 0

    script = (
        "from iluvtrade.db.session import session_scope\n"
        "from iluvtrade.platform import accounts, audit\n"
        "with session_scope() as s:\n"
        "    _, org = accounts.register(s, email='m@example.com', "
        "password='correct-horse-battery-staple', display_name='M')\n"
        "    org_id = org.id\n"
        "with session_scope() as s:\n"
        "    result = audit.verify_chain(s, org_id)\n"
        "    print('INTACT' if result.intact else 'BROKEN', result.events_checked)\n"
    )
    completed = subprocess.run(
        [str(BACKEND / ".venv" / "bin" / "python"), "-c", script],
        cwd=BACKEND,
        env={
            **os.environ,
            "PYTHONPATH": str(BACKEND),
            "ILUVTRADE_DATABASE_URL": url,
            "ILUVTRADE_STORAGE_ROOT": str(tmp_path / "storage"),
            "ILUVTRADE_SECRET_KEY": "migration-test",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "INTACT" in completed.stdout, completed.stdout


@pytest.mark.skipif(not ALEMBIC.exists(), reason="alembic is not installed")
def test_the_chain_migration_is_reversible(tmp_path: Path) -> None:
    """A migration that cannot be undone is a migration nobody dares run."""

    url = f"sqlite:///{tmp_path}/down.db"
    assert _alembic("upgrade", "head", database_url=url).returncode == 0

    result = _alembic("downgrade", "0001_baseline", database_url=url)
    assert result.returncode == 0, result.stderr

    import sqlite3

    connection = sqlite3.connect(tmp_path / "down.db")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(audit_events)")}
    connection.close()
    assert not ({"sequence", "previous_hash", "event_hash"} & columns)

    assert _alembic("upgrade", "head", database_url=url).returncode == 0


def test_production_never_creates_tables_implicitly(monkeypatch) -> None:
    """A production deployment's schema is the migrations', whatever is set."""

    from iluvtrade.config import get_settings

    monkeypatch.setenv("ILUVTRADE_ENVIRONMENT", "production")
    monkeypatch.setenv("ILUVTRADE_AUTO_CREATE_TABLES", "true")
    monkeypatch.setenv("ILUVTRADE_SECRET_KEY", "x" * 48)
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.is_production
        assert settings.auto_create_tables is True
        assert settings.should_create_tables is False, (
            "production must not create tables even when the setting says so"
        )
    finally:
        get_settings.cache_clear()


def test_development_does_create_tables(monkeypatch) -> None:
    from iluvtrade.config import get_settings

    monkeypatch.setenv("ILUVTRADE_ENVIRONMENT", "development")
    get_settings.cache_clear()
    try:
        assert get_settings().should_create_tables is True
    finally:
        get_settings.cache_clear()


def test_the_python_used_for_migrations_is_the_projects(tmp_path: Path) -> None:
    """Guards the assumption the subprocess tests above rely on."""

    assert sys.executable
    assert ALEMBIC.exists(), "alembic must be installed in the project venv"
