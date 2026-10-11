"""Snapshot each run's and paper session's temporal evidence.

A dataset version records what its bar times rest on (``temporal_json``): a
declared interval its instruments' timestamps are consistent with, or one that
the importer *declared* for instruments whose timestamps cannot show it. A
backtest result, a paper session and a RedDesk listing citing a run must carry
that limitation with them, not drop it. These columns hold a snapshot of
``iluvtrade.data.ingest.temporal_evidence`` taken from the exact dataset
version, when the run executes and when the session is created.

Nullable, with no backfill, deliberately: a run or session stored before this
revision did not record its evidence, and reconstructing it from today's rules
would invent a record. It stays ``NULL`` and is shown as "not recorded"; the
paper runner refuses to start such a session. A plain nullable add cannot
half-apply on SQLite.

Downgrading is refused while any row holds a snapshot: dropping it and upgrading
again would leave ``NULL`` ("not recorded") where evidence was recorded.

Revision ID: 0008_temporal_evidence
Revises: 0007_artifact_digest
Create Date: 2026-10-12 09:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_temporal_evidence"
down_revision: str | None = "0007_artifact_digest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("backtest_runs", "trading_sessions")


def upgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("temporal_evidence_json", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    recorded = {
        table: bind.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE temporal_evidence_json IS NOT NULL")
        ).scalar_one()
        for table in _TABLES
    }
    if any(recorded.values()):
        raise RuntimeError(
            "Refusing to downgrade below 0008: "
            + ", ".join(f"{count} row(s) in {table}" for table, count in recorded.items())
            + " record temporal evidence that this downgrade would drop and a later upgrade "
            "could not restore. Restore a backup taken before upgrading instead."
        )
    for table in reversed(_TABLES):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("temporal_evidence_json")
