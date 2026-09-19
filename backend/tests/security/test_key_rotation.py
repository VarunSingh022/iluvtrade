"""Credential key rotation.

The operational sequence is: append the old key to the retired list, set the new
key, restart, rotate, then remove the old key. Each step is asserted here,
including what happens when the sequence is done wrong — because doing it wrong
is the likely case and its failure must be recoverable.
"""

from __future__ import annotations

import json

import pytest

from iluvtrade.brokers import crypto
from iluvtrade.config import get_settings

pytestmark = pytest.mark.security

OLD_KEY = "old-secret-key-value-one"
NEW_KEY = "new-secret-key-value-two"
TOKEN = "REAL-ACCESS-TOKEN-xyz789"


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch):
    """Control the current and retired keys for one test."""

    def use(current: str, retired: tuple[str, ...] = ()) -> None:
        monkeypatch.setenv("ILUVTRADE_SECRET_KEY", current)
        monkeypatch.setenv("ILUVTRADE_RETIRED_SECRET_KEYS", json.dumps(list(retired)))
        get_settings.cache_clear()

    use(OLD_KEY)
    yield use
    get_settings.cache_clear()


def _sealed(connection_id: str = "conn-1") -> bytes:
    return crypto.encrypt_credentials({"access_token": TOKEN}, connection_id=connection_id)


# --- the envelope ------------------------------------------------------------


def test_an_envelope_names_the_key_that_sealed_it(keys) -> None:
    blob = _sealed()
    version, stored = crypto.envelope_info(blob)
    assert version == crypto.ENVELOPE_VERSION
    assert stored == crypto.key_id()


def test_the_key_id_is_not_the_key(keys) -> None:
    """It identifies key material without revealing anything usable about it."""

    identifier = crypto.key_id()
    assert len(identifier) == 8
    assert OLD_KEY.encode() not in identifier
    assert identifier != crypto._derive(OLD_KEY)[:8]


# --- rotation ----------------------------------------------------------------


def test_a_blob_under_the_current_key_needs_no_rotation(keys) -> None:
    assert crypto.needs_rotation(_sealed()) is False


def test_changing_the_key_marks_existing_blobs_for_rotation(keys) -> None:
    blob = _sealed()
    keys(NEW_KEY, retired=(OLD_KEY,))
    assert crypto.needs_rotation(blob) is True


def test_rotation_preserves_the_plaintext_and_reseals(keys) -> None:
    blob = _sealed()
    keys(NEW_KEY, retired=(OLD_KEY,))

    resealed, outcome = crypto.rotate_credential(blob, connection_id="conn-1")

    assert outcome.rotated is True
    assert outcome.previous_key_id != outcome.current_key_id
    assert crypto.envelope_info(resealed)[1] == crypto.key_id()
    assert crypto.decrypt_credentials(resealed, connection_id="conn-1")["access_token"] == TOKEN
    assert TOKEN.encode() not in resealed


def test_rotating_twice_is_a_no_op(keys) -> None:
    """A rotation that rewrote every row would hide which ones actually moved."""

    blob = _sealed()
    keys(NEW_KEY, retired=(OLD_KEY,))
    resealed, _ = crypto.rotate_credential(blob, connection_id="conn-1")

    again, outcome = crypto.rotate_credential(resealed, connection_id="conn-1")
    assert outcome.rotated is False
    assert again == resealed


def test_a_rotated_blob_survives_removing_the_retired_key(keys) -> None:
    """The last step of the sequence, and the one that proves it worked."""

    blob = _sealed()
    keys(NEW_KEY, retired=(OLD_KEY,))
    resealed, _ = crypto.rotate_credential(blob, connection_id="conn-1")

    keys(NEW_KEY)  # retired list emptied
    assert crypto.decrypt_credentials(resealed, connection_id="conn-1")["access_token"] == TOKEN


def test_rotation_preserves_the_connection_binding(keys) -> None:
    """A rotated blob must still refuse to decrypt under another connection."""

    blob = _sealed("conn-1")
    keys(NEW_KEY, retired=(OLD_KEY,))
    resealed, _ = crypto.rotate_credential(blob, connection_id="conn-1")

    with pytest.raises(crypto.CredentialError):
        crypto.decrypt_credentials(resealed, connection_id="conn-2")


# --- the sequence done wrong --------------------------------------------------


def test_changing_the_key_without_retiring_the_old_one_is_recoverable(keys) -> None:
    """The likely mistake. It must refuse clearly, not destroy anything."""

    blob = _sealed()
    keys(NEW_KEY)  # old key NOT retired

    with pytest.raises(crypto.CredentialError, match="RETIRED_SECRET_KEYS"):
        crypto.decrypt_credentials(blob, connection_id="conn-1")

    # Adding the old key recovers it; nothing was lost.
    keys(NEW_KEY, retired=(OLD_KEY,))
    assert crypto.decrypt_credentials(blob, connection_id="conn-1")["access_token"] == TOKEN


def test_several_retired_keys_are_all_tried(keys) -> None:
    """A deployment that has rotated more than once still reads its oldest blobs."""

    oldest = _sealed()
    keys("middle-key", retired=(OLD_KEY,))
    middle = _sealed()
    keys(NEW_KEY, retired=(OLD_KEY, "middle-key"))

    assert crypto.decrypt_credentials(oldest, connection_id="conn-1")["access_token"] == TOKEN
    assert crypto.decrypt_credentials(middle, connection_id="conn-1")["access_token"] == TOKEN


def test_nothing_is_ever_encrypted_under_a_retired_key(keys) -> None:
    keys(NEW_KEY, retired=(OLD_KEY,))
    assert crypto.envelope_info(_sealed())[1] == crypto.key_id()


# --- the bulk operation --------------------------------------------------------


def _connected_account(db, principal, token: str = TOKEN):
    """A broker account with a stored credential, built through the service."""

    from iluvtrade.brokers import service
    from iluvtrade.db.models.broker import BrokerKind, ConnectionState

    account = service.create_account(
        db, principal, broker=BrokerKind.ZERODHA, label=f"acct-{token[-4:]}"
    )
    db.flush()
    connection = account.connection
    assert connection is not None
    connection.encrypted_credentials = crypto.encrypt_credentials(
        {"access_token": token}, connection_id=connection.id
    )
    connection.state = ConnectionState.CONNECTED
    db.flush()
    return account, connection


def test_the_bulk_rotation_reseals_every_credential(db, keys) -> None:
    from iluvtrade.brokers import service
    from tests.conftest import make_principal

    principal = make_principal(db)
    _, first = _connected_account(db, principal, "TOKEN-AAAA")
    _, second = _connected_account(db, principal, "TOKEN-BBBB")

    keys(NEW_KEY, retired=(OLD_KEY,))
    report = service.rotate_credentials(db)

    assert report.examined == 2
    assert report.rotated == 2
    assert report.complete

    for connection, expected in ((first, "TOKEN-AAAA"), (second, "TOKEN-BBBB")):
        assert crypto.envelope_info(connection.encrypted_credentials)[1] == crypto.key_id()
        assert (
            crypto.decrypt_credentials(
                connection.encrypted_credentials, connection_id=connection.id
            )["access_token"]
            == expected
        )


def test_a_dry_run_changes_nothing(db, keys) -> None:
    from iluvtrade.brokers import service
    from tests.conftest import make_principal

    principal = make_principal(db)
    _, connection = _connected_account(db, principal)
    before = connection.encrypted_credentials

    keys(NEW_KEY, retired=(OLD_KEY,))
    report = service.rotate_credentials(db, dry_run=True)

    assert report.rotated == 1
    assert connection.encrypted_credentials == before, "a dry run must not write"


def test_an_undecryptable_credential_is_reported_and_left_untouched(db, keys) -> None:
    """Destroying the ciphertext would turn a misconfiguration into data loss."""

    from iluvtrade.brokers import service
    from tests.conftest import make_principal

    principal = make_principal(db)
    _, connection = _connected_account(db, principal)
    before = connection.encrypted_credentials

    keys(NEW_KEY)  # the sealing key is not available at all
    report = service.rotate_credentials(db)

    assert report.rotated == 0
    assert len(report.failed) == 1
    assert not report.complete
    assert connection.encrypted_credentials == before


def test_rotation_is_audited(db, keys) -> None:
    from sqlalchemy import select

    from iluvtrade.brokers import service
    from iluvtrade.db.models.platform import AuditEvent
    from tests.conftest import make_principal

    principal = make_principal(db)
    _connected_account(db, principal)
    keys(NEW_KEY, retired=(OLD_KEY,))
    service.rotate_credentials(db)

    actions = [
        row.action
        for row in db.execute(
            select(AuditEvent).where(AuditEvent.organization_id == principal.organization_id)
        ).scalars()
    ]
    assert "broker.credential.rotated" in actions


def test_no_audit_payload_from_a_rotation_contains_a_credential(db, keys) -> None:
    from sqlalchemy import select

    from iluvtrade.brokers import service
    from iluvtrade.db.models.platform import AuditEvent
    from tests.conftest import make_principal

    principal = make_principal(db)
    _connected_account(db, principal)
    keys(NEW_KEY, retired=(OLD_KEY,))
    service.rotate_credentials(db)

    payloads = " ".join(row.payload_json for row in db.execute(select(AuditEvent)).scalars())
    assert TOKEN not in payloads
    assert OLD_KEY not in payloads and NEW_KEY not in payloads
