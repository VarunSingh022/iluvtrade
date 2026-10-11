"""What a dataset's bar interval rested on travels with every result built on it.

A dataset version records whether its declared interval is consistent with each
instrument's timestamps ("validated") or was, for some instruments, the
importer's unverifiable declaration ("missing_observations_confirmed"). The
final review found that limitation stopped at the dataset: backtest results,
paper sessions and RedDesk listing evidence repeated the interval without it.

These tests drive the product API and then read the persisted rows: each run
and paper session snapshots ``ingest.temporal_evidence`` from the exact
version it uses, results and listings repeat that snapshot, older records say
"not recorded" rather than being upgraded, and a buyer sees the limitation
without the creator's private dataset identifiers.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from zoneinfo import ZoneInfo

from tests.conftest import register

IST = ZoneInfo("Asia/Kolkata")
OPEN = dt.datetime(2024, 1, 2, 9, 15, tzinfo=IST)
FIFTEEN = dt.timedelta(minutes=15)
UPLOAD = "/api/v1/datasets/upload"


def _csv(hourly_bbb: bool) -> bytes:
    """AAA: complete 15-minute bars. BBB: hourly-spaced (ambiguous) or 15-minute too."""

    rows = [("AAA", OPEN + FIFTEEN * i) for i in range(12)]
    step = dt.timedelta(hours=1) if hourly_bbb else FIFTEEN
    rows += [("BBB", OPEN + step * i) for i in range(4)]
    rows.sort(key=lambda r: (r[1], r[0]))
    lines = ["Timestamp,Symbol,Open,High,Low,Close,Volume"]
    lines += [f"{m.isoformat()},{s},10,11,9,10,100" for s, m in rows]
    return ("\n".join(lines) + "\n").encode()


def _approved_version(client: Any, headers: dict[str, str], *, declared_bbb: bool) -> dict:
    """Upload through the real preview-then-import flow and approve the version."""

    payload = _csv(hourly_bbb=declared_bbb)
    declaration = {"timestamp_convention": "interval_start", "bar_interval": "15m"}
    data: dict[str, Any] = {"dataset_name": "evidence", **declaration}
    if declared_bbb:
        check = client.post(
            "/api/v1/datasets/inspect",
            files={"file": ("bars.csv", payload, "text/csv")},
            data=declaration,
            headers=headers,
        ).json()["interval_check"]
        assert check["ambiguous"] == ["BBB"]
        data |= {"missing_observations_confirmed_for": ["BBB"], "preview_id": check["preview_id"]}
    version = client.post(
        UPLOAD, files={"file": ("bars.csv", payload, "text/csv")}, data=data, headers=headers
    ).json()
    assert version["status"] == "pending_approval", version
    approved = client.post(f"/api/v1/datasets/versions/{version['id']}/approve", headers=headers)
    assert approved.status_code == 200, approved.text
    return dict(version)


def _strategy(client: Any, headers: dict[str, str]) -> tuple[str, str]:
    strategy = client.post("/api/v1/strategies", json={"name": "Hold"}, headers=headers).json()
    draft = client.post(
        f"/api/v1/strategies/{strategy['id']}/versions",
        json={"implementation_key": "buy_and_hold", "parameters": {"quantity": 1}},
        headers=headers,
    ).json()
    client.post(f"/api/v1/strategies/versions/{draft['id']}/publish", headers=headers)
    return str(strategy["id"]), str(draft["id"])


def _backtest(client: Any, headers: dict[str, str], version_id: str, strategy: str) -> dict:
    from iluvtrade.backtests.worker import BacktestWorkerPool

    job = client.post(
        "/api/v1/backtests",
        json={"dataset_version_id": version_id, "strategy_version_id": strategy, "seed": 7},
        headers=headers,
    ).json()
    BacktestWorkerPool(size=1).drain(timeout=120)
    result = client.get(f"/api/v1/backtests/{job['id']}/result", headers=headers)
    assert result.status_code == 200, result.text
    return dict(result.json()) | {"job_id": job["id"]}


def _run_row(run_id: str) -> Any:
    from iluvtrade.db.models.backtest import BacktestRun
    from iluvtrade.db.session import session_scope

    with session_scope() as session:
        run = session.get(BacktestRun, run_id)
        assert run is not None
        session.expunge(run)
        return run


def _expected(version_id: str) -> dict[str, Any]:
    """The evidence computed now from the stored version: the authority."""

    from iluvtrade.data import ingest
    from iluvtrade.db.models.data import DatasetVersion
    from iluvtrade.db.session import session_scope

    with session_scope() as session:
        version = session.get(DatasetVersion, version_id)
        assert version is not None
        return ingest.temporal_evidence(version)


# --- backtests ------------------------------------------------------------------


def test_a_backtest_records_the_user_declared_evidence_of_its_exact_version(
    client, headers
) -> None:
    from iluvtrade.common import storage

    register(client, "evidence-run@example.com")
    _, strategy = _strategy(client, headers)
    version = _approved_version(client, headers, declared_bbb=True)
    result = _backtest(client, headers, version["id"], strategy)

    evidence = result["reproducibility"]["temporal_evidence"]
    assert evidence == _expected(version["id"])
    assert evidence["status"] == "missing_observations_confirmed"
    assert evidence["user_declared_instruments"] == ["BBB"]
    assert evidence["bar_interval"] == "15m"
    assert evidence["convention"] == "interval_start"
    assert "Neither iluvtrade nor AlphaLab verified it" in evidence["meaning"]
    assert "validated" not in evidence["meaning"].lower()
    # The evidence names the version and canonical file the run actually read.
    assert evidence["dataset_version_id"] == result["reproducibility"]["dataset_version_id"]
    assert evidence["dataset_canonical_hash"] == result["reproducibility"]["dataset_canonical_hash"]

    # Persisted on the run row, and inside the digested result document.
    run = _run_row(result["run_id"])
    assert json.loads(run.temporal_evidence_json) == evidence
    document = storage.read_json(run.artifact_path)
    assert document["temporal_evidence"] == evidence
    assert result["result_integrity"]["verified"] is True
    assert result["result"]["temporal_evidence"] == evidence


def test_two_versions_keep_their_own_evidence(client, headers) -> None:
    register(client, "evidence-two@example.com")
    _, strategy = _strategy(client, headers)
    declared = _approved_version(client, headers, declared_bbb=True)
    validated = _approved_version(client, headers, declared_bbb=False)
    first = _backtest(client, headers, declared["id"], strategy)["reproducibility"]
    second = _backtest(client, headers, validated["id"], strategy)["reproducibility"]

    assert first["temporal_evidence"]["status"] == "missing_observations_confirmed"
    assert first["temporal_evidence"]["dataset_version_id"] == declared["id"]
    assert second["temporal_evidence"]["status"] == "validated"
    assert second["temporal_evidence"]["user_declared_instruments"] == []
    assert second["temporal_evidence"]["dataset_version_id"] == validated["id"]
    assert "consistency with the observations, not proof" in second["temporal_evidence"]["meaning"]


def test_a_run_stored_before_snapshots_is_not_recorded_rather_than_upgraded(
    client, headers
) -> None:
    from iluvtrade.db.models.backtest import BacktestRun
    from iluvtrade.db.session import session_scope

    register(client, "evidence-legacy-run@example.com")
    _, strategy = _strategy(client, headers)
    version = _approved_version(client, headers, declared_bbb=False)
    result = _backtest(client, headers, version["id"], strategy)
    with session_scope() as session:
        run = session.get(BacktestRun, result["run_id"])
        assert run is not None
        run.temporal_evidence_json = None  # as migration 0008 leaves older runs
    again = client.get(f"/api/v1/backtests/{result['job_id']}/result", headers=headers)
    evidence = again.json()["reproducibility"]["temporal_evidence"]
    assert evidence["status"] == "not_recorded"
    assert "not reconstructed" in evidence["meaning"]
    assert "bar_interval" not in evidence
    assert _run_row(result["run_id"]).temporal_evidence_json is None  # nothing written back


def test_evidence_for_another_dataset_is_refused_when_a_run_is_recorded(client, headers) -> None:
    """``record_success`` checks the snapshot names the dataset the run read."""

    import pytest

    from iluvtrade.backtests import service
    from iluvtrade.db.models.backtest import BacktestJob
    from iluvtrade.db.session import session_scope

    register(client, "evidence-mismatch@example.com")
    _, strategy = _strategy(client, headers)
    version = _approved_version(client, headers, declared_bbb=False)
    other = _approved_version(client, headers, declared_bbb=True)
    job = client.post(
        "/api/v1/backtests",
        json={"dataset_version_id": version["id"], "strategy_version_id": strategy, "seed": 1},
        headers=headers,
    ).json()
    with session_scope() as session:
        row = session.get(BacktestJob, job["id"])
        with pytest.raises(ValueError, match="does not describe the dataset this run read"):
            service.record_success(
                session,
                row,
                executed=object(),
                dataset_canonical_hash=version["canonical_hash"],
                strategy_content_hash=None,
                starting_cash="100000",
                temporal_evidence=_expected(other["id"]),
            )


# --- paper sessions -------------------------------------------------------------


def _paper_session(client: Any, headers: dict[str, str], version_id: str, strategy: str) -> dict:
    created = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": f"paper-{version_id[:6]}",
            "mode": "paper",
            "strategy_version_id": strategy,
            "dataset_version_id": version_id,
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


def test_a_paper_session_keeps_and_logs_its_versions_evidence(client, headers) -> None:
    from iluvtrade.db.models.trading import SessionEvent, TradingSession
    from iluvtrade.db.session import session_scope
    from iluvtrade.trading.runner import RUNNER

    register(client, "evidence-session@example.com")
    _, strategy = _strategy(client, headers)
    version = _approved_version(client, headers, declared_bbb=True)
    created = _paper_session(client, headers, version["id"], strategy)
    expected = _expected(version["id"])
    assert created["temporal_evidence"] == expected
    assert created["fill_timing"] == "next_event"  # separate from the temporal evidence
    assert "fill_timing" not in created["temporal_evidence"]

    client.post(f"/api/v1/trading/sessions/{created['id']}/start", headers=headers)
    RUNNER.join(created["id"], timeout=120)
    after = client.get(f"/api/v1/trading/sessions/{created['id']}", headers=headers).json()
    assert after["records_processed"] == 16
    assert after["temporal_evidence"] == expected

    with session_scope() as session:
        row = session.get(TradingSession, created["id"])
        assert row is not None and json.loads(row.temporal_evidence_json) == expected
        events = session.query(SessionEvent).filter_by(session_id=created["id"]).all()
        logged = [e for e in events if e.kind == "session.temporal_evidence"]
        assert len(logged) == 1
        assert logged[0].severity == "warning"
        assert "user-declared, not verified, for BBB" in logged[0].message
        assert json.loads(logged[0].payload_json) == expected


def test_a_session_created_before_snapshots_is_refused_not_upgraded(client, headers) -> None:
    from iluvtrade.db.models.trading import TradingSession
    from iluvtrade.db.session import session_scope
    from iluvtrade.trading.runner import RUNNER

    register(client, "evidence-legacy-session@example.com")
    _, strategy = _strategy(client, headers)
    version = _approved_version(client, headers, declared_bbb=False)
    created = _paper_session(client, headers, version["id"], strategy)
    with session_scope() as session:
        row = session.get(TradingSession, created["id"])
        assert row is not None
        row.temporal_evidence_json = None  # as migration 0008 leaves older sessions
    shown = client.get(f"/api/v1/trading/sessions/{created['id']}", headers=headers).json()
    assert shown["temporal_evidence"]["status"] == "not_recorded"

    client.post(f"/api/v1/trading/sessions/{created['id']}/start", headers=headers)
    RUNNER.join(created["id"], timeout=120)
    state = client.get(f"/api/v1/trading/sessions/{created['id']}", headers=headers).json()
    assert state["status"] == "failed" and state["records_processed"] == 0
    assert "created before temporal evidence was recorded" in (state["failure_reason"] or "")
    with session_scope() as session:
        row = session.get(TradingSession, created["id"])
        assert row is not None and row.temporal_evidence_json is None


def test_later_imports_and_approvals_do_not_touch_older_snapshots(client, headers) -> None:
    register(client, "evidence-stable@example.com")
    _, strategy = _strategy(client, headers)
    declared = _approved_version(client, headers, declared_bbb=True)
    result = _backtest(client, headers, declared["id"], strategy)
    session_ = _paper_session(client, headers, declared["id"], strategy)
    run_before = _run_row(result["run_id"]).temporal_evidence_json

    # The same file re-imported and approved as a new version; another version too.
    _approved_version(client, headers, declared_bbb=True)
    _approved_version(client, headers, declared_bbb=False)

    assert _run_row(result["run_id"]).temporal_evidence_json == run_before
    again = client.get(f"/api/v1/backtests/{result['job_id']}/result", headers=headers).json()
    assert again["reproducibility"]["temporal_evidence"]["dataset_version_id"] == declared["id"]
    shown = client.get(f"/api/v1/trading/sessions/{session_['id']}", headers=headers).json()
    assert shown["temporal_evidence"] == session_["temporal_evidence"]


# --- RedDesk ----------------------------------------------------------------------


def _published_listing(
    client: Any, headers: dict[str, str], strategy_id: str, version: str, run_id: str | None
) -> str:
    listing = client.post(
        "/api/v1/reddesk/listings",
        json={
            "strategy_id": strategy_id,
            "title": "Evidence listing",
            "description": "A strategy described at more than forty characters in length.",
            "methodology": "Buys once and holds.",
            "risk_disclosure": "Backtests are simulations, not forecasts.",
            "price_amount": "0",
        },
        headers=headers,
    )
    assert listing.status_code == 201, listing.text
    listing_id = str(listing.json()["id"])
    offered = client.post(
        f"/api/v1/reddesk/listings/{listing_id}/versions",
        json={"strategy_version_id": version, "evidence_backtest_run_id": run_id},
        headers=headers,
    )
    assert offered.status_code == 201, offered.text
    for step, body in (("submit", None), ("review", {"approve": True}), ("publish", None)):
        done = client.post(
            f"/api/v1/reddesk/listings/{listing_id}/{step}", json=body, headers=headers
        )
        assert done.status_code == 200, (step, done.text)
    return listing_id


def test_listing_evidence_carries_the_cited_runs_limitation_to_buyers(client, headers) -> None:
    register(client, "evidence-creator@example.com")
    strategy_id, strategy = _strategy(client, headers)
    version = _approved_version(client, headers, declared_bbb=True)
    result = _backtest(client, headers, version["id"], strategy)
    listing_id = _published_listing(client, headers, strategy_id, strategy, result["run_id"])

    creator_view = client.get(f"/api/v1/reddesk/listings/{listing_id}", headers=headers).json()
    run_evidence = json.loads(_run_row(result["run_id"]).temporal_evidence_json)

    register(client, "evidence-buyer@example.com")  # another organization
    response = client.get(f"/api/v1/reddesk/listings/{listing_id}", headers=headers)
    assert response.status_code == 200
    public = response.json()["evidence"]["temporal_evidence"]
    assert public["status"] == "missing_observations_confirmed"
    assert public["user_declared_instruments"] == ["BBB"]
    assert public["bar_interval"] == "15m"
    assert "Neither iluvtrade nor AlphaLab verified it" in public["meaning"]
    # Everything but the creator's private dataset identifiers, from the run's snapshot.
    assert public == {
        k: v
        for k, v in run_evidence.items()
        if k not in ("dataset_version_id", "dataset_canonical_hash")
    }
    assert version["id"] not in response.text
    assert version["canonical_hash"] not in response.text
    assert creator_view["evidence"]["temporal_evidence"] == public
    discovered = client.get("/api/v1/reddesk/discover", headers=headers).json()
    listed = next(item for item in discovered if item["id"] == listing_id)
    assert listed["evidence"]["temporal_evidence"] == public


def test_a_creator_cannot_relabel_listing_evidence(client, headers) -> None:
    register(client, "evidence-relabel@example.com")
    strategy_id, strategy = _strategy(client, headers)
    version = _approved_version(client, headers, declared_bbb=True)
    result = _backtest(client, headers, version["id"], strategy)
    listing = client.post(
        "/api/v1/reddesk/listings",
        json={"strategy_id": strategy_id, "title": "Relabel"},
        headers=headers,
    ).json()
    forged = client.post(
        f"/api/v1/reddesk/listings/{listing['id']}/versions",
        json={
            "strategy_version_id": strategy,
            "evidence_backtest_run_id": result["run_id"],
            "temporal_evidence": {"status": "validated"},
        },
        headers=headers,
    )
    assert forged.status_code == 422  # request models refuse unknown fields


def test_listing_evidence_without_a_recorded_snapshot_says_so(client, headers) -> None:
    from iluvtrade.db.models.backtest import BacktestRun
    from iluvtrade.db.session import session_scope

    register(client, "evidence-legacy-listing@example.com")
    strategy_id, strategy = _strategy(client, headers)
    version = _approved_version(client, headers, declared_bbb=False)
    result = _backtest(client, headers, version["id"], strategy)
    listing_id = _published_listing(client, headers, strategy_id, strategy, result["run_id"])
    with session_scope() as session:
        run = session.get(BacktestRun, result["run_id"])
        assert run is not None
        run.temporal_evidence_json = None
    shown = client.get(f"/api/v1/reddesk/listings/{listing_id}", headers=headers).json()
    assert shown["evidence"]["temporal_evidence"]["status"] == "not_recorded"
    assert "bar_interval" not in shown["evidence"]["temporal_evidence"]

    with session_scope() as session:
        run = session.get(BacktestRun, result["run_id"])
        session.delete(run)
    gone = client.get(f"/api/v1/reddesk/listings/{listing_id}", headers=headers).json()
    assert gone["evidence"]["available"] is False
    assert gone["evidence"]["temporal_evidence"]["status"] == "unavailable"
    assert "sharpe_ratio" not in gone["evidence"]
