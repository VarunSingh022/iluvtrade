"""A broker credential must never leave the server."""

from __future__ import annotations

import json

import pytest

from iluvtrade.brokers.crypto import (
    CredentialError,
    SecretString,
    decrypt_credentials,
    encrypt_credentials,
)
from tests.conftest import register

pytestmark = pytest.mark.security

_SECRET = "REAL-ACCESS-TOKEN-abc123xyz"


def test_secret_string_never_renders() -> None:
    """A value that will not print itself cannot be logged by accident."""

    secret = SecretString(_SECRET)
    assert _SECRET not in repr(secret)
    assert _SECRET not in str(secret)
    assert _SECRET not in f"{secret}"
    assert _SECRET not in json.dumps({"t": str(secret)})
    assert secret.reveal() == _SECRET
    assert secret.hint == "…3xyz"


def test_credentials_round_trip_under_the_right_connection() -> None:
    blob = encrypt_credentials({"access_token": _SECRET}, connection_id="conn-1")
    assert _SECRET.encode() not in blob
    assert decrypt_credentials(blob, connection_id="conn-1")["access_token"] == _SECRET


def test_a_credential_moved_to_another_connection_will_not_decrypt() -> None:
    """Binding to the connection id stops a row being copied between accounts."""

    blob = encrypt_credentials({"access_token": _SECRET}, connection_id="conn-1")
    with pytest.raises(CredentialError):
        decrypt_credentials(blob, connection_id="conn-2")


def test_a_tampered_blob_will_not_decrypt() -> None:
    blob = bytearray(encrypt_credentials({"access_token": _SECRET}, connection_id="c"))
    blob[-1] ^= 0xFF
    with pytest.raises(CredentialError):
        decrypt_credentials(bytes(blob), connection_id="c")


def test_no_broker_endpoint_returns_a_credential(client, headers) -> None:
    """Every broker response body is checked for credential-shaped fields."""

    register(client, "b@example.com")
    created = client.post(
        "/api/v1/brokers",
        json={"broker": "paper", "label": "Paper account"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    account_id = created.json()["account_id"]

    bodies = [
        created.text,
        client.get("/api/v1/brokers").text,
        client.get(f"/api/v1/brokers/{account_id}").text,
        client.get("/api/v1/brokers/supported").text,
        client.post(f"/api/v1/brokers/{account_id}/disconnect", headers=headers).text,
    ]
    forbidden = (
        "access_token",
        "api_secret",
        "encrypted_credentials",
        "checksum",
        "request_token",
    )
    for body in bodies:
        for marker in forbidden:
            assert marker not in body, f"{marker!r} appeared in a broker response"


def test_zerodha_session_repr_hides_the_token() -> None:
    from datetime import UTC, datetime

    from iluvtrade.brokers.zerodha import ZerodhaSession

    session = ZerodhaSession(
        api_key="KEY",
        access_token=SecretString(_SECRET),
        user_id="AB1234",
        user_name="Test",
        email="t@example.com",
        expires_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert _SECRET not in repr(session)
    assert _SECRET not in str(session)
