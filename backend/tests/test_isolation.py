"""The test suite cannot touch the developer's own database.

This file exists because it once could. A trading-session runner is a daemon
thread; when a test ended and the fixture cleared the settings cache, a still-
polling runner re-resolved the database URL *without* the monkeypatch, got the
default, and wrote 33 rows into ``var/iluvtrade.db``.

Three mechanisms now stand in the way, and they fail differently on purpose:

1. **Prevention** — the engine guard refuses to build an engine for that path,
   before a connection is opened.
2. **Containment** — every application thread is stopped and joined before the
   settings cache is cleared, so nothing is left to re-resolve anything.
3. **Detection** — the file's SHA-256 is compared across the whole run, in case
   something reaches it by a route the guard does not recognise.

The tests below exercise all three. A guard nobody tests is a guard nobody
knows is still connected.
"""

from __future__ import annotations

import threading

import pytest

from tests.support import RealDatabaseAccess, live_application_threads, real_database_path

# --- prevention -------------------------------------------------------------


def test_building_an_engine_for_the_real_database_is_refused() -> None:
    """The control, exercised directly."""

    import iluvtrade.db.session as db_session

    with pytest.raises(RealDatabaseAccess, match="developer's own"):
        db_session.create_engine(f"sqlite:///{real_database_path()}")


def test_the_refusal_survives_a_relative_or_indirect_path(tmp_path) -> None:
    """A path that resolves to the same file is the same file."""

    import iluvtrade.db.session as db_session

    real = real_database_path()
    indirect = real.parent / "." / real.name
    with pytest.raises(RealDatabaseAccess):
        db_session.create_engine(f"sqlite:///{indirect}")


def test_a_temporary_database_is_allowed(tmp_path) -> None:
    """The guard must not be so broad that nothing can open anything."""

    import iluvtrade.db.session as db_session

    engine = db_session.create_engine(f"sqlite:///{tmp_path / 'fine.db'}")
    assert engine is not None
    engine.dispose()


def test_an_in_memory_database_is_allowed() -> None:
    import iluvtrade.db.session as db_session

    engine = db_session.create_engine("sqlite:///:memory:")
    assert engine is not None
    engine.dispose()


def test_a_test_can_opt_in_explicitly(allows_real_database) -> None:
    """A policy with a stated exception, rather than one people disable wholesale.

    Nothing in the suite needs this. It exists so the first person who
    genuinely does has a documented door instead of a reason to delete the
    guard — and so that using it is visible in the test's own signature.
    """

    import iluvtrade.db.session as db_session

    engine = db_session.create_engine(f"sqlite:///{real_database_path()}")
    assert engine is not None
    engine.dispose()


def test_settings_inside_a_test_never_resolve_to_the_real_database() -> None:
    """Belt and braces: the URL a test would get is a temporary one."""

    from iluvtrade.config import get_settings

    url = get_settings().database_url
    assert url.startswith("sqlite:///")
    assert str(real_database_path()) not in url
    assert "iluvtrade-test-" in url or "/tmp" in url or "/var/folders/" in url


# --- containment ------------------------------------------------------------


def test_no_application_thread_is_running_at_the_start_of_a_test() -> None:
    """Every previous test cleaned up after itself, or this fails."""

    assert live_application_threads() == []


def test_a_started_worker_pool_is_stoppable_and_deregisters_itself(db) -> None:
    # ``db`` creates the schema: starting a pool requeues orphaned jobs, which
    # is a query, and a pool that cannot query is not the thing under test.
    from iluvtrade.backtests.worker import BacktestWorkerPool, stop_all_pools

    pool = BacktestWorkerPool(size=2, poll_seconds=0.02)
    pool.start()
    assert any(name.startswith("backtest-worker-") for name in live_application_threads())

    assert stop_all_pools(timeout=10) == 1
    assert live_application_threads() == []
    # Idempotent: a second sweep finds nothing to do.
    assert stop_all_pools(timeout=1) == 0


def test_a_session_runner_is_joined_rather_than_abandoned(client, headers) -> None:
    """The specific thread that caused the contamination."""

    from iluvtrade.trading.runner import RUNNER
    from tests.conftest import make_csv, register

    register(client, "join@example.com")
    version = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=30), "text/csv")},
        headers=headers,
    ).json()
    client.post(f"/api/v1/datasets/versions/{version['id']}/approve", headers=headers)
    strategy = client.post("/api/v1/strategies", json={"name": "S"}, headers=headers).json()
    draft = client.post(
        f"/api/v1/strategies/{strategy['id']}/versions",
        json={"implementation_key": "buy_and_hold", "parameters": {"quantity": 1}},
        headers=headers,
    ).json()
    client.post(f"/api/v1/strategies/versions/{draft['id']}/publish", headers=headers)
    session = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "Joined",
            "mode": "paper",
            "dataset_version_id": version["id"],
            "strategy_version_id": draft["id"],
            "starting_cash": "100000",
        },
        headers=headers,
    ).json()
    client.post(f"/api/v1/trading/sessions/{session['id']}/start", headers=headers)

    RUNNER.join_all(timeout=30)
    assert live_application_threads() == [], (
        "a session runner survived join_all(); it would re-resolve settings "
        "after this test's fixture tears them down"
    )


# --- detection --------------------------------------------------------------


def test_the_leak_detector_actually_detects_a_leak() -> None:
    """A detector nobody has seen fire is a detector nobody should trust."""

    stop = threading.Event()
    thread = threading.Thread(target=stop.wait, name="session-probe", daemon=True)
    thread.start()
    try:
        assert "session-probe" in live_application_threads()
    finally:
        stop.set()
        thread.join(timeout=5)
    assert live_application_threads() == []


# --- the original failure, reproduced ---------------------------------------


def test_the_exact_sequence_that_caused_the_contamination_is_now_refused(
    monkeypatch,
) -> None:
    """Reproduce the bug, and prove the guard stops it.

    On 2026-09-19 the sequence was:

    1. A test started a paper session, which launched a daemon runner thread.
    2. The test ended. The fixture cleared the settings cache and reset the
       engine — while the runner was still polling.
    3. The runner called ``get_session_factory()``. The monkeypatched
       ``ILUVTRADE_DATABASE_URL`` was gone, so settings resolved to the default:
       ``var/iluvtrade.db``.
    4. It wrote 33 rows into the developer's own database.

    This performs steps 2 and 3 deliberately — the settings cache cleared and
    the environment override removed, exactly as a torn-down fixture leaves
    them — and asserts step 3 now raises instead of succeeding.
    """

    from iluvtrade.config import get_settings
    from iluvtrade.db.session import get_session_factory, reset_engine

    # Step 2: the fixture has torn down, and the override with it.
    monkeypatch.delenv("ILUVTRADE_DATABASE_URL", raising=False)
    get_settings.cache_clear()
    reset_engine()

    # The settings a stranded thread would now see are the developer's own.
    assert str(real_database_path()) in get_settings().database_url

    # Step 3: and building a session factory against them is refused.
    with pytest.raises(RealDatabaseAccess):
        get_session_factory()


def test_a_stranded_daemon_thread_cannot_write_either(monkeypatch) -> None:
    """The same thing, from an actual background thread.

    The refusal has to hold on a thread that is not the one pytest is running,
    because that is where it originally failed. A thread's exception does not
    propagate, so the outcome is captured and asserted here.
    """

    from iluvtrade.config import get_settings
    from iluvtrade.db.session import get_session_factory, reset_engine

    monkeypatch.delenv("ILUVTRADE_DATABASE_URL", raising=False)
    get_settings.cache_clear()
    reset_engine()

    outcome: list[object] = []

    def stranded() -> None:
        try:
            get_session_factory()
            outcome.append("WROTE")
        except BaseException as caught:
            outcome.append(type(caught).__name__)

    thread = threading.Thread(target=stranded, name="session-stranded", daemon=True)
    thread.start()
    thread.join(timeout=10)

    assert outcome == ["RealDatabaseAccess"], (
        f"a stranded background thread got {outcome} instead of being refused"
    )
