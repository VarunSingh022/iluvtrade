"""Each backtest's detailed result is its own, and is only ever served as its own.

Until 2026-10-11 the artifact key was built before the run had an id, so every
run in an organization wrote ``backtests/None/result.json``. The key is now the
run's own, the document names its run, and the row records the artifact's
digest; retrieval checks all three and otherwise serves only the headline
metrics recorded on the run, with the reason.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.conftest import make_csv, register
from tests.support import DECLARED


def _ready(client: Any, headers: dict[str, str]) -> tuple[str, str]:
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


def _run(client: Any, headers: dict[str, str], dataset: str, strategy: str, key: str) -> str:
    from iluvtrade.backtests.worker import BacktestWorkerPool

    job = client.post(
        "/api/v1/backtests",
        json={
            "dataset_version_id": dataset,
            "strategy_version_id": strategy,
            "seed": 1,
            "idempotency_key": key,  # an identical request, deliberately run twice
        },
        headers=headers,
    ).json()
    BacktestWorkerPool(size=1).drain(timeout=120)
    assert client.get(f"/api/v1/backtests/{job['id']}").json()["status"] == "completed"
    return str(job["id"])


def _row(job_id: str) -> Any:
    from sqlalchemy import select

    from iluvtrade.db.models.backtest import BacktestRun
    from iluvtrade.db.session import session_scope

    with session_scope() as session:
        return session.execute(select(BacktestRun).where(BacktestRun.job_id == job_id)).scalar_one()


def _result(client: Any, headers: dict[str, str], job_id: str) -> dict[str, Any]:
    return dict(client.get(f"/api/v1/backtests/{job_id}/result", headers=headers).json())


def test_an_older_result_is_still_its_own_after_a_newer_identical_run(client, headers) -> None:
    register(client, "older@example.com")
    dataset, strategy = _ready(client, headers)
    older = _run(client, headers, dataset, strategy, "first")
    newer = _run(client, headers, dataset, strategy, "second")

    older_row, newer_row = _row(older), _row(newer)
    assert older_row.artifact_path != newer_row.artifact_path
    assert older_row.id in older_row.artifact_path and newer_row.id in newer_row.artifact_path

    document = _result(client, headers, older)
    assert document["result_integrity"]["verified"] is True
    assert document["result"]["run_id"] == older_row.id == document["run_id"]
    assert document["result"]["job_id"] == older
    assert _result(client, headers, newer)["result"]["run_id"] == newer_row.id


def test_a_document_that_is_not_the_runs_own_is_never_served(client, headers) -> None:
    from sqlalchemy import update

    from iluvtrade.common import storage
    from iluvtrade.db.models.backtest import BacktestRun
    from iluvtrade.db.session import session_scope

    register(client, "swapped@example.com")
    dataset, strategy = _ready(client, headers)
    older = _run(client, headers, dataset, strategy, "a")
    newer = _run(client, headers, dataset, strategy, "b")
    older_row, newer_row = _row(older), _row(newer)

    # Another run's bytes at this run's key: the digest refuses them.
    storage.write_bytes(older_row.artifact_path, storage.read_bytes(newer_row.artifact_path))
    refused = _result(client, headers, older)
    assert refused["result"] is None
    assert refused["result_integrity"] == {
        "verified": False,
        "reason": refused["result_integrity"]["reason"],
    }
    assert "do not match the digest" in refused["result_integrity"]["reason"]
    assert refused["metrics"]["fill_count"] == older_row.fill_count  # headline from the row

    # Bytes whose digest matches but which name another run.
    foreign = {**storage.read_json(newer_row.artifact_path)}
    digest = storage.write_json(older_row.artifact_path, foreign)
    with session_scope() as session:
        session.execute(
            update(BacktestRun).where(BacktestRun.id == older_row.id).values(artifact_sha256=digest)
        )
    assert "names a different run" in _result(client, headers, older)["result_integrity"]["reason"]

    # Missing altogether.
    storage.resolve(older_row.artifact_path).unlink()
    assert "missing" in _result(client, headers, older)["result_integrity"]["reason"]


def test_a_run_stored_before_results_were_keyed_by_run_is_shown_as_unverified(
    client, headers
) -> None:
    from sqlalchemy import update

    from iluvtrade.common import storage
    from iluvtrade.db.models.backtest import BacktestRun
    from iluvtrade.db.session import session_scope

    register(client, "legacy-result@example.com")
    dataset, strategy = _ready(client, headers)
    job = _run(client, headers, dataset, strategy, "legacy")
    row = _row(job)
    shared = f"org/{row.organization_id}/backtests/None/result.json"
    storage.write_bytes(shared, storage.read_bytes(row.artifact_path))
    with session_scope() as session:  # what a pre-0007 row looks like
        session.execute(
            update(BacktestRun)
            .where(BacktestRun.id == row.id)
            .values(artifact_path=shared, artifact_sha256=None)
        )
    document = _result(client, headers, job)
    assert document["result"] is None
    assert "every run in its organization shared" in document["result_integrity"]["reason"]
    assert document["metrics"]["fill_count"] == row.fill_count
    assert document["reproducibility"]["seed"] == row.seed


def test_a_failed_save_leaves_nothing_that_can_be_served(client, headers, monkeypatch) -> None:
    """An artifact written before the save failed is orphaned, never served as a result."""

    from iluvtrade.backtests import service
    from iluvtrade.backtests.worker import BacktestWorkerPool

    register(client, "partial@example.com")
    dataset, strategy = _ready(client, headers)
    real_notify = service.notifications.notify

    def failing_notify(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("kind") == "backtest.completed":
            raise RuntimeError("the save failed after the artifact was written")
        return real_notify(*args, **kwargs)

    monkeypatch.setattr(service.notifications, "notify", failing_notify)
    job = client.post(
        "/api/v1/backtests",
        json={"dataset_version_id": dataset, "strategy_version_id": strategy, "seed": 3},
        headers=headers,
    ).json()
    BacktestWorkerPool(size=1).drain(timeout=120)
    assert client.get(f"/api/v1/backtests/{job['id']}").json()["status"] == "failed"
    assert client.get(f"/api/v1/backtests/{job['id']}/result").status_code == 400

    monkeypatch.setattr(service.notifications, "notify", real_notify)
    again = _run(client, headers, dataset, strategy, "after-failure")
    assert _result(client, headers, again)["result_integrity"]["verified"] is True


def test_another_organization_cannot_read_a_result(client, headers, app) -> None:
    from fastapi.testclient import TestClient

    register(client, "owner@example.com")
    dataset, strategy = _ready(client, headers)
    job = _run(client, headers, dataset, strategy, "mine")
    with TestClient(app) as other:
        register(other, "stranger@example.com")
        assert other.get(f"/api/v1/backtests/{job}/result").status_code == 404


def test_storage_writes_are_atomic(monkeypatch) -> None:
    from iluvtrade.common import storage

    storage.write_json("org/x/backtests/r/result.json", {"run_id": "r", "fills": [1, 2, 3]})
    path = storage.resolve("org/x/backtests/r/result.json")

    def interrupted(source: str, target: object) -> None:
        raise OSError("disk full at the worst moment")

    monkeypatch.setattr(storage.os, "replace", interrupted)
    with pytest.raises(OSError):
        storage.write_json("org/x/backtests/r/result.json", {"run_id": "r", "fills": []})
    # The previous object is whole, and no temporary file is left behind.
    assert json.loads(path.read_text()) == {"run_id": "r", "fills": [1, 2, 3]}
    assert [p.name for p in path.parent.iterdir()] == ["result.json"]
