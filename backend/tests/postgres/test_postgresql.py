"""Behaviour that only PostgreSQL can establish.

These run only when ILUVTRADE_TEST_DATABASE_URL names a PostgreSQL server, and
are refused -- not skipped, not run on SQLite -- when it was requested without
one (``tests/conftest.py``). Every test here starts by proving it is connected to
PostgreSQL; the conftest proves it again for each test's own database.

The defects below were found by running the whole suite against PostgreSQL for
the first time. Each test fails against the code as it was:

* two concurrent uploads in one workspace froze the worker -- the upload route
  ran blocking database work on the event loop, and PostgreSQL waits for a lock
  forever where SQLite's busy timeout gave up;
* concurrent audited writes in one workspace failed on the chain's unique
  sequence instead of queueing;
* a derived seed (up to 2**32 - 1) overflowed PostgreSQL's 32-bit INTEGER, so
  roughly half of all backtests failed to save;
* a NUL character in a path reached the driver and answered 500.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import make_csv, register
from tests.support import (
    DECLARED,
    POSTGRES_PREFIX,
    create_postgres_database,
    drop_postgres_database,
    postgres_admin_connection,
    postgres_url_for,
)

pytestmark = pytest.mark.postgres

HEADERS = {"X-Requested-With": "XMLHttpRequest"}


def _engine() -> Any:
    from iluvtrade.db.session import get_engine

    return get_engine()


def _scalar(sql: str, **params: Any) -> Any:
    from sqlalchemy import text

    with _engine().connect() as connection:
        return connection.execute(text(sql), params).scalar_one()


def _ready(client: Any, *, key: str = "buy_and_hold") -> tuple[str, str]:
    version = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=40), "text/csv")},
        data=DECLARED,
        headers=HEADERS,
    ).json()
    approved = client.post(f"/api/v1/datasets/versions/{version['id']}/approve", headers=HEADERS)
    assert approved.status_code == 200, approved.text
    strategy = client.post("/api/v1/strategies", json={"name": "PG"}, headers=HEADERS).json()
    draft = client.post(
        f"/api/v1/strategies/{strategy['id']}/versions",
        json={"implementation_key": key, "parameters": {"quantity": 1}},
        headers=HEADERS,
    ).json()
    client.post(f"/api/v1/strategies/versions/{draft['id']}/publish", headers=HEADERS)
    return version["id"], draft["id"]


# --- connection and schema ----------------------------------------------------


def test_this_run_is_connected_to_postgresql() -> None:
    engine = _engine()
    assert engine.dialect.name == "postgresql"
    assert int(str(_scalar("SHOW server_version_num"))) >= 130000
    assert str(_scalar("SELECT current_database()")).startswith(POSTGRES_PREFIX)
    # Bounded lock waits are configured on every application connection.
    assert _scalar("SHOW lock_timeout") == "5s"


def test_the_schema_is_the_one_the_migrations_build() -> None:
    from pathlib import Path

    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from iluvtrade.db.models import Base

    backend = Path(__file__).resolve().parents[2]
    from alembic.config import Config

    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()

    with _engine().connect() as connection:
        assert _scalar("SELECT version_num FROM alembic_version") == head
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


def _alembic_on(url: str, *steps: tuple[str, str]) -> None:
    """Run alembic commands in-process against ``url``, restoring the test's settings."""

    import os

    from alembic import command
    from alembic.config import Config

    from iluvtrade.config import get_settings

    backend = Path(__file__).resolve().parents[2]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    previous = os.environ["ILUVTRADE_DATABASE_URL"]
    os.environ["ILUVTRADE_DATABASE_URL"] = url
    get_settings.cache_clear()
    try:
        for action, target in steps:
            getattr(command, action)(config, target)
    finally:
        os.environ["ILUVTRADE_DATABASE_URL"] = previous
        get_settings.cache_clear()


def test_migrations_go_down_to_base_and_back_up_with_a_row_in_every_table() -> None:
    """0006 and 0007 against real rows, then down to base and up again.

    Existing sessions come out ``same_event`` (what they ran), existing dataset
    versions with no provenance, existing runs with no artifact digest, every
    seed intact. A downgrade that would truncate a 64-bit seed is refused by
    PostgreSQL rather than performed -- the alternative corrupts the number a run
    is reproduced from.
    """

    import uuid

    from sqlalchemy import create_engine, text

    from tests.support import OLD_SEED_MAX, seed_every_table

    name = f"{POSTGRES_PREFIX}migrate_{uuid.uuid4().hex[:10]}"
    url = postgres_url_for(name)
    create_postgres_database(name)
    engine = create_engine(url)
    try:
        _alembic_on(url, ("upgrade", "0005_teams_and_reset"))
        with engine.begin() as connection:
            ids = seed_every_table(connection)
        assert {"trading_sessions", "dataset_versions", "backtest_runs"} <= set(ids)

        _alembic_on(url, ("upgrade", "head"))
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT seed, fill_timing FROM trading_sessions")
            ).one() == (OLD_SEED_MAX, "same_event")
            assert connection.execute(
                text("SELECT seed, artifact_sha256 FROM backtest_runs")
            ).one() == (OLD_SEED_MAX, None)
            assert connection.execute(text("SELECT temporal_json FROM dataset_versions")).one() == (
                "{}",
            )
            types = dict(
                connection.execute(
                    text(
                        "SELECT table_name || '.' || column_name, data_type "
                        "FROM information_schema.columns WHERE column_name = 'seed'"
                    )
                ).all()
            )
        assert types == {"backtest_runs.seed": "bigint", "trading_sessions.seed": "bigint"}

        # A seed only BIGINT can hold: the downgrade refuses instead of truncating.
        with engine.begin() as connection:
            connection.execute(text("UPDATE trading_sessions SET seed = :s"), {"s": 2**32 - 1})
        engine.dispose()
        with pytest.raises(Exception, match="out of range"):
            _alembic_on(url, ("downgrade", "0005_teams_and_reset"))
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT seed FROM trading_sessions")).scalar_one()
                == 2**32 - 1
            )
            # Still at head: the refused downgrade -- 0008's step included --
            # rolled back as one transaction.
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == ("0008_temporal_evidence")

        # A stated fill timing would be replaced by "same_event" on re-upgrade.
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE trading_sessions SET seed = :s, fill_timing = 'next_event'"),
                {"s": OLD_SEED_MAX},
            )
        engine.dispose()
        with pytest.raises(RuntimeError, match="Refusing to downgrade below 0006"):
            _alembic_on(url, ("downgrade", "0005_teams_and_reset"))

        # With only backfilled values and representable seeds, the cycle works.
        with engine.begin() as connection:
            connection.execute(text("UPDATE trading_sessions SET fill_timing = 'same_event'"))
        engine.dispose()
        _alembic_on(url, ("downgrade", "0005_teams_and_reset"))
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT count(*) FROM trading_sessions")).scalar_one() == 1
            )
        engine.dispose()
        _alembic_on(url, ("downgrade", "base"), ("upgrade", "head"))
    finally:
        engine.dispose()
        drop_postgres_database(name)


# --- defects only PostgreSQL showed ---------------------------------------------


def test_a_seed_beyond_32_bits_is_stored(client) -> None:
    from iluvtrade.backtests.worker import BacktestWorkerPool

    register(client, "bigseed@example.com")
    dataset, strategy = _ready(client)
    seed = 2**32 - 1
    job = client.post(
        "/api/v1/backtests",
        json={"dataset_version_id": dataset, "strategy_version_id": strategy, "seed": seed},
        headers=HEADERS,
    ).json()
    BacktestWorkerPool(size=1).drain(timeout=120)
    finished = client.get(f"/api/v1/backtests/{job['id']}").json()
    assert finished["status"] == "completed", finished
    result = client.get(f"/api/v1/backtests/{job['id']}/result").json()
    assert result["reproducibility"]["seed"] == seed

    session = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "Big seed",
            "mode": "paper",
            "strategy_version_id": strategy,
            "dataset_version_id": dataset,
            "seed": seed,
        },
        headers=HEADERS,
    )
    assert session.status_code == 201, session.text
    assert session.json()["seed"] == seed


def test_concurrent_audited_writes_in_one_workspace_all_succeed_in_order(client) -> None:
    """Six transactions overlap on one organization's audit chain; all commit."""

    from iluvtrade.db.models.platform import Organization
    from iluvtrade.db.session import session_scope
    from iluvtrade.platform import audit

    register(client, "chain@example.com")
    with session_scope() as session:
        organization_id = session.query(Organization.id).one()[0]
        before = audit.chain_head(session, organization_id)[0]

    start = threading.Barrier(6)

    def write(index: int) -> int:
        start.wait()
        with session_scope() as session:
            event = audit.record(
                session,
                organization_id=organization_id,
                action="test.concurrent",
                resource_type="thing",
                resource_id=str(index),
            )
            time.sleep(0.2)  # hold the transaction open so the writers overlap
            return int(event.sequence)

    with ThreadPoolExecutor(max_workers=6) as pool:
        sequences = sorted(pool.map(write, range(6)))

    assert sequences == list(range(before + 1, before + 7))
    with session_scope() as session:
        verification = audit.verify_chain(session, organization_id)
    assert verification.breaks == ()


def test_concurrent_uploads_complete_rather_than_freeze_the_worker(app) -> None:
    from fastapi.testclient import TestClient

    with TestClient(app, raise_server_exceptions=False) as client:
        register(client, "parallel@example.com")

        def upload(index: int) -> Any:
            return client.post(
                "/api/v1/datasets/upload",
                files={"file": (f"d{index}.csv", make_csv(bars=20 + index), "text/csv")},
                data={**DECLARED, "dataset_name": f"set-{index}"},
                headers=HEADERS,
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(upload, index) for index in range(4)]
            responses = [future.result(timeout=90) for future in futures]

    assert [response.status_code for response in responses] == [201] * 4, [
        response.text for response in responses
    ]
    assert len({response.json()["id"] for response in responses}) == 4


def test_a_lock_wait_is_bounded_and_answers_busy(client) -> None:
    """A writer stuck behind a held lock gets a retryable 503 in seconds, never a hang."""

    from iluvtrade.db.models.platform import Organization
    from iluvtrade.db.session import session_scope

    register(client, "busy@example.com")
    with session_scope() as session:
        organization_id = session.query(Organization.id).one()[0]
    database = str(_scalar("SELECT current_database()"))

    import psycopg
    from sqlalchemy.engine import make_url

    holder = psycopg.connect(
        make_url(postgres_url_for(database))
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    try:
        holder.execute("SELECT id FROM organizations WHERE id = %s FOR UPDATE", (organization_id,))
        started = time.monotonic()
        response = client.post("/api/v1/strategies", json={"name": "Blocked"}, headers=HEADERS)
        waited = time.monotonic() - started
    finally:
        holder.rollback()
        holder.close()

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "DatabaseBusy"
    assert 4.0 <= waited < 30.0
    # The failed transaction was rolled back and its connection returned clean:
    # nothing is left idle in a transaction (aborted or not), and nothing waits.
    with postgres_admin_connection() as admin:
        lingering = admin.execute(
            "SELECT state FROM pg_stat_activity WHERE datname = %s "
            "AND state LIKE 'idle in transaction%%'",
            (database,),
        ).fetchall()
        waiting = admin.execute(
            "SELECT count(*) FROM pg_locks l JOIN pg_database d ON d.oid = l.database "
            "WHERE d.datname = %s AND NOT l.granted",
            (database,),
        ).fetchone()
    assert lingering == [] and waiting == (0,)
    # And once the lock is gone, the pool's connections serve requests normally.
    for index in range(5):
        response = client.post(
            "/api/v1/strategies", json={"name": f"Free {index}"}, headers=HEADERS
        )
        assert response.status_code == 201, response.text


def test_nul_characters_are_refused_before_postgresql_sees_them(client) -> None:
    register(client, "nul@example.com")
    assert client.get("/api/v1/datasets/versions/%00").status_code == 400
    assert client.get("/api/v1/datasets?x=%00").status_code == 400
    body = client.post("/api/v1/strategies", json={"name": "bad\x00name"}, headers=HEADERS)
    assert body.status_code == 422
    # Multipart fields are not request models; they are checked too.
    form = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=10), "text/csv")},
        data={**DECLARED, "dataset_name": "bad\x00name"},
        headers=HEADERS,
    )
    assert form.status_code == 422, form.text


# --- PostgreSQL semantics the application relies on -------------------------


def test_a_constraint_violation_rolls_back_and_the_next_request_succeeds(client) -> None:
    register(client, "twice@example.com")
    again = client.post(
        "/api/v1/auth/register",
        json={
            "email": "twice@example.com",
            "password": "correct-horse-battery-staple",
            "display_name": "Again",
        },
    )
    assert 400 <= again.status_code < 500, again.text
    # The aborted transaction did not poison the connection the next request gets.
    assert (
        client.post("/api/v1/strategies", json={"name": "After"}, headers=HEADERS).status_code
        == 201
    )


def test_deleting_an_organization_cascades_in_the_database(client) -> None:
    from sqlalchemy import text

    register(client, "cascade@example.com")
    _ready(client)
    with _engine().begin() as connection:
        organization_id = connection.execute(text("SELECT id FROM organizations")).scalar_one()
        assert connection.execute(text("SELECT count(*) FROM dataset_versions")).scalar_one() == 1
        connection.execute(
            text("DELETE FROM organizations WHERE id = :id"), {"id": organization_id}
        )
        for table in ("memberships", "datasets", "dataset_versions", "strategies"):
            assert connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0


def test_timestamps_come_back_timezone_aware_in_utc(client) -> None:
    import datetime as dt

    from iluvtrade.db.models.platform import User
    from iluvtrade.db.session import session_scope

    register(client, "tz@example.com")
    with session_scope() as session:
        created = session.query(User.created_at).one()[0]
    assert created.tzinfo is not None and created.utcoffset() == dt.timedelta(0)
    assert (
        _scalar(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'created_at'"
        )
        == "timestamp with time zone"
    )


def test_the_research_and_paper_workflow_runs_end_to_end(client) -> None:
    from iluvtrade.backtests.worker import BacktestWorkerPool
    from iluvtrade.trading.runner import RUNNER

    register(client, "workflow@example.com")
    dataset, strategy = _ready(client)
    job = client.post(
        "/api/v1/backtests",
        json={"dataset_version_id": dataset, "strategy_version_id": strategy},
        headers=HEADERS,
    ).json()
    BacktestWorkerPool(size=1).drain(timeout=120)
    result = client.get(f"/api/v1/backtests/{job['id']}/result").json()
    assert result["result"]["fills"], "the run traded"
    assert result["result"]["execution_assumptions"]["fill_timing"] == "next_event"

    session = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "PG paper",
            "mode": "paper",
            "strategy_version_id": strategy,
            "dataset_version_id": dataset,
        },
        headers=HEADERS,
    ).json()
    client.post(f"/api/v1/trading/sessions/{session['id']}/start", headers=HEADERS)
    RUNNER.join(session["id"], timeout=180)
    state = client.get(f"/api/v1/trading/sessions/{session['id']}").json()
    assert state["status"] == "stopped", state
    fills = client.get(f"/api/v1/trading/sessions/{session['id']}/fills").json()
    assert fills and fills[0]["fill_timestamp"] == result["result"]["fills"][0]["timestamp"]


def test_the_test_database_prefix_guards_drops() -> None:
    with pytest.raises(ValueError, match="not a test database"):
        drop_postgres_database("postgres")
    with postgres_admin_connection() as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)
