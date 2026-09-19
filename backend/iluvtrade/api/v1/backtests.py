"""Backtest submission, status and results."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session as DbSession

from iluvtrade.api.deps import current_principal, db_session, rate_limit, require_trader
from iluvtrade.api.v1.schemas import (
    BacktestJobResponse,
    BacktestResultResponse,
    SubmitBacktestRequest,
)
from iluvtrade.backtests import service
from iluvtrade.backtests.requests import BacktestRequest
from iluvtrade.db.models.backtest import BacktestJob, JobStatus
from iluvtrade.platform.accounts import Principal

router = APIRouter(prefix="/backtests", tags=["backtests"])


def _job(job: BacktestJob, *, deduplicated: bool = False) -> BacktestJobResponse:
    return BacktestJobResponse(
        id=job.id,
        status=job.status.value,
        dataset_version_id=job.dataset_version_id,
        strategy_version_id=job.strategy_version_id,
        queued_at=job.queued_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        attempts=job.attempts,
        progress=job.progress,
        error_code=job.error_code,
        error_message=job.error_message,
        deduplicated=deduplicated,
    )


@router.post(
    "",
    response_model=BacktestJobResponse,
    status_code=202,
    dependencies=[Depends(rate_limit("backtest"))],
)
def submit(
    payload: SubmitBacktestRequest,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> BacktestJobResponse:
    """Queue a backtest.

    ``202 Accepted``, not ``201``: the run has not happened yet, and a client
    that treats this as "done" would read an empty result. Resubmitting an
    identical request returns the original job with ``deduplicated: true``.
    """

    request = BacktestRequest(
        dataset_version_id=payload.dataset_version_id,
        strategy_version_id=payload.strategy_version_id,
        parameters=payload.parameters,
        universe=tuple(payload.universe),
        starting_cash=payload.starting_cash,
        currency=payload.currency,
        exchange=payload.exchange,
        risk_profile=payload.risk_profile,
        commission_kind=payload.commission_kind,
        commission_rate=payload.commission_rate,
        risk_free_rate=payload.risk_free_rate,
        seed=payload.seed,
    )
    job, created = service.submit(
        session, principal, request, idempotency_key=payload.idempotency_key
    )
    return _job(job, deduplicated=not created)


@router.get("", response_model=list[BacktestJobResponse])
def list_jobs(
    status: str | None = None,
    limit: int = 50,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> list[BacktestJobResponse]:
    parsed = JobStatus(status) if status else None
    return [_job(job) for job in service.list_jobs(session, principal, limit=limit, status=parsed)]


@router.get("/{job_id}", response_model=BacktestJobResponse)
def get_job(
    job_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> BacktestJobResponse:
    return _job(service.get_job(session, principal, job_id))


@router.post("/{job_id}/cancel", response_model=BacktestJobResponse)
def cancel(
    job_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> BacktestJobResponse:
    """Request cancellation.

    A running job is *asked* to stop and is honoured between records, so the
    response may still say ``running``. That is the truth, not a failure.
    """

    return _job(service.cancel(session, principal, job_id))


@router.get("/{job_id}/result", response_model=BacktestResultResponse)
def result(
    job_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> BacktestResultResponse:
    """The full stored result, with the record that makes it reproducible."""

    document = service.result_document(session, principal, job_id)
    return BacktestResultResponse(**document)
