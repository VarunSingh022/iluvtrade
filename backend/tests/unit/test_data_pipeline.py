"""Schema detection, cleaning and quality reporting."""

from __future__ import annotations

from decimal import Decimal

import pytest

from iluvtrade.data.cleaning import CleaningPolicy, DuplicatePolicy, clean
from iluvtrade.data.quality import analyse, infer_frequency
from iluvtrade.data.schema import FieldRole, TimestampFormat, detect_schema, parse_timestamp


def _clean(text: str, policy: CleaningPolicy | None = None):
    schema = detect_schema(text)
    return schema, clean(text, schema, policy or CleaningPolicy())


# --- detection --------------------------------------------------------------


def test_detects_standard_headers() -> None:
    schema = detect_schema(
        "Date,Symbol,Open,High,Low,Close,Volume\n2024-01-01,ACME,1,2,0.5,1.5,100\n"
    )
    assert schema.column_for(FieldRole.TIMESTAMP) == "Date"
    assert schema.column_for(FieldRole.SYMBOL) == "Symbol"
    assert schema.is_ohlc_complete
    assert schema.timestamp_format is TimestampFormat.DATE_ONLY


@pytest.mark.parametrize(
    ("header", "role"),
    [
        ("tradingsymbol", FieldRole.SYMBOL),
        ("ltp", FieldRole.CLOSE),
        ("adj_close", FieldRole.CLOSE),
        ("tradedqty", FieldRole.VOLUME),
    ],
)
def test_detects_common_aliases(header: str, role: FieldRole) -> None:
    text = f"when,{header}\n2024-01-01,100\n2024-01-02,101\n"
    schema = detect_schema(text)
    assert schema.column_for(role) == header


@pytest.mark.parametrize("spelling", ["datetime", "timestamp", "tradedate", "candledate"])
def test_detects_timestamp_spellings(spelling: str) -> None:
    schema = detect_schema(f"{spelling},close\n2024-01-01,100\n2024-01-02,101\n")
    assert schema.column_for(FieldRole.TIMESTAMP) == spelling


def test_ltp_is_a_close_not_a_low() -> None:
    """``ltp`` starts with l, and a greedy prefix match once read it as *low*.

    The result was a bar whose low equalled its close — a backtest quietly wrong
    in a way no metric reveals.
    """

    schema = detect_schema("when,ltp\n2024-01-01,100\n2024-01-02,101\n")
    assert schema.column_for(FieldRole.CLOSE) == "ltp"
    assert schema.column_for(FieldRole.LOW) is None


def test_ambiguous_slash_dates_are_refused_not_guessed() -> None:
    """03/04/2024 is two different days and the file does not say which."""

    text = "date,close\n03/04/2024,100\n05/06/2024,101\n07/08/2024,102\n"
    schema = detect_schema(text)
    assert schema.timestamp_ambiguous is True
    _, result = _clean(text)
    assert result.fatal is not None
    assert "day and month" in result.fatal.lower()


def test_unambiguous_slash_dates_are_read() -> None:
    text = "date,close\n25/04/2024,100\n26/04/2024,101\n27/04/2024,102\n"
    schema = detect_schema(text)
    assert schema.timestamp_format is TimestampFormat.SLASH_DMY
    assert schema.timestamp_ambiguous is False


def test_epoch_seconds_and_millis_are_distinguished() -> None:
    seconds = detect_schema("t,close\n1704067200,100\n1704153600,101\n1704240000,102\n")
    millis = detect_schema("t,close\n1704067200000,100\n1704153600000,101\n1704240000000,102\n")
    assert seconds.timestamp_format is TimestampFormat.EPOCH_SECONDS
    assert millis.timestamp_format is TimestampFormat.EPOCH_MILLIS


def test_a_bom_does_not_break_detection() -> None:
    """Excel writes a BOM; left in place it makes the first column unmatchable."""

    from iluvtrade.data.ingest import _decode

    payload = "﻿Date,Close\n2024-01-01,100\n2024-01-02,101\n".encode()
    schema = detect_schema(_decode(payload))
    assert schema.column_for(FieldRole.TIMESTAMP) == "Date"


def test_parse_timestamp_reads_naive_values_as_utc() -> None:
    assert parse_timestamp("2024-01-01", TimestampFormat.DATE_ONLY) == 1704067200.0


# --- cleaning ---------------------------------------------------------------


def test_every_rejection_carries_a_line_number_and_a_reason() -> None:
    text = (
        "Date,Symbol,Open,High,Low,Close,Volume\n"
        "2024-01-01,ACME,100,110,90,105,1000\n"
        "2024-01-01,ACME,100,110,90,105,1000\n"  # duplicate
        "2024-01-02,ACME,notanumber,110,90,105,1000\n"  # unparseable
        "2024-01-03,ACME,100,90,110,105,1000\n"  # high < low
        "2024-01-04,ACME,100,110,90,105,-5\n"  # negative volume
        ",ACME,100,110,90,105,1000\n"  # no timestamp
        "2024-01-05,ACME,0,110,90,105,1000\n"  # zero price
    )
    _, result = _clean(text)
    reasons = {row.reason.value for row in result.rejected}
    assert reasons == {
        "duplicate",
        "invalid_price",
        "inconsistent_ohlc",
        "negative_volume",
        "missing_timestamp",
        "non_positive_price",
    }
    for row in result.rejected:
        assert row.line_number > 0
        assert row.detail
        assert row.raw


def test_an_unparseable_price_is_rejected_not_filled() -> None:
    """A present-but-garbage value must not become synthetic data."""

    text = "Date,Symbol,Open,High,Low,Close\n2024-01-01,ACME,notanumber,110,90,105\n"
    _, result = _clean(text)
    assert result.rows == []
    assert result.rejected[0].reason.value == "invalid_price"


def test_an_absent_price_is_filled_from_close_and_recorded() -> None:
    text = "Date,Symbol,Open,High,Low,Close\n2024-01-01,ACME,,,,105\n"
    _, result = _clean(text)
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.open == row.high == row.low == Decimal("105")
    filled = [t for t in result.transformations if t.kind.value == "filled_ohlc_from_close"]
    assert {t.column for t in filled} == {"open", "high", "low"}
    for transformation in filled:
        assert transformation.after == "105"


def test_duplicate_policy_keep_last_supersedes(db=None) -> None:
    text = "Date,Symbol,Close\n2024-01-01,ACME,100\n2024-01-01,ACME,999\n"
    _, result = _clean(text, CleaningPolicy(duplicate_policy=DuplicatePolicy.KEEP_LAST))
    assert len(result.rows) == 1
    assert result.rows[0].close == Decimal("999")


def test_duplicate_policy_reject_dataset_is_fatal() -> None:
    text = "Date,Symbol,Close\n2024-01-01,ACME,100\n2024-01-01,ACME,100\n"
    _, result = _clean(text, CleaningPolicy(duplicate_policy=DuplicatePolicy.REJECT_DATASET))
    assert result.fatal is not None
    assert result.rows == []


def test_out_of_order_rows_are_dropped_and_recorded() -> None:
    text = "Date,Symbol,Close\n2024-01-01,ACME,100\n2024-01-03,ACME,102\n2024-01-02,ACME,101\n"
    _, result = _clean(text)
    assert len(result.rows) == 2
    assert result.rejected[0].reason.value == "out_of_order"


def test_rows_are_sorted_chronologically_and_the_sort_is_recorded() -> None:
    """AlphaLab requires chronological records; reordering must be visible."""

    text = "Date,Symbol,Close\n2024-01-01,BETA,50\n2024-01-01,ALPHA,100\n2024-01-02,BETA,51\n"
    _, result = _clean(text)
    assert [r.timestamp for r in result.rows] == sorted(r.timestamp for r in result.rows)
    assert result.rows[0].symbol == "ALPHA"
    assert any(t.kind.value == "reordered" for t in result.transformations)


# --- quality ----------------------------------------------------------------


def test_frequency_inference_uses_the_median_gap() -> None:
    """A weekend must not turn daily bars into something else."""

    day = 86400.0
    timestamps = [0, day, 2 * day, 5 * day, 6 * day, 7 * day, 8 * day]
    label, gap = infer_frequency(timestamps)
    assert label == "1d"
    assert gap == day


def test_irregular_spacing_is_reported_not_rounded() -> None:
    label, gap = infer_frequency([0.0, 137.0, 940.0, 2600.0, 41000.0])
    assert label is None
    assert gap is not None


def test_a_clean_file_scores_high_and_a_broken_one_scores_zero() -> None:
    clean_text = "Date,Symbol,Open,High,Low,Close,Volume\n" + "".join(
        f"2024-01-{d:02d},ACME,100,110,90,105,1000\n" for d in range(1, 21)
    )
    schema, result = _clean(clean_text)
    assert analyse(result, schema).score > 90

    schema, result = _clean("date,close\n03/04/2024,100\n05/06/2024,101\n07/08/2024,1\n")
    assert analyse(result, schema).score == 0.0


def test_the_report_counts_match_the_cleaning_result() -> None:
    text = "Date,Symbol,Close\n2024-01-01,ACME,100\n2024-01-01,ACME,100\n,ACME,100\n"
    schema, result = _clean(text)
    report = analyse(result, schema)
    assert report.total_input_rows == 3
    assert report.accepted_rows == len(result.rows)
    assert report.rejected_rows == result.rejected_count
    assert report.duplicate_count == 1
