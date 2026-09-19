"""Shared fixtures.

Every test gets its own temporary database and storage root, so nothing leaks
between tests and none of them touch a developer's real data.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import os
import random
import tempfile
from collections.abc import Iterator
from typing import Any

import pytest

from tests.support import (
    RealDatabaseAccess,
    live_application_threads,
    real_database_path,
)

os.environ.setdefault("ILUVTRADE_SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("ILUVTRADE_ENVIRONMENT", "test")


#: Set by the ``allows_real_database`` fixture, and by nothing else.
_REAL_DATABASE_ALLOWED = False


@pytest.fixture
def allows_real_database() -> Iterator[None]:
    """Opt out of the guard below, for a test that genuinely needs the real file.

    Nothing in the suite uses this today. It exists so that the guard is a
    *policy with an exception*, rather than something the next person disables
    wholesale the first time they need to read the developer's database — which
    is how a control like this actually dies.
    """

    global _REAL_DATABASE_ALLOWED
    _REAL_DATABASE_ALLOWED = True
    try:
        yield
    finally:
        _REAL_DATABASE_ALLOWED = False


@pytest.fixture(scope="session", autouse=True)
def _no_test_may_open_the_real_database() -> Iterator[None]:
    """**Prevention.** Refuse to build an engine against the developer's database.

    This is the control that matters, and it replaces a detective one. The
    earlier version compared the file's mtime at the start and end of the run,
    which reported the damage *after* it was done. This raises at the moment an
    engine is constructed for that path — before a connection is opened, before
    a statement runs, before a row exists.

    Why it is needed at all, given every test gets a temporary database: a
    trading-session runner is a **daemon thread** that outlives the request that
    started it. When a test finished and the per-test fixture cleared the
    settings cache and reset the engine, a still-polling runner called
    ``get_session_factory()`` again, re-resolved settings *without* the
    monkeypatched URL, and got the default — the developer's own file. It wrote
    33 rows into it on 2026-09-19.

    Two mechanisms now stand in the way and both are kept, because they fail
    differently. The runner lifecycle fix (below) stops the thread from
    outliving its test at all. This one catches anything else that reaches for
    the real path, whatever the route.

    Patched at ``create_engine`` rather than at ``get_engine`` because every
    engine in the process — including one built by a thread that bypassed the
    cache — goes through it.
    """

    import iluvtrade.db.session as db_session

    real = str(real_database_path().resolve())
    original = db_session.create_engine

    def guarded(url: str, *args: Any, **kwargs: Any) -> Any:
        if not _REAL_DATABASE_ALLOWED and url.startswith("sqlite"):
            path = url.split("///", 1)[-1]
            if path and path != ":memory:":
                from pathlib import Path

                if str(Path(path).resolve()) == real:
                    raise RealDatabaseAccess(
                        f"A test tried to open {real}, which is the developer's own "
                        "application database. Every test gets a temporary one; "
                        "something re-resolved settings outside its fixture — most "
                        "likely a background thread that outlived it. If a test "
                        "genuinely needs the real file, request the "
                        "'allows_real_database' fixture and say why."
                    )
        return original(url, *args, **kwargs)

    db_session.create_engine = guarded  # type: ignore[assignment]
    try:
        yield
    finally:
        db_session.create_engine = original  # type: ignore[assignment]


@pytest.fixture(scope="session", autouse=True)
def _the_real_database_is_byte_for_byte_unchanged() -> Iterator[None]:
    """**Detection**, kept alongside the prevention above.

    A guard that only refuses can be defeated by a path it does not recognise —
    a relative URL, a symlink, a subprocess. This hashes the file's contents
    before and after the whole run, so a write that got past the refusal is
    still caught. Contents rather than mtime: a touch is harmless, a changed
    byte is not.
    """

    import hashlib

    def digest() -> str | None:
        path = real_database_path()
        if not path.exists():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()

    before = digest()
    yield
    after = digest()
    assert before == after, (
        f"The contents of {real_database_path()} changed during the test run. "
        "Something wrote to the developer's real database and got past the "
        "engine guard."
    )


@pytest.fixture(autouse=True)
def _isolated_storage(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point the database and blob storage at a fresh temporary directory.

    Rate limiting is **off** by default here. Otherwise a test that registers
    six accounts would trip the registration policy and fail for a reason
    unrelated to what it is testing. ``tests/security/test_rate_limiting.py``
    turns it back on explicitly, which is also the only place its behaviour is
    asserted — so the control is tested, and nothing else is throttled by it.
    """

    from iluvtrade.config import get_settings
    from iluvtrade.db.session import reset_engine
    from iluvtrade.platform.ratelimit import reset_limiter

    workspace = tempfile.mkdtemp(prefix="iluvtrade-test-")
    monkeypatch.setenv("ILUVTRADE_DATABASE_URL", f"sqlite:///{workspace}/test.db")
    monkeypatch.setenv("ILUVTRADE_STORAGE_ROOT", f"{workspace}/storage")
    monkeypatch.setenv("ILUVTRADE_RATE_LIMIT_ENABLED", "false")
    get_settings.cache_clear()
    reset_engine()
    reset_limiter()
    yield workspace

    # Every application-owned background thread is stopped and joined *before*
    # the settings cache is cleared. Order matters: a runner still polling when
    # the cache is cleared re-resolves the database URL without the monkeypatch
    # above and gets the developer's real file. That is precisely what happened.
    leaked = _stop_background_threads()

    get_settings.cache_clear()
    reset_engine()
    reset_limiter()

    assert not leaked, (
        f"These application threads outlived their test: {leaked}. A daemon "
        "thread that survives its fixture re-resolves settings the fixture has "
        "already torn down — which is how the suite once wrote into the "
        "developer's own database. Stop and join it where it is created."
    )


def _stop_background_threads(timeout: float = 20.0) -> list[str]:
    """Stop and join everything this application started. Returns what leaked."""

    from iluvtrade.backtests.worker import stop_all_pools
    from iluvtrade.trading.runner import RUNNER

    RUNNER.join_all(timeout=timeout)
    stop_all_pools(timeout=timeout)
    return live_application_threads()


@pytest.fixture
def db() -> Iterator[Any]:
    """A session against a freshly created schema."""

    from iluvtrade.db.session import create_all, session_scope

    create_all()
    with session_scope() as session:
        yield session


@pytest.fixture
def app() -> Iterator[Any]:
    from iluvtrade.main import create_app

    yield create_app(start_workers=False, create_tables=True)


@pytest.fixture
def client(app: Any) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def headers() -> dict[str, str]:
    """The header cookie-authenticated state-changing requests must carry."""

    return {"X-Requested-With": "XMLHttpRequest"}


def make_csv(
    *, symbols: tuple[str, ...] = ("ACME",), bars: int = 120, seed: int = 3, flaws: bool = False
) -> bytes:
    """A synthetic OHLCV CSV, optionally with known defects."""

    random.seed(seed)
    rows: list[list[Any]] = [["Date", "Symbol", "Open", "High", "Low", "Close", "Volume"]]
    for index, symbol in enumerate(symbols):
        price = 100.0 * (index + 1)
        day = dt.date(2024, 1, 1)
        for step in range(bars):
            price = max(5.0, price + (0.8 if step < bars * 0.6 else -0.6) + random.uniform(-2, 2))
            open_ = round(price - random.uniform(0, 1), 2)
            close = round(price, 2)
            high = round(max(open_, close) + random.uniform(0, 1), 2)
            low = round(min(open_, close) - random.uniform(0, 1), 2)
            rows.append(
                [day.isoformat(), symbol, open_, high, low, close, random.randint(1000, 90000)]
            )
            day += dt.timedelta(days=1)

    header, body = rows[0], sorted(rows[1:], key=lambda r: (r[0], r[1]))
    if flaws:
        body.insert(10, list(body[9]))
        body.insert(20, [body[19][0], symbols[0], "oops", 110, 90, 100, 5000])
        body.insert(30, [body[29][0], symbols[0], 100, 90, 110, 95, 5000])
        body.insert(40, [body[39][0], symbols[0], 100, 110, 90, 105, -7])
        body.insert(50, ["", symbols[0], 100, 110, 90, 105, 5000])
    buffer = io.StringIO()
    csv.writer(buffer).writerows([header, *body])
    return buffer.getvalue().encode("utf-8")


@pytest.fixture
def csv_bytes() -> bytes:
    return make_csv()


def register(client: Any, email: str, *, name: str = "Test User") -> dict[str, Any]:
    """Register an account and return the user payload; the client keeps the cookie."""

    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery-staple",
            "display_name": name,
        },
    )
    assert response.status_code == 201, response.text
    return dict(response.json()["user"])


def make_principal(session: Any, email: str = "t@example.com") -> Any:
    """Create an account directly and return its principal."""

    from iluvtrade.platform import accounts

    accounts.register(
        session,
        email=email,
        password="correct-horse-battery-staple",
        display_name="Tester",
    )
    _, token = accounts.login(session, email=email, password="correct-horse-battery-staple")
    return accounts.resolve_principal(session, token)
