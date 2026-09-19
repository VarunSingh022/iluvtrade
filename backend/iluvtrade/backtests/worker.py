"""The backtest worker: a background thread pool that drains the job queue.

Why a thread pool and not Celery
--------------------------------

PHASE 7's requirement is that *backtests must not block web request threads*,
and that jobs have explicit states, ownership, cancellation and structured
failures. An in-process pool satisfies all of it for a single-node deployment
and has no broker to operate. What it does not survive is a process restart —
a ``RUNNING`` job whose worker died stays ``RUNNING`` forever — so
:func:`requeue_orphans` runs at startup and puts them back.

The seam for a distributed queue is :func:`iluvtrade.backtests.service.claim_next`,
which already takes the row lock a multi-process worker needs (and degrades to a
plain select on SQLite, which has no ``SELECT … FOR UPDATE``). Swapping the pool
for external workers is that function plus a different ``run_forever``;
``docs/DEPLOYMENT.md`` says so explicitly rather than implying this scales further
than it does.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from iluvtrade.backtests import service
from iluvtrade.backtests.requests import BacktestRequest
from iluvtrade.backtests.runner import execute
from iluvtrade.common import storage
from iluvtrade.common.observability import correlated, set_context
from iluvtrade.config import get_settings
from iluvtrade.db.base import utcnow
from iluvtrade.db.models.backtest import BacktestJob, JobStatus
from iluvtrade.db.models.data import DatasetVersion
from iluvtrade.db.session import session_scope
from iluvtrade.strategies import service as strategy_service

logger = logging.getLogger("iluvtrade.backtests.worker")

__all__ = ["BacktestWorkerPool", "process_one", "requeue_orphans"]


def _load_rows(version: DatasetVersion) -> list[dict[str, Any]]:
    if not version.canonical_path:
        raise ValueError("The dataset version has no canonical data.")
    return list(storage.read_jsonl(version.canonical_path))


def process_one() -> bool:
    """Claim and run one job. Returns whether anything was claimed.

    Claiming and executing are in **separate transactions** on purpose. A
    backtest can run for minutes; holding a write transaction open for its
    duration would block every other writer on SQLite and hold a connection
    needlessly on Postgres. The cost is that a crash mid-run leaves the job
    ``RUNNING``, which :func:`requeue_orphans` is for.
    """

    with session_scope() as session:
        job = service.claim_next(session)
        if job is None:
            return False
        job_id = job.id
        # The id the submitting request carried, so a job's log lines join to
        # the HTTP call that queued it. A ContextVar does not cross a thread,
        # so this adoption is deliberate.
        correlation = job.correlation_id
        organization_id = job.organization_id

    with correlated(correlation, job_id=job_id, organization_id=organization_id):
        return _execute_claimed(job_id)


def _execute_claimed(job_id: str) -> bool:
    """Run a job already claimed by this worker, under its correlation scope."""

    try:
        with session_scope() as session:
            job = session.get(BacktestJob, job_id)
            if job is None:
                return True
            request = BacktestRequest.from_json(job.request_json)
            version = session.get(DatasetVersion, job.dataset_version_id)
            strategy_version = service.strategy_version_of(session, job)
            if version is None:
                raise ValueError("The dataset version this job referenced no longer exists.")
            if not strategy_service.verify_integrity(strategy_version):
                raise ValueError(
                    "The strategy version's content hash no longer matches the one recorded "
                    "at publication; refusing to run it."
                )
            rows = _load_rows(version)
            dataset_hash = version.canonical_hash
            strategy_hash = strategy_version.content_hash
            implementation_key = strategy_version.implementation_key
            version_defaults = json.loads(strategy_version.default_parameters_json or "{}")
            frequency = version.inferred_frequency
            account_name = f"{job.organization_id[:8]} backtest"
            cancelled = job.cancel_requested_at is not None

        if cancelled:
            with session_scope() as session:
                job = session.get(BacktestJob, job_id)
                if job is not None and not job.status.is_terminal:
                    job.status = JobStatus.CANCELLED
                    job.finished_at = utcnow()
            return True

        set_context(
            dataset_version_id=request.dataset_version_id,
            strategy_version_id=request.strategy_version_id,
        )
        logger.info("Executing backtest over %d records", len(rows))
        executed = execute(
            request,
            rows=rows,
            implementation_key=implementation_key,
            version_defaults=version_defaults,
            dataset_identity=f"dsv-{request.dataset_version_id}",
            frequency=frequency,
            account_name=account_name,
        )

        with session_scope() as session:
            job = session.get(BacktestJob, job_id)
            if job is None:
                return True
            if job.cancel_requested_at is not None:
                # Cancelled while the engine was running. The result is discarded
                # rather than stored: the user asked for it not to happen, and a
                # run they cancelled appearing in their history is worse than the
                # wasted CPU.
                job.status = JobStatus.CANCELLED
                job.finished_at = utcnow()
                return True
            service.record_success(
                session,
                job,
                executed=executed,
                dataset_canonical_hash=dataset_hash,
                strategy_content_hash=strategy_hash,
                starting_cash=request.starting_cash,
            )
    except Exception as exc:
        logger.exception("Backtest job %s failed", job_id)
        with session_scope() as session:
            job = session.get(BacktestJob, job_id)
            if job is not None and not job.status.is_terminal:
                service.record_failure(session, job, code=type(exc).__name__, message=str(exc))
    return True


def requeue_orphans() -> int:
    """Return jobs left ``RUNNING`` by a dead worker to the queue.

    Bounded by ``backtest_max_attempts``: a job that reliably kills its worker
    would otherwise be requeued forever.
    """

    settings = get_settings()
    requeued = 0
    with session_scope() as session:
        from sqlalchemy import select

        jobs = session.execute(
            select(BacktestJob).where(BacktestJob.status == JobStatus.RUNNING)
        ).scalars()
        for job in jobs:
            if job.attempts >= settings.backtest_max_attempts:
                service.record_failure(
                    session,
                    job,
                    code="WorkerLost",
                    message=(
                        f"The worker running this job stopped before it finished, and it has "
                        f"already been attempted {job.attempts} time(s)."
                    ),
                )
            else:
                job.status = JobStatus.QUEUED
                job.started_at = None
                requeued += 1
    return requeued


class BacktestWorkerPool:
    """A small pool of daemon threads draining the queue."""

    def __init__(self, size: int | None = None, poll_seconds: float = 0.25) -> None:
        self._size = size if size is not None else get_settings().backtest_worker_count
        self._poll = poll_seconds
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._threads:
            return
        requeue_orphans()
        for index in range(self._size):
            thread = threading.Thread(
                target=self._loop, name=f"backtest-worker-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)
        logger.info("Started %d backtest worker(s)", self._size)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not process_one():
                    self._stop.wait(self._poll)
            except Exception:
                logger.exception("Backtest worker loop error")
                self._stop.wait(self._poll)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self._threads.clear()

    def drain(self, timeout: float = 60.0) -> None:
        """Run queued jobs to completion inline. For tests and the CLI demo."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not process_one():
                return
