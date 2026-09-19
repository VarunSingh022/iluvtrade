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

Rotation
--------

:func:`rotate_credential` re-seals one blob under the current key. It decrypts
with whichever key sealed it — the current one, or one of the *retired* keys a
deployment lists in ``ILUVTRADE_RETIRED_SECRET_KEYS`` — and re-encrypts under
the current one.

An operational rotation is therefore: add the old key to the retired list, set
the new key, restart, run the rotation, and only then remove the old key from
the list. Skipping the retired-list step makes every stored credential
undecryptable, which is why :func:`decrypt_credentials` names that specific
cause rather than reporting a generic failure.

**Retired keys are never dropped automatically.** Removing one is the operator's
decision, taken after :func:`rotation_status` reports nothing left under it.
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
    "RotationOutcome",
    "SecretString",
    "decrypt_credentials",
    "encrypt_credentials",
    "envelope_info",
    "key_id",
    "needs_rotation",
    "rotate_credential",
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


def _derive(secret: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=None,
        info=_INFO,
    ).derive(secret.encode("utf-8"))


def _key() -> bytes:
    """The key new ciphertext is sealed under."""

    return _derive(get_settings().secret_key)


def _retired_keys() -> list[bytes]:
    """Keys that may still decrypt, in the order a deployment listed them.

    Present only so a rotation can read what an older key sealed. Nothing is
    ever encrypted under one.
    """

    return [_derive(secret) for secret in get_settings().retired_secret_keys if secret]


def _keys_by_id() -> dict[bytes, bytes]:
    """Every key this process can decrypt with, indexed by its id."""

    keys = {key_id(k): k for k in _retired_keys()}
    current = _key()
    keys[key_id(current)] = current  # current wins a collision, which cannot happen
    return keys


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

    available = _keys_by_id()
    key = available.get(stored_key_id)
    if key is None:
        # Named specifically rather than reported as a generic failure: this is
        # the one cause an operator can act on, and the fix is exact.
        raise CredentialError(
            "The stored broker credential was sealed under a key this process does not "
            "hold. Add the previous ILUVTRADE_SECRET_KEY to "
            "ILUVTRADE_RETIRED_SECRET_KEYS and restart, then run a rotation. Until "
            "then the credential cannot be read; reconnecting the broker account also "
            "resolves it."
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


@dataclass(frozen=True, slots=True)
class RotationOutcome:
    """What a rotation did to one credential."""

    rotated: bool
    previous_key_id: str
    current_key_id: str
    reason: str = ""


def needs_rotation(blob: bytes) -> bool:
    """Whether ``blob`` is sealed under something other than the current key."""

    try:
        _, stored = envelope_info(blob)
    except CredentialError:
        return False
    return stored != key_id()


def rotate_credential(blob: bytes, *, connection_id: str) -> tuple[bytes, RotationOutcome]:
    """Re-seal one credential under the current key.

    Decrypts with whichever key sealed it and re-encrypts under the current one.
    The plaintext exists only inside this function and is never returned,
    logged, or written anywhere but the new ciphertext.

    A blob already current is returned unchanged rather than re-encrypted: a
    no-op rotation that still rewrites every row makes it impossible to tell,
    afterwards, which credentials actually moved.
    """

    version, stored_key_id = envelope_info(blob)
    current = key_id()
    if version == ENVELOPE_VERSION and stored_key_id == current:
        return blob, RotationOutcome(
            rotated=False,
            previous_key_id=stored_key_id.hex(),
            current_key_id=current.hex(),
            reason="already sealed under the current key",
        )

    payload = decrypt_credentials(blob, connection_id=connection_id)
    resealed = encrypt_credentials(payload, connection_id=connection_id)
    return resealed, RotationOutcome(
        rotated=True,
        previous_key_id=stored_key_id.hex(),
        current_key_id=current.hex(),
    )
