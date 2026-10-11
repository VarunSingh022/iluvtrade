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

import contextlib
import hashlib
import json
import os
import shutil
import tempfile
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
    """Store ``payload`` at ``key``, atomically. Returns its SHA-256.

    Written to a temporary file in the same directory, flushed to disk, then
    renamed over the key: a reader sees the old object or the whole new one,
    never a truncated one. It used to write in place, so a crash mid-write left
    a partial file at the real key.
    """

    path = resolve(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
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
    """Stream rows as JSON Lines, atomically. Returns ``(sha256, row_count)``.

    Hashed while writing rather than by re-reading, so a multi-gigabyte dataset
    is never held in memory twice. Written to a temporary file and renamed over
    the key, as :func:`write_bytes` is: it used to write in place, so a crash
    mid-write left a truncated canonical file whose stored hash no longer
    matched -- and nothing read it back against that hash.
    """

    path = resolve(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    count = 0
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            for row in rows:
                line = (json.dumps(row, default=str, sort_keys=True) + "\n").encode("utf-8")
                digest.update(line)
                stream.write(line)
                count += 1
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    return digest.hexdigest(), count


def read_jsonl(key: str) -> Iterator[dict[str, Any]]:
    path = resolve(key)
    if not path.is_file():
        raise StorageError(f"No object at {key!r}.")
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


_JSONL_HASH_CHUNK_SIZE = 1024 * 1024


def read_jsonl_verified(
    key: str, sha256: str, expected_count: int | None = None
) -> list[dict[str, Any]]:
    """Every row at ``key``, only if the file's bytes hash to ``sha256``.

    For anything that *computes* on stored rows (a backtest, a paper session):
    the rows must be the ones whose hash was recorded when they were written,
    not whatever the file holds now. Hashing uses bounded chunks and parsing
    happens only after the initial digest and optional row count are verified.
    The returned records are still materialized as a list, so memory use is
    proportional to the dataset size.
    """

    path = resolve(key)
    if not path.is_file():
        raise StorageError(f"No object at {key!r}.")

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        row_count = 0
        line_has_content = False
        while chunk := handle.read(_JSONL_HASH_CHUNK_SIZE):
            digest.update(chunk)
            parts = chunk.split(b"\n")
            for line in parts[:-1]:
                if line_has_content or line.strip():
                    row_count += 1
                line_has_content = False
            line_has_content = line_has_content or bool(parts[-1].strip())
        if line_has_content:
            row_count += 1

    if digest.hexdigest() != sha256:
        raise StorageError(
            f"The object at {key!r} does not match its recorded SHA-256: it changed after it "
            "was written."
        )
    if expected_count is not None and row_count != expected_count:
        raise StorageError(
            f"The object at {key!r} records {expected_count} rows but its canonical data holds "
            f"{row_count}."
        )

    rows: list[dict[str, Any]] = []
    parsed_digest = hashlib.sha256()
    parsed_count = 0
    with path.open("rb") as handle:
        for line in handle:
            parsed_digest.update(line)
            if line.strip():
                rows.append(json.loads(line))
                parsed_count += 1

    # The second digest also prevents an in-place change between verification
    # and parsing from being accepted under a stale recorded hash.
    if parsed_digest.hexdigest() != sha256:
        raise StorageError(
            f"The object at {key!r} does not match its recorded SHA-256: it changed after it "
            "was written."
        )
    if expected_count is not None and parsed_count != expected_count:
        raise StorageError(
            f"The object at {key!r} records {expected_count} rows but its canonical data holds "
            f"{parsed_count}."
        )
    return rows


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
