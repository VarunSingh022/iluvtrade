"""Record each backtest artifact's digest, so a result is served only as its own run's.

Until 2026-10-11 every backtest artifact key was built from a not-yet-assigned
run id, so every run in an organization wrote ``backtests/None/result.json``
and an older run's page could show a newer run's fills and report. The key is
now the run's own; this column lets retrieval prove it: the bytes must hash to
the digest recorded when they were written, and the document must name the run.

Nullable, with no backfill, deliberately: a run stored before this revision has
no trustworthy digest to record -- its artifact may be another run's -- so it
stays ``NULL`` and its detailed result is never served as verified. Its headline
metrics live on the row itself and are unaffected. A plain nullable add cannot
half-apply on SQLite.

Revision ID: 0007_artifact_digest
Revises: 0006_temporal_and_fill_timing
Create Date: 2026-10-11 15:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_artifact_digest"
down_revision: str | None = "0006_temporal_and_fill_timing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("backtest_runs") as batch:
        batch.add_column(sa.Column("artifact_sha256", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("backtest_runs") as batch:
        batch.drop_column("artifact_sha256")
