"""Timestamp declarations, explicit fill timing, and 64-bit seeds.

Three changes, each found by the hardening pass after the AlphaLab 4.1
integration:

* ``dataset_versions.temporal_json`` records what a version's timestamps mean
  and how AlphaLab read them (``iluvtrade.data.temporal``). Existing versions
  are backfilled ``'{}'`` -- "imported before conventions were declared" --
  which the application refuses to run on, because their dates were read as UTC
  midnight. The column is not given a value they never had.
* ``trading_sessions.fill_timing`` records when a session's simulated orders
  fill. Existing sessions ran with AlphaLab's ``SAME_EVENT`` and are backfilled
  ``'same_event'`` -- what they actually did, not today's default. No server
  default is left behind: a new row must state it.
* ``backtest_runs.seed`` and ``trading_sessions.seed`` widen to BIGINT. A derived
  seed reaches 2**32 - 1, which PostgreSQL's 32-bit INTEGER refuses; on
  PostgreSQL roughly half of all backtests failed to save. SQLite's integers
  were already 64-bit, which is why nothing noticed.

Both added columns follow the form that cannot half-apply on SQLite (whose DDL
is not transactional): add nullable, backfill, then tighten.
``tests/integration/test_migrations.py`` steps through this revision with rows
present in every affected table.

Downgrading is refused while any session records a fill timing other than the
backfilled ``same_event`` or any dataset version records timestamp provenance:
dropping them and upgrading again would invent values, not restore them. It also
narrows the seeds back to INTEGER, which PostgreSQL refuses while a stored seed
exceeds 2**31 - 1 -- the alternative is silently corrupting the seed a run is
reproduced from.

Revision ID: 0006_temporal_and_fill_timing
Revises: 0005_teams_and_reset
Create Date: 2026-10-11 13:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_temporal_and_fill_timing"
down_revision: str | None = "0005_teams_and_reset"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- 1. add, nullable ---------------------------------------------------
    with op.batch_alter_table("dataset_versions") as batch:
        batch.add_column(sa.Column("temporal_json", sa.Text(), nullable=True))
    with op.batch_alter_table("trading_sessions") as batch:
        batch.add_column(sa.Column("fill_timing", sa.String(length=16), nullable=True))

    # --- 2. backfill with what existing rows actually mean --------------------
    op.execute(sa.text("UPDATE dataset_versions SET temporal_json = '{}' WHERE temporal_json IS NULL"))
    op.execute(
        sa.text("UPDATE trading_sessions SET fill_timing = 'same_event' WHERE fill_timing IS NULL")
    )

    # --- 3. tighten, and widen the seeds ------------------------------------
    with op.batch_alter_table("dataset_versions") as batch:
        batch.alter_column("temporal_json", existing_type=sa.Text(), nullable=False)
    with op.batch_alter_table("trading_sessions") as batch:
        batch.alter_column("fill_timing", existing_type=sa.String(length=16), nullable=False)
        batch.alter_column(
            "seed", existing_type=sa.Integer(), type_=sa.BigInteger(), existing_nullable=False
        )
    with op.batch_alter_table("backtest_runs") as batch:
        batch.alter_column(
            "seed", existing_type=sa.Integer(), type_=sa.BigInteger(), existing_nullable=False
        )


def downgrade() -> None:
    # Refused while any row holds information this revision introduced, because
    # dropping it and upgrading again would not restore it but invent it: every
    # session would be backfilled ``same_event`` (false for a ``next_event``
    # session) and every dataset version would lose its timestamp provenance.
    # Restore a backup taken before the upgrade instead.
    bind = op.get_bind()
    stated = bind.execute(
        sa.text("SELECT count(*) FROM trading_sessions WHERE fill_timing <> 'same_event'")
    ).scalar_one()
    declared = bind.execute(
        sa.text("SELECT count(*) FROM dataset_versions WHERE temporal_json <> '{}'")
    ).scalar_one()
    if stated or declared:
        raise RuntimeError(
            f"Refusing to downgrade below 0006: {stated} session(s) record a fill timing and "
            f"{declared} dataset version(s) record timestamp provenance that this downgrade "
            "would drop and a later upgrade would replace with false values. Restore a backup "
            "taken before upgrading instead."
        )
    with op.batch_alter_table("backtest_runs") as batch:
        batch.alter_column(
            "seed", existing_type=sa.BigInteger(), type_=sa.Integer(), existing_nullable=False
        )
    with op.batch_alter_table("trading_sessions") as batch:
        batch.alter_column(
            "seed", existing_type=sa.BigInteger(), type_=sa.Integer(), existing_nullable=False
        )
        batch.drop_column("fill_timing")
    with op.batch_alter_table("dataset_versions") as batch:
        batch.drop_column("temporal_json")
