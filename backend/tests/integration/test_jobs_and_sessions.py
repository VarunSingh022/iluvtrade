"""Background work: state transitions, idempotency, duplicate prevention.

Covers both asynchronous paths — the backtest queue and the trading-session
runner — because they have the same failure modes and different implementations.
"""

from __future__ import annotations

import pytest

from iluvtrade.db.models.backtest import BacktestJob, JobStatus
from iluvtrade.db.models.trading import SessionStatus
from tests.conftest import make_csv, register

pytestmark = pytest.mark.integration


def _ready(client, headers, *, bars: int = 60) -> tuple[str, str]:
    version = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=bars), "text/csv")},
        headers=headers,
    ).json()
    client.post(f"/api/v1/datasets/versions/{version['id']}/approve", headers=headers)
    strategy = client.post("/api/v1/strategies", json={"name": "S"}, headers=headers).json()
    draft = client.post(
        f"/api/v1/strategies/{strategy['id']}/versions",
        json={"implementation_key": "buy_and_hold", "parameters": {"quantity": 2}},
        headers=headers,
    ).json()
    client.post(f"/api/v1/strategies/versions/{draft['id']}/publish", headers=headers)
    return version["id"], draft["id"]


# --- backtest jobs ----------------------------------------------------------


def test_a_job_moves_queued_to_running_to_completed(client, headers) -> None:
    from iluvtrade.backtests.worker import BacktestWorkerPool

    register(client, "a@example.com")
    dataset_version_id, strategy_version_id = _ready(client, headers)

    job = client.post(
        "/api/v1/backtests",
        json={
            "dataset_version_id": dataset_version_id,
            "strategy_version_id": strategy_version_id,
        },
        headers=headers,
    ).json()
    assert job["status"] == JobStatus.QUEUED.value
    assert job["started_at"] is None and job["finished_at"] is None

    BacktestWorkerPool(size=1).drain(timeout=120)

    finished = client.get(f"/api/v1/backtests/{job['id']}").json()
    assert finished["status"] == JobStatus.COMPLETED.value
    assert finished["started_at"] is not None
    assert finished["finished_at"] is not None
    assert finished["attempts"] == 1
    assert finished["error_code"] is None


def test_a_failing_job_records_a_structured_reason(client, headers) -> None:
    """Never a generic failure: the code and the message are separate fields."""

    from iluvtrade.backtests.worker import BacktestWorkerPool
    from iluvtrade.db.session import session_scope

    register(client, "a@example.com")
    dataset_version_id, strategy_version_id = _ready(client, headers)
    job = client.post(
        "/api/v1/backtests",
        json={
            "dataset_version_id": dataset_version_id,
            "strategy_version_id": strategy_version_id,
        },
        headers=headers,
    ).json()

    # Break the canonical file the worker will read.
    with session_scope() as session:
        from iluvtrade.db.models.data import DatasetVersion

        version = session.get(DatasetVersion, dataset_version_id)
        assert version is not None
        version.canonical_path = "org/does/not/exist.jsonl"

    BacktestWorkerPool(size=1).drain(timeout=120)

    failed = client.get(f"/api/v1/backtests/{job['id']}").json()
    assert failed["status"] == JobStatus.FAILED.value
    assert failed["error_code"]
    assert failed["error_message"]
    assert failed["finished_at"] is not None


def test_a_failed_job_notifies_the_submitter(client, headers) -> None:
    """PHASE 16: a failure must not be silent."""

    from iluvtrade.backtests.worker import BacktestWorkerPool
    from iluvtrade.db.session import session_scope

    register(client, "a@example.com")
    dataset_version_id, strategy_version_id = _ready(client, headers)
    client.post(
        "/api/v1/backtests",
        json={
            "dataset_version_id": dataset_version_id,
            "strategy_version_id": strategy_version_id,
        },
        headers=headers,
    )
    with session_scope() as session:
        from iluvtrade.db.models.data import DatasetVersion

        session.get(DatasetVersion, dataset_version_id).canonical_path = "nope.jsonl"

    BacktestWorkerPool(size=1).drain(timeout=120)

    kinds = {n["kind"] for n in client.get("/api/v1/notifications").json()}
    assert "backtest.failed" in kinds


def test_a_terminal_job_cannot_be_cancelled(client, headers) -> None:
    from iluvtrade.backtests.worker import BacktestWorkerPool

    register(client, "a@example.com")
    dataset_version_id, strategy_version_id = _ready(client, headers)
    job = client.post(
        "/api/v1/backtests",
        json={
            "dataset_version_id": dataset_version_id,
            "strategy_version_id": strategy_version_id,
        },
        headers=headers,
    ).json()
    BacktestWorkerPool(size=1).drain(timeout=120)

    response = client.post(f"/api/v1/backtests/{job['id']}/cancel", headers=headers)
    assert response.status_code == 400
    assert "cannot be cancelled" in response.json()["error"]["message"]


def test_a_cancelled_job_is_not_executed(client, headers) -> None:
    from iluvtrade.backtests.worker import BacktestWorkerPool

    register(client, "a@example.com")
    dataset_version_id, strategy_version_id = _ready(client, headers)
    job = client.post(
        "/api/v1/backtests",
        json={
            "dataset_version_id": dataset_version_id,
            "strategy_version_id": strategy_version_id,
        },
        headers=headers,
    ).json()
    client.post(f"/api/v1/backtests/{job['id']}/cancel", headers=headers)

    BacktestWorkerPool(size=1).drain(timeout=60)

    after = client.get(f"/api/v1/backtests/{job['id']}").json()
    assert after["status"] == JobStatus.CANCELLED.value
    assert client.get(f"/api/v1/backtests/{job['id']}/result").status_code == 400


def test_a_job_is_claimed_by_exactly_one_worker(db) -> None:
    """Two workers must not both run one job."""

    from iluvtrade.backtests import service
    from iluvtrade.data import ingest
    from iluvtrade.db.base import utcnow
    from iluvtrade.strategies import service as strategies
    from tests.conftest import make_principal

    principal = make_principal(db)
    outcome = ingest.ingest_upload(
        db, principal, filename="d.csv", payload=make_csv(bars=30), dataset_name="D"
    )
    strategy = strategies.create_strategy(db, principal, name="S")
    version = strategies.publish(
        db,
        principal,
        strategies.create_version(
            db, principal, strategy_id=strategy.id, implementation_key="buy_and_hold"
        ).id,
    )
    job = BacktestJob(
        organization_id=principal.organization_id,
        submitted_by_user_id=principal.user_id,
        idempotency_key="k",
        status=JobStatus.QUEUED,
        request_json="{}",
        dataset_version_id=outcome.version.id,
        strategy_version_id=version.id,
        queued_at=utcnow(),
    )
    db.add(job)
    db.flush()

    first = service.claim_next(db)
    assert first is not None and first.status is JobStatus.RUNNING
    second = service.claim_next(db)
    assert second is None, "a running job must not be claimable again"


def test_orphaned_jobs_are_requeued_within_the_attempt_budget(db) -> None:
    """A worker that died leaves a job RUNNING forever without this."""

    from iluvtrade.backtests.worker import requeue_orphans
    from iluvtrade.data import ingest
    from iluvtrade.db.base import utcnow
    from iluvtrade.strategies import service as strategies
    from tests.conftest import make_principal

    principal = make_principal(db)
    outcome = ingest.ingest_upload(
        db, principal, filename="d.csv", payload=make_csv(bars=30), dataset_name="D"
    )
    strategy = strategies.create_strategy(db, principal, name="S")
    version = strategies.publish(
        db,
        principal,
        strategies.create_version(
            db, principal, strategy_id=strategy.id, implementation_key="buy_and_hold"
        ).id,
    )
    job = BacktestJob(
        organization_id=principal.organization_id,
        submitted_by_user_id=principal.user_id,
        idempotency_key="orphan",
        status=JobStatus.RUNNING,
        request_json="{}",
        dataset_version_id=outcome.version.id,
        strategy_version_id=version.id,
        queued_at=utcnow(),
        attempts=0,
    )
    db.add(job)
    db.flush()
    db.commit()

    assert requeue_orphans() == 1

    db.expire_all()
    assert db.get(BacktestJob, job.id).status is JobStatus.QUEUED


def test_an_orphan_past_its_attempt_budget_fails_rather_than_looping(db) -> None:
    """A job that reliably kills its worker must not be requeued forever."""

    from iluvtrade.backtests.worker import requeue_orphans
    from iluvtrade.data import ingest
    from iluvtrade.db.base import utcnow
    from iluvtrade.strategies import service as strategies
    from tests.conftest import make_principal

    principal = make_principal(db)
    outcome = ingest.ingest_upload(
        db, principal, filename="d.csv", payload=make_csv(bars=30), dataset_name="D"
    )
    strategy = strategies.create_strategy(db, principal, name="S")
    version = strategies.publish(
        db,
        principal,
        strategies.create_version(
            db, principal, strategy_id=strategy.id, implementation_key="buy_and_hold"
        ).id,
    )
    job = BacktestJob(
        organization_id=principal.organization_id,
        submitted_by_user_id=principal.user_id,
        idempotency_key="poison",
        status=JobStatus.RUNNING,
        request_json="{}",
        dataset_version_id=outcome.version.id,
        strategy_version_id=version.id,
        queued_at=utcnow(),
        attempts=99,
    )
    db.add(job)
    db.flush()
    db.commit()

    assert requeue_orphans() == 0

    db.expire_all()
    reloaded = db.get(BacktestJob, job.id)
    assert reloaded.status is JobStatus.FAILED
    assert reloaded.error_code == "WorkerLost"


# --- trading sessions -------------------------------------------------------


def test_invalid_session_transitions_are_rejected(client, headers) -> None:
    """The lifecycle is enforced, not merely documented."""

    register(client, "a@example.com")
    dataset_version_id, strategy_version_id = _ready(client, headers)
    session = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "S",
            "mode": "paper",
            "strategy_version_id": strategy_version_id,
            "dataset_version_id": dataset_version_id,
        },
        headers=headers,
    ).json()
    session_id = session["id"]

    # Cannot pause or resume something that never started.
    for action in ("pause", "resume"):
        response = client.post(f"/api/v1/trading/sessions/{session_id}/{action}", headers=headers)
        assert response.status_code == 400
        assert "cannot be" in response.json()["error"]["message"]


def test_a_session_cannot_be_started_twice(client, headers) -> None:
    """The second start must not spawn a second runner over one session."""

    from iluvtrade.trading.runner import RUNNER

    register(client, "a@example.com")
    dataset_version_id, strategy_version_id = _ready(client, headers)
    session = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "S",
            "mode": "paper",
            "strategy_version_id": strategy_version_id,
            "dataset_version_id": dataset_version_id,
        },
        headers=headers,
    ).json()

    assert (
        client.post(f"/api/v1/trading/sessions/{session['id']}/start", headers=headers).status_code
        == 200
    )
    second = client.post(f"/api/v1/trading/sessions/{session['id']}/start", headers=headers)
    assert second.status_code == 400, "a started session must refuse a second start"

    RUNNER.join(session["id"], timeout=120)
    final = client.get(f"/api/v1/trading/sessions/{session['id']}").json()
    assert final["status"] == SessionStatus.STOPPED.value


def test_a_second_runner_refuses_an_already_claimed_session(client, headers) -> None:
    """Directly exercising the claim, which the API-level test cannot reach."""

    from iluvtrade.db.models.trading import TradingSession
    from iluvtrade.db.session import session_scope
    from iluvtrade.trading.runner import run_session

    register(client, "a@example.com")
    dataset_version_id, strategy_version_id = _ready(client, headers)
    session = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "S",
            "mode": "paper",
            "strategy_version_id": strategy_version_id,
            "dataset_version_id": dataset_version_id,
        },
        headers=headers,
    ).json()

    # Put it in RUNNING without a runner, as a live claim would.
    with session_scope() as db_session:
        row = db_session.get(TradingSession, session["id"])
        row.status = SessionStatus.RUNNING

    assert run_session(session["id"]) == SessionStatus.RUNNING.value


def test_a_stopped_session_reports_its_final_state(client, headers) -> None:
    from iluvtrade.trading.runner import RUNNER

    register(client, "a@example.com")
    dataset_version_id, strategy_version_id = _ready(client, headers)
    session = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": "S",
            "mode": "paper",
            "strategy_version_id": strategy_version_id,
            "dataset_version_id": dataset_version_id,
        },
        headers=headers,
    ).json()
    client.post(f"/api/v1/trading/sessions/{session['id']}/start", headers=headers)
    RUNNER.join(session["id"], timeout=120)

    final = client.get(f"/api/v1/trading/sessions/{session['id']}").json()
    assert final["status"] == SessionStatus.STOPPED.value
    assert final["stopped_at"] is not None
    assert final["records_processed"] > 0
    assert final["equity"] is not None

    kinds = {
        e["kind"] for e in client.get(f"/api/v1/trading/sessions/{session['id']}/events").json()
    }
    assert {"session.created", "session.running", "session.finished"} <= kinds
