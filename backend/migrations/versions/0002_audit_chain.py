"""Add the audit hash chain, backfilling existing events.

Revision ID: 0002_audit_chain
Revises: 0001_baseline

What this does, and what it honestly provides
---------------------------------------------

Adds ``sequence``, ``previous_hash`` and ``event_hash`` to ``audit_events`` and
computes the chain over rows that already exist, per organization, in
``created_at`` order.

**A backfilled chain proves nothing about the past.** It is computed from the
rows as they stand at migration time, so it cannot detect tampering that
happened before this migration ran — if a row was altered yesterday, the
backfill hashes the altered row and reports an intact chain. What it does
provide is detection from this point forward.

That distinction is why the backfill is not silent: it prints how many events
it chained per organization, so an operator knows exactly which events carry
retroactive hashes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0002_audit_chain"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Duplicated from ``iluvtrade.platform.audit`` on purpose. A migration must
#: keep working when the application's constants change — importing them would
#: make this revision's behaviour depend on whatever the code says today rather
#: than on what it said when the revision was written.
GENESIS_HASH = hashlib.sha256(b"iluvtrade/audit-chain/genesis/v1").hexdigest()


def _canonical_bytes(
    *,
    organization_id: str,
    sequence: int,
    created_at_microseconds: int,
    actor_user_id: str | None,
    action: str,
    resource_type: str,
    resource_id: str | None,
    outcome: str,
    ip_address: str | None,
    payload_json: str,
) -> bytes:
    document = {
        "v": 1,
        "organization_id": organization_id,
        "sequence": sequence,
        "created_at_us": created_at_microseconds,
        "actor_user_id": actor_user_id,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "outcome": outcome,
        "ip_address": ip_address,
        "payload": payload_json,
    }
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _microseconds(value: object) -> int:
    if isinstance(value, datetime):
        stamp = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    else:
        stamp = datetime.fromisoformat(str(value))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
    return int(stamp.timestamp() * 1_000_000)


def upgrade() -> None:
    # Added nullable so existing rows survive the ALTER; tightened below once
    # every row has a value.
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("sequence", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("previous_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("event_hash", sa.String(length=64), nullable=True))

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, organization_id, created_at, actor_user_id, action, "
            "resource_type, resource_id, outcome, ip_address, payload_json "
            "FROM audit_events ORDER BY organization_id, created_at, id"
        )
    ).mappings().all()

    heads: dict[str, tuple[int, str]] = {}
    for row in rows:
        organization_id = row["organization_id"]
        sequence, previous_hash = heads.get(organization_id, (0, GENESIS_HASH))
        sequence += 1

        digest = hashlib.sha256()
        digest.update(
            _canonical_bytes(
                organization_id=organization_id,
                sequence=sequence,
                created_at_microseconds=_microseconds(row["created_at"]),
                actor_user_id=row["actor_user_id"],
                action=row["action"],
                resource_type=row["resource_type"],
                resource_id=row["resource_id"],
                outcome=row["outcome"],
                ip_address=row["ip_address"],
                payload_json=row["payload_json"],
            )
        )
        digest.update(b"|")
        digest.update(previous_hash.encode("ascii"))
        event_hash = digest.hexdigest()

        connection.execute(
            sa.text(
                "UPDATE audit_events SET sequence = :sequence, "
                "previous_hash = :previous_hash, event_hash = :event_hash WHERE id = :id"
            ),
            {
                "sequence": sequence,
                "previous_hash": previous_hash,
                "event_hash": event_hash,
                "id": row["id"],
            },
        )
        heads[organization_id] = (sequence, event_hash)

    for organization_id, (count, _) in heads.items():
        print(
            f"  backfilled {count} audit event(s) for organization {organization_id}; "
            "these carry retroactive hashes and prove nothing about the period before "
            "this migration."
        )

    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.alter_column("sequence", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column(
            "previous_hash", existing_type=sa.String(length=64), nullable=False
        )
        batch_op.alter_column(
            "event_hash", existing_type=sa.String(length=64), nullable=False
        )
        batch_op.create_index(
            batch_op.f("ix_audit_events_event_hash"), ["event_hash"], unique=False
        )
        batch_op.create_index(
            "ix_audit_org_sequence", ["organization_id", "sequence"], unique=False
        )
        batch_op.create_unique_constraint(
            "uq_audit_sequence", ["organization_id", "sequence"]
        )


def downgrade() -> None:
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.drop_constraint("uq_audit_sequence", type_="unique")
        batch_op.drop_index("ix_audit_org_sequence")
        batch_op.drop_index(batch_op.f("ix_audit_events_event_hash"))
        batch_op.drop_column("event_hash")
        batch_op.drop_column("previous_hash")
        batch_op.drop_column("sequence")
