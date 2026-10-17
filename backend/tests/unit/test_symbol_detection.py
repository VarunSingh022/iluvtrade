"""Which column, if any, names the instrument -- decided on evidence, never on a hunch.

The regression: plotly's ``finance-charts-apple.csv`` has no symbol column; its
prices are headed ``AAPL.Open`` ... ``AAPL.Volume`` and it carries a
``direction`` column (``Increasing`` / ``Decreasing``). The content fallback
took ``direction`` as the symbol because it was text with few distinct values,
and the import split one stock into two fake instruments. A column is now the
symbol by content only when the file demonstrably holds several instruments;
an instrument named in every price header is recognised as such; anything in
between is refused with the reason.
"""

from __future__ import annotations

import pytest

from iluvtrade.data.cleaning import CleaningPolicy, clean
from iluvtrade.data.schema import SAMPLE_ROWS, FieldRole, detect_schema

#: The header and first twelve rows of the original file, verbatim.
APPLE_CSV = """\
Date,AAPL.Open,AAPL.High,AAPL.Low,AAPL.Close,AAPL.Volume,AAPL.Adjusted,dn,mavg,up,direction
2015-02-17,127.489998,128.880005,126.919998,127.830002,63152400,122.905254,106.7410523,117.9276669,129.1142814,Increasing
2015-02-18,127.629997,128.779999,127.449997,128.720001,44891700,123.760965,107.842423,118.9403335,130.0382439,Increasing
2015-02-19,128.479996,129.029999,128.330002,128.449997,37362400,123.501363,108.8942449,119.8891668,130.8840887,Decreasing
2015-02-20,128.619995,129.5,128.050003,129.5,48948400,124.510914,109.7854494,120.7635001,131.7415509,Increasing
2015-02-23,130.020004,133,129.660004,133,70974100,127.876074,110.3725162,121.7201668,133.0678174,Increasing
2015-02-24,132.940002,133.600006,131.169998,132.169998,69228100,127.078049,111.0948689,122.6648335,134.2347981,Decreasing
2015-02-25,131.559998,131.600006,128.149994,128.789993,74711700,123.828261,113.2119183,123.6296667,134.0474151,Decreasing
2015-02-26,128.789993,130.869995,126.610001,130.419998,91287500,125.395469,114.1652991,124.2823333,134.3993674,Increasing
2015-02-27,130,130.570007,128.240005,128.460007,62014800,123.510987,114.9668484,124.8426669,134.7184854,Decreasing
2015-03-02,129.25,130.279999,128.300003,129.089996,48096700,124.116706,115.8770904,125.4036668,134.9302432,Decreasing
2015-03-03,128.960007,129.520004,128.089996,129.360001,37816300,124.376308,116.9535132,125.9551669,134.9568205,Increasing
2015-03-04,129.100006,129.559998,128.320007,128.539993,31666300,123.587892,118.0874253,126.4730002,134.8585751,Decreasing
"""

DAYS = [f"2024-01-{d:02d}" for d in range(1, 13)]


def _clean(text: str):
    schema = detect_schema(text)
    return schema, clean(text, schema, CleaningPolicy())


def _long(header: str, rows: list[tuple[str, ...]]) -> str:
    return "\n".join([header, *(",".join(r) for r in rows)]) + "\n"


# --- the regression -------------------------------------------------------------


def test_the_apple_file_is_one_instrument_named_by_its_price_headers() -> None:
    schema, result = _clean(APPLE_CSV)

    assert schema.column_for(FieldRole.SYMBOL) is None  # ``direction`` is not a symbol
    assert schema.header_symbol == "AAPL"
    assert schema.symbol_ambiguous is False
    assert [schema.column_for(r) for r in (FieldRole.OPEN, FieldRole.CLOSE, FieldRole.VOLUME)] == [
        "AAPL.Open",
        "AAPL.Close",  # the raw close, not AAPL.Adjusted
        "AAPL.Volume",
    ]
    assert any("'AAPL'" in w for w in schema.warnings)

    assert result.fatal is None
    assert {row.symbol for row in result.rows} == {"AAPL"}
    assert len(result.rows) == 12
    assert str(result.rows[0].close) == "127.830002"


def test_without_the_header_prefix_the_direction_column_is_still_not_a_symbol() -> None:
    """Same file, plain headers: one instrument, the policy default, and a note why."""

    plain = APPLE_CSV.replace("AAPL.", "", 5)
    schema, result = _clean(plain)
    assert schema.column_for(FieldRole.SYMBOL) is None
    assert schema.header_symbol is None
    assert schema.symbol_ambiguous is False
    assert any("'direction'" in w and "every timestamp occurs once" in w for w in schema.warnings)
    assert {row.symbol for row in result.rows} == {CleaningPolicy().default_symbol}
    assert len(result.rows) == 12


# --- legitimate multi-instrument files keep working ----------------------------


def test_an_explicit_symbol_column_is_used_as_before() -> None:
    rows = [(d, s, "10", "11", "9", "10", "100") for d in DAYS for s in ("TCS", "INFY")]
    schema, result = _clean(_long("Date,Symbol,Open,High,Low,Close,Volume", rows))
    assert schema.column_for(FieldRole.SYMBOL) == "Symbol"
    assert schema.matches[FieldRole.SYMBOL].method == "header-exact"
    assert {row.symbol for row in result.rows} == {"TCS", "INFY"}
    assert len(result.rows) == 24


def test_an_unnamed_column_of_instrument_codes_is_found_by_its_structure() -> None:
    rows = [(d, s, "10", "11", "9", "10", "100") for d in DAYS for s in ("RELIANCE", "TCS")]
    schema, result = _clean(_long("Date,code,Open,High,Low,Close,Volume", rows))
    match = schema.matches[FieldRole.SYMBOL]
    assert (match.column, match.method) == ("code", "content-symbol")
    assert {row.symbol for row in result.rows} == {"RELIANCE", "TCS"}


def test_a_file_grouped_by_instrument_is_judged_on_all_of_it() -> None:
    """The preview sees only the first instrument; the decision reads the whole file."""

    days = [
        f"{2000 + i // 365}-{1 + (i // 28) % 12:02d}-{1 + i % 28:02d}"
        for i in range(SAMPLE_ROWS + 50)
    ]
    days = sorted(set(days))
    assert len(days) > SAMPLE_ROWS  # the preview holds only AAA rows
    rows = [(d, s, "10", "11", "9", "10", "100") for s in ("AAA", "BBB") for d in days]
    schema, result = _clean(_long("Date,code,Open,High,Low,Close,Volume", rows))
    assert schema.column_for(FieldRole.SYMBOL) == "code"
    assert {row.symbol for row in result.rows} == {"AAA", "BBB"}
    assert len(result.rows) == 2 * len(days)


# --- ambiguity is refused, not guessed -----------------------------------------


def test_a_categorical_column_that_partitions_repeated_timestamps_is_refused() -> None:
    """Timestamps repeat and ``side`` separates them, but Buy/Sell are not instruments."""

    rows = [(d, side, "10", "11", "9", "10", "100") for d in DAYS for side in ("Buy", "Sell")]
    schema, result = _clean(_long("Date,side,Open,High,Low,Close,Volume", rows))
    assert schema.column_for(FieldRole.SYMBOL) is None
    assert schema.symbol_ambiguous is True
    assert result.fatal is not None and result.rows == []
    assert "'side'" in result.fatal and "Symbol" in result.fatal
    assert schema.to_dict()["symbol_ambiguous"] is True


def test_two_columns_that_could_each_be_the_symbol_are_refused() -> None:
    rows = [(d, s, f"{s}-EQ", "10", "11", "9", "10", "100") for d in DAYS for s in ("TCS", "INFY")]
    schema, result = _clean(_long("Date,code,series,Open,High,Low,Close,Volume", rows))
    assert schema.symbol_ambiguous is True
    assert result.fatal is not None
    assert "'code'" in result.fatal and "'series'" in result.fatal


@pytest.mark.parametrize(
    "header",
    [
        "Date,AAPL.Open,AAPL.High,AAPL.Low,AAPL.Close,MSFT.Close",
        "Date,AAPL_Close,MSFT_Close",
    ],
)
def test_one_column_per_instrument_is_refused_rather_than_half_imported(header: str) -> None:
    """Matching takes one close column; the others would be dropped without a word."""

    width = header.count(",")
    rows = [(d, *(["10"] * width)) for d in DAYS]
    schema, result = _clean(_long(header, rows))
    assert schema.symbol_ambiguous is True
    assert schema.header_symbol is None
    assert result.fatal is not None
    assert "'AAPL'" in result.fatal and "'MSFT'" in result.fatal


def test_a_header_prefix_on_only_some_price_columns_names_no_instrument() -> None:
    rows = [(d, "10", "11", "9", "10") for d in DAYS]
    schema, result = _clean(_long("Date,AAPL.Open,High,Low,Close", rows))
    assert schema.header_symbol is None
    assert schema.symbol_ambiguous is False
    assert {row.symbol for row in result.rows} == {CleaningPolicy().default_symbol}
