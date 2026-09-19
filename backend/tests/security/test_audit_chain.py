"""The audit hash chain.

Each test names the tampering it simulates, because "the chain works" is only
meaningful as a list of the specific edits it catches.
"""

from __future__ import annotations

from itertools import pairwise

import pytest
from sqlalchemy import select

from iluvtrade.db.models.platform import AuditEvent
from iluvtrade.platform import audit
from tests.conftest import make_principal

pytestmark = pytest.mark.security


def _events(session, organization_id: str) -> list[AuditEvent]:
    return list(
        session.execute(
            select(AuditEvent)
            .where(AuditEvent.organization_id == organization_id)
            .order_by(AuditEvent.sequence)
        ).scalars()
    )


def _write(session, organization_id: str, count: int) -> None:
    for index in range(count):
        audit.record(
            session,
            organization_id=organization_id,
            action=f"test.event.{index}",
            resource_type="thing",
            resource_id=str(index),
            payload={"index": index},
        )


# --- canonical serialization ------------------------------------------------


def test_canonical_bytes_are_stable_across_key_order() -> None:
    """The hash must not depend on how a dict happened to be built."""

    first = audit.canonical_bytes(
        organization_id="o",
        sequence=1,
        created_at_microseconds=1_700_000_000_000_000,
        actor_user_id="u",
        action="a",
        resource_type="r",
        resource_id="rid",
        outcome="success",
        ip_address=None,
        payload_json='{"a":1,"b":2}',
    )
    second = audit.canonical_bytes(
        payload_json='{"a":1,"b":2}',
        ip_address=None,
        outcome="success",
        resource_id="rid",
        resource_type="r",
        action="a",
        actor_user_id="u",
        created_at_microseconds=1_700_000_000_000_000,
        sequence=1,
        organization_id="o",
    )
    assert first == second


def test_canonical_bytes_change_when_any_covered_field_changes() -> None:
    """Every field the chain claims to cover must actually affect the hash."""

    base = {
        "organization_id": "o",
        "sequence": 1,
        "created_at_microseconds": 1_700_000_000_000_000,
        "actor_user_id": "u",
        "action": "a",
        "resource_type": "r",
        "resource_id": "rid",
        "outcome": "success",
        "ip_address": "1.2.3.4",
        "payload_json": '{"a":1}',
    }
    reference = audit.canonical_bytes(**base)
    for field, altered in (
        ("organization_id", "other"),
        ("sequence", 2),
        ("created_at_microseconds", 1_700_000_000_000_001),
        ("actor_user_id", "other"),
        ("action", "other"),
        ("resource_type", "other"),
        ("resource_id", "other"),
        ("outcome", "failure"),
        ("ip_address", "5.6.7.8"),
        ("payload_json", '{"a":2}'),
    ):
        assert audit.canonical_bytes(**{**base, field: altered}) != reference, field


def test_a_null_field_is_distinguishable_from_a_missing_one() -> None:
    with_null = audit.canonical_bytes(
        organization_id="o",
        sequence=1,
        created_at_microseconds=1,
        actor_user_id=None,
        action="a",
        resource_type="r",
        resource_id=None,
        outcome="s",
        ip_address=None,
        payload_json="{}",
    )
    with_empty = audit.canonical_bytes(
        organization_id="o",
        sequence=1,
        created_at_microseconds=1,
        actor_user_id="",
        action="a",
        resource_type="r",
        resource_id="",
        outcome="s",
        ip_address="",
        payload_json="{}",
    )
    assert with_null != with_empty


# --- a healthy chain --------------------------------------------------------


def test_a_fresh_chain_verifies(db) -> None:
    principal = make_principal(db)
    _write(db, principal.organization_id, 5)

    result = audit.verify_chain(db, principal.organization_id)
    assert result.intact
    assert result.breaks == ()
    assert result.events_checked >= 5


def test_the_first_event_links_to_genesis(db) -> None:
    principal = make_principal(db)
    events = _events(db, principal.organization_id)
    assert events[0].sequence == 1
    assert events[0].previous_hash == audit.GENESIS_HASH


def test_sequences_are_contiguous_and_each_links_to_the_last(db) -> None:
    principal = make_principal(db)
    _write(db, principal.organization_id, 6)

    events = _events(db, principal.organization_id)
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    for earlier, later in pairwise(events):
        assert later.previous_hash == earlier.event_hash


def test_the_head_advances_with_each_event(db) -> None:
    principal = make_principal(db)
    before_sequence, before_hash = audit.chain_head(db, principal.organization_id)
    _write(db, principal.organization_id, 1)
    after_sequence, after_hash = audit.chain_head(db, principal.organization_id)

    assert after_sequence == before_sequence + 1
    assert after_hash != before_hash


def test_an_empty_chain_reports_genesis(db) -> None:
    sequence, head = audit.chain_head(db, "organization-with-no-events")
    assert sequence == 0
    assert head == audit.GENESIS_HASH


# --- tampering --------------------------------------------------------------


def test_a_modified_payload_is_detected(db) -> None:
    """The commonest case: someone edits what an event says happened."""

    principal = make_principal(db)
    _write(db, principal.organization_id, 5)

    events = _events(db, principal.organization_id)
    events[2].payload_json = '{"index": 999, "tampered": true}'
    db.flush()

    result = audit.verify_chain(db, principal.organization_id)
    assert not result.intact
    assert any("does not match this event's content" in b.reason for b in result.breaks)
    assert any(b.sequence == events[2].sequence for b in result.breaks)


def test_a_modified_action_is_detected(db) -> None:
    principal = make_principal(db)
    _write(db, principal.organization_id, 4)

    events = _events(db, principal.organization_id)
    events[1].action = "something.else"
    db.flush()

    assert not audit.verify_chain(db, principal.organization_id).intact


def test_a_modified_outcome_is_detected(db) -> None:
    """Flipping a failure to a success is exactly what a chain must catch."""

    principal = make_principal(db)
    _write(db, principal.organization_id, 4)

    events = _events(db, principal.organization_id)
    events[2].outcome = "success" if events[2].outcome == "failure" else "failure"
    db.flush()

    assert not audit.verify_chain(db, principal.organization_id).intact


def test_a_deleted_event_is_detected(db) -> None:
    principal = make_principal(db)
    _write(db, principal.organization_id, 6)

    events = _events(db, principal.organization_id)
    db.delete(events[3])
    db.flush()

    result = audit.verify_chain(db, principal.organization_id)
    assert not result.intact
    reasons = " ".join(b.reason for b in result.breaks)
    assert "Sequence jumped" in reasons or "does not link" in reasons


def test_the_database_refuses_two_events_at_one_position(db) -> None:
    """The unique constraint is the first line: a naive swap cannot even commit.

    ``(organization_id, sequence)`` is unique, so an attacker cannot assign two
    events the same position — not even transiently while swapping them. This
    is a database fact rather than something the writer is trusted to maintain.
    """

    from sqlalchemy.exc import IntegrityError

    principal = make_principal(db)
    _write(db, principal.organization_id, 4)

    events = _events(db, principal.organization_id)
    events[2].sequence = events[3].sequence
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_reordering_two_events_is_detected(db) -> None:
    """A determined swap — via a spare position — still breaks verification.

    The constraint above blocks the one-step swap, so this does what an attacker
    with SQL access would: park one event at an unused sequence, move the other,
    then bring the first back. The chain still catches it, because the links no
    longer follow the order.
    """

    principal = make_principal(db)
    _write(db, principal.organization_id, 6)

    events = _events(db, principal.organization_id)
    first, second = events[2], events[3]
    first_position, second_position = first.sequence, second.sequence
    parked = 10_000

    first.sequence = parked
    db.flush()
    second.sequence = first_position
    db.flush()
    first.sequence = second_position
    db.flush()

    reordered = _events(db, principal.organization_id)
    assert [e.id for e in reordered[2:4]] == [second.id, first.id], "the swap took effect"

    result = audit.verify_chain(db, principal.organization_id)
    assert not result.intact
    assert any("does not link to its predecessor" in b.reason for b in result.breaks)


def test_a_forged_hash_that_ignores_content_is_detected(db) -> None:
    """Writing a plausible-looking hash is not enough; it must match."""

    principal = make_principal(db)
    _write(db, principal.organization_id, 4)

    events = _events(db, principal.organization_id)
    events[2].payload_json = '{"tampered": true}'
    events[2].event_hash = "0" * 64
    db.flush()

    result = audit.verify_chain(db, principal.organization_id)
    assert not result.intact


def test_repairing_one_event_still_breaks_the_rest_of_the_chain(db) -> None:
    """An attacker must recompute *every* later hash, not just the one edited.

    This is the property that makes the chain worth having.
    """

    principal = make_principal(db)
    _write(db, principal.organization_id, 6)

    events = _events(db, principal.organization_id)
    target = events[2]
    target.payload_json = '{"tampered": true}'
    # Recompute only this event's own hash, as a naive attacker would.
    target.event_hash = audit.compute_hash(target, target.previous_hash)
    db.flush()

    result = audit.verify_chain(db, principal.organization_id)
    assert not result.intact
    assert any("does not link to its predecessor" in b.reason for b in result.breaks)


# --- tenancy ----------------------------------------------------------------


def test_chains_are_independent_per_organization(db) -> None:
    """Breaking one tenant's chain must not affect another's."""

    alice = make_principal(db, "alice@example.com")
    bob = make_principal(db, "bob@example.com")
    _write(db, alice.organization_id, 4)
    _write(db, bob.organization_id, 4)

    assert audit.verify_chain(db, alice.organization_id).intact
    assert audit.verify_chain(db, bob.organization_id).intact

    alice_events = _events(db, alice.organization_id)
    alice_events[1].payload_json = '{"tampered": true}'
    db.flush()

    assert not audit.verify_chain(db, alice.organization_id).intact
    assert audit.verify_chain(db, bob.organization_id).intact, (
        "one tenant's tampering must not invalidate another's chain"
    )


def test_each_organization_starts_its_own_sequence_at_one(db) -> None:
    alice = make_principal(db, "alice@example.com")
    bob = make_principal(db, "bob@example.com")

    assert _events(db, alice.organization_id)[0].sequence == 1
    assert _events(db, bob.organization_id)[0].sequence == 1


# --- redaction still holds ---------------------------------------------------


def test_no_secret_reaches_a_chained_event(db) -> None:
    """Hashing must not have changed what gets redacted."""

    principal = make_principal(db)
    audit.record(
        db,
        organization_id=principal.organization_id,
        action="broker.connected",
        resource_type="broker_account",
        payload={
            "access_token": "REAL-TOKEN-VALUE",
            "api_secret": "REAL-SECRET",
            "nested": {"password": "hunter2"},
            "safe": "visible",
        },
    )
    stored = _events(db, principal.organization_id)[-1].payload_json
    assert "REAL-TOKEN-VALUE" not in stored
    assert "REAL-SECRET" not in stored
    assert "hunter2" not in stored
    assert "visible" in stored
    assert audit.verify_chain(db, principal.organization_id).intact


# --- through the API ---------------------------------------------------------


def test_the_verify_endpoint_reports_an_intact_chain(client, headers) -> None:
    from tests.conftest import register

    register(client, "admin@example.com")
    client.post("/api/v1/strategies", json={"name": "S"}, headers=headers)

    response = client.get("/api/v1/audit/verify")
    assert response.status_code == 200
    body = response.json()
    assert body["intact"] is True
    assert body["events_checked"] >= 2
    assert len(body["head_hash"]) == 64


def test_audit_responses_carry_the_chain_fields(client, headers) -> None:
    """A reader must be able to verify independently of the server's own check."""

    from tests.conftest import register

    register(client, "admin@example.com")
    rows = client.get("/api/v1/audit").json()

    assert rows
    for row in rows:
        assert isinstance(row["sequence"], int)
        assert len(row["previous_hash"]) == 64
        assert len(row["event_hash"]) == 64
