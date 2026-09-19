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


@pytest.fixture(autouse=True)
def _isolated_storage(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point the database and blob storage at a fresh temporary directory."""

    from iluvtrade.config import get_settings
    from iluvtrade.db.session import reset_engine

    workspace = tempfile.mkdtemp(prefix="iluvtrade-test-")
    monkeypatch.setenv("ILUVTRADE_DATABASE_URL", f"sqlite:///{workspace}/test.db")
    monkeypatch.setenv("ILUVTRADE_STORAGE_ROOT", f"{workspace}/storage")
    get_settings.cache_clear()
    reset_engine()
    yield workspace
    get_settings.cache_clear()
    reset_engine()


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
