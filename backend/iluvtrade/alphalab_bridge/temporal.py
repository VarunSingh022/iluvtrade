"""What a dataset's timestamps mean, decided by AlphaLab's rules.

A bar summarizes an interval and its close is knowable only when the interval
ends; AlphaLab's canonical ``market.Bar`` is stamped at that end (ledger
DAT-001). How a *source's* timestamp becomes that instant is AlphaLab's
interpretation, and this module only hands it the decisions the user declared
(:mod:`iluvtrade.data.temporal`). Nothing here guesses a zone, a session close or
a calendar:

* a **bare date** names a whole day in a declared IANA zone, and is read as
  23:59:59.999999 of that civil date (``alphalab.data.parse_timestamp`` with
  ``DATE_ONLY`` and ``DateOnlyPolicy.END_OF_DAY``). That is the end of the
  calendar day, not the exchange's closing time: it is at or after the session's
  end only when the session ends on that date in that zone, which is why the
  zone must be the exchange's;
* a **wall-clock time** without an offset is read in its declared zone
  (``ISO_8601_NAIVE``); one *with* an offset, or an epoch value, already names an
  instant and AlphaLab refuses a zone for it;
* a bar **stamped at the start** of its interval is moved to the end of its
  *declared* interval (never one inferred from the data) by
  ``alphalab.market.normalize_wire_bar`` with ``BarStamp.INTERVAL_START`` -- which
  refuses an interval with no fixed length, such as a month.

Only the timestamp of the wire bar built for the move is used. AlphaLab's wire
``Bar`` carries ``float`` prices, so the bar's prices stay the exact decimals the
canonical row holds.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from alphalab.data import (
    Bar as WireBar,
)
from alphalab.data import (
    BarStamp,
    DataValidationError,
    DateOnlyPolicy,
    TimestampFormat,
    parse_timestamp,
    resolve_zone,
)
from alphalab.market import (
    MarketValidationError,
    NormalizationPolicy,
    TimeFrame,
    normalize_wire_bar,
)

__all__ = [
    "TemporalInterpretationError",
    "TimestampKind",
    "end_of_interval",
    "instant",
    "interval",
    "require_zone",
]


class TimestampKind(StrEnum):
    """How one raw timestamp is written, which decides what AlphaLab needs to read it."""

    #: A calendar date and nothing else: needs a zone, names a whole day.
    DATE = "date"
    #: A wall-clock date and time with no offset: needs a zone.
    NAIVE = "naive"
    #: An ISO-8601 date and time with an offset: already an instant.
    AWARE = "aware"
    EPOCH_SECONDS = "epoch_seconds"
    EPOCH_MILLIS = "epoch_millis"


class TemporalInterpretationError(ValueError):
    """AlphaLab could not read a timestamp under the declared conventions."""


_FORMATS: dict[TimestampKind, TimestampFormat] = {
    TimestampKind.DATE: TimestampFormat.DATE_ONLY,
    TimestampKind.NAIVE: TimestampFormat.ISO_8601_NAIVE,
    TimestampKind.AWARE: TimestampFormat.ISO_8601_AWARE,
    TimestampKind.EPOCH_SECONDS: TimestampFormat.EPOCH_SECONDS,
    TimestampKind.EPOCH_MILLIS: TimestampFormat.EPOCH_MILLISECONDS,
}

#: The kinds whose text names no instant until a zone is supplied.
ZONED_KINDS = frozenset({TimestampKind.DATE, TimestampKind.NAIVE})


def require_zone(name: str) -> str:
    """``name`` if it is an IANA zone this host can resolve, else a refusal."""

    try:
        resolve_zone(name)
    except DataValidationError as exc:
        raise TemporalInterpretationError(str(exc)) from None
    return name


def interval(code: str) -> tuple[str, float | None]:
    """AlphaLab's canonical code for a bar interval, and its length in seconds.

    The length is ``None`` for an interval counted in months, which has no fixed
    length. A code AlphaLab does not accept is refused.
    """

    try:
        timeframe = TimeFrame.parse(code)
    except MarketValidationError as exc:
        raise TemporalInterpretationError(str(exc)) from None
    return timeframe.code, timeframe.seconds


def instant(text: str, kind: TimestampKind, zone: str | None) -> float:
    """The instant ``text`` names, in Unix seconds, as AlphaLab reads it.

    ``zone`` is passed only for the kinds that need one; AlphaLab refuses one
    for an instant that already carries its offset, and so does this.
    """

    try:
        return parse_timestamp(
            text,
            _FORMATS[kind],
            zone if kind in ZONED_KINDS else None,
            DateOnlyPolicy.END_OF_DAY if kind is TimestampKind.DATE else None,
        )
    except DataValidationError as exc:
        raise TemporalInterpretationError(str(exc)) from None


def end_of_interval(
    timestamp: float,
    frequency: str,
    *,
    symbol: str,
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: Decimal,
) -> float:
    """Where a bar stamped at the *start* of its ``frequency`` interval ends.

    ``frequency`` is an AlphaLab interval code (``"1d"``, ``"15m"``, ``"1w"``). The
    move is ``normalize_wire_bar``'s; it refuses an interval of no fixed length.
    """

    try:
        policy = NormalizationPolicy(
            timeframe=TimeFrame.parse(frequency), bar_stamp=BarStamp.INTERVAL_START
        )
        bar = normalize_wire_bar(
            WireBar(
                symbol,
                timestamp,
                float(open_),
                float(high),
                float(low),
                float(close),
                float(volume),
            ),
            policy,
        )
    except (MarketValidationError, DataValidationError) as exc:
        raise TemporalInterpretationError(str(exc)) from None
    return bar.timestamp
