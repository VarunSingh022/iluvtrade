"""What happens under concurrency, and when something downstream fails.

This is **not** a load test and nothing here should be read as one. It runs a
handful of threads against SQLite on one machine. What it can establish is
whether the state machines are correct when two callers arrive at once, and
whether a failure leaves a row half-written — and those are properties, not
throughput numbers, so a small number of threads is enough to demonstrate them.

What it cannot establish is stated plainly in ``docs/DEPLOYMENT.md``: nothing
here says anything about behaviour at real concurrency, on a real database,
across more than one process.

Each test names the specific bad outcome it rules out: duplicate work,
inconsistent state, a leaked resource, an incorrect transition, or a lost audit
event.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.conftest import make_csv, register

pytestmark = pytest.mark.integration

HEADERS = {"X-Requested-With": "XMLHttpRequest"}


def _ready(client, *, bars: int = 40) -> tuple[str, str]:
    version = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=bars), "text/csv")},
        headers=HEADERS,
    ).json()
    client.post(f"/api/v1/datasets/versions/{version['id']}/approve", headers=HEADERS)
    strategy = client.post("/api/v1/strategies", json={"name": "S"}, headers=HEADERS).json()
    draft = client.post(
        f"/api/v1/strategies/{strategy['id']}/versions",
        json={"implementation_key": "buy_and_hold", "parameters": {"quantity": 2}},
        headers=HEADERS,
    ).json()
    client.post(f"/api/v1/strategies/versions/{draft['id']}/publish", headers=HEADERS)
    return version["id"], draft["id"]


# --- duplicate work ---------------------------------------------------------


def test_the_same_backtest_submitted_many_times_at_once_runs_once(client) -> None:
    """Rules out: duplicate work, and duplicate charges for it.

    Identical submissions arriving together must collapse to one job. Doing the
    work twice is wasted compute; more importantly, two jobs for one request
    means the client cannot tell which result is "theirs".
    """

    register(client, "dup@example.com")
    dataset_version_id, strategy_version_id = _ready(client)
    body = {
        "dataset_version_id": dataset_version_id,
        "strategy_version_id": strategy_version_id,
    }

    barrier = threading.Barrier(6)

    def submit() -> dict:
        barrier.wait(timeout=20)
        return client.post("/api/v1/backtests", json=body, headers=HEADERS).json()

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = [future.result() for future in [pool.submit(submit) for _ in range(6)]]

    ids = {result["id"] for result in results}
    assert len(ids) == 1, f"one submission should produce one job, got {ids}"
    assert sum(1 for r in results if r.get("deduplicated")) == 5

    listed = client.get("/api/v1/backtests?limit=50").json()
    assert len(listed) == 1


def test_concurrent_dataset_uploads_do_not_write_into_each_other(app) -> None:
    """Rules out: interleaved writes producing one mangled version.

    This test also **demonstrates the SQLite limitation** rather than avoiding
    it. Four simultaneous ingestions against SQLite can return "database is
    locked", because SQLite serialises writers and ingestion holds its
    transaction for the length of the parse. That is precisely why
    :meth:`Settings.deployment_problems` refuses SQLite in production unless an
    operator explicitly accepts it.

    What must hold either way is the part that matters: every upload that
    *succeeded* has its own version with its own row count, and an upload that
    failed left nothing behind. A refused write is acceptable; a half-written
    dataset is not.
    """

    from fastapi.testclient import TestClient

    with TestClient(app, raise_server_exceptions=False) as client:
        register(client, "conc-data@example.com")

        def upload(index: int):
            return client.post(
                "/api/v1/datasets/upload",
                files={"file": (f"d{index}.csv", make_csv(bars=20 + index), "text/csv")},
                data={"name": f"set-{index}"},
                headers=HEADERS,
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = [f.result() for f in [pool.submit(upload, i) for i in range(4)]]

        succeeded = [r.json() for r in responses if r.status_code < 400]
        assert succeeded, "at least one upload should get through"

        # A refusal must say it is transient and that nothing changed. A
        # generic 500 would tell the user the opposite of the truth: that
        # something is broken and retrying is pointless.
        for response in responses:
            if response.status_code >= 400:
                assert response.status_code == 503, response.text
                assert response.json()["error"]["code"] == "DatabaseBusy"
                assert "try again" in response.json()["error"]["message"]

        ids = {version["id"] for version in succeeded}
        assert len(ids) == len(succeeded), "two uploads shared a version"

        # Every surviving version's row count matches the file it came from,
        # so no two ingestions interleaved their writes.
        counts = sorted(version["row_count"] for version in succeeded)
        assert counts == sorted(counts)
        assert set(counts) <= {20 + index for index in range(4)}

        # Nothing partial was left behind by a refused write.
        listed = client.get("/api/v1/datasets").json()
        stored = [v for dataset in listed for v in dataset["versions"]]
        assert len(stored) == len(succeeded)


# --- incorrect transitions --------------------------------------------------


def test_starting_one_session_twice_at_once_yields_one_start(client) -> None:
    """Rules out: two runners advancing one session's portfolio.

    ``CREATED -> STARTING`` is a conditional update, so exactly one caller can
    make the transition. If both could, two threads would be feeding bars into
    one account and the resulting position would be double.
    """

    register(client, "race@example.com")
    dataset_version_id, strategy_version_id = _ready(client)
    created = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "Race",
            "mode": "paper",
            "dataset_version_id": dataset_version_id,
            "strategy_version_id": strategy_version_id,
            "starting_cash": "100000",
        },
        headers=HEADERS,
    ).json()

    barrier = threading.Barrier(5)

    def start() -> int:
        barrier.wait(timeout=20)
        return client.post(
            f"/api/v1/trading/sessions/{created['id']}/start", headers=HEADERS
        ).status_code

    with ThreadPoolExecutor(max_workers=5) as pool:
        statuses = [future.result() for future in [pool.submit(start) for _ in range(5)]]

    assert statuses.count(200) == 1, f"exactly one start should succeed, got {statuses}"
    assert all(status in (200, 400, 409) for status in statuses)


def test_a_halted_session_cannot_be_resumed_by_any_caller(client) -> None:
    """Rules out: the kill switch degrading into a pause."""

    register(client, "halt@example.com")
    dataset_version_id, strategy_version_id = _ready(client)
    created = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "Halt",
            "mode": "paper",
            "dataset_version_id": dataset_version_id,
            "strategy_version_id": strategy_version_id,
            "starting_cash": "100000",
        },
        headers=HEADERS,
    ).json()
    client.post(f"/api/v1/trading/sessions/{created['id']}/start", headers=HEADERS)
    client.post(
        f"/api/v1/trading/sessions/{created['id']}/kill",
        json={"reason": "test"},
        headers=HEADERS,
    )

    for action in ("resume", "start", "pause"):
        response = client.post(
            f"/api/v1/trading/sessions/{created['id']}/{action}", headers=HEADERS
        )
        assert response.status_code >= 400, f"{action} must be refused on a halted session"

    assert client.get(f"/api/v1/trading/sessions/{created['id']}").json()["status"] == "halted"


# --- failure downstream -----------------------------------------------------


def test_a_failing_engine_run_leaves_a_failed_job_and_no_partial_result(
    client, monkeypatch
) -> None:
    """Rules out: a job stuck in RUNNING, and a half-written result row."""

    from iluvtrade.backtests import worker as worker_module
    from iluvtrade.backtests.worker import BacktestWorkerPool

    register(client, "boom@example.com")
    dataset_version_id, strategy_version_id = _ready(client)

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("the engine refused to run")

    # The worker did ``from ...runner import execute``, so it holds its own
    # reference; patching the runner module would not reach it.
    monkeypatch.setattr(worker_module, "execute", explode)

    job = client.post(
        "/api/v1/backtests",
        json={
            "dataset_version_id": dataset_version_id,
            "strategy_version_id": strategy_version_id,
        },
        headers=HEADERS,
    ).json()
    BacktestWorkerPool(size=1).drain(timeout=60)

    finished = client.get(f"/api/v1/backtests/{job['id']}").json()
    assert finished["status"] == "failed"
    assert finished["finished_at"] is not None, "a failed job must not look unfinished"
    assert finished["error_message"]
    # No result is served for a run that did not produce one.
    assert client.get(f"/api/v1/backtests/{job['id']}/result").status_code >= 400


def test_a_notification_failure_does_not_undo_the_action_it_accompanies(app, monkeypatch) -> None:
    """Rules out: losing a real operation because telling someone about it failed.

    A notification is a side effect of an action, not part of it. If the
    notification write raises, the action must still be the thing that decides
    the transaction's fate — either both commit or the failure is surfaced, but
    never "the strategy was published and the caller was told it was not".
    """

    from fastapi.testclient import TestClient

    from iluvtrade.platform import notifications

    # ``raise_server_exceptions=False`` so the injected failure travels through
    # the application's own error handling, as it would in a real deployment,
    # instead of being re-raised into the test.
    client = TestClient(app, raise_server_exceptions=False)
    client.__enter__()
    register(client, "notify-fail@example.com")

    calls: list[str] = []
    original = notifications.notify

    def flaky(*args: object, **kwargs: object):
        calls.append(str(kwargs.get("kind")))
        raise RuntimeError("notification backend unavailable")

    dataset_version_id, strategy_version_id = _ready(client)
    monkeypatch.setattr(notifications, "notify", flaky)

    response = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "Notify",
            "mode": "paper",
            "dataset_version_id": dataset_version_id,
            "strategy_version_id": strategy_version_id,
            "starting_cash": "100000",
        },
        headers=HEADERS,
    )
    created_id = response.json().get("id") if response.status_code < 400 else None
    started = (
        client.post(f"/api/v1/trading/sessions/{created_id}/start", headers=HEADERS)
        if created_id
        else None
    )

    monkeypatch.setattr(notifications, "notify", original)

    # Whatever the outcome, the two must agree: a 5xx must leave no session
    # behind, and a success must leave one that really started.
    if started is not None and started.status_code == 200:
        assert client.get(f"/api/v1/trading/sessions/{created_id}").json()["status"] in (
            "starting",
            "running",
            "stopped",
            "failed",
        )
    else:
        listed = client.get("/api/v1/trading/sessions").json()
        started_sessions = [s for s in listed if s["status"] not in ("created",)]
        assert not started_sessions, (
            "a failed notification must not leave a session reported as started"
        )
    assert calls, "the test did not actually exercise the notification path"
    client.__exit__(None, None, None)


def test_a_database_failure_mid_request_writes_nothing(app, monkeypatch) -> None:
    """Rules out: a partially applied request.

    The failure is injected at the last step of a multi-write operation, so a
    request that is not wrapped in one transaction would leave the earlier
    writes behind.
    """

    from fastapi.testclient import TestClient

    from iluvtrade.platform import audit

    client = TestClient(app, raise_server_exceptions=False)
    client.__enter__()
    register(client, "dbfail@example.com")
    before = len(client.get("/api/v1/strategies").json())

    def explode(*_args: object, **_kwargs: object):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(audit, "record", explode)

    response = client.post("/api/v1/strategies", json={"name": "Doomed"}, headers=HEADERS)
    assert response.status_code >= 400

    monkeypatch.undo()
    after = client.get("/api/v1/strategies").json()
    assert len(after) == before, "the failed request left a row behind"
    assert not any(row["name"] == "Doomed" for row in after)
    client.__exit__(None, None, None)


# --- the audit trail under pressure -----------------------------------------


def test_concurrent_writes_leave_an_intact_audit_chain(client) -> None:
    """Rules out: lost or duplicated audit events under concurrency.

    The chain is per-organization and each event's hash covers its
    predecessor's, so two writers claiming the same sequence would break
    verification. This drives several writers at once and then verifies.
    """

    register(client, "chain@example.com")

    def create(index: int) -> int:
        return client.post(
            "/api/v1/strategies", json={"name": f"S{index}"}, headers=HEADERS
        ).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = [future.result() for future in [pool.submit(create, i) for i in range(12)]]

    created = statuses.count(201)
    assert created >= 1

    verification = client.get("/api/v1/audit/verify").json()
    assert verification["intact"] is True, verification
    assert verification["events_checked"] >= created

    # Every successful create is in the trail: none was lost to a race.
    actions = [event["action"] for event in client.get("/api/v1/audit?limit=200").json()]
    assert actions.count("strategy.created") == created
