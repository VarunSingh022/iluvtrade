"""Submitting, querying and cancelling backtest jobs."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from iluvtrade.alphalab_bridge import runconfig
from iluvtrade.backtests.requests import BacktestRequest
from iluvtrade.common import storage
from iluvtrade.common.observability import correlation_id
from iluvtrade.data import canonical, ingest
from iluvtrade.db.base import new_id, utcnow
from iluvtrade.db.models.backtest import BacktestJob, BacktestRun, JobStatus
from iluvtrade.db.models.platform import Role
from iluvtrade.db.models.strategy import StrategyVersion
from iluvtrade.platform import audit, notifications
from iluvtrade.platform.accounts import Principal
from iluvtrade.platform.tenancy import require_owned, scoped
from iluvtrade.strategies import service as strategy_service

__all__ = ["BacktestSubmissionError", "cancel", "get_job", "list_jobs", "result_document", "submit"]


class BacktestSubmissionError(ValueError):
    """The backtest could not be accepted as asked."""


def submit(
    session: DbSession,
    principal: Principal,
    request: BacktestRequest,
    *,
    idempotency_key: str | None = None,
) -> tuple[BacktestJob, bool]:
    """Queue a backtest. Returns ``(job, created)``.

    Idempotent in two layers. The first is a lookup on the key, which catches
    the ordinary double-submit. The second is the unique constraint plus the
    ``IntegrityError`` below, which catches two concurrent requests that both
    passed the lookup — the case a check-then-insert always loses.
    """

    principal.require(Role.TRADER)
    key = (idempotency_key or request.idempotency_key())[:80]

    existing = session.execute(
        scoped(BacktestJob, principal.organization_id).where(BacktestJob.idempotency_key == key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    # Validate everything the job depends on *before* queueing it, so a job that
    # cannot possibly run is refused at the API rather than failing in a worker
    # where the user has to go looking for the reason.
    dataset_version = ingest.require_approved(
        session, principal.organization_id, request.dataset_version_id
    )
    strategy_version = strategy_service.resolve_runnable(
        session, principal.organization_id, request.strategy_version_id
    )
    try:
        Decimal(request.starting_cash)
        Decimal(request.commission_rate)
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise BacktestSubmissionError(
            "starting_cash and commission_rate must be decimals."
        ) from exc
    if Decimal(request.starting_cash) <= 0:
        raise BacktestSubmissionError("starting_cash must be positive.")
    try:
        runconfig.trading_day_zone(request.exchange)
        runconfig.fill_timing_for(request.fill_timing)
    except (runconfig.UnsupportedExchangeError, runconfig.UnsupportedFillTimingError) as exc:
        raise BacktestSubmissionError(str(exc)) from exc

    job = BacktestJob(
        organization_id=principal.organization_id,
        submitted_by_user_id=principal.user_id,
        idempotency_key=key,
        status=JobStatus.QUEUED,
        # Stored with the effective seed resolved: the idempotency key above is
        # the request as asked, and every attempt reads this seed back.
        request_json=request.resolved().to_json(),
        dataset_version_id=dataset_version.id,
        strategy_version_id=strategy_version.id,
        correlation_id=correlation_id(),
        queued_at=utcnow(),
    )
    session.add(job)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raced = session.execute(
            scoped(BacktestJob, principal.organization_id).where(BacktestJob.idempotency_key == key)
        ).scalar_one_or_none()
        if raced is None:
            raise
        return raced, False

    audit.record(
        session,
        organization_id=principal.organization_id,
        action="backtest.submitted",
        resource_type="backtest_job",
        resource_id=job.id,
        actor_user_id=principal.user_id,
        payload={
            "dataset_version_id": dataset_version.id,
            "strategy_version_id": strategy_version.id,
            "idempotency_key": key,
        },
    )
    return job, True


def get_job(session: DbSession, principal: Principal, job_id: str) -> BacktestJob:
    return require_owned(session, BacktestJob, job_id, principal.organization_id)


def list_jobs(
    session: DbSession, principal: Principal, *, limit: int = 50, status: JobStatus | None = None
) -> list[BacktestJob]:
    query = scoped(BacktestJob, principal.organization_id).order_by(BacktestJob.queued_at.desc())
    if status is not None:
        query = query.where(BacktestJob.status == status)
    return list(session.execute(query.limit(min(limit, 200))).scalars())


def cancel(session: DbSession, principal: Principal, job_id: str) -> BacktestJob:
    """Request cancellation.

    A ``QUEUED`` job is cancelled outright. A ``RUNNING`` one is *asked* to stop
    and the worker honours it between records — so this returns a job that is
    still running, which is the truth rather than a comforting lie.
    """

    principal.require(Role.TRADER)
    job = require_owned(session, BacktestJob, job_id, principal.organization_id)
    if job.status.is_terminal:
        raise BacktestSubmissionError(f"A {job.status.value} job cannot be cancelled.")

    job.cancel_requested_at = utcnow()
    if job.status is JobStatus.QUEUED:
        job.status = JobStatus.CANCELLED
        job.finished_at = utcnow()
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="backtest.cancelled",
        resource_type="backtest_job",
        resource_id=job.id,
        actor_user_id=principal.user_id,
        payload={"status": job.status.value},
    )
    return job


def get_run(session: DbSession, principal: Principal, job_id: str) -> BacktestRun | None:
    job = get_job(session, principal, job_id)
    return session.execute(
        scoped(BacktestRun, principal.organization_id).where(BacktestRun.job_id == job.id)
    ).scalar_one_or_none()


def result_document(session: DbSession, principal: Principal, job_id: str) -> dict[str, Any]:
    """The full stored result for a completed job."""

    run = get_run(session, principal, job_id)
    if run is None or not run.artifact_path:
        raise BacktestSubmissionError("This job has no stored result.")
    document, integrity = _verified_document(run)
    return {
        "run_id": run.id,
        "reproducibility": {
            "dataset_version_id": run.dataset_version_id,
            "dataset_canonical_hash": run.dataset_canonical_hash,
            "strategy_version_id": run.strategy_version_id,
            "strategy_content_hash": run.strategy_content_hash,
            "seed": run.seed,
            "engine": f"{run.engine_name} {run.engine_version}",
            "parameters": json.loads(run.parameters_json),
            "universe": json.loads(run.universe_json),
            "configuration": json.loads(run.configuration_json),
            # What the dataset's bar interval rested on when this run executed,
            # from the run's own snapshot; "not_recorded" for older runs.
            "temporal_evidence": ingest.evidence_snapshot(run.temporal_evidence_json),
            # AlphaLab's own result_id / configuration_id. Absent from runs
            # stored before the engine produced them, rather than invented.
            "engine_identity": (document or {}).get("engine_identity"),
        },
        "metrics": json.loads(run.metrics_json),
        # ``None`` unless the stored document is proven to be this run's; the
        # headline metrics above come from the run's own row either way.
        "result": document,
        "result_integrity": integrity,
    }


def _verified_document(run: BacktestRun) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The stored result document, only if it is provably this run's.

    Three checks: the artifact sits at this run's own key, its bytes hash to the
    digest recorded when it was written, and the document names this run. A run
    stored before 0007 fails the first two -- its key was
    ``backtests/None/result.json``, shared by every run in its organization, so
    its document may be another run's -- and is answered with its headline
    metrics and the reason, never with a document that merely exists.
    """

    expected_key = canonical.artifact_key(run.organization_id, run.id)
    if run.artifact_path != expected_key or run.artifact_sha256 is None:
        return None, {
            "verified": False,
            "reason": (
                "Stored before results were keyed by run: this run's detailed result was "
                "written to a location every run in its organization shared, so it may hold "
                "another run's fills and report. Only the headline metrics, recorded on the "
                "run itself, are shown."
            ),
        }
    try:
        payload = storage.read_bytes(run.artifact_path)
    except storage.StorageError:
        return None, {"verified": False, "reason": "The detailed result is missing from storage."}
    if hashlib.sha256(payload).hexdigest() != run.artifact_sha256:
        return None, {
            "verified": False,
            "reason": "The detailed result's bytes do not match the digest recorded when it was "
            "written; it is not shown.",
        }
    document = json.loads(payload.decode("utf-8"))
    if document.get("run_id") != run.id:
        return None, {"verified": False, "reason": "The detailed result names a different run."}
    return document, {"verified": True, "artifact_sha256": run.artifact_sha256}


def record_success(
    session: DbSession,
    job: BacktestJob,
    *,
    executed: Any,
    dataset_canonical_hash: str | None,
    strategy_content_hash: str | None,
    starting_cash: str,
    temporal_evidence: dict[str, Any],
) -> BacktestRun:
    """Persist a finished run. Called by the worker, inside its own transaction.

    ``temporal_evidence`` is ``ingest.temporal_evidence`` of the exact dataset
    version the run read; it must name that version and its canonical hash.
    """

    if (
        temporal_evidence.get("dataset_version_id") != job.dataset_version_id
        or temporal_evidence.get("dataset_canonical_hash") != dataset_canonical_hash
    ):
        raise ValueError("The temporal evidence does not describe the dataset this run read.")

    request = BacktestRequest.from_json(job.request_json)
    headline = executed.headline
    valuation = executed.document["valuation"]

    run = BacktestRun(
        # Assigned here, not left to the column default: the default only runs
        # at flush, so the artifact key below was built from ``None`` and every
        # backtest in an organization wrote ``backtests/None/result.json`` --
        # each run overwrote the last, and an older run's page showed the
        # newest run's fills and report beside its own configuration.
        id=new_id(),
        organization_id=job.organization_id,
        job_id=job.id,
        dataset_version_id=job.dataset_version_id,
        dataset_canonical_hash=dataset_canonical_hash,
        strategy_version_id=job.strategy_version_id,
        strategy_content_hash=strategy_content_hash,
        parameters_json=json.dumps(
            executed.document["run_configuration"]["parameters"], sort_keys=True, default=str
        ),
        universe_json=json.dumps(executed.document["run_configuration"]["universe"]),
        configuration_json=json.dumps(executed.document["run_configuration"], default=str),
        temporal_evidence_json=json.dumps(temporal_evidence, sort_keys=True),
        seed=executed.seed,
        engine_name="alphalab",
        engine_version=executed.engine_version,
        records_processed=executed.records_processed,
        order_count=int(headline["order_count"]),
        fill_count=int(headline["fill_count"]),
        starting_cash=starting_cash,
        ending_equity=str(valuation["equity"]),
        realized_pnl=str(valuation["realized_pnl"]),
        unrealized_pnl=str(valuation["unrealized_pnl"]),
        commission_paid=str(valuation["commission_paid"]),
        total_return=headline["total_return"],
        cagr=headline["cagr"],
        volatility=headline["volatility"],
        sharpe_ratio=headline["sharpe_ratio"],
        max_drawdown=headline["max_drawdown"],
        metrics_json=json.dumps(headline, default=str),
    )
    key = canonical.artifact_key(job.organization_id, run.id)
    # The document says whose it is; the row records its digest. Retrieval
    # checks both before serving it as this run's (``result_document``).
    document = {
        **executed.document,
        "run_id": run.id,
        "job_id": job.id,
        "organization_id": job.organization_id,
        # Inside the digested document too, so the stored result carries the
        # limitation even when read on its own.
        "temporal_evidence": temporal_evidence,
    }
    run.artifact_sha256 = storage.write_json(key, document)
    run.artifact_path = key
    session.add(run)

    job.status = JobStatus.COMPLETED
    job.finished_at = utcnow()
    job.progress = 1.0
    audit.record(
        session,
        organization_id=job.organization_id,
        action="backtest.completed",
        resource_type="backtest_run",
        resource_id=run.id,
        actor_user_id=job.submitted_by_user_id,
        payload={
            "job_id": job.id,
            "records": executed.records_processed,
            "orders": run.order_count,
            "seed": run.seed,
        },
    )
    notifications.notify(
        session,
        organization_id=job.organization_id,
        user_id=job.submitted_by_user_id,
        kind="backtest.completed",
        title="Backtest completed",
        body=(
            f"{run.order_count} order(s) over {executed.records_processed} records. "
            f"Ending equity {run.ending_equity}."
        ),
        resource_type="backtest_job",
        resource_id=job.id,
    )
    _ = request
    return run


def record_failure(session: DbSession, job: BacktestJob, *, code: str, message: str) -> None:
    """Persist a failure. The reason is structured, never a generic 'failed'."""

    job.status = JobStatus.FAILED
    job.finished_at = utcnow()
    job.error_code = code[:64]
    job.error_message = message[:4000]
    audit.record(
        session,
        organization_id=job.organization_id,
        action="backtest.failed",
        resource_type="backtest_job",
        resource_id=job.id,
        actor_user_id=job.submitted_by_user_id,
        outcome="failure",
        payload={"code": code, "message": message[:500]},
    )
    notifications.notify(
        session,
        organization_id=job.organization_id,
        user_id=job.submitted_by_user_id,
        kind="backtest.failed",
        severity="error",
        title="Backtest failed",
        body=f"{code}: {message[:300]}",
        resource_type="backtest_job",
        resource_id=job.id,
    )


def strategy_version_of(session: DbSession, job: BacktestJob) -> StrategyVersion:
    version = session.get(StrategyVersion, job.strategy_version_id)
    if version is None:
        raise BacktestSubmissionError("The strategy version this job referenced no longer exists.")
    return version


def claim_next(session: DbSession) -> BacktestJob | None:
    """Take the oldest queued job and mark it running.

    Cross-tenant on purpose: this is the *worker's* view, not a user's, and a
    worker that could only see one organization would need one worker per tenant.
    Every tenant-scoped read the job then performs still goes through the normal
    helpers with the job's own ``organization_id``.
    """

    job = session.execute(
        select(BacktestJob)
        .where(BacktestJob.status == JobStatus.QUEUED)
        .order_by(BacktestJob.queued_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        if session.bind is not None and session.bind.dialect.name != "sqlite"
        else select(BacktestJob)
        .where(BacktestJob.status == JobStatus.QUEUED)
        .order_by(BacktestJob.queued_at)
        .limit(1)
    ).scalar_one_or_none()
    if job is None:
        return None
    if job.cancel_requested_at is not None:
        job.status = JobStatus.CANCELLED
        job.finished_at = utcnow()
        return None
    job.status = JobStatus.RUNNING
    job.started_at = utcnow()
    job.attempts += 1
    return job
