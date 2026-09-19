"""Canonical dataset rows → AlphaLab market datasets.

The canonical row written by :mod:`iluvtrade.data.cleaning` is exactly the
shape of an ``alphalab.market.bar.Bar``, which is not a coincidence: the
importer targets AlphaLab's vocabulary so that nothing in between has to
translate or round.

``Decimal`` is preserved end to end. The canonical file stores prices as strings
for that reason — a float round-trip would make the accounting identity
AlphaLab asserts (``equity == cash + realized + unrealized - commission``) fail
by fractions of a cent for reasons that have nothing to do with the strategy.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from decimal import Decimal
from typing import Any

from alphalab.backtesting import MarketDataset
from alphalab.market.bar import Bar, TimeFrame
from alphalab.market.source import OrderingGuarantee, SequenceSource

from iluvtrade.alphalab_bridge.instruments import InstrumentUniverse

__all__ = [
    "TIMEFRAMES",
    "bars_from_rows",
    "dataset_from_rows",
    "source_from_rows",
    "timeframe_for",
]

#: Frequency label (as the quality report infers it) → AlphaLab's enum.
TIMEFRAMES: dict[str, TimeFrame] = {
    "1m": TimeFrame.M1,
    "5m": TimeFrame.M5,
    "15m": TimeFrame.M15,
    "1h": TimeFrame.H1,
    "4h": TimeFrame.H4,
    "1d": TimeFrame.D1,
    "1w": TimeFrame.W1,
    "1M": TimeFrame.MN1,
}


def timeframe_for(label: str | None) -> TimeFrame:
    """AlphaLab's timeframe for a label, defaulting to daily.

    A dataset whose spacing the importer could not classify is carried as daily.
    That is a *label*, not a resampling: no bar is moved, merged or created, and
    the quality report says the frequency is irregular.
    """

    return TIMEFRAMES.get(label or "", TimeFrame.D1)


def bars_from_rows(
    rows: Iterable[dict[str, Any]],
    *,
    timeframe: TimeFrame,
    universe: InstrumentUniverse,
    symbols: frozenset[str] | None = None,
) -> Iterator[Bar]:
    """Turn canonical rows into AlphaLab bars, optionally narrowing the universe.

    ``asset_id`` is the identity AlphaLab derived for the symbol, not the symbol
    itself: ``alphalab.core.Fill`` refuses a non-UUID asset id, so a bar carrying
    a raw ticker reaches the engine and fails at the last stage of the first
    fill. See :mod:`iluvtrade.alphalab_bridge.instruments`.
    """

    for row in rows:
        symbol = str(row["symbol"])
        if symbols is not None and symbol not in symbols:
            continue
        asset_id = universe.by_symbol.get(symbol)
        if asset_id is None:
            # A row for an instrument the universe did not declare. Refusing is
            # the only honest answer: silently skipping it would produce a
            # backtest over less data than the user selected, and nothing in the
            # result would say so.
            raise KeyError(
                f"Symbol {symbol!r} is not in the declared instrument universe "
                f"({', '.join(sorted(universe.by_symbol)) or 'empty'})."
            )
        yield Bar(
            asset_id=asset_id,
            timestamp=float(row["timestamp"]),
            open=Decimal(str(row["open"])),
            high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])),
            close=Decimal(str(row["close"])),
            volume=Decimal(str(row["volume"])),
            vwap=Decimal(str(row["vwap"])),
            trade_count=int(row.get("trade_count", 0)),
            timeframe=timeframe,
        )


def dataset_from_rows(
    dataset_id: str,
    rows: Iterable[dict[str, Any]],
    *,
    universe: InstrumentUniverse,
    frequency: str | None = None,
    symbols: frozenset[str] | None = None,
) -> MarketDataset:
    """Build the dataset a backtest walks.

    ``dataset_id`` becomes the prefix of every record id, so a run's record
    identities are derived from the dataset version it actually read — which is
    what makes two runs of the same version comparable record by record.
    """

    bars = list(
        bars_from_rows(rows, timeframe=timeframe_for(frequency), universe=universe, symbols=symbols)
    )
    return MarketDataset.of(dataset_id, bars)


def source_from_rows(
    source_id: str,
    rows: Iterable[dict[str, Any]],
    *,
    universe: InstrumentUniverse,
    frequency: str | None = None,
    symbols: frozenset[str] | None = None,
) -> SequenceSource:
    """Build the source a paper session reads.

    Identities match :func:`dataset_from_rows` for the same inputs, because
    AlphaLab derives both the same way — so a paper session and a backtest over
    one dataset version read the same records under the same ids.
    """

    bars = list(
        bars_from_rows(rows, timeframe=timeframe_for(frequency), universe=universe, symbols=symbols)
    )
    return SequenceSource.of(source_id, bars)


__all__ += ["OrderingGuarantee"]
