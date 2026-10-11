"""Run seeds: derived the same way in every process, stored once, reused everywhere.

AlphaLab takes the seed as an input and reports it beside the run's
configuration and dataset identities (``lifecycle.digest_run``), so a change to
any one is attributable. These tests pin the application's half: a derived seed
never depends on Python's per-process ``hash()``, it is persisted before
anything runs, and every retry or reconstruction reads that stored value.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from tests.conftest import make_csv, register
from tests.support import DECLARED

BACKEND = Path(__file__).resolve().parents[2]


def _in_fresh_process(code: str, *, hash_seed: str) -> str:
    """Run ``code`` in a new interpreter with a fixed ``PYTHONHASHSEED``."""

    env = dict(os.environ, PYTHONHASHSEED=hash_seed)
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return done.stdout.strip()


def _dataset(client: Any, headers: dict[str, str], *, bars: int = 30) -> str:
    version = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=bars), "text/csv")},
        data=DECLARED,
        headers=headers,
    ).json()
    client.post(f"/api/v1/datasets/versions/{version['id']}/approve", headers=headers)
    return str(version["id"])


def _ready(client: Any, headers: dict[str, str]) -> tuple[str, str]:
    dataset = _dataset(client, headers)
    strategy = client.post("/api/v1/strategies", json={"name": "Hold"}, headers=headers).json()
    draft = client.post(
        f"/api/v1/strategies/{strategy['id']}/versions",
        json={"implementation_key": "buy_and_hold", "parameters": {"quantity": 1}},
        headers=headers,
    ).json()
    client.post(f"/api/v1/strategies/versions/{draft['id']}/publish", headers=headers)
    return dataset, draft["id"]


def _session(
    client: Any, headers: dict[str, str], dataset: str, strategy: str, **extra: Any
) -> Any:
    return client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "Seeded",
            "mode": "paper",
            "strategy_version_id": strategy,
            "dataset_version_id": dataset,
            **extra,
        },
        headers=headers,
    ).json()


# --- the derivation ------------------------------------------------------------


def test_the_derivation_is_the_documented_digest() -> None:
    from iluvtrade.common.seeds import derive_seed

    inputs = {"b": "2", "a": 1}
    text = '{"a":1,"b":"2"}'  # sorted keys, compact separators
    assert derive_seed(inputs) == int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
    assert 0 <= derive_seed(inputs) < 2**32


def test_derived_seeds_are_identical_in_separate_processes() -> None:
    code = (
        "from iluvtrade.common.seeds import derive_seed;"
        "from iluvtrade.backtests.requests import BacktestRequest;"
        "print(derive_seed({'strategy_version_id': 'v1', 'dataset_version_id': 'd1'}),"
        " BacktestRequest('d1', 'v1').effective_seed())"
    )
    outputs = {_in_fresh_process(code, hash_seed=seed) for seed in ("1", "2", "random")}
    assert len(outputs) == 1, outputs

    # The control: Python's own hash() is what would differ.
    control = "print(hash('the strategy version id'))"
    assert _in_fresh_process(control, hash_seed="1") != _in_fresh_process(control, hash_seed="2")


def test_a_session_seed_follows_its_material_inputs(client, headers) -> None:
    """Same inputs, same seed; a different dataset or fill timing, a different one."""

    register(client, "session-seed@example.com")
    dataset, strategy = _ready(client, headers)
    other_dataset = _dataset(client, headers, bars=40)

    first = _session(client, headers, dataset, strategy)
    again = _session(client, headers, dataset, strategy)
    other_data = _session(client, headers, other_dataset, strategy)
    other_timing = _session(client, headers, dataset, strategy, fill_timing="same_event")
    stated = _session(client, headers, dataset, strategy, seed=2**32 - 1)

    assert first["seed"] == again["seed"]
    assert first["seed"] != other_data["seed"]
    assert first["seed"] != other_timing["seed"]
    assert stated["seed"] == 2**32 - 1
    assert 0 <= first["seed"] < 2**32


# --- stored once, reused ----------------------------------------------------------


def test_a_paper_session_reuses_its_stored_seed_on_every_run(client, headers, monkeypatch) -> None:
    """Run, then reconstructed from the database (here and in another process)."""

    from sqlalchemy import update

    from iluvtrade.alphalab_bridge import runconfig
    from iluvtrade.db.models.trading import SessionStatus, TradingSession
    from iluvtrade.db.session import session_scope
    from iluvtrade.trading.runner import RUNNER, run_session

    seen: list[int] = []
    real = runconfig.build_run_config

    def spy(**kwargs: Any) -> Any:
        seen.append(kwargs["seed"])
        return real(**kwargs)

    monkeypatch.setattr(runconfig, "build_run_config", spy)

    register(client, "reuse@example.com")
    dataset, strategy = _ready(client, headers)
    created = _session(client, headers, dataset, strategy)
    client.post(f"/api/v1/trading/sessions/{created['id']}/start", headers=headers)
    RUNNER.join(created["id"], timeout=180)

    # Reconstruct from persisted state: as a restart after a crash, or a resume
    # (which today replays from the first record), would.
    with session_scope() as session:
        session.execute(
            update(TradingSession)
            .where(TradingSession.id == created["id"])
            .values(status=SessionStatus.STARTING)
        )
    from iluvtrade.common.observability import current_context

    before = current_context()
    run_session(created["id"])
    # Driven synchronously, the session leaves the caller's logging context as it
    # found it (its organization and mode fields used to stay set).
    assert current_context() == before

    assert seen == [created["seed"], created["seed"]]

    code = f"from iluvtrade.trading.runner import _load;print(_load({created['id']!r}).seed)"
    assert _in_fresh_process(code, hash_seed="7") == str(created["seed"])


def test_a_retried_backtest_reuses_the_seed_stored_at_submission(
    client, headers, monkeypatch
) -> None:
    """A job whose worker died is requeued and runs with the seed it was stored with."""

    from sqlalchemy import select

    from iluvtrade.alphalab_bridge import runconfig
    from iluvtrade.backtests import service
    from iluvtrade.backtests.requests import BacktestRequest
    from iluvtrade.backtests.worker import BacktestWorkerPool, requeue_orphans
    from iluvtrade.db.models.backtest import BacktestJob, BacktestRun
    from iluvtrade.db.session import session_scope

    seen: list[int] = []
    real = runconfig.build_run_config

    def spy(**kwargs: Any) -> Any:
        seen.append(kwargs["seed"])
        return real(**kwargs)

    monkeypatch.setattr(runconfig, "build_run_config", spy)
    # Retries are off by default (one attempt; a lost worker fails the job with
    # WorkerLost). An operator who allows them gets this behaviour.
    from iluvtrade.config import get_settings

    monkeypatch.setenv("ILUVTRADE_BACKTEST_MAX_ATTEMPTS", "2")
    get_settings.cache_clear()

    register(client, "retry@example.com")
    dataset, strategy = _ready(client, headers)
    job = client.post(
        "/api/v1/backtests",
        json={"dataset_version_id": dataset, "strategy_version_id": strategy},
        headers=headers,
    ).json()

    with session_scope() as session:
        stored = json.loads(session.get(BacktestJob, job["id"]).request_json)
        # A crash after claiming: the job is RUNNING, nothing ran.
        assert service.claim_next(session).id == job["id"]
    assert stored["seed"] is not None, "the derived seed is stored, not re-derived later"
    assert requeue_orphans() == 1
    BacktestWorkerPool(size=1).drain(timeout=120)

    with session_scope() as session:
        run = session.execute(
            select(BacktestRun).where(BacktestRun.job_id == job["id"])
        ).scalar_one()
        attempts = session.get(BacktestJob, job["id"]).attempts
    assert attempts == 2
    assert seen == [stored["seed"]]
    assert run.seed == stored["seed"]

    code = (
        "from iluvtrade.backtests.requests import BacktestRequest;"
        f"print(BacktestRequest.from_json({json.dumps(json.dumps(stored))}).effective_seed())"
    )
    assert _in_fresh_process(code, hash_seed="11") == str(stored["seed"])
    assert BacktestRequest.from_json(json.dumps(stored)).effective_seed() == stored["seed"]


# --- AlphaLab's contract: seed, dataset and configuration are separate ----------


def test_seed_dataset_and_configuration_changes_are_each_attributable(client, headers) -> None:
    from iluvtrade.backtests.worker import BacktestWorkerPool

    register(client, "attributable@example.com")
    dataset, strategy = _ready(client, headers)
    other_dataset = _dataset(client, headers, bars=40)

    def run(**request: Any) -> dict[str, Any]:
        job = client.post(
            "/api/v1/backtests",
            json={"dataset_version_id": dataset, "strategy_version_id": strategy, **request},
            headers=headers,
        ).json()
        BacktestWorkerPool(size=1).drain(timeout=120)
        result = client.get(f"/api/v1/backtests/{job['id']}/result").json()
        return dict(result["reproducibility"]["engine_identity"])

    base = run(seed=5)
    costlier = run(seed=5, commission_rate="0.001")
    reseeded = run(seed=6)
    moved = run(seed=5, dataset_version_id=other_dataset)

    assert (
        costlier["seed"] == base["seed"]
        and costlier["configuration_id"] != base["configuration_id"]
    )
    assert (
        reseeded["seed"] != base["seed"]
        and reseeded["configuration_id"] == base["configuration_id"]
    )
    assert moved["dataset_id"] != base["dataset_id"] and moved["seed"] == base["seed"]
    assert (
        len({base["result_id"], costlier["result_id"], reseeded["result_id"], moved["result_id"]})
        == 4
    )


def test_the_seed_survives_request_persistence_execution_and_result(client, headers) -> None:
    from sqlalchemy import select

    from iluvtrade.backtests.worker import BacktestWorkerPool
    from iluvtrade.db.models.backtest import BacktestJob, BacktestRun
    from iluvtrade.db.session import session_scope

    register(client, "survives@example.com")
    dataset, strategy = _ready(client, headers)
    seed = 2**32 - 1
    job = client.post(
        "/api/v1/backtests",
        json={"dataset_version_id": dataset, "strategy_version_id": strategy, "seed": seed},
        headers=headers,
    ).json()
    BacktestWorkerPool(size=1).drain(timeout=120)
    result = client.get(f"/api/v1/backtests/{job['id']}/result").json()

    with session_scope() as session:
        stored = json.loads(session.get(BacktestJob, job["id"]).request_json)["seed"]
        run = session.execute(
            select(BacktestRun).where(BacktestRun.job_id == job["id"])
        ).scalar_one()
    assert stored == run.seed == seed
    assert result["reproducibility"]["seed"] == seed
    assert result["reproducibility"]["engine_identity"]["seed"] == seed
    assert result["result"]["seed"] == seed
