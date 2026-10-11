"""Fill timing: explicit, conservative by default, recorded, and actually applied.

AlphaLab offers two fill timings (``alphalab.execution.FillTiming``):
``NEXT_EVENT`` fills an order at its asset's next market event -- for bars, the
next bar -- and ``SAME_EVENT`` fills it at the event that produced it, which
AlphaLab calls optimistic by construction. These tests pin the semantics rather
than any P&L: *when* the fills happen, what is recorded, and that two timings
are two runs.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import make_csv, register
from tests.support import DECLARED

SAME_EVENT_SENTENCE = "FillTiming.SAME_EVENT"


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
        # Buys once, on the first bar it sees: the fill's bar is the whole test.
        json={"implementation_key": "buy_and_hold", "parameters": {"quantity": 1}},
        headers=headers,
    ).json()
    client.post(f"/api/v1/strategies/versions/{draft['id']}/publish", headers=headers)
    return version["id"], draft["id"]


def _submit(client: Any, headers: dict[str, str], dataset: str, strategy: str, **extra: Any) -> Any:
    return client.post(
        "/api/v1/backtests",
        json={"dataset_version_id": dataset, "strategy_version_id": strategy, **extra},
        headers=headers,
    )


def _result(client: Any, headers: dict[str, str], job_id: str) -> dict[str, Any]:
    from iluvtrade.backtests.worker import BacktestWorkerPool

    BacktestWorkerPool(size=1).drain(timeout=120)
    job = client.get(f"/api/v1/backtests/{job_id}").json()
    assert job["status"] == "completed", job
    return dict(client.get(f"/api/v1/backtests/{job_id}/result").json())


def _bar_timestamps(dataset_version_id: str) -> list[float]:
    from iluvtrade.common import storage
    from iluvtrade.db.models.data import DatasetVersion
    from iluvtrade.db.session import session_scope

    with session_scope() as session:
        version = session.get(DatasetVersion, dataset_version_id)
        assert version is not None and version.canonical_path
        return sorted(
            {float(row["timestamp"]) for row in storage.read_jsonl(version.canonical_path)}
        )


def test_the_default_is_next_event_and_it_is_recorded(client, headers) -> None:
    register(client, "default-timing@example.com")
    dataset, strategy = _ready(client, headers)
    document = _result(client, headers, _submit(client, headers, dataset, strategy).json()["id"])

    assert document["reproducibility"]["configuration"]["fill_timing"] == "next_event"
    applied = document["result"]["execution_assumptions"]
    assert applied["fill_timing"] == "next_event"
    assert not any(SAME_EVENT_SENTENCE in sentence for sentence in applied["optimistic"])


def test_same_event_only_when_asked_for_and_then_flagged_optimistic(client, headers) -> None:
    register(client, "same-timing@example.com")
    dataset, strategy = _ready(client, headers)
    job = _submit(client, headers, dataset, strategy, fill_timing="same_event").json()
    document = _result(client, headers, job["id"])

    assert document["reproducibility"]["configuration"]["fill_timing"] == "same_event"
    applied = document["result"]["execution_assumptions"]
    assert applied["fill_timing"] == "same_event"
    # AlphaLab's own sentence, not this application's paraphrase.
    assert any(SAME_EVENT_SENTENCE in sentence for sentence in applied["optimistic"])


def test_the_setting_reaches_the_simulator(client, headers) -> None:
    """Same data, same strategy: next-event fills land exactly one bar later."""

    register(client, "reaches@example.com")
    dataset, strategy = _ready(client, headers)
    bars = _bar_timestamps(dataset)
    same = _result(
        client,
        headers,
        _submit(client, headers, dataset, strategy, fill_timing="same_event").json()["id"],
    )
    nxt = _result(
        client,
        headers,
        _submit(client, headers, dataset, strategy, fill_timing="next_event").json()["id"],
    )

    same_fill = same["result"]["fills"][0]["timestamp"]
    next_fill = nxt["result"]["fills"][0]["timestamp"]
    assert same_fill == bars[0], "a same-event order fills at the bar that decided it"
    assert next_fill == bars[1], "a next-event order fills at the following bar"


def test_two_timings_are_two_runs_with_two_identities(client, headers) -> None:
    register(client, "identities@example.com")
    dataset, strategy = _ready(client, headers)
    first = _submit(client, headers, dataset, strategy, fill_timing="same_event", seed=7).json()
    second = _submit(client, headers, dataset, strategy, fill_timing="next_event", seed=7).json()
    again = _submit(client, headers, dataset, strategy, fill_timing="next_event", seed=7).json()

    assert first["id"] != second["id"] and not second["deduplicated"]
    assert again["id"] == second["id"] and again["deduplicated"]

    one = _result(client, headers, first["id"])["reproducibility"]["engine_identity"]
    two = _result(client, headers, second["id"])["reproducibility"]["engine_identity"]
    assert one["configuration_id"] != two["configuration_id"]
    assert one["result_id"] != two["result_id"]


def test_an_unknown_timing_is_refused_everywhere(client, headers) -> None:
    from iluvtrade.backtests import service
    from iluvtrade.backtests.requests import BacktestRequest
    from iluvtrade.db.session import session_scope
    from iluvtrade.platform import accounts

    register(client, "unknown-timing@example.com")
    dataset, strategy = _ready(client, headers)
    assert _submit(client, headers, dataset, strategy, fill_timing="eventually").status_code == 422

    with session_scope() as session:
        _, token = accounts.login(
            session, email="unknown-timing@example.com", password="correct-horse-battery-staple"
        )
        principal = accounts.resolve_principal(session, token)
        with pytest.raises(service.BacktestSubmissionError, match="eventually"):
            service.submit(
                session,
                principal,
                BacktestRequest(dataset, strategy, fill_timing="eventually"),
            )


def test_a_request_stored_before_timings_were_recorded_reads_back_as_what_ran() -> None:
    """Reproducing an old run under today's default would be a different run."""

    from iluvtrade.backtests.requests import BacktestRequest

    legacy = '{"dataset_version_id": "d", "strategy_version_id": "s", "seed": 1}'
    assert BacktestRequest.from_json(legacy).fill_timing == "same_event"
    assert BacktestRequest("d", "s").fill_timing == "next_event"
    stated = BacktestRequest("d", "s", fill_timing="next_event")
    assert BacktestRequest.from_json(stated.to_json()) == stated


def test_a_paper_session_states_its_timing_and_the_simulator_applies_it(client, headers) -> None:
    from iluvtrade.trading.runner import RUNNER

    register(client, "paper-timing@example.com")
    dataset, strategy = _ready(client, headers)
    bars = _bar_timestamps(dataset)
    created = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "Timed",
            "mode": "paper",
            "strategy_version_id": strategy,
            "dataset_version_id": dataset,
            "seed": 3,
        },
        headers=headers,
    ).json()
    assert created["fill_timing"] == "next_event"

    client.post(f"/api/v1/trading/sessions/{created['id']}/start", headers=headers)
    RUNNER.join(created["id"], timeout=180)
    fills = client.get(f"/api/v1/trading/sessions/{created['id']}/fills").json()
    assert fills[0]["fill_timestamp"] == bars[1]

    events = client.get(f"/api/v1/trading/sessions/{created['id']}/events").json()
    stated = [event for event in events if event["kind"] == "execution_assumptions"]
    assert len(stated) == 1 and "next_event" in stated[0]["message"]

    refused = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "Bad",
            "mode": "paper",
            "strategy_version_id": strategy,
            "dataset_version_id": dataset,
            "fill_timing": "whenever",
        },
        headers=headers,
    )
    assert refused.status_code == 422


def test_each_backtest_keeps_its_own_result_document(client, headers) -> None:
    """Rules out: every run in an organization sharing one artifact file.

    The run id used to be ``None`` when the artifact key was built, so each run
    overwrote ``backtests/None/result.json`` and an older run's page served the
    newest run's document. Found by the identity test above.
    """

    from sqlalchemy import select

    from iluvtrade.db.models.backtest import BacktestRun
    from iluvtrade.db.session import session_scope

    register(client, "own-document@example.com")
    dataset, strategy = _ready(client, headers)
    first = _submit(client, headers, dataset, strategy, fill_timing="same_event").json()
    second = _submit(client, headers, dataset, strategy, fill_timing="next_event").json()
    documents = [_result(client, headers, job["id"]) for job in (first, second)]

    for document in documents:
        stated = document["reproducibility"]["configuration"]["fill_timing"]
        assert document["result"]["execution_assumptions"]["fill_timing"] == stated
    with session_scope() as session:
        paths = {
            run.id: run.artifact_path for run in session.execute(select(BacktestRun)).scalars()
        }
    assert len(set(paths.values())) == len(paths) == 2
    assert all(run_id in path for run_id, path in paths.items())


def test_a_run_whose_engine_timing_disagrees_with_the_request_is_refused(
    client, headers, monkeypatch
) -> None:
    """If the setting ever failed to reach AlphaLab, nothing would be stored as if it had."""

    from iluvtrade.alphalab_bridge import runconfig
    from iluvtrade.backtests.worker import BacktestWorkerPool
    from iluvtrade.trading.runner import RUNNER

    register(client, "disagree@example.com")
    dataset, strategy = _ready(client, headers)
    swapped = {"next_event": runconfig.FILL_TIMINGS["same_event"]}
    real = runconfig.fill_timing_for
    monkeypatch.setattr(runconfig, "fill_timing_for", lambda name: swapped.get(name) or real(name))

    job = _submit(client, headers, dataset, strategy).json()
    BacktestWorkerPool(size=1).drain(timeout=120)
    failed = client.get(f"/api/v1/backtests/{job['id']}").json()
    assert failed["status"] == "failed"
    assert "AlphaLab recorded 'same_event'" in failed["error_message"]
    assert client.get(f"/api/v1/backtests/{job['id']}/result").status_code == 400

    session = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "Mismatched",
            "mode": "paper",
            "strategy_version_id": strategy,
            "dataset_version_id": dataset,
        },
        headers=headers,
    ).json()
    client.post(f"/api/v1/trading/sessions/{session['id']}/start", headers=headers)
    RUNNER.join(session["id"], timeout=120)
    state = client.get(f"/api/v1/trading/sessions/{session['id']}").json()
    assert state["status"] == "failed"
    assert "configured with 'same_event'" in state["failure_reason"]
    assert state["records_processed"] == 0
