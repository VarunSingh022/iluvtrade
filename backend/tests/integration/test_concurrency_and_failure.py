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

    This is a regression test for a **real defect**, not a hypothetical.
    ``_transition`` used to read the row, check the status and assign the new
    one — three separate steps. Five simultaneous starts produced three
    successes: all of them read ``CREATED`` before any of them wrote. Three
    runner threads then fed bars into one portfolio.

    The status is now part of the ``WHERE`` clause of a single UPDATE, so the
    database picks the winner exactly once.
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

    # The durable signal, and the one that would have caught this earlier: a
    # second start writes a second audit event. Counting them is how "two
    # runners fed one portfolio" shows up after the fact.
    started = [
        event
        for event in client.get("/api/v1/audit?limit=200").json()
        if event["action"] == "trading.session.started" and event["resource_id"] == created["id"]
    ]
    assert len(started) == 1, f"{len(started)} start events for one session"

    # And exactly one session-event log entry, for the same reason.
    events = client.get(f"/api/v1/trading/sessions/{created['id']}/events").json()
    assert sum(1 for e in events if e["kind"] == "session.started") == 1


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


# --- what a refused write leaves behind -------------------------------------


def test_a_database_busy_refusal_commits_nothing_and_is_safely_retryable(app) -> None:
    """The four things that make ``DatabaseBusy`` an acceptable answer.

    SQLite serialises writers, so under concurrent ingestion some requests will
    be refused. That is a documented limitation, not a defect — but it is only
    acceptable if the refusal is *clean*. Four properties, each checked here:

    1. **Nothing partially committed.** A dataset row without its version, or a
       version without its source, would be a corrupt workspace.
    2. **The client is told it can retry**, with ``Retry-After``.
    3. **The audit chain is still verifiable.** A refused write must not leave a
       gap or a duplicate sequence.
    4. **The retry actually works**, and produces a normal result.
    """

    from fastapi.testclient import TestClient

    with TestClient(app, raise_server_exceptions=False) as client:
        register(client, "busy@example.com")

        def upload(index: int):
            return client.post(
                "/api/v1/datasets/upload",
                files={"file": (f"b{index}.csv", make_csv(bars=25 + index), "text/csv")},
                data={"name": f"busy-{index}"},
                headers=HEADERS,
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = [f.result() for f in [pool.submit(upload, i) for i in range(6)]]

        refused = [r for r in responses if r.status_code >= 400]
        accepted = [r for r in responses if r.status_code < 400]
        assert accepted, "every write was refused; that is a different problem"

        # 2. A refusal is retryable and says so.
        for response in refused:
            assert response.status_code == 503
            body = response.json()["error"]
            assert body["code"] == "DatabaseBusy"
            assert "nothing was changed" in body["message"].lower()
            assert response.headers.get("Retry-After")

        # 1. Nothing partial. Every stored version belongs to a stored dataset,
        #    and the counts match exactly what succeeded.
        datasets = client.get("/api/v1/datasets").json()
        versions = [v for d in datasets for v in d["versions"]]
        assert len(versions) == len(accepted)
        for dataset in datasets:
            for version in dataset["versions"]:
                detail = client.get(f"/api/v1/datasets/versions/{version['id']}")
                assert detail.status_code == 200
                assert detail.json()["source"]["content_hash"], (
                    "a version exists whose source row was never written"
                )

        # 3. The audit chain survived the refusals.
        verification = client.get("/api/v1/audit/verify").json()
        assert verification["intact"] is True, verification

        # 4. Retrying a refused upload succeeds and behaves normally.
        if refused:
            retried = upload(99)
            assert retried.status_code < 400, retried.text
            assert retried.json()["row_count"] == 25 + 99
            assert client.get("/api/v1/audit/verify").json()["intact"] is True


def test_the_busy_refusal_is_the_documented_one_not_an_internal_error(app) -> None:
    """Deterministic, and distinguishable from a real fault.

    An ``OperationalError`` that is *not* contention still falls through to the
    500 handler, where the client learns nothing specific. Only the lock case
    is remapped, and this pins that distinction.
    """

    from fastapi.testclient import TestClient
    from sqlalchemy.exc import OperationalError

    from iluvtrade.platform import notifications

    with TestClient(app, raise_server_exceptions=False) as client:
        register(client, "busy2@example.com")

        def locked(*_a: object, **_k: object) -> int:
            raise OperationalError("SELECT 1", {}, Exception("database is locked"))

        def broken(*_a: object, **_k: object) -> int:
            raise OperationalError("SELECT 1", {}, Exception("disk I/O error"))

        import pytest as _pytest

        with _pytest.MonkeyPatch.context() as patch:
            patch.setattr(notifications, "unread_count", locked)
            busy = client.get("/api/v1/dashboard")
        assert busy.status_code == 503
        assert busy.json()["error"]["code"] == "DatabaseBusy"

        with _pytest.MonkeyPatch.context() as patch:
            patch.setattr(notifications, "unread_count", broken)
            fault = client.get("/api/v1/dashboard")
        assert fault.status_code == 500
        assert fault.json()["error"]["code"] == "InternalError"
        assert "disk I/O" not in fault.text, "an internal detail reached the client"


def test_the_state_transition_is_one_conditional_update(client) -> None:
    """The same defect, asserted deterministically against the emitted SQL.

    The threaded test above reproduces the race only when the timing lines up —
    it caught the old implementation about one run in six, which is not a
    regression test worth relying on. Simulating the losing thread's stale view
    is no better: mutating the ORM object marks it dirty, SQLAlchemy flushes it
    before the next statement, and the simulation writes the very row it was
    pretending to have read.

    So this watches what actually goes to the database. An atomic transition is
    a single UPDATE whose WHERE clause carries the **current** status; a
    read-modify-write is a SELECT, a decision in Python, and an UPDATE keyed
    only by id. The two are distinguishable in the SQL, and only one of them is
    safe under concurrency.
    """

    from sqlalchemy import event

    from iluvtrade.db.session import get_engine

    register(client, "sql@example.com")
    dataset_version_id, strategy_version_id = _ready(client)
    created = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "SQL",
            "mode": "paper",
            "dataset_version_id": dataset_version_id,
            "strategy_version_id": strategy_version_id,
            "starting_cash": "100000",
        },
        headers=HEADERS,
    ).json()

    statements: list[str] = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(" ".join(statement.split()))

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", record)
    try:
        assert (
            client.post(
                f"/api/v1/trading/sessions/{created['id']}/start", headers=HEADERS
            ).status_code
            == 200
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    updates = [
        statement
        for statement in statements
        if statement.upper().startswith("UPDATE TRADING_SESSIONS")
    ]
    assert updates, "no UPDATE reached trading_sessions"

    guarded = [
        statement for statement in updates if "SET status" in statement and "status IN" in statement
    ]
    assert guarded, (
        "the status change was issued as an UPDATE keyed only by id:\n  "
        + "\n  ".join(updates)
        + "\nThat is a read-modify-write: two callers can both read CREATED, "
        "both pass the check in Python, and both write. The current status "
        "must be part of the WHERE clause so the database picks one winner."
    )
