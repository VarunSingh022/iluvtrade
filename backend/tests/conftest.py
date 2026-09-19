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

os.environ.setdefault("ILUVTRADE_SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("ILUVTRADE_ENVIRONMENT", "test")


@pytest.fixture(scope="session", autouse=True)
def _the_developers_database_is_never_touched() -> Iterator[None]:
    """Fail the run if the suite writes to the real local database.

    Every test gets a temporary database, so this should be impossible — and it
    happened anyway. A trading-session runner is a daemon thread that outlives
    the request that started it. When a test finished and the fixture below
    cleared the settings cache and reset the engine, a still-polling runner
    called ``get_session_factory()`` again, re-read settings *without* the
    monkeypatched URL, and wrote into ``var/iluvtrade.db`` — the developer's own
    data.

    The fix is joining runners before the reset (see below). This is the guard
    that proves the fix holds, because the failure is silent: nothing in a
    passing test run would ever mention it.
    """

    from iluvtrade.config import REPO_ROOT

    default = REPO_ROOT / "var" / "iluvtrade.db"
    before = default.stat().st_mtime_ns if default.exists() else None
    yield
    after = default.stat().st_mtime_ns if default.exists() else None
    assert before == after, (
        f"The test suite wrote to {default}, which is the developer's real "
        "database. Something escaped the per-test temporary database — most "
        "likely a background thread that outlived its test and re-resolved "
        "settings after the fixture reset them."
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

    # Session runners are daemon threads, so nothing waits for them. Repointing
    # the database while one is still polling makes it raise "no such table"
    # from a background thread — which pytest reports against whichever test is
    # running *next*, turning a leak in one test into a flake in another.
    from iluvtrade.trading.runner import RUNNER

    RUNNER.join_all(timeout=15)

    get_settings.cache_clear()
    reset_engine()
    reset_limiter()


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
