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

Key versioning
--------------

Every envelope carries a **version byte** and the **key id** the ciphertext was
sealed under. That is not rotation — nothing re-encrypts anything — but it is
the thing rotation cannot be added without: today's blobs are self-describing,
so a future implementation can decrypt an old blob with an old key while
writing new ones under a new key. Adding the header later would require a
migration over every stored credential, and would have to guess which key each
one used.

The key id is a **truncated HMAC of the derived key**, not the secret and not
derivable back to it. It identifies which key material sealed a blob without
revealing anything about that material.

Rotation itself is still unimplemented: changing ``ILUVTRADE_SECRET_KEY``
invalidates every stored credential and every session. ``docs/SECURITY.md``
states what a real rotation would additionally need.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from iluvtrade.config import get_settings

__all__ = [
    "ENVELOPE_VERSION",
    "CredentialError",
    "SecretString",
    "decrypt_credentials",
    "encrypt_credentials",
    "envelope_info",
    "key_id",
]

_NONCE_BYTES = 12
_KEY_BYTES = 32
_INFO = b"iluvtrade/broker-credentials/v1"

#: Envelope format version. Bumped only when the *layout* below changes, so a
#: reader can tell an old blob from a new one without guessing.
ENVELOPE_VERSION = 1
#: Bytes of key-id carried in the header. Eight is enough to distinguish the
#: handful of keys a deployment will ever hold, and short enough to be cheap.
_KEY_ID_BYTES = 8

#: ``version(1) || key_id(8) || nonce(12) || ciphertext``
_HEADER_BYTES = 1 + _KEY_ID_BYTES


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


def key_id(key: bytes | None = None) -> bytes:
    """A short, non-reversible identifier for the current key material.

    A truncated SHA-256 of the *derived* key. It names which key sealed a blob
    and reveals nothing usable about it: recovering the key from this would
    require inverting SHA-256.
    """

    digest = hashes.Hash(hashes.SHA256())
    digest.update(b"iluvtrade/key-id/v1")
    digest.update(key if key is not None else _key())
    return digest.finalize()[:_KEY_ID_BYTES]


def encrypt_credentials(payload: dict[str, Any], *, connection_id: str) -> bytes:
    """Encrypt a credential document, bound to one connection.

    The header travels as GCM's *additional authenticated data* alongside the
    connection id, so neither the version nor the key id can be altered without
    the decryption failing. A header that could be edited would let an attacker
    claim a blob was sealed under a different key.
    """

    import json

    key = _key()
    header = bytes([ENVELOPE_VERSION]) + key_id(key)
    nonce = os.urandom(_NONCE_BYTES)
    plaintext = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, header + connection_id.encode("utf-8"))
    return header + nonce + ciphertext


def envelope_info(blob: bytes) -> tuple[int, bytes]:
    """The version and key id a blob declares, without decrypting it.

    What a future rotation reads to decide which key to try. Readable without
    the key, which is the point of putting it outside the ciphertext.
    """

    if len(blob) < _HEADER_BYTES:
        raise CredentialError("The stored credential is truncated.")
    return blob[0], blob[1:_HEADER_BYTES]


def decrypt_credentials(blob: bytes, *, connection_id: str) -> dict[str, Any]:
    """Decrypt a credential document, or refuse."""

    import json

    if not blob or len(blob) <= _HEADER_BYTES + _NONCE_BYTES:
        raise CredentialError("The stored credential is empty or truncated.")

    version, stored_key_id = envelope_info(blob)
    if version != ENVELOPE_VERSION:
        raise CredentialError(
            f"The stored credential uses envelope version {version}, and this build "
            f"understands version {ENVELOPE_VERSION}. Reconnect the broker account."
        )

    key = _key()
    if stored_key_id != key_id(key):
        # Named specifically rather than reported as a generic failure: this is
        # the one cause an operator can act on, and it is the case a future
        # rotation would handle by reaching for the old key instead of failing.
        raise CredentialError(
            "The stored broker credential was encrypted under a different application "
            "key. Key rotation is not implemented, so the credential cannot be read. "
            "Reconnect the broker account."
        )

    header = blob[:_HEADER_BYTES]
    nonce = blob[_HEADER_BYTES : _HEADER_BYTES + _NONCE_BYTES]
    ciphertext = blob[_HEADER_BYTES + _NONCE_BYTES :]
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, header + connection_id.encode("utf-8"))
    except Exception as exc:  # cryptography raises InvalidTag and friends
        raise CredentialError(
            "The stored broker credential could not be decrypted. This happens when the "
            "row was moved between connections, or when the blob has been altered. "
            "Reconnect the broker account."
        ) from exc
    result: dict[str, Any] = json.loads(plaintext.decode("utf-8"))
    return result
