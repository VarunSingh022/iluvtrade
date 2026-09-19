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
