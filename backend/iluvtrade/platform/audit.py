"""The audit writer, and the hash chain that makes it tamper-evident.

One writer, because an audit trail with several writers grows several redaction
policies and several ideas of what a chain link covers. :func:`record` is the
only thing in the application that inserts into ``audit_events``.

**Redaction happens here.** Any key whose name looks like a credential is
replaced before the payload is serialized, so a caller that carelessly passes a
whole request body cannot write a secret into the permanent record.

The chain
---------

Each event is hashed over its own canonical form *and* its predecessor's hash::

    event_hash = SHA-256( canonical(event) || previous_hash )

Serialization is canonical — sorted keys, no insignificant whitespace, explicit
nulls, a fixed field order, and timestamps as integer microseconds — so the same
event always produces the same hash on any machine and any Python version. A
hash over ``str(dict)`` would depend on insertion order and on ``repr``
formatting, and would start failing verification for reasons that have nothing
to do with tampering.

The chain is **per organization**, which keeps verification inside one tenant.

What it detects, and what it does not
-------------------------------------

Detects: an altered payload, a deleted event, two events swapped, an event
inserted, a forged hash that does not match its content.

Does **not** detect: an attacker with write access recomputing the whole chain.
Nothing self-contained can — that needs the head published somewhere the
attacker does not control (an external WORM store, a transparency log, a
periodic signed anchor). This is tamper-*evidence*, not immutability, and
``docs/SECURITY.md`` says so in those words.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from iluvtrade.db.base import utcnow
from iluvtrade.db.models.platform import AuditEvent

#: Substrings that make a key a secret. Matched case-insensitively against the
#: whole key, at every depth.
_SECRET_KEY_MARKERS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "credential",
    "authorization",
    "signature",
    "private",
    "checksum",
)

REDACTED = "[redacted]"

#: The ``previous_hash`` of the first event in an organization's chain. A fixed,
#: domain-separated constant rather than an empty string, so a genesis link
#: cannot be confused with a missing one.
GENESIS_HASH = hashlib.sha256(b"iluvtrade/audit-chain/genesis/v1").hexdigest()


def redact(value: Any, _depth: int = 0) -> Any:
    """Return ``value`` with anything credential-shaped replaced."""

    if _depth > 8:
        return "[truncated]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if any(marker in name.lower() for marker in _SECRET_KEY_MARKERS):
                out[name] = REDACTED
            else:
                out[name] = redact(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth + 1) for item in value]
    return value


def record(
    session: Session,
    *,
    organization_id: str,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    actor_user_id: str | None = None,
    outcome: str = "success",
    ip_address: str | None = None,
    payload: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append one audit event. The caller must still commit the session."""

    # The head is read inside the caller's transaction, so two concurrent
    # writers in the same organization serialise on the unique
    # (organization_id, sequence) constraint rather than silently both claiming
    # the same position. One of them fails its INSERT, which is the correct
    # outcome: an audit trail with two events at position 7 is not verifiable.
    previous_sequence, previous_hash = chain_head(session, organization_id)

    event = AuditEvent(
        organization_id=organization_id,
        created_at=utcnow(),
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        ip_address=ip_address,
        payload_json=json.dumps(redact(payload or {}), default=str, sort_keys=True),
        sequence=previous_sequence + 1,
        previous_hash=previous_hash,
    )
    event.event_hash = compute_hash(event, previous_hash)
    session.add(event)
    # Flushed here so the next call in this transaction reads this event as the
    # head. Without it, two events written in one request would both link to the
    # same predecessor and both claim the same sequence.
    session.flush()
    return event


# ---------------------------------------------------------------------------
# The hash chain
# ---------------------------------------------------------------------------


def canonical_bytes(
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
    """The exact bytes an event's hash is taken over.

    A fixed field order and explicit ``null``s, serialized with sorted keys and
    no insignificant whitespace. Every covered field is named here — which is
    also the definition of what the chain protects: a field not in this list
    could be changed without breaking verification, so adding a column means
    deciding, deliberately, whether it belongs.

    ``created_at`` is passed as integer microseconds rather than a datetime,
    because a timestamp that round-trips through a database with different
    sub-second precision would otherwise hash differently after a restore.
    """

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
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def compute_hash(event: AuditEvent, previous_hash: str) -> str:
    """``SHA-256(canonical(event) || previous_hash)``."""

    digest = hashlib.sha256()
    digest.update(
        canonical_bytes(
            organization_id=event.organization_id,
            sequence=event.sequence,
            created_at_microseconds=_microseconds(event),
            actor_user_id=event.actor_user_id,
            action=event.action,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            outcome=event.outcome,
            ip_address=event.ip_address,
            payload_json=event.payload_json,
        )
    )
    digest.update(b"|")
    digest.update(previous_hash.encode("ascii"))
    return digest.hexdigest()


def _microseconds(event: AuditEvent) -> int:
    stamp = event.created_at
    if stamp.tzinfo is None:  # pragma: no cover - UtcDateTime prevents this
        raise ValueError("An audit event's timestamp must be timezone-aware.")
    return int(stamp.timestamp() * 1_000_000)


@dataclass(frozen=True, slots=True)
class ChainBreak:
    """One verification failure, named precisely enough to act on."""

    sequence: int
    event_id: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"sequence": self.sequence, "event_id": self.event_id, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class ChainVerification:
    """The result of verifying one organization's chain."""

    organization_id: str
    events_checked: int
    breaks: tuple[ChainBreak, ...]
    head_hash: str

    @property
    def intact(self) -> bool:
        return not self.breaks

    def to_dict(self) -> dict[str, Any]:
        return {
            "organization_id": self.organization_id,
            "events_checked": self.events_checked,
            "intact": self.intact,
            "head_hash": self.head_hash,
            "breaks": [b.to_dict() for b in self.breaks],
        }


def verify_chain(session: Session, organization_id: str) -> ChainVerification:
    """Recompute an organization's chain and report every break.

    Reports *all* breaks rather than stopping at the first, because the shape of
    the damage is diagnostic: one bad link is an edited row, and a run of them
    from a point onward is a deletion or an insertion.
    """

    events = list(
        session.execute(
            select(AuditEvent)
            .where(AuditEvent.organization_id == organization_id)
            .order_by(AuditEvent.sequence)
        ).scalars()
    )

    breaks: list[ChainBreak] = []
    previous_hash = GENESIS_HASH
    expected_sequence = 1

    for event in events:
        if event.sequence != expected_sequence:
            breaks.append(
                ChainBreak(
                    event.sequence,
                    event.id,
                    f"Sequence jumped: expected {expected_sequence}, found {event.sequence}. "
                    "An event was deleted, inserted or reordered.",
                )
            )
            expected_sequence = event.sequence

        if event.previous_hash != previous_hash:
            breaks.append(
                ChainBreak(
                    event.sequence,
                    event.id,
                    "This event does not link to its predecessor. The predecessor was "
                    "altered or removed.",
                )
            )

        recomputed = compute_hash(event, event.previous_hash)
        if recomputed != event.event_hash:
            breaks.append(
                ChainBreak(
                    event.sequence,
                    event.id,
                    "The recorded hash does not match this event's content. The row was "
                    "modified after it was written.",
                )
            )

        previous_hash = event.event_hash
        expected_sequence += 1

    return ChainVerification(
        organization_id=organization_id,
        events_checked=len(events),
        breaks=tuple(breaks),
        head_hash=previous_hash,
    )


def chain_head(session: Session, organization_id: str) -> tuple[int, str]:
    """The last sequence and hash in an organization's chain.

    ``(0, GENESIS_HASH)`` when the chain is empty.
    """

    row = session.execute(
        select(AuditEvent.sequence, AuditEvent.event_hash)
        .where(AuditEvent.organization_id == organization_id)
        .order_by(AuditEvent.sequence.desc())
        .limit(1)
    ).first()
    if row is None:
        return 0, GENESIS_HASH
    return int(row[0]), str(row[1])
