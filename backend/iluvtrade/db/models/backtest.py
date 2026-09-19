"""Backtest job and run tables.

A **job** is the unit of work: it is queued, it may fail, it may be retried, and
it is addressed by an idempotency key so a double-submitted form produces one
backtest rather than two.

A **run** is the immutable evidence a successful job produced. It records every
input by identity — dataset version, strategy version, parameters, seed, engine
version — because PHASE 17 requires that a result can be re-derived rather than
merely believed. The heavy artifacts (equity curve, orders, fills, steps) are
files under the storage root; only the headline metrics are columns.

AlphaLab computes every number here. This table stores them; it does not derive
them.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from iluvtrade.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin, UtcDateTime


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}


class BacktestJob(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """One submitted backtest, and everything about how it went."""

    __tablename__ = "backtest_jobs"
    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", name="uq_backtest_idempotency"),
    )

    submitted_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    #: Supplied by the caller, or derived from the request body when absent, so
    #: resubmitting the identical request returns the original job.
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False), default=JobStatus.QUEUED, nullable=False, index=True
    )
    request_json: Mapped[str] = mapped_column(Text, nullable=False)

    dataset_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dataset_versions.id"), nullable=False
    )
    strategy_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("strategy_versions.id"), nullable=False
    )

    #: The correlation id of the request that queued this job, so a worker's
    #: log lines join to the HTTP call that submitted it. Nullable because a job
    #: queued outside a request (a future scheduler) has no originating trace.
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    queued_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    #: A machine-readable reason, so the UI can say something specific. Free-text
    #: goes in ``error_message``; the two are never conflated.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[BacktestRun | None] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False
    )


class BacktestRun(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """The immutable result of one completed job."""

    __tablename__ = "backtest_runs"

    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("backtest_jobs.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    # --- reproducibility identity (PHASE 17) ------------------------------
    dataset_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    dataset_canonical_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strategy_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    strategy_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parameters_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    universe_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    configuration_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    engine_name: Mapped[str] = mapped_column(String(64), nullable=False, default="alphalab")
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)

    # --- headline metrics, all produced by AlphaLab analytics -------------
    records_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    order_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fill_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    starting_cash: Mapped[str] = mapped_column(String(40), nullable=False)
    ending_equity: Mapped[str] = mapped_column(String(40), nullable=False)
    realized_pnl: Mapped[str] = mapped_column(String(40), nullable=False)
    unrealized_pnl: Mapped[str] = mapped_column(String(40), nullable=False)
    commission_paid: Mapped[str] = mapped_column(String(40), nullable=False)
    total_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    cagr: Mapped[float | None] = mapped_column(Float, nullable=True)
    volatility: Mapped[float | None] = mapped_column(Float, nullable=True)
    sharpe_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: Everything AlphaLab returned that is too large for a column: the equity
    #: curve, every order, every fill, the full analytics report.
    artifact_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: Metrics AlphaLab's report exposed that have no column here, verbatim.
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    job: Mapped[BacktestJob] = relationship(back_populates="run")
