"""Multi-instrument files through the real upload path: per-instrument intervals and ordering.

Two review findings, pinned end to end (multipart upload -> cleaning -> spacing
check -> move to interval end -> stored canonical file -> API response):

* **Look-ahead across instruments.** The bar-interval check counted one-interval
  pairs across the whole file, so a 15-minute instrument vouched for an hourly
  one declared alongside it, and each hourly bar was moved 15 minutes forward --
  its close stamped 45 minutes before it happened. Each instrument now answers
  for itself; an instrument whose own bars never show the interval is refused
  unless the upload confirms its gaps are missing observations.
* **Symbol-grouped files lost rows.** Out-of-order was judged against one
  file-wide high-water mark, so a file listing all of AAA and then all of BBB
  rejected BBB's rows as "out of order". Order is now judged per instrument.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.conftest import register

IST = ZoneInfo("Asia/Kolkata")
UPLOAD = "/api/v1/datasets/upload"
OPEN = dt.datetime(2024, 1, 2, 9, 15, tzinfo=IST)
FIFTEEN = dt.timedelta(minutes=15)


def _times(step: dt.timedelta, count: int, start: dt.datetime = OPEN) -> list[dt.datetime]:
    return [start + step * i for i in range(count)]


def _csv(rows: list[tuple[str, dt.datetime]]) -> bytes:
    lines = ["Timestamp,Symbol,Open,High,Low,Close,Volume"]
    lines += [f"{moment.isoformat()},{symbol},10,11,9,10,100" for symbol, moment in rows]
    return ("\n".join(lines) + "\n").encode()


def _grouped(series: dict[str, list[dt.datetime]]) -> list[tuple[str, dt.datetime]]:
    return [(symbol, moment) for symbol, moments in series.items() for moment in moments]


def _interleaved(series: dict[str, list[dt.datetime]]) -> list[tuple[str, dt.datetime]]:
    return sorted(_grouped(series), key=lambda row: (row[1], row[0]))


def _preview(client: Any, headers: dict[str, str], payload: bytes, **declared: str) -> dict:
    """The interval check a user sees before deciding, as the UI asks for it."""

    response = client.post(
        "/api/v1/datasets/inspect",
        files={"file": ("bars.csv", payload, "text/csv")},
        data={"timestamp_convention": "interval_start", **declared},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return dict(response.json()["interval_check"])


def _upload(
    client: Any,
    headers: dict[str, str],
    payload: bytes,
    *,
    confirmed: tuple[str, ...] = (),
    preview_id: str | None = None,
    expect: int = 201,
    **extra: str,
) -> dict[str, Any]:
    """Upload; a confirmation is sent with the preview_id of a check of these bytes."""

    if confirmed and preview_id is None:
        declared = {k: v for k, v in extra.items() if k in ("bar_interval", "timezone")}
        preview_id = _preview(client, headers, payload, **declared)["preview_id"]
    response = client.post(
        UPLOAD,
        files={"file": ("bars.csv", payload, "text/csv")},
        data={
            "dataset_name": "multi",
            "timestamp_convention": "interval_start",
            "missing_observations_confirmed_for": list(confirmed),
            **({"preview_id": preview_id} if preview_id else {}),
            **extra,
        },
        headers=headers,
    )
    assert response.status_code == expect, response.text
    return dict(response.json())


def _stored(version_id: str) -> list[dict[str, Any]]:
    from iluvtrade.common import storage
    from iluvtrade.db.models.data import DatasetVersion
    from iluvtrade.db.session import session_scope

    with session_scope() as session:
        version = session.get(DatasetVersion, version_id)
        assert version is not None
        if not version.canonical_path:
            return []
        return list(storage.read_jsonl(version.canonical_path))


def _by_symbol(rows: list[dict[str, Any]]) -> dict[str, list[float]]:
    found: dict[str, list[float]] = {}
    for row in rows:
        found.setdefault(row["symbol"], []).append(row["timestamp"])
    return found


def _listed(client: Any, headers: dict[str, str], version_id: str) -> dict[str, Any]:
    for dataset in client.get("/api/v1/datasets", headers=headers).json():
        for version in dataset["versions"]:
            if version["id"] == version_id:
                return dict(version)
    raise AssertionError(f"version {version_id} not listed")


# --- the reviewed look-ahead case --------------------------------------------


def test_an_hourly_instrument_is_not_validated_by_a_fifteen_minute_one(client, headers) -> None:
    """The exact case the review reproduced, through the upload API."""

    register(client, "lookahead@example.com")
    series = {"AAA": _times(FIFTEEN, 12), "BBB": _times(dt.timedelta(hours=1), 4)}
    version = _upload(client, headers, _csv(_interleaved(series)), bar_interval="15m")

    assert version["status"] == "failed", version
    assert "BBB: no two consecutive bars are one 15m interval apart" in version["failure_reason"]
    assert "AAA:" not in version["failure_reason"]
    # Nothing was stored, so no BBB bar was moved to 15 minutes after its start.
    assert version["canonical_hash"] is None and version["row_count"] == 0
    assert _stored(version["id"]) == []
    assert version["bar_interval"] is None and version["interval_status"] == "none"


def _strategy(client: Any, headers: dict[str, str]) -> str:
    strategy = client.post("/api/v1/strategies", json={"name": "Hold"}, headers=headers).json()
    draft = client.post(
        f"/api/v1/strategies/{strategy['id']}/versions",
        json={"implementation_key": "buy_and_hold", "parameters": {"quantity": 1}},
        headers=headers,
    ).json()
    client.post(f"/api/v1/strategies/versions/{draft['id']}/publish", headers=headers)
    return str(draft["id"])


def _usable(client: Any, headers: dict[str, str], version_id: str, strategy: str) -> dict[str, Any]:
    """Whether a version can be approved, backtested and run as a paper session."""

    from iluvtrade.backtests.worker import BacktestWorkerPool
    from iluvtrade.trading.runner import RUNNER

    found: dict[str, Any] = {}
    approve = client.post(f"/api/v1/datasets/versions/{version_id}/approve", headers=headers)
    found["approved"] = approve.status_code == 200
    job = client.post(
        "/api/v1/backtests",
        json={"dataset_version_id": version_id, "strategy_version_id": strategy, "seed": 1},
        headers=headers,
    )
    found["backtest"] = None
    if job.status_code == 202:
        BacktestWorkerPool(size=1).drain(timeout=120)
        found["backtest"] = client.get(
            f"/api/v1/backtests/{job.json()['id']}", headers=headers
        ).json()["status"]
    session = client.post(
        "/api/v1/trading/sessions",
        json={
            "name": f"s-{version_id[:8]}",
            "mode": "paper",
            "strategy_version_id": strategy,
            "dataset_version_id": version_id,
        },
        headers=headers,
    )
    found["paper_records"] = None
    if session.status_code == 201:
        client.post(f"/api/v1/trading/sessions/{session.json()['id']}/start", headers=headers)
        RUNNER.join(session.json()["id"], timeout=120)
        found["paper_records"] = client.get(
            f"/api/v1/trading/sessions/{session.json()['id']}", headers=headers
        ).json()["records_processed"]
    return found


def test_the_reviewed_case_in_both_confirmation_states(client, headers) -> None:
    """AAA 15m, BBB hourly, declared 15m interval_start: what each state lets through.

    Unconfirmed, the version fails and nothing downstream accepts it. Confirmed
    for BBB by name, the user has declared that BBB's bars are 15-minute bars
    with three of every four observations missing; the import cannot verify that
    and does not claim to. It records the declaration, moves each BBB bar by 15
    minutes as declared, and shows the version as user-declared, not validated.
    A dataset-wide "yes" no longer exists: the old boolean field is refused
    with 422, so BBB stays refused unless it is named.
    """

    register(client, "both-states@example.com")
    strategy = _strategy(client, headers)
    series = {"AAA": _times(FIFTEEN, 12), "BBB": _times(dt.timedelta(hours=1), 4)}
    payload = _csv(_interleaved(series))

    refused = _upload(client, headers, payload, bar_interval="15m")
    # The retired dataset-wide flag is refused outright, before anything is stored.
    blanket = _upload(
        client,
        headers,
        payload,
        bar_interval="15m",
        missing_observations_confirmed="true",
        expect=422,
    )
    assert "no longer accepted" in blanket["error"]["message"]
    for version in (refused,):
        assert version["status"] == "failed", version
        assert version["row_count"] == 0 and version["temporal"]["spacing"] is None
        assert "BBB: no two consecutive bars" in version["failure_reason"]
        assert _usable(client, headers, version["id"], strategy) == {
            "approved": False,
            "backtest": None,
            "paper_records": None,
        }

    declared = _upload(client, headers, payload, bar_interval="15m", confirmed=("BBB",))
    assert declared["status"] == "pending_approval", declared
    assert declared["row_count"] == 16
    assert declared["interval_status"] == "missing_observations_confirmed"
    assert declared["accepted_on_confirmation"] == ["BBB"]
    spacing = declared["temporal"]["spacing"]
    assert spacing["ambiguous"] == ["BBB"]
    assert spacing["instruments"]["BBB"] == {
        "bars": 4,
        "one_interval_pairs": 0,
        "longer_gaps": 3,
        "accepted_on_confirmation": True,
    }
    assert spacing["instruments"]["AAA"]["one_interval_pairs"] == 11
    # As declared: the 09:15 BBB bar is stamped 09:30. Were BBB hourly, its
    # close is 10:15 -- this is exactly what the declaration takes responsibility for.
    first_bbb = _by_symbol(_stored(declared["id"]))["BBB"][0]
    assert first_bbb == (OPEN + FIFTEEN).timestamp()
    assert _usable(client, headers, declared["id"], strategy) == {
        "approved": True,
        "backtest": "completed",
        "paper_records": 16,
    }


def test_inspect_names_the_ambiguous_instruments_before_anything_is_stored(client, headers) -> None:
    from sqlalchemy import func, select

    from iluvtrade.db.models.data import DataSource
    from iluvtrade.db.session import session_scope

    register(client, "preview-check@example.com")
    series = {"AAA": _times(FIFTEEN, 12), "BBB": _times(dt.timedelta(hours=1), 4)}
    detected = client.post(
        "/api/v1/datasets/inspect",
        files={"file": ("bars.csv", _csv(_interleaved(series)), "text/csv")},
        data={"timestamp_convention": "interval_start", "bar_interval": "15m"},
        headers=headers,
    ).json()
    check = detected["interval_check"]
    assert check["refused"] is None
    assert check["ambiguous"] == ["BBB"]
    assert check["spacing"]["instruments"]["AAA"]["one_interval_pairs"] == 11
    mixed = {"AAA": _times(FIFTEEN, 12), "BBB": _times(dt.timedelta(minutes=5), 6)}
    refused = client.post(
        "/api/v1/datasets/inspect",
        files={"file": ("bars.csv", _csv(_interleaved(mixed)), "text/csv")},
        data={"timestamp_convention": "interval_start", "bar_interval": "15m"},
        headers=headers,
    ).json()["interval_check"]
    assert "Two BBB bars are 5m apart" in refused["refused"]
    with session_scope() as session:
        assert session.execute(select(func.count()).select_from(DataSource)).scalar_one() == 0


def test_a_confirmation_naming_the_wrong_instruments_is_refused(client, headers) -> None:
    register(client, "wrong-names@example.com")
    series = {"AAA": _times(FIFTEEN, 12), "BBB": _times(dt.timedelta(hours=1), 4)}
    payload = _csv(_interleaved(series))
    for named, reason in (
        (("AAA", "BBB"), "confirmed for AAA, whose own bars already sit one 15m interval apart"),
        (("BBB", "ZZZ"), "confirmed for ZZZ, which this file has no bars for"),
    ):
        version = _upload(client, headers, payload, bar_interval="15m", confirmed=named)
        assert version["status"] == "failed"
        assert reason in version["failure_reason"]


def test_a_confirmed_sparse_instrument_is_accepted_and_shown_as_confirmed(client, headers) -> None:
    """15-minute bars with observations missing: refused without the statement, kept with it."""

    register(client, "sparse-multi@example.com")
    sparse = [OPEN + FIFTEEN * i for i in (0, 2, 5, 9)]
    series = {"AAA": _times(FIFTEEN, 12), "BBB": sparse}
    payload = _csv(_interleaved(series))

    refused = _upload(client, headers, payload, bar_interval="15m")
    assert refused["status"] == "failed"
    assert refused["failure_reason"].count("BBB") >= 1

    kept = _upload(client, headers, payload, bar_interval="15m", confirmed=("BBB",))
    assert kept["status"] == "pending_approval", kept
    # Every bar of both instruments moved by the declared 15 minutes -- never by
    # BBB's typical gap.
    assert _by_symbol(_stored(kept["id"])) == {
        symbol: [m.timestamp() + 900 for m in moments] for symbol, moments in series.items()
    }
    assert kept["bar_interval"] == "15m"
    assert kept["interval_status"] == "missing_observations_confirmed"
    assert kept["accepted_on_confirmation"] == ["BBB"]
    spacing = kept["temporal"]["spacing"]
    assert spacing["instruments"]["AAA"]["accepted_on_confirmation"] is False
    assert spacing["instruments"]["BBB"]["accepted_on_confirmation"] is True
    assert kept["temporal"]["missing_observations_confirmed_for"] == ["BBB"]
    listed = _listed(client, headers, kept["id"])
    assert (listed["bar_interval"], listed["interval_status"]) == (
        "15m",
        "missing_observations_confirmed",
    )


def test_two_complete_instruments_are_each_validated(client, headers) -> None:
    register(client, "complete-multi@example.com")
    series = {"AAA": _times(FIFTEEN, 12), "BBB": _times(FIFTEEN, 8)}
    version = _upload(client, headers, _csv(_interleaved(series)), bar_interval="15m")
    assert version["status"] == "pending_approval", version
    assert version["interval_status"] == "validated"
    assert version["accepted_on_confirmation"] == []
    instruments = version["temporal"]["spacing"]["instruments"]
    assert {name: i["one_interval_pairs"] for name, i in instruments.items()} == {
        "AAA": 11,
        "BBB": 7,
    }


def test_an_instrument_with_shorter_bars_is_refused_even_when_confirmed(client, headers) -> None:
    register(client, "mixed-multi@example.com")
    series = {"AAA": _times(FIFTEEN, 12), "BBB": _times(dt.timedelta(minutes=5), 6)}
    for named in ((), ("BBB",)):
        version = _upload(
            client, headers, _csv(_interleaved(series)), bar_interval="15m", confirmed=named
        )
        assert version["status"] == "failed"
        assert "Two BBB bars are 5m apart, less than the declared 15m" in version["failure_reason"]


def test_a_single_instrument_of_only_longer_gaps_needs_the_confirmation(client, headers) -> None:
    register(client, "multiples@example.com")
    payload = _csv(_grouped({"TCS": _times(FIFTEEN * 2, 8)}))
    assert _upload(client, headers, payload, bar_interval="15m")["status"] == "failed"
    confirmed = _upload(client, headers, payload, bar_interval="15m", confirmed=("TCS",))
    assert confirmed["status"] == "pending_approval"
    assert confirmed["accepted_on_confirmation"] == ["TCS"]


@pytest.mark.parametrize(
    "record_version",
    [
        2,  # one-interval pairs counted across instruments
        3,  # a dataset-wide missing-observation flag, given before the instruments were known
    ],
)
def test_a_superseded_validation_record_is_not_runnable(
    client, headers, record_version: int
) -> None:
    """Records 2 and 3 were written by unreleased builds; neither is validated provenance."""

    import json

    from iluvtrade.db.models.data import DatasetVersion
    from iluvtrade.db.session import session_scope

    register(client, f"record{record_version}@example.com")
    version = _upload(
        client, headers, _csv(_grouped({"TCS": _times(FIFTEEN, 6)})), bar_interval="15m"
    )
    with session_scope() as session:
        row = session.get(DatasetVersion, version["id"])
        assert row is not None
        record = json.loads(row.temporal_json)
        record["record_version"] = record_version
        if record_version == 3:
            record.pop("missing_observations_confirmed_for")
            record["missing_observations_confirmed"] = True
        row.temporal_json = json.dumps(record)
    approve = client.post(f"/api/v1/datasets/versions/{version['id']}/approve", headers=headers)
    assert approve.status_code == 400
    assert "no validated timestamp provenance" in approve.json()["error"]["message"]
    listed = _listed(client, headers, version["id"])
    assert (listed["bar_interval"], listed["interval_status"]) == (None, "unvalidated")


# --- row order: per instrument, not per file ----------------------------------


def test_a_symbol_grouped_file_keeps_every_row(client, headers) -> None:
    register(client, "grouped@example.com")
    series = {"AAA": _times(FIFTEEN, 12), "BBB": _times(FIFTEEN, 12)}
    interleaved = _upload(client, headers, _csv(_interleaved(series)), bar_interval="15m")
    grouped = _upload(client, headers, _csv(_grouped(series)), bar_interval="15m")

    for version in (interleaved, grouped):
        assert version["status"] == "pending_approval", version
        assert version["row_count"] == 24 and version["rejected_row_count"] == 0
        stored = _stored(version["id"])
        assert _by_symbol(stored) == {
            symbol: [m.timestamp() + 900 for m in moments] for symbol, moments in series.items()
        }
        # The canonical file is chronological, symbols tied in name order.
        assert [(r["timestamp"], r["symbol"]) for r in stored] == sorted(
            (r["timestamp"], r["symbol"]) for r in stored
        )
    # Same bars, same canonical dataset, whichever order the file listed them in.
    assert interleaved["canonical_hash"] == grouped["canonical_hash"]
    assert grouped["transformations"]["counts"].get("reordered") == 1


def test_a_row_out_of_order_within_its_instrument_is_still_rejected(client, headers) -> None:
    register(client, "out-of-order@example.com")
    aaa = _times(FIFTEEN, 12)
    rows = _grouped({"AAA": aaa, "BBB": _times(FIFTEEN, 12)})
    # A new AAA time, earlier than bars already read: backwards for AAA only.
    late = ("AAA", OPEN - FIFTEEN)
    rows.insert(12, late)
    version = _upload(client, headers, _csv(rows), bar_interval="15m")
    assert version["row_count"] == 24
    assert version["rejected_row_count"] == 1
    rejected = version["rejected_rows"]["logged"]
    assert [r["reason"] for r in rejected] == ["out_of_order"]
    assert rejected[0]["line_number"] == 14  # header + 12 AAA rows, then the late one
    assert len(_by_symbol(_stored(version["id"]))["BBB"]) == 12


def test_a_grouped_file_with_missing_observations(client, headers) -> None:
    register(client, "grouped-sparse@example.com")
    series = {"AAA": _times(FIFTEEN, 12), "BBB": [OPEN + FIFTEEN * i for i in (0, 3, 7)]}
    payload = _csv(_grouped(series))
    assert _upload(client, headers, payload, bar_interval="15m")["status"] == "failed"
    kept = _upload(client, headers, payload, bar_interval="15m", confirmed=("BBB",))
    assert kept["row_count"] == 15 and kept["rejected_row_count"] == 0
    assert _by_symbol(_stored(kept["id"]))["BBB"] == [m.timestamp() + 900 for m in series["BBB"]]


def test_a_grouped_file_with_incompatible_intervals_is_refused(client, headers) -> None:
    register(client, "grouped-mixed@example.com")
    series = {"AAA": _times(FIFTEEN, 12), "BBB": _times(dt.timedelta(minutes=25), 6)}
    version = _upload(
        client,
        headers,
        _csv(_grouped(series)),
        bar_interval="15m",
        confirmed=("BBB",),
    )
    assert version["status"] == "failed"
    assert (
        "Two BBB bars are 25m apart, which is not a whole number of 15m"
        in version["failure_reason"]
    )
    assert _stored(version["id"]) == []
