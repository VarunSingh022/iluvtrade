"""Encrypting broker credentials at rest.

AES-256-GCM under a key derived from the application secret with HKDF. Three
properties this buys, each of which matters on its own:

* **Confidentiality at rest.** A database dump does not yield an access token.
* **Integrity.** GCM authenticates the ciphertext, so a tampered blob fails to
  decrypt rather than decrypting to something attacker-chosen.
* **Binding.** The broker connection's id is passed as additional authenticated
  data, so a ciphertext copied from one connection row to another will not
  decrypt — which stops a low-privilege user who can write rows from moving a
  colleague's token onto their own connection.

The key is derived rather than used directly so that the same application secret
can protect other things without one compromise being all of them.

Rotation is not implemented. Changing ``ILUVTRADE_SECRET_KEY`` invalidates every
stored credential and every session; ``docs/SECURITY.md`` states that, and states
what a real rotation would need.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from iluvtrade.config import get_settings

__all__ = ["CredentialError", "SecretString", "decrypt_credentials", "encrypt_credentials"]

_NONCE_BYTES = 12
_KEY_BYTES = 32
_INFO = b"iluvtrade/broker-credentials/v1"


class CredentialError(RuntimeError):
    """The credential blob could not be decrypted — wrong key, or tampered."""


@dataclass(frozen=True)
class SecretString:
    """A string that will not render itself.

    Modelled on AlphaLab's ``VenueCredentials``, for the same reason: a value
    that refuses to appear in a ``repr`` cannot reach a log line, a traceback, a
    pytest assertion diff or a serialized error by accident. Reading it requires
    saying :meth:`reveal`, which is greppable.
    """

    _value: str

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"SecretString(len={len(self._value)}, value=[redacted])"

    def __str__(self) -> str:
        return "[redacted]"

    def __eq__(self, other: object) -> bool:
        # Comparison is disabled rather than constant-time: an accidental `==`
        # against a literal is a code smell here, not an operation to optimise.
        return NotImplemented

    def __hash__(self) -> int:
        return hash(id(self))

    @property
    def hint(self) -> str:
        """A short, non-secret suffix safe to show in a UI."""

        return f"…{self._value[-4:]}" if len(self._value) >= 8 else "…"


def _key() -> bytes:
    settings = get_settings()
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=None,
        info=_INFO,
    ).derive(settings.secret_key.encode("utf-8"))


def encrypt_credentials(payload: dict[str, Any], *, connection_id: str) -> bytes:
    """Encrypt a credential document, bound to one connection."""

    import json

    nonce = os.urandom(_NONCE_BYTES)
    plaintext = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(_key()).encrypt(nonce, plaintext, connection_id.encode("utf-8"))
    return nonce + ciphertext


def decrypt_credentials(blob: bytes, *, connection_id: str) -> dict[str, Any]:
    """Decrypt a credential document, or refuse."""

    import json

    if not blob or len(blob) <= _NONCE_BYTES:
        raise CredentialError("The stored credential is empty or truncated.")
    nonce, ciphertext = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    try:
        plaintext = AESGCM(_key()).decrypt(nonce, ciphertext, connection_id.encode("utf-8"))
    except Exception as exc:  # cryptography raises InvalidTag and friends
        raise CredentialError(
            "The stored broker credential could not be decrypted. This happens when the "
            "application secret has changed, or when the row was moved between "
            "connections. Reconnect the broker account."
        ) from exc
    result: dict[str, Any] = json.loads(plaintext.decode("utf-8"))
    return result
