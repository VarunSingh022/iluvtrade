"""Blob storage for anything too large or too raw to be a database row.

PHASE 17: raw uploads, canonical datasets and backtest artifacts are files.
A deployment points ``ILUVTRADE_STORAGE_ROOT`` at a volume; the object-store
implementation of the same three operations is a later swap, which is why every
caller addresses a *relative key* and never a filesystem path.

**Path traversal is refused here**, not at each call site: :func:`resolve`
rejects a key that escapes the root, so a crafted dataset id cannot read
``../../etc/passwd``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from iluvtrade.config import get_settings


class StorageError(RuntimeError):
    """The key was refused, or the object is missing."""


def root() -> Path:
    return get_settings().storage_root


def resolve(key: str) -> Path:
    """Turn a storage key into a path under the root, or refuse."""

    if not key or key.startswith("/") or "\x00" in key:
        raise StorageError(f"Invalid storage key: {key!r}")
    base = root().resolve()
    candidate = (base / key).resolve()
    if candidate != base and base not in candidate.parents:
        raise StorageError(f"Storage key {key!r} escapes the storage root.")
    return candidate


def write_bytes(key: str, payload: bytes) -> str:
    """Store ``payload`` at ``key``. Returns its SHA-256."""

    path = resolve(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def read_bytes(key: str) -> bytes:
    path = resolve(key)
    if not path.is_file():
        raise StorageError(f"No object at {key!r}.")
    return path.read_bytes()


def write_json(key: str, payload: Any) -> str:
    return write_bytes(key, json.dumps(payload, default=str, sort_keys=True).encode("utf-8"))


def read_json(key: str) -> Any:
    return json.loads(read_bytes(key).decode("utf-8"))


def write_jsonl(key: str, rows: Iterable[dict[str, Any]]) -> tuple[str, int]:
    """Stream rows as JSON Lines. Returns ``(sha256, row_count)``.

    Hashed while writing rather than by re-reading, so a multi-gigabyte dataset
    is never held in memory twice.
    """

    path = resolve(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    count = 0
    with path.open("wb") as handle:
        for row in rows:
            line = (json.dumps(row, default=str, sort_keys=True) + "\n").encode("utf-8")
            digest.update(line)
            handle.write(line)
            count += 1
    return digest.hexdigest(), count


def read_jsonl(key: str) -> Iterator[dict[str, Any]]:
    path = resolve(key)
    if not path.is_file():
        raise StorageError(f"No object at {key!r}.")
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def exists(key: str) -> bool:
    try:
        return resolve(key).is_file()
    except StorageError:
        return False


def delete_prefix(prefix: str) -> None:
    """Remove everything under ``prefix``. Used when an organization is deleted."""

    path = resolve(prefix)
    if path.is_dir():
        shutil.rmtree(path)


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
