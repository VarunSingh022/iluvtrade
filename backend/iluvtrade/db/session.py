"""Engine and session construction."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from iluvtrade.config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None

#: How long a statement waits for a lock before giving up, in milliseconds. The
#: same five seconds as SQLite's ``busy_timeout`` below, so contention answers
#: alike on both backends.
_LOCK_TIMEOUT_MS = 5000


def _configure_sqlite(engine: Engine) -> None:
    """Turn on the SQLite behaviours the schema assumes.

    Foreign keys are **off** by default in SQLite. The schema uses
    ``ON DELETE CASCADE`` to make a deleted organization take its data with it,
    which silently does nothing without this pragma — a tenant-isolation hole
    that looks like working code.
    """

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def get_engine() -> Engine:
    """The process-wide engine."""

    global _engine
    if _engine is None:
        settings = get_settings()
        url = settings.database_url
        if url.startswith("sqlite"):
            path = url.split("///", 1)[-1]
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            _engine = create_engine(url, future=True, connect_args={"check_same_thread": False})
            _configure_sqlite(_engine)
        elif url.startswith("postgresql"):
            # A lock wait is bounded, as SQLite's busy_timeout bounds it there.
            # PostgreSQL's default is to wait forever, so a writer blocked behind
            # a transaction that cannot finish hangs its request — and, before
            # the upload route stopped running database work on the event loop,
            # froze the whole worker. The timeout surfaces as the same retryable
            # 503 ``DatabaseBusy`` (see ``api/errors.py``).
            _engine = create_engine(
                url,
                future=True,
                pool_pre_ping=True,
                connect_args={"options": f"-c lock_timeout={_LOCK_TIMEOUT_MS}"},
            )
        else:
            _engine = create_engine(url, future=True, pool_pre_ping=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """A transactional scope. Commits on success, rolls back on any exception."""

    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Drop the cached engine. Tests use this when they repoint the database."""

    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def create_all() -> None:
    """Create every table. Development and tests; migrations own production."""

    from iluvtrade.db import models

    models.Base.metadata.create_all(get_engine())
