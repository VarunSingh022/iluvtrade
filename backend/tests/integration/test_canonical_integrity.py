"""A version's canonical file is written atomically and verified before anything computes on it.

The canonical rows are what a version id *means*: its hash is recorded at
import and its backtests and sessions are reproducible from it. Until this
pass the file was written in place (a crash left a truncated file under the
real key) and read back without checking the recorded hash, so a truncated or
altered file would have fed the engine different bars under the same version
id -- and a result that looked reproducible but was not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.conftest import make_csv, register
from tests.support import DECLARED


def _approved(client: Any, headers: dict[str, str]) -> tuple[str, str]:
    version = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=30), "text/csv")},
        data=DECLARED,
        headers=headers,
    ).json()
    client.post(f"/api/v1/datasets/versions/{version['id']}/approve", headers=headers)
    strategy = client.post("/api/v1/strategies", json={"name": "Hold"}, headers=headers).json()
    draft = client.post(
        f"/api/v1/strategies/{strategy['id']}/versions",
        json={"implementation_key": "buy_and_hold", "parameters": {"quantity": 1}},
        headers=headers,
    ).json()
    client.post(f"/api/v1/strategies/versions/{draft['id']}/publish", headers=headers)
    return version["id"], draft["id"]


def _canonical_path(version_id: str) -> Any:
    from iluvtrade.common import storage
    from iluvtrade.db.models.data import DatasetVersion
    from iluvtrade.db.session import session_scope

    with session_scope() as session:
        version = session.get(DatasetVersion, version_id)
        assert version is not None and version.canonical_path
        return storage.resolve(version.canonical_path)


def test_canonical_writes_are_atomic(monkeypatch) -> None:
    from iluvtrade.common import storage

    key = "org/x/datasets/v/canonical.jsonl"
    digest, count = storage.write_jsonl(key, [{"t": 1}, {"t": 2}])
    path = storage.resolve(key)
    before = path.read_bytes()
    assert count == 2 and storage.sha256(before) == digest

    def interrupted(source: str, target: object) -> None:
        raise OSError("disk full at the worst moment")

    monkeypatch.setattr(storage.os, "replace", interrupted)
    with pytest.raises(OSError):
        storage.write_jsonl(key, [{"t": 9}] * 1000)
    # The previous file is whole, and no temporary file is left behind.
    assert path.read_bytes() == before
    assert [p.name for p in path.parent.iterdir()] == ["canonical.jsonl"]


def test_a_verified_read_refuses_a_changed_file() -> None:
    from iluvtrade.common import storage

    key = "org/x/datasets/w/canonical.jsonl"
    digest, _ = storage.write_jsonl(key, [{"t": 1}, {"t": 2}])
    assert storage.read_jsonl_verified(key, digest) == [{"t": 1}, {"t": 2}]
    path = storage.resolve(key)
    path.write_bytes(path.read_bytes()[:-8])  # truncated
    with pytest.raises(storage.StorageError, match="does not match its recorded SHA-256"):
        storage.read_jsonl_verified(key, digest)


def test_verified_read_parses_valid_jsonl_and_checks_the_recorded_count() -> None:
    from iluvtrade.common import storage

    key = "org/x/datasets/valid/canonical.jsonl"
    expected = [{"t": 1}, {"t": 2}]
    digest, count = storage.write_jsonl(key, expected)

    assert storage.read_jsonl_verified(key, digest, expected_count=count) == expected


def test_a_changed_valid_file_is_rejected_before_json_is_parsed(monkeypatch) -> None:
    from iluvtrade.common import storage

    key = "org/x/datasets/changed/canonical.jsonl"
    digest, _ = storage.write_jsonl(key, [{"t": 1}, {"t": 2}])
    path = storage.resolve(key)
    path.write_text('{"t": 3}\n{"t": 2}\n', encoding="utf-8")
    parsed: list[bytes] = []
    original_loads = storage.json.loads

    def observe_loads(payload: str | bytes | bytearray) -> Any:
        parsed.append(payload if isinstance(payload, bytes) else str(payload).encode())
        return original_loads(payload)

    monkeypatch.setattr(storage.json, "loads", observe_loads)
    with pytest.raises(storage.StorageError, match="does not match its recorded SHA-256"):
        storage.read_jsonl_verified(key, digest, expected_count=2)
    assert parsed == []


def test_malformed_jsonl_with_its_own_digest_is_not_accepted() -> None:
    from iluvtrade.common import storage

    key = "org/x/datasets/malformed/canonical.jsonl"
    malformed = b'{"t":\n'
    storage.resolve(key).parent.mkdir(parents=True, exist_ok=True)
    storage.resolve(key).write_bytes(malformed)

    with pytest.raises(ValueError):
        storage.read_jsonl_verified(key, storage.sha256(malformed), expected_count=1)


def test_verified_read_checks_row_count_before_parsing(monkeypatch) -> None:
    from iluvtrade.common import storage

    key = "org/x/datasets/count/canonical.jsonl"
    digest, _ = storage.write_jsonl(key, [{"t": 1}, {"t": 2}])
    parsed: list[bytes] = []
    original_loads = storage.json.loads

    def observe_loads(payload: str | bytes | bytearray) -> Any:
        parsed.append(payload if isinstance(payload, bytes) else str(payload).encode())
        return original_loads(payload)

    monkeypatch.setattr(storage.json, "loads", observe_loads)
    with pytest.raises(storage.StorageError, match="records 3 rows"):
        storage.read_jsonl_verified(key, digest, expected_count=3)
    assert parsed == []


def test_verified_read_handles_missing_file() -> None:
    from iluvtrade.common import storage

    with pytest.raises(storage.StorageError, match="No object"):
        storage.read_jsonl_verified("org/x/datasets/missing/canonical.jsonl", "0" * 64)


def test_large_verified_read_hashes_in_chunks_before_streaming_parse(monkeypatch) -> None:
    from iluvtrade.common import storage

    key = "org/x/datasets/large/canonical.jsonl"
    count = 12_000
    digest, written_count = storage.write_jsonl(
        key, ({"index": index, "payload": "x" * 128} for index in range(count))
    )
    assert written_count == count

    opened: list[dict[str, Any]] = []
    original_open = Path.open

    class TrackedFile:
        def __init__(self, handle: Any) -> None:
            self.handle = handle
            self.read_sizes: list[int] = []
            self.iterated = False

        def __enter__(self) -> TrackedFile:
            self.handle.__enter__()
            return self

        def __exit__(self, *args: Any) -> Any:
            return self.handle.__exit__(*args)

        def read(self, size: int = -1) -> bytes:
            self.read_sizes.append(size)
            return self.handle.read(size)

        def __iter__(self) -> TrackedFile:
            self.iterated = True
            return self

        def __next__(self) -> bytes:
            return next(self.handle)

    def track_open(path: Path, *args: Any, **kwargs: Any) -> TrackedFile:
        tracked = TrackedFile(original_open(path, *args, **kwargs))
        opened.append({"file": tracked, "mode": args[0] if args else kwargs.get("mode", "r")})
        return tracked

    monkeypatch.setattr(Path, "open", track_open)
    rows = storage.read_jsonl_verified(key, digest, expected_count=count)

    assert len(rows) == count
    assert rows[0]["index"] == 0 and rows[-1]["index"] == count - 1
    assert len(opened) == 2
    hashing = opened[0]["file"]
    parsing = opened[1]["file"]
    assert opened[0]["mode"] == opened[1]["mode"] == "rb"
    assert hashing.read_sizes and all(
        size == storage._JSONL_HASH_CHUNK_SIZE for size in hashing.read_sizes
    )
    assert not hashing.iterated
    assert parsing.iterated and parsing.read_sizes == []


@pytest.mark.parametrize("damage", ["truncated", "altered"])
def test_a_backtest_refuses_a_canonical_file_that_no_longer_matches(
    client, headers, damage: str
) -> None:
    from iluvtrade.backtests.worker import BacktestWorkerPool

    register(client, f"canon-{damage}@example.com")
    version_id, strategy_id = _approved(client, headers)
    path = _canonical_path(version_id)
    original = path.read_bytes()
    if damage == "truncated":
        path.write_bytes(original[: len(original) // 2])
    else:  # one close price changed, same length
        path.write_bytes(original.replace(b'"close": "', b'"close": "9', 1))

    job = client.post(
        "/api/v1/backtests",
        json={"dataset_version_id": version_id, "strategy_version_id": strategy_id, "seed": 1},
        headers=headers,
    ).json()
    BacktestWorkerPool(size=1).drain(timeout=120)
    finished = client.get(f"/api/v1/backtests/{job['id']}", headers=headers).json()
    assert finished["status"] == "failed", finished
    assert "does not match its recorded SHA-256" in finished["error_message"]

    # The intact file runs.
    path.write_bytes(original)
    rerun = client.post(
        "/api/v1/backtests",
        json={"dataset_version_id": version_id, "strategy_version_id": strategy_id, "seed": 2},
        headers=headers,
    ).json()
    BacktestWorkerPool(size=1).drain(timeout=120)
    assert client.get(f"/api/v1/backtests/{rerun['id']}", headers=headers).json()["status"] == (
        "completed"
    )


def test_a_paper_session_refuses_a_canonical_file_that_no_longer_matches(client, headers) -> None:
    from iluvtrade.trading.runner import RUNNER

    register(client, "canon-session@example.com")
    version_id, strategy_id = _approved(client, headers)
    created = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "tampered",
            "mode": "paper",
            "strategy_version_id": strategy_id,
            "dataset_version_id": version_id,
        },
        headers=headers,
    ).json()
    path = _canonical_path(version_id)
    # Altered, not truncated: still valid JSON Lines, so only the hash check can
    # tell. (A truncated file also fails to parse, which would hide a missing check.)
    original = path.read_bytes()
    altered = original.replace(b'"close": "', b'"close": "9', 1)
    assert altered != original
    path.write_bytes(altered)

    client.post(f"/api/v1/trading/sessions/{created['id']}/start", headers=headers)
    RUNNER.join(created["id"], timeout=120)
    state = client.get(f"/api/v1/trading/sessions/{created['id']}", headers=headers).json()
    assert state["status"] == "failed", state
    assert state["records_processed"] == 0
    assert "does not match its recorded SHA-256" in (state["failure_reason"] or ""), state
