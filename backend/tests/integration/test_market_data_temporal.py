"""Market-data temporal semantics and missing values: the research-integrity contract.

AlphaLab stamps a bar at the end of its interval, the instant its close became
knowable. These tests pin what an uploaded file's timestamps become, under each
declared convention, against expectations computed here from the calendar and
the zone -- never against a number copied out of a previous run. The fixtures
are shaped like real NSE data: daily bars named by session date, and
fifteen-minute bars inside the 09:15-15:30 IST session.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from tests.conftest import register
from tests.support import DECLARED

IST = ZoneInfo("Asia/Kolkata")
UPLOAD = "/api/v1/datasets/upload"

#: Five NSE sessions in January 2024 (a Tuesday to the following Monday,
#: skipping the weekend), as a vendor's end-of-day file writes them.
SESSIONS = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]


def _daily_csv(*, vwap: list[str] | None = None, trades: list[str] | None = None) -> bytes:
    header = ["Date", "Symbol", "Open", "High", "Low", "Close", "Volume"]
    if vwap is not None:
        header.append("VWAP")
    if trades is not None:
        header.append("Trades")
    lines = [",".join(header)]
    for index, day in enumerate(SESSIONS):
        close = 2500 + index * 10
        row = [
            day,
            "RELIANCE",
            str(close - 5),
            str(close + 15),
            str(close - 20),
            str(close),
            "900000",
        ]
        if vwap is not None:
            row.append(vwap[index])
        if trades is not None:
            row.append(trades[index])
        lines.append(",".join(row))
    return ("\n".join(lines) + "\n").encode()


def _daily_csv_for(days: list[str], *, symbol: str = "SPX") -> bytes:
    lines = ["Date,Symbol,Open,High,Low,Close,Volume"]
    for index, day in enumerate(days):
        close = 100 + index
        lines.append(f"{day},{symbol},{close - 1},{close + 2},{close - 3},{close},100000")
    return ("\n".join(lines) + "\n").encode()


def _intraday_times(day: str = "2024-01-02", bars: int = 12) -> list[dt.datetime]:
    start = dt.datetime.fromisoformat(f"{day}T09:15:00").replace(tzinfo=IST)
    return [start + dt.timedelta(minutes=15 * i) for i in range(bars)]


def _intraday_csv(render: Any) -> bytes:
    lines = ["Timestamp,Symbol,Open,High,Low,Close,Volume"]
    for index, moment in enumerate(_intraday_times()):
        close = 2500 + index
        lines.append(f"{render(moment)},TCS,{close - 1},{close + 2},{close - 3},{close},12000")
    return ("\n".join(lines) + "\n").encode()


def _upload(client: Any, headers: dict[str, str], payload: bytes, **declared: str) -> Any:
    return client.post(
        UPLOAD,
        files={"file": ("bars.csv", payload, "text/csv")},
        data={"dataset_name": "bars", **declared},
        headers=headers,
    )


def _rows(version_id: str) -> list[dict[str, Any]]:
    from iluvtrade.common import storage
    from iluvtrade.db.models.data import DatasetVersion
    from iluvtrade.db.session import session_scope

    with session_scope() as session:
        version = session.get(DatasetVersion, version_id)
        assert version is not None and version.canonical_path
        return list(storage.read_jsonl(version.canonical_path))


def _end_of_day(day: str) -> float:
    """The last instant of ``day`` in IST: the end of the session date, by definition."""

    midnight = dt.datetime.fromisoformat(day).replace(tzinfo=IST) + dt.timedelta(days=1)
    return (midnight - dt.timedelta(microseconds=1)).timestamp()


# --- a declaration is required, and checked against the file ----------------


def test_an_undeclared_file_is_refused_before_anything_is_stored(client, headers) -> None:
    from sqlalchemy import func, select

    from iluvtrade.db.models.data import DataSource
    from iluvtrade.db.session import session_scope

    register(client, "undeclared@example.com")
    response = _upload(client, headers, _daily_csv())
    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "session_date" in message and "interval_end" in message and "time zone" in message
    with session_scope() as session:
        assert session.execute(select(func.count()).select_from(DataSource)).scalar_one() == 0


def test_inspect_reports_what_must_be_declared(client, headers) -> None:
    register(client, "inspect@example.com")
    detected = client.post(
        "/api/v1/datasets/inspect",
        files={"file": ("bars.csv", _daily_csv(), "text/csv")},
        headers=headers,
    ).json()
    assert detected["timestamp_semantics"] == {
        "kind": "date",
        "kinds": {"date": len(SESSIONS)},
        "requires_timezone": True,
        "conventions": ["session_date", "interval_end"],
    }


def test_a_date_labelling_the_start_of_its_interval_is_refused_not_shifted(client, headers) -> None:
    register(client, "start-date@example.com")
    response = _upload(
        client,
        headers,
        _daily_csv(),
        timestamp_convention="interval_start",
        timezone="Asia/Kolkata",
    )
    assert response.status_code == 400
    assert "not done on a guess" in response.json()["error"]["message"]


def test_bare_dates_need_a_zone_and_an_invalid_zone_is_refused(client, headers) -> None:
    register(client, "zones@example.com")
    missing = _upload(client, headers, _daily_csv(), timestamp_convention="session_date")
    assert missing.status_code == 400
    assert "time zone" in missing.json()["error"]["message"]

    invalid = _upload(
        client, headers, _daily_csv(), timestamp_convention="session_date", timezone="Mars/Olympus"
    )
    assert invalid.status_code == 400
    assert "Mars/Olympus" in invalid.json()["error"]["message"]

    unknown = _upload(
        client, headers, _daily_csv(), timestamp_convention="sometime", timezone="UTC"
    )
    assert unknown.status_code == 400


def test_an_instant_with_an_offset_refuses_a_zone(client, headers) -> None:
    register(client, "aware@example.com")
    payload = _intraday_csv(lambda moment: moment.isoformat())
    refused = _upload(
        client,
        headers,
        payload,
        timestamp_convention="interval_end",
        bar_interval="15m",
        timezone="Asia/Kolkata",
    )
    assert refused.status_code == 400
    assert "already name an instant" in refused.json()["error"]["message"]
    assert (
        _upload(
            client, headers, payload, timestamp_convention="interval_end", bar_interval="15m"
        ).status_code
        == 201
    )


def test_ambiguous_dates_are_never_resolved_by_a_declaration(client, headers) -> None:
    """``03/04/2024`` is the third of April or the fourth of March; nothing here picks."""

    register(client, "ambiguous@example.com")
    payload = (
        b"Date,Symbol,Open,High,Low,Close,Volume\n"
        b"03/04/2024,INFY,1500,1510,1490,1505,1000\n"
        b"04/04/2024,INFY,1505,1515,1495,1510,1000\n"
        b"05/04/2024,INFY,1510,1520,1500,1515,1000\n"
    )
    version = _upload(client, headers, payload, **DECLARED).json()
    assert version["status"] == "failed"
    assert "unambiguously" in (version["failure_reason"] or "")


def test_mixed_timestamp_forms_are_refused(client, headers) -> None:
    register(client, "mixed@example.com")
    moments = _intraday_times()
    lines = ["Timestamp,Symbol,Open,High,Low,Close,Volume"]
    for index, moment in enumerate(moments):
        text = moment.isoformat() if index % 2 else moment.replace(tzinfo=None).isoformat()
        lines.append(f"{text},TCS,10,11,9,10,100")
    response = _upload(
        client, headers, ("\n".join(lines) + "\n").encode(), timestamp_convention="interval_end"
    )
    assert response.status_code == 400
    assert "mixes forms" in response.json()["error"]["message"]


# --- what each convention produces ------------------------------------------


def test_a_session_date_is_stamped_at_the_end_of_that_day_in_the_exchange_zone(
    client, headers
) -> None:
    register(client, "session@example.com")
    version = _upload(client, headers, _daily_csv(), **DECLARED).json()
    assert version["status"] == "pending_approval", version

    stamps = [row["timestamp"] for row in _rows(version["id"])]
    assert stamps == [_end_of_day(day) for day in SESSIONS]
    # The point of it: every bar is stamped after that session's 15:30 IST close,
    # where the old reading (UTC midnight) put it at 05:30 IST, before the open.
    for day, stamp in zip(SESSIONS, stamps, strict=True):
        close = dt.datetime.fromisoformat(f"{day}T15:30:00").replace(tzinfo=IST).timestamp()
        assert stamp > close

    assert version["temporal"]["convention"] == "session_date"
    assert version["temporal"]["timezone"] == "Asia/Kolkata"
    assert version["temporal"]["timestamp_kind"] == "date"
    assert "END_OF_DAY" in version["temporal"]["rule"]


def test_pre_1970_session_dates_are_refused_before_alpha_lab_sees_a_bar(client, headers) -> None:
    register(client, "pre-epoch@example.com")
    version = _upload(
        client,
        headers,
        _daily_csv_for(["1950-01-03", "1950-01-04", "1950-01-05"]),
        timestamp_convention="session_date",
        timezone="UTC",
    ).json()

    assert version["status"] == "failed", version
    assert version["canonical_hash"] is None
    assert version["row_count"] == 0
    assert "strictly positive Unix seconds" in version["failure_reason"]
    assert "-630892800.000001" in version["failure_reason"]


def test_date_only_daily_bars_persist_reload_and_reach_backtest_execution(client, headers) -> None:
    from iluvtrade.backtests.worker import BacktestWorkerPool
    from iluvtrade.common import storage
    from iluvtrade.data import ingest
    from iluvtrade.db.models.data import DatasetVersion
    from iluvtrade.db.session import session_scope

    register(client, "post-epoch@example.com")
    days = [f"1970-01-0{day}" for day in range(1, 7)]
    created = _upload(
        client,
        headers,
        _daily_csv_for(days),
        timestamp_convention="session_date",
        timezone="UTC",
    ).json()
    assert created["status"] == "pending_approval", created
    approved = client.post(f"/api/v1/datasets/versions/{created['id']}/approve", headers=headers)
    assert approved.status_code == 200, approved.text

    expected = [
        (
            dt.datetime.fromisoformat(day).replace(tzinfo=dt.UTC)
            + dt.timedelta(days=1, microseconds=-1)
        ).timestamp()
        for day in days
    ]
    with session_scope() as session:
        version = session.get(DatasetVersion, created["id"])
        assert version is not None and version.canonical_path and version.canonical_hash
        rows = ingest.canonical_rows(version)
        persisted = storage.read_jsonl_verified(
            version.canonical_path, version.canonical_hash, expected_count=version.row_count
        )
    assert rows == persisted
    assert [row["timestamp"] for row in rows] == expected

    strategy = _strategy(client, headers)
    job = client.post(
        "/api/v1/backtests",
        json={"dataset_version_id": created["id"], "strategy_version_id": strategy, "seed": 1970},
        headers=headers,
    ).json()
    BacktestWorkerPool(size=1).drain(timeout=120)
    finished = client.get(f"/api/v1/backtests/{job['id']}").json()
    assert finished["status"] == "completed", finished
    result = client.get(f"/api/v1/backtests/{job['id']}/result").json()
    assert result["result"]["records_processed"] == len(days)


#: The first eight sessions of the Apple file that hit the production failure
#: report (plotly ``finance-charts-apple.csv``), verbatim: Date, Open, High, Low,
#: Close, Volume.
APPLE_2015 = [
    ("2015-02-17", "127.489998", "128.880005", "126.919998", "127.830002", "63152400"),
    ("2015-02-18", "127.629997", "128.779999", "127.449997", "128.720001", "44891700"),
    ("2015-02-19", "128.479996", "129.029999", "128.330002", "128.449997", "37362400"),
    ("2015-02-20", "128.619995", "129.5", "128.050003", "129.5", "48948400"),
    ("2015-02-23", "130.020004", "133", "129.660004", "133", "70974100"),
    ("2015-02-24", "132.940002", "133.600006", "131.169998", "132.169998", "69228100"),
    ("2015-02-25", "131.559998", "131.600006", "128.149994", "128.789993", "74711700"),
    ("2015-02-26", "128.789993", "130.869995", "126.610001", "130.419998", "91287500"),
]


def test_apple_2015_sessions_with_a_mixed_case_symbol_reach_a_completed_backtest(
    client, headers
) -> None:
    """The Apple-era case: positive timestamps, and a symbol not already upper case.

    Its timestamps were never the problem -- 2015-02-17 ends at
    1424217599.999999 Unix seconds, which AlphaLab accepts. What stopped such a
    dataset was the instrument universe being keyed on AlphaLab's normalized
    (upper-case) symbol while canonical rows keep the source's spelling: the
    first bar failed as "not in the declared instrument universe".
    """

    from iluvtrade.backtests.worker import BacktestWorkerPool

    register(client, "apple-2015@example.com")
    lines = ["Date,Symbol,Open,High,Low,Close,Volume"]
    lines += [f"{d},Aapl,{o},{h},{lo},{c},{v}" for d, o, h, lo, c, v in APPLE_2015]
    created = _upload(
        client,
        headers,
        ("\n".join(lines) + "\n").encode(),
        timestamp_convention="session_date",
        timezone="UTC",
    ).json()
    assert created["status"] == "pending_approval", created
    stamps = [row["timestamp"] for row in _rows(created["id"])]
    assert stamps[0] == 1424217599.999999  # the value the stored production file holds
    assert all(stamp > 0 for stamp in stamps)
    approved = client.post(f"/api/v1/datasets/versions/{created['id']}/approve", headers=headers)
    assert approved.status_code == 200, approved.text

    strategy = _strategy(client, headers)
    job = client.post(
        "/api/v1/backtests",
        json={"dataset_version_id": created["id"], "strategy_version_id": strategy, "seed": 2015},
        headers=headers,
    ).json()
    BacktestWorkerPool(size=1).drain(timeout=120)
    finished = client.get(f"/api/v1/backtests/{job['id']}").json()
    assert finished["status"] == "completed", finished
    result = client.get(f"/api/v1/backtests/{job['id']}/result").json()
    assert result["result"]["records_processed"] == len(APPLE_2015)
    assert result["reproducibility"]["universe"] == ["Aapl"]


def test_an_approved_pre_1970_version_cannot_be_backtested(client, headers) -> None:
    """The state the production ``sp500`` and ``GSPC`` versions are in.

    They were imported and approved before the import refused pre-1970 bars, so
    the job reached AlphaLab and failed on its first bar with "Invalid timestamp
    for bar." Submission now refuses such a version up front, saying why.
    """

    from iluvtrade.db.models.data import DatasetVersion
    from iluvtrade.db.session import session_scope

    register(client, "approved-pre-epoch@example.com")
    created = _upload(
        client,
        headers,
        _daily_csv_for(["1970-01-02", "1970-01-05", "1970-01-06"]),
        timestamp_convention="session_date",
        timezone="UTC",
    ).json()
    client.post(f"/api/v1/datasets/versions/{created['id']}/approve", headers=headers)
    with session_scope() as session:
        version = session.get(DatasetVersion, created["id"])
        assert version is not None
        version.start_timestamp = -630979200.0  # 1950-01-03, as stored for "sp500"
    strategy = _strategy(client, headers)
    refused = client.post(
        "/api/v1/backtests",
        json={"dataset_version_id": created["id"], "strategy_version_id": strategy},
        headers=headers,
    )
    assert refused.status_code == 400, refused.text
    message = refused.json()["error"]["message"]
    assert "-630979200.0" in message and "strictly positive Unix seconds" in message


def test_the_adapter_refuses_a_non_positive_bar_before_alphalab_does() -> None:
    import pytest
    from alphalab.market import TimeFrame

    from iluvtrade.alphalab_bridge.instruments import universe_for
    from iluvtrade.alphalab_bridge.market import bars_from_rows

    universe = universe_for(["SPX"])
    row = {"symbol": "SPX", "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "0"}
    for stamp in (-630979200.0, 0.0, float("nan")):
        with pytest.raises(ValueError, match=r"AlphaLab 4\.1\.0 market data rejects"):
            list(
                bars_from_rows(
                    [{**row, "timestamp": stamp}],
                    timeframe=TimeFrame.parse("1d"),
                    universe=universe,
                )
            )
    bar = next(
        bars_from_rows(
            [{**row, "timestamp": 1424217599.999999}],
            timeframe=TimeFrame.parse("1d"),
            universe=universe,
        )
    )
    assert bar.timestamp == 1424217599.999999


def test_symbols_that_normalize_to_one_instrument_are_refused() -> None:
    import pytest

    from iluvtrade.alphalab_bridge.instruments import universe_for

    universe = universe_for(["Aapl", "MSFT"])
    assert set(universe.by_symbol) == {"Aapl", "MSFT"}
    assert universe.symbol_of(universe.by_symbol["Aapl"]) == "Aapl"
    # The asset id is still AlphaLab's, derived from the normalized symbol.
    assert universe.by_symbol["Aapl"] == universe_for(["AAPL"]).by_symbol["AAPL"]
    with pytest.raises(ValueError, match="one AlphaLab instrument once normalized"):
        universe_for(["aapl", "AAPL"])


def test_session_dates_must_be_daily(client, headers) -> None:
    register(client, "weekly@example.com")
    weekly = [dt.date(2024, 1, 5) + dt.timedelta(weeks=i) for i in range(6)]
    lines = ["Date,Symbol,Open,High,Low,Close,Volume"]
    lines += [f"{day.isoformat()},RELIANCE,10,11,9,10,100" for day in weekly]
    version = _upload(client, headers, ("\n".join(lines) + "\n").encode(), **DECLARED).json()
    assert version["status"] == "failed"
    assert "one 1d interval apart" in version["failure_reason"]

    contradicted = _upload(client, headers, _daily_csv(), **DECLARED, bar_interval="1h")
    assert contradicted.status_code == 400
    assert "contradicts" in contradicted.json()["error"]["message"]


def test_interval_end_wall_clock_times_are_read_in_the_declared_zone(client, headers) -> None:
    register(client, "end@example.com")
    payload = _intraday_csv(lambda moment: moment.replace(tzinfo=None).isoformat(sep=" "))
    version = _upload(
        client,
        headers,
        payload,
        timestamp_convention="interval_end",
        bar_interval="15m",
        timezone="Asia/Kolkata",
    ).json()
    assert [row["timestamp"] for row in _rows(version["id"])] == [
        moment.timestamp() for moment in _intraday_times()
    ]
    assert version["temporal"]["moved_to_interval_end"] == 0


def test_interval_start_bars_are_moved_to_the_end_of_their_interval(client, headers) -> None:
    register(client, "start@example.com")
    payload = _intraday_csv(lambda moment: moment.replace(tzinfo=None).isoformat(sep=" "))
    version = _upload(
        client,
        headers,
        payload,
        timestamp_convention="interval_start",
        bar_interval="15m",
        timezone="Asia/Kolkata",
    ).json()
    fifteen_minutes = 15 * 60
    assert [row["timestamp"] for row in _rows(version["id"])] == [
        moment.timestamp() + fifteen_minutes for moment in _intraday_times()
    ]
    # The 09:15 bar is the one that closes at 09:30.
    first = dt.datetime.fromtimestamp(_rows(version["id"])[0]["timestamp"], IST)
    assert (first.hour, first.minute) == (9, 30)
    assert version["temporal"]["moved_to_interval_end"] == len(_intraday_times())
    assert version["transformations"]["counts"]["moved_to_interval_end"] == len(_intraday_times())


def test_epoch_values_name_instants_and_take_no_zone(client, headers) -> None:
    register(client, "epoch@example.com")
    payload = _intraday_csv(lambda moment: str(int(moment.timestamp())))
    assert (
        _upload(
            client,
            headers,
            payload,
            timestamp_convention="interval_end",
            bar_interval="15m",
            timezone="UTC",
        ).status_code
        == 400
    )
    version = _upload(
        client, headers, payload, timestamp_convention="interval_start", bar_interval="15m"
    ).json()
    assert [row["timestamp"] for row in _rows(version["id"])] == [
        moment.timestamp() + 900 for moment in _intraday_times()
    ]


# --- the bar interval: declared, validated, never inferred --------------------


def _bars_at(moments: list[dt.datetime], symbol: str = "TCS") -> bytes:
    lines = ["Timestamp,Symbol,Open,High,Low,Close,Volume"]
    lines += [f"{m.isoformat()},{symbol},10,11,9,10,100" for m in moments]
    return ("\n".join(lines) + "\n").encode()


def test_times_without_a_declared_interval_are_refused_before_storing(client, headers) -> None:
    from sqlalchemy import func, select

    from iluvtrade.db.models.data import DataSource
    from iluvtrade.db.session import session_scope

    register(client, "no-interval@example.com")
    payload = _intraday_csv(lambda moment: moment.isoformat())
    for convention in ("interval_start", "interval_end"):
        refused = _upload(client, headers, payload, timestamp_convention=convention)
        assert refused.status_code == 400
        assert "never inferred" in refused.json()["error"]["message"]
    bad_code = _upload(
        client, headers, payload, timestamp_convention="interval_end", bar_interval="fortnightly"
    )
    assert bad_code.status_code == 400
    months = _upload(
        client, headers, payload, timestamp_convention="interval_start", bar_interval="1M"
    )
    assert months.status_code == 400 and "no fixed length" in months.json()["error"]["message"]
    with session_scope() as session:
        assert session.execute(select(func.count()).select_from(DataSource)).scalar_one() == 0


def test_missing_bars_overnight_and_weekend_gaps_are_kept_not_used_to_guess(
    client, headers
) -> None:
    """Friday's session with two bars missing, then Monday's: still 15-minute bars."""

    register(client, "gaps@example.com")
    friday = _intraday_times("2024-01-05", bars=25)
    monday = _intraday_times("2024-01-08", bars=25)
    kept = [m for i, m in enumerate(friday) if i not in (6, 7)] + monday
    version = _upload(
        client, headers, _bars_at(kept), timestamp_convention="interval_start", bar_interval="15m"
    ).json()
    assert version["status"] == "pending_approval", version

    # Every bar moved by exactly the declared 15 minutes, gaps or not.
    assert [row["timestamp"] for row in _rows(version["id"])] == [
        moment.timestamp() + 900 for moment in kept
    ]
    spacing = version["temporal"]["spacing"]
    # One gap where two bars are missing, one overnight-plus-weekend gap.
    assert spacing["longer_gaps"] == 2
    assert spacing["one_interval_pairs"] == len(kept) - 1 - 2
    assert version["temporal"]["bar_interval"] == "15m"
    assert version["temporal"]["bar_interval_source"] == "declared"


def test_the_declared_interval_wins_over_what_the_spacing_suggests(client, headers) -> None:
    """Fifteen-minute bars, mostly an hour apart: the median gap says 1h; the bars are 15m.

    A sparse feed of 15-minute bars (only one bar an hour survives, except one
    adjacent pair) is still a feed of 15-minute bars. Each must move by 15
    minutes, not by the hour the median gap would suggest.
    """

    register(client, "sparse@example.com")
    start = _intraday_times()[0]
    moments = [start, start + dt.timedelta(minutes=15)]
    moments += [moments[-1] + dt.timedelta(hours=h) for h in range(1, 6)]
    version = _upload(
        client,
        headers,
        _bars_at(moments),
        timestamp_convention="interval_start",
        bar_interval="15m",
    ).json()
    assert version["status"] == "pending_approval", version
    assert version["inferred_frequency"] == "1h"  # what a guess would have used
    assert [row["timestamp"] for row in _rows(version["id"])] == [
        m.timestamp() + 900 for m in moments
    ]
    assert version["temporal"]["spacing"] == {
        "arithmetic": "absolute",
        "instruments": {
            "TCS": {
                "bars": 7,
                "one_interval_pairs": 1,
                "longer_gaps": 5,
                "accepted_on_confirmation": False,
            }
        },
        "instrument_count": 1,
        "one_interval_pairs": 1,
        "longer_gaps": 5,
        "accepted_on_confirmation": [],
        "ambiguous": [],
        "missing_observations_confirmed_for": [],
    }


def test_an_unusual_declared_interval_is_honoured_not_snapped(client, headers) -> None:
    """25-minute bars are 25-minute bars; nothing rounds them to a standard interval."""

    register(client, "twentyfive@example.com")
    start = _intraday_times()[0]
    moments = [start + dt.timedelta(minutes=25 * i) for i in range(8)]
    version = _upload(
        client,
        headers,
        _bars_at(moments),
        timestamp_convention="interval_start",
        bar_interval="25m",
    ).json()
    assert [row["timestamp"] for row in _rows(version["id"])] == [
        m.timestamp() + 1500 for m in moments
    ]


def test_spacing_refuses_a_declaration_it_contradicts(client, headers) -> None:
    register(client, "contradict@example.com")
    fifteen = _bars_at(_intraday_times(bars=12))
    start = _intraday_times()[0]
    cases = [
        # Irregular for the declared interval: 25 minutes is not a whole number of 15s.
        (
            _bars_at([start + dt.timedelta(minutes=25 * i) for i in range(8)]),
            "15m",
            "not a whole number",
        ),
        # Declared longer than the data: bars closer together than one interval.
        (fifteen, "1h", "less than the declared 1h"),
        # Declared shorter than the data: whole multiples, but none one interval apart.
        (fifteen, "5m", "one 5m interval apart"),
    ]
    for payload, declared, reason in cases:
        version = _upload(
            client, headers, payload, timestamp_convention="interval_start", bar_interval=declared
        ).json()
        assert version["status"] == "failed", (declared, version)
        assert reason in version["failure_reason"], (declared, version["failure_reason"])


def test_mixed_frequencies_are_refused(client, headers) -> None:
    register(client, "mixedfreq@example.com")
    start = _intraday_times()[0]
    five_minute = [start + dt.timedelta(minutes=5 * i) for i in range(6)]
    fifteen_minute = [five_minute[-1] + dt.timedelta(minutes=15 * i) for i in range(1, 7)]
    version = _upload(
        client,
        headers,
        _bars_at(five_minute + fifteen_minute),
        timestamp_convention="interval_end",
        bar_interval="15m",
    ).json()
    assert version["status"] == "failed"
    assert "mixes bar frequencies" in version["failure_reason"]


def test_a_remote_fetch_takes_the_same_declaration_and_checks(client, headers, monkeypatch) -> None:
    """The fetch path is the upload path after the bytes arrive: same gate, same record."""

    from iluvtrade.data import ingest
    from iluvtrade.data.fetch import FetchResult

    def fake_fetch(url: str, settings: object) -> FetchResult:
        return FetchResult(
            content=_daily_csv(),
            final_url=url,
            requested_url=url,
            content_type="text/csv",
            retrieved_at=dt.datetime.now(dt.UTC),
            redirect_chain=(),
        )

    monkeypatch.setattr(ingest, "fetch_csv", fake_fetch)
    register(client, "fetch@example.com")
    url = "https://data.example.com/nse.csv"
    refused = client.post("/api/v1/datasets/fetch", json={"url": url}, headers=headers)
    assert refused.status_code == 400 and "Declare" in refused.json()["error"]["message"]
    fetched = client.post(
        "/api/v1/datasets/fetch",
        json={"url": url, "timestamp_convention": "session_date", "timezone": "Asia/Kolkata"},
        headers=headers,
    ).json()
    assert fetched["temporal"]["convention"] == "session_date"
    assert fetched["temporal"]["bar_interval"] == "1d"
    assert [row["timestamp"] for row in _rows(fetched["id"])] == [_end_of_day(d) for d in SESSIONS]


# --- identity -----------------------------------------------------------------


def test_the_declaration_changes_the_canonical_dataset_identity(client, headers) -> None:
    register(client, "identity@example.com")
    payload = _daily_csv()
    in_ist = _upload(client, headers, payload, **DECLARED).json()
    in_utc = _upload(
        client, headers, payload, timestamp_convention="session_date", timezone="UTC"
    ).json()
    assert in_ist["source"]["content_hash"] == in_utc["source"]["content_hash"]
    assert in_ist["canonical_hash"] != in_utc["canonical_hash"]
    assert in_ist["temporal"] != in_utc["temporal"]


def _strategy(client: Any, headers: dict[str, str]) -> str:
    strategy = client.post("/api/v1/strategies", json={"name": "Hold"}, headers=headers).json()
    draft = client.post(
        f"/api/v1/strategies/{strategy['id']}/versions",
        json={"implementation_key": "buy_and_hold", "parameters": {"quantity": 1}},
        headers=headers,
    ).json()
    client.post(f"/api/v1/strategies/versions/{draft['id']}/publish", headers=headers)
    return str(draft["id"])


def _make_legacy(version_id: str, record: str) -> None:
    from iluvtrade.db.models.data import DatasetVersion
    from iluvtrade.db.session import session_scope

    with session_scope() as session:
        row = session.get(DatasetVersion, version_id)
        assert row is not None
        row.temporal_json = record


#: What migration 0006 gives every pre-existing version, and what a version
#: imported before bar intervals were declared recorded (its interval inferred).
LEGACY_RECORDS = {
    "no declaration": "{}",
    "inferred interval": json.dumps({"convention": "session_date", "timezone": "Asia/Kolkata"}),
    # Declared, but validated across the whole file: one instrument's bars could
    # vouch for another's (record version 2, before per-instrument validation).
    "whole-file validation": json.dumps(
        {
            "record_version": 2,
            "convention": "session_date",
            "timezone": "Asia/Kolkata",
            "bar_interval": "1d",
        }
    ),
}


def test_a_version_without_validated_provenance_passes_no_gate(client, headers) -> None:
    """Approval, submission, the worker, session creation and the runner all refuse it.

    The worker and the runner load a version directly; they re-check, because a
    job or a session created before an upgrade can name a version that is no
    longer runnable.
    """

    from iluvtrade.backtests.worker import BacktestWorkerPool
    from iluvtrade.trading.runner import RUNNER

    register(client, "legacy@example.com")
    strategy = _strategy(client, headers)
    for label, record in LEGACY_RECORDS.items():
        pending = _upload(client, headers, _daily_csv(), **DECLARED).json()
        _make_legacy(pending["id"], record)
        approve = client.post(f"/api/v1/datasets/versions/{pending['id']}/approve", headers=headers)
        assert approve.status_code == 400, label
        assert "no validated timestamp provenance" in approve.json()["error"]["message"]

        # Approved while valid, then (as an upgrade would leave it) without provenance.
        approved = _upload(client, headers, _daily_csv(), **DECLARED).json()
        client.post(f"/api/v1/datasets/versions/{approved['id']}/approve", headers=headers)
        job = client.post(
            "/api/v1/backtests",
            json={"dataset_version_id": approved["id"], "strategy_version_id": strategy},
            headers=headers,
        ).json()
        created = client.post(
            "/api/v1/trading/sessions",
            json={
                "name": f"legacy {label}",
                "mode": "paper",
                "strategy_version_id": strategy,
                "dataset_version_id": approved["id"],
            },
            headers=headers,
        ).json()
        _make_legacy(approved["id"], record)

        submitted = client.post(
            "/api/v1/backtests",
            json={
                "dataset_version_id": approved["id"],
                "strategy_version_id": strategy,
                "seed": 99,
            },
            headers=headers,
        )
        assert submitted.status_code == 400, label
        session_refused = client.post(
            "/api/v1/trading/sessions",
            json={
                "name": "new",
                "mode": "paper",
                "strategy_version_id": strategy,
                "dataset_version_id": approved["id"],
            },
            headers=headers,
        )
        assert session_refused.status_code == 400, label

        BacktestWorkerPool(size=1).drain(timeout=120)
        finished = client.get(f"/api/v1/backtests/{job['id']}").json()
        assert finished["status"] == "failed", (label, finished)
        assert "no validated timestamp provenance" in finished["error_message"]

        client.post(f"/api/v1/trading/sessions/{created['id']}/start", headers=headers)
        RUNNER.join(created["id"], timeout=120)
        state = client.get(f"/api/v1/trading/sessions/{created['id']}").json()
        assert state["status"] == "failed", (label, state)
        assert state["records_processed"] == 0


# --- VWAP and trade counts: reported or absent --------------------------------


def test_missing_vwap_stays_missing_all_the_way_to_the_alphalab_bar(client, headers) -> None:
    from iluvtrade.alphalab_bridge import instruments, market

    register(client, "novwap@example.com")
    version = _upload(client, headers, _daily_csv(), **DECLARED).json()
    rows = _rows(version["id"])
    assert all(row["vwap"] is None and row["trade_count"] is None for row in rows)
    for row in rows:
        typical = (Decimal(row["high"]) + Decimal(row["low"]) + Decimal(row["close"])) / 3
        assert row["vwap"] != str(typical) and row["vwap"] != row["close"]
    assert "derived_vwap" not in version["transformations"]["counts"]

    universe = instruments.universe_for(["RELIANCE"], currency="INR")
    bars = list(
        market.bars_from_rows(rows, timeframe=market.timeframe_for("1d"), universe=universe)
    )
    assert all(bar.vwap is None and bar.trade_count is None for bar in bars)


def test_a_reported_vwap_is_kept_exactly(client, headers) -> None:
    register(client, "vwap@example.com")
    reported = ["2499.37", "2508.12", "2519.98", "2530.05", "2540.61"]
    version = _upload(client, headers, _daily_csv(vwap=reported, trades=["8812"] * 5), **DECLARED)
    rows = _rows(version.json()["id"])
    assert [row["vwap"] for row in rows] == reported
    assert [row["trade_count"] for row in rows] == [8812] * 5


def test_an_impossible_vwap_is_dropped_and_the_drop_recorded(client, headers) -> None:
    register(client, "badvwap@example.com")
    # Outside the bar's own low-high range, and not a number.
    reported = ["9999", "n/a", "2519.98", "2530.05", "2540.61"]
    version = _upload(
        client, headers, _daily_csv(vwap=reported, trades=["-4", "x", "7", "7", "7"]), **DECLARED
    ).json()
    rows = _rows(version["id"])
    assert [row["vwap"] for row in rows] == [None, None, "2519.98", "2530.05", "2540.61"]
    assert [row["trade_count"] for row in rows] == [None, None, 7, 7, 7]
    counts = version["transformations"]["counts"]
    assert counts["discarded_invalid_vwap"] == 2
    assert counts["discarded_invalid_trade_count"] == 2


def test_a_stored_policy_naming_derive_vwap_still_reads(client, headers) -> None:
    """Versions written before VWAP stopped being invented recorded the flag."""

    from iluvtrade.data.cleaning import CleaningPolicy

    policy = CleaningPolicy.from_dict({"derive_vwap": True, "duplicate_policy": "keep_first"})
    assert "derive_vwap" not in policy.to_dict()
    # And a request may not ask for it any more.
    register(client, "policy@example.com")
    response = _upload(
        client,
        headers,
        _daily_csv(),
        policy_json=json.dumps({"derive_vwap": True}),
        **DECLARED,
    )
    assert response.status_code == 422
