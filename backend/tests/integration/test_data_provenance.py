"""The full chain from uploaded bytes to a backtest result, asserted at each link.

The claim under test is that a user can always answer three questions: what did
I upload, what was done to it, and what exactly produced this result.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from tests.conftest import make_csv, register

pytestmark = pytest.mark.integration


def test_the_bytes_as_uploaded_are_kept_and_hashed(client, headers) -> None:
    """The original must remain identifiable, byte for byte."""

    from iluvtrade.common import storage
    from iluvtrade.db.models.data import DataSource
    from iluvtrade.db.session import session_scope

    payload = make_csv(bars=40, flaws=True)
    register(client, "a@example.com")
    version = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("original.csv", payload, "text/csv")},
        headers=headers,
    ).json()

    assert version["source"]["content_hash"] == hashlib.sha256(payload).hexdigest()
    assert version["source"]["size_bytes"] == len(payload)
    assert version["source"]["origin"] == "original.csv"

    with session_scope() as session:
        source = session.execute(__import__("sqlalchemy").select(DataSource)).scalars().one()
        stored = storage.read_bytes(source.raw_path)

    assert stored == payload, "the stored original must be byte-identical"


def test_the_canonical_file_is_hashed_and_the_run_records_that_hash(client, headers) -> None:
    """ "Did the data change under this result" must be answerable."""

    from iluvtrade.backtests.worker import BacktestWorkerPool

    register(client, "a@example.com")
    version = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=60), "text/csv")},
        headers=headers,
    ).json()
    client.post(f"/api/v1/datasets/versions/{version['id']}/approve", headers=headers)
    strategy = client.post("/api/v1/strategies", json={"name": "S"}, headers=headers).json()
    draft = client.post(
        f"/api/v1/strategies/{strategy['id']}/versions",
        json={"implementation_key": "buy_and_hold", "parameters": {"quantity": 2}},
        headers=headers,
    ).json()
    published = client.post(
        f"/api/v1/strategies/versions/{draft['id']}/publish", headers=headers
    ).json()

    job = client.post(
        "/api/v1/backtests",
        json={"dataset_version_id": version["id"], "strategy_version_id": published["id"]},
        headers=headers,
    ).json()
    BacktestWorkerPool(size=1).drain(timeout=120)

    repro = client.get(f"/api/v1/backtests/{job['id']}/result").json()["reproducibility"]
    assert repro["dataset_canonical_hash"] == version["canonical_hash"]
    assert repro["strategy_content_hash"] == published["content_hash"]
    assert repro["engine"].startswith("alphalab ")
    assert isinstance(repro["seed"], int)


def test_every_rejected_row_is_traceable_to_its_line(client, headers) -> None:
    register(client, "a@example.com")
    version = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=60, flaws=True), "text/csv")},
        headers=headers,
    ).json()

    rejected = version["rejected_rows"]
    assert rejected["logged"], "a flawed file must produce rejections"
    assert sum(rejected["counts"].values()) == version["rejected_row_count"]
    for row in rejected["logged"]:
        assert row["line_number"] > 0
        assert row["reason"]
        assert row["detail"]
        assert row["raw"], "the row as read must be retained"


def test_every_transformation_is_recorded_with_before_and_after(client, headers) -> None:
    register(client, "a@example.com")
    version = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=40), "text/csv")},
        headers=headers,
    ).json()

    transformations = version["transformations"]
    assert transformations["counts"], "deriving VWAP alone guarantees entries"
    for entry in transformations["logged"]:
        assert entry["kind"]
        assert entry["column"]
        assert "before" in entry and "after" in entry


def test_the_counts_and_the_logged_entries_agree(client, headers) -> None:
    """A truncated log must still report exact totals."""

    register(client, "a@example.com")
    version = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=60, flaws=True), "text/csv")},
        headers=headers,
    ).json()

    quality = version["quality"]
    assert quality["accepted_rows"] + quality["rejected_rows"] == quality["total_input_rows"]
    assert quality["rejected_rows"] == sum(quality["rejection_breakdown"].values())
    assert version["row_count"] == quality["accepted_rows"]


def test_the_cleaning_policy_that_ran_is_stored_with_the_version(client, headers) -> None:
    """Re-running the same policy over the same bytes must be possible."""

    register(client, "a@example.com")
    version = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=30), "text/csv")},
        data={"policy_json": json.dumps({"duplicate_policy": "keep_last"})},
        headers=headers,
    ).json()
    assert version["cleaning_policy"]["duplicate_policy"] == "keep_last"


def test_the_detected_schema_is_stored_with_the_version(client, headers) -> None:
    register(client, "a@example.com")
    version = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=30), "text/csv")},
        headers=headers,
    ).json()

    detected = version["schema_detection"]
    assert detected["columns"]
    assert detected["fields"]["timestamp"]["column"]
    assert detected["timestamp_format"]


def test_an_approved_version_cannot_have_its_canonical_data_replaced(client, headers) -> None:
    """Approval pins the data. A later import is a new version, not an edit."""

    register(client, "a@example.com")
    first = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=40, seed=1), "text/csv")},
        data={"dataset_name": "Same name"},
        headers=headers,
    ).json()
    client.post(f"/api/v1/datasets/versions/{first['id']}/approve", headers=headers)

    second = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=40, seed=2), "text/csv")},
        data={"dataset_name": "Same name"},
        headers=headers,
    ).json()

    assert second["id"] != first["id"]
    assert second["version"] == first["version"] + 1
    assert second["dataset_id"] == first["dataset_id"], "same logical dataset"

    unchanged = client.get(f"/api/v1/datasets/versions/{first['id']}").json()
    assert unchanged["canonical_hash"] == first["canonical_hash"]
    assert unchanged["status"] == "approved"


def test_two_identical_uploads_produce_identical_canonical_hashes(client, headers) -> None:
    """Canonicalization must be deterministic, or nothing downstream is."""

    register(client, "a@example.com")
    payload = make_csv(bars=50, seed=7)
    hashes = []
    for index in range(2):
        version = client.post(
            "/api/v1/datasets/upload",
            files={"file": ("d.csv", payload, "text/csv")},
            data={"dataset_name": f"Run {index}"},
            headers=headers,
        ).json()
        hashes.append(version["canonical_hash"])
    assert hashes[0] == hashes[1]


def test_alphalab_remains_the_only_source_of_result_numbers(client, headers) -> None:
    """Every stored metric must be reproducible from the artifact AlphaLab produced."""

    from iluvtrade.backtests.worker import BacktestWorkerPool

    register(client, "a@example.com")
    version = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=80), "text/csv")},
        headers=headers,
    ).json()
    client.post(f"/api/v1/datasets/versions/{version['id']}/approve", headers=headers)
    strategy = client.post("/api/v1/strategies", json={"name": "S"}, headers=headers).json()
    draft = client.post(
        f"/api/v1/strategies/{strategy['id']}/versions",
        json={
            "implementation_key": "moving_average_crossover",
            "parameters": {"fast": 5, "slow": 15, "quantity": 3},
        },
        headers=headers,
    ).json()
    published = client.post(
        f"/api/v1/strategies/versions/{draft['id']}/publish", headers=headers
    ).json()
    job = client.post(
        "/api/v1/backtests",
        json={
            "dataset_version_id": version["id"],
            "strategy_version_id": published["id"],
            "starting_cash": "500000.00",
        },
        headers=headers,
    ).json()
    BacktestWorkerPool(size=1).drain(timeout=120)

    payload = client.get(f"/api/v1/backtests/{job['id']}/result").json()
    metrics, document = payload["metrics"], payload["result"]

    # The headline figures are the artifact's figures, not a second computation.
    assert metrics["ending_equity"] == document["valuation"]["equity"]
    assert metrics["realized_pnl"] == document["valuation"]["realized_pnl"]
    assert metrics["commission_paid"] == document["valuation"]["commission_paid"]
    assert metrics["order_count"] == len(document["orders"])
    assert metrics["fill_count"] == len(document["fills"])

    # And the accounting identity AlphaLab guarantees still holds over them.
    from decimal import Decimal

    valuation = document["valuation"]
    assert Decimal(valuation["equity"]) == (
        Decimal("500000.00")
        + Decimal(valuation["realized_pnl"])
        + Decimal(valuation["unrealized_pnl"])
        - Decimal(valuation["commission_paid"])
    )


def test_a_result_reports_what_the_engine_declined(client, headers) -> None:
    """Refusals and skips are part of the record, not omitted."""

    from iluvtrade.backtests.worker import BacktestWorkerPool

    register(client, "a@example.com")
    version = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=50), "text/csv")},
        headers=headers,
    ).json()
    client.post(f"/api/v1/datasets/versions/{version['id']}/approve", headers=headers)
    strategy = client.post("/api/v1/strategies", json={"name": "S"}, headers=headers).json()
    draft = client.post(
        f"/api/v1/strategies/{strategy['id']}/versions",
        json={"implementation_key": "buy_and_hold", "parameters": {"quantity": 1}},
        headers=headers,
    ).json()
    published = client.post(
        f"/api/v1/strategies/versions/{draft['id']}/publish", headers=headers
    ).json()
    job = client.post(
        "/api/v1/backtests",
        json={"dataset_version_id": version["id"], "strategy_version_id": published["id"]},
        headers=headers,
    ).json()
    BacktestWorkerPool(size=1).drain(timeout=120)

    document = client.get(f"/api/v1/backtests/{job['id']}/result").json()["result"]
    assert "risk_refusals" in document
    assert "skipped_records" in document
    assert "unpriced_assets" in document
    assert isinstance(document["risk_refusals"]["count"], int)
