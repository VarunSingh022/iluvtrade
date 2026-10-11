"""What an uploaded dataset's timestamps mean: declared by the user, never guessed.

AlphaLab stamps a bar at the **end** of its interval, the instant its close
became knowable (ledger DAT-001). A CSV says nothing about that: ``2024-01-02``
might be the trading session of that date, ``09:15`` might open or close a
fifteen-minute bar, and a wall-clock time names no instant without a zone.
Until this module existed the importer read every bare date as UTC midnight and
every wall-clock time as UTC, so an NSE daily bar reached the engine at 05:30
IST -- before the market opened -- carrying a close not knowable until 15:30.

So the meaning is a **declaration** the importer requires, and the importer
refuses a file it cannot read under it:

====================  =====================================  ==================
Convention            Means                                  Accepted for
====================  =====================================  ==================
``session_date``      a bare date naming the trading         dates, daily bars
                      session the bar summarizes
``interval_end``      the timestamp names the end of the     dates and times
                      bar's interval
``interval_start``    the timestamp names the start of the   times only
                      bar's interval
====================  =====================================  ==================

A zone is required exactly when the values need one -- bare dates and
wall-clock times -- and refused when they already carry an offset or are epoch
values. A date labelling the *start* of an interval is refused rather than
moved by a guessed number of days: declare ``session_date`` for daily bars
named by their trading date, or re-export with interval-end labels.

The reading itself is AlphaLab's (:mod:`iluvtrade.alphalab_bridge.temporal`).
This module owns the vocabulary, the rules about which declarations make sense
for which file, and the record kept on the dataset version.
"""

from __future__ import annotations

import calendar
import csv
import io
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

from iluvtrade.alphalab_bridge.temporal import (
    TemporalInterpretationError,
    TimestampKind,
    end_of_interval,
    instant,
    interval,
    require_zone,
)
from iluvtrade.data.schema import DetectedSchema, FieldRole, TimestampFormat, parse_timestamp

__all__ = [
    "InstrumentSpacing",
    "SpacingReport",
    "TemporalDeclaration",
    "TemporalError",
    "TimestampConvention",
    "TimestampKind",
    "TimestampSurvey",
    "check_spacing",
    "declaration_from_record",
    "end_of_interval_for",
    "kind_of",
    "label_instant",
    "survey",
    "validate",
]

#: Bumped if what a recorded declaration means ever changes. 2: the bar interval
#: is declared rather than inferred. 3: it is validated per instrument, with
#: calendar-aware arithmetic and an explicit missing-observation confirmation;
#: version 2 counted evidence across the whole file, so one instrument's bars
#: could vouch for another's, and is not accepted as validated provenance.
#: 4: the missing-observation confirmation names the instruments it covers; in
#: 3 it was one dataset-wide flag, given before the user could see which
#: instruments it would cover (no released build wrote 3).
RECORD_VERSION = 4


class TimestampConvention(StrEnum):
    SESSION_DATE = "session_date"
    INTERVAL_START = "interval_start"
    INTERVAL_END = "interval_end"


class TemporalError(ValueError):
    """The declaration is missing, contradicts the file, or names a bad zone."""


@dataclass(frozen=True, slots=True)
class TemporalDeclaration:
    convention: TimestampConvention
    #: IANA zone the values are written in. Only for bare dates and wall-clock
    #: times; ``None`` otherwise.
    timezone: str | None = None
    #: The interval each bar covers, as an AlphaLab interval code (``"15m"``,
    #: ``"1h"``, ``"1d"``). Declared, never inferred: ``session_date`` implies
    #: ``"1d"``; every other convention must state it. The data may refuse it
    #: (:func:`check_spacing`) but never replaces it.
    bar_interval: str | None = None
    #: The instruments for which the user declares that the gaps are *missing
    #: observations* of bars of the declared interval -- not longer bars. Only an
    #: instrument whose own timestamps cannot show the interval (no two
    #: consecutive bars one interval apart) needs it, and it must name exactly
    #: those: timestamps alone cannot tell a sparse 15-minute series from an
    #: hourly one, so the user says which it is, instrument by instrument. It is
    #: a declaration, never a verification.
    missing_observations_confirmed_for: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class TimestampSurvey:
    """How a file's timestamp column is written, row by row."""

    kinds: Counter[TimestampKind] = field(default_factory=Counter)

    @property
    def kind(self) -> TimestampKind | None:
        """The one kind every readable value has, or ``None`` if mixed or empty."""

        return next(iter(self.kinds)) if len(self.kinds) == 1 else None

    @property
    def requires_timezone(self) -> bool:
        return self.kind in (TimestampKind.DATE, TimestampKind.NAIVE)

    @property
    def conventions(self) -> tuple[TimestampConvention, ...]:
        """The conventions that make sense for this file; empty if none do."""

        if self.kind is None:
            return ()
        if self.kind is TimestampKind.DATE:
            return (TimestampConvention.SESSION_DATE, TimestampConvention.INTERVAL_END)
        return (TimestampConvention.INTERVAL_START, TimestampConvention.INTERVAL_END)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value if self.kind else None,
            "kinds": {kind.value: count for kind, count in sorted(self.kinds.items())},
            "requires_timezone": self.requires_timezone,
            "conventions": [c.value for c in self.conventions],
        }


_SLASH_FORMATS = (TimestampFormat.SLASH_DMY, TimestampFormat.SLASH_MDY)


def kind_of(raw: str, fmt: TimestampFormat) -> TimestampKind | None:
    """How one raw timestamp is written, or ``None`` if it cannot be read at all."""

    text = raw.strip()
    if not text:
        return None
    if fmt is TimestampFormat.EPOCH_SECONDS:
        return TimestampKind.EPOCH_SECONDS
    if fmt is TimestampFormat.EPOCH_MILLIS:
        return TimestampKind.EPOCH_MILLIS
    if fmt is TimestampFormat.DATE_ONLY:
        return TimestampKind.DATE
    if fmt in _SLASH_FORMATS:
        return TimestampKind.NAIVE if ":" in text else TimestampKind.DATE
    if fmt in (TimestampFormat.ISO_8601, TimestampFormat.DATETIME_SPACE):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if len(text) <= 10:
            return TimestampKind.DATE
        return TimestampKind.AWARE if parsed.tzinfo is not None else TimestampKind.NAIVE
    return None


def survey(text: str, schema: DetectedSchema) -> TimestampSurvey:
    """Classify every timestamp cell in ``text`` under the detected format."""

    index = schema.index_for(FieldRole.TIMESTAMP)
    kinds: Counter[TimestampKind] = Counter()
    if index is None:
        return TimestampSurvey(kinds)
    reader = csv.reader(io.StringIO(text), delimiter=schema.delimiter)
    for line_number, row in enumerate(reader, start=1):
        if (line_number == 1 and schema.has_header) or index >= len(row):
            continue
        kind = kind_of(row[index], schema.timestamp_format)
        if kind is not None:
            kinds[kind] += 1
    return TimestampSurvey(kinds)


def _choices(found: TimestampSurvey) -> str:
    return ", ".join(c.value for c in found.conventions)


def validate(
    declaration: TemporalDeclaration | None, found: TimestampSurvey
) -> TemporalDeclaration:
    """The declaration, if it makes sense for the file; otherwise a refusal saying why."""

    kind = found.kind
    if kind is None:
        mixed = ", ".join(sorted(k.value for k in found.kinds)) or "none readable"
        raise TemporalError(
            "The timestamp column mixes forms that need different readings "
            f"({mixed}). Re-export it in one form."
        )
    if declaration is None:
        raise TemporalError(
            f"Declare what this file's timestamps mean before importing it. They are "
            f"{kind.value} values; choose one of: {_choices(found)}"
            + (", and the IANA time zone they are written in." if found.requires_timezone else ".")
        )
    convention = declaration.convention
    if convention not in found.conventions:
        if kind is TimestampKind.DATE and convention is TimestampConvention.INTERVAL_START:
            raise TemporalError(
                "A bare date that labels the start of its interval would have to be moved by "
                "a whole interval, which is not done on a guess. Declare session_date for daily "
                "bars named by their trading date, or interval_end if each date is the last day "
                "of its bar."
            )
        raise TemporalError(
            f"{convention.value} does not apply to {kind.value} values; choose one of: "
            f"{_choices(found)}."
        )
    if found.requires_timezone:
        if not declaration.timezone:
            raise TemporalError(
                f"{kind.value.capitalize()} values name no instant without a time zone. "
                "Declare the IANA zone they are written in (for an NSE or BSE session, "
                "Asia/Kolkata)."
            )
        try:
            require_zone(declaration.timezone)
        except TemporalInterpretationError as exc:
            raise TemporalError(str(exc)) from None
    elif declaration.timezone:
        raise TemporalError(
            f"These timestamps are {kind.value} values and already name an instant, so a "
            f"time zone would have no effect. Leave it unset."
        )
    checked = _with_interval(declaration)
    _, seconds = interval(checked.bar_interval or "")
    if kind is TimestampKind.DATE and seconds is not None and seconds < _DAY:
        raise TemporalError(
            f"Bare dates cannot label {checked.bar_interval} bars: a date names a whole day."
        )
    if kind not in _ZONED_KINDS and seconds is None:
        raise TemporalError(
            f"{checked.bar_interval} bars are calendar months, which need dates or wall-clock "
            "times in a declared zone to count; these timestamps are "
            f"{kind.value} values. Re-export them as dates."
        )
    return checked


def _with_interval(declaration: TemporalDeclaration) -> TemporalDeclaration:
    """The declaration with its bar interval stated in AlphaLab's canonical code."""

    declared = declaration.bar_interval
    if declaration.convention is TimestampConvention.SESSION_DATE:
        if declared is None:
            declared = "1d"
        try:
            code, _ = interval(declared)
        except TemporalInterpretationError as exc:
            raise TemporalError(str(exc)) from None
        if code != "1d":
            raise TemporalError(
                f"A session date names one trading day, so its bars are daily; {code!r} "
                "contradicts it. Declare interval_end if each date is the last day of a longer bar."
            )
        return replace(declaration, bar_interval=code)
    if not declared:
        raise TemporalError(
            "Declare the bar interval (for example 15m, 1h or 1d). It is never inferred "
            "from the spacing of the bars, which missing bars, overnight and weekend gaps "
            "and mixed frequencies all distort."
        )
    try:
        code, seconds = interval(declared)
    except TemporalInterpretationError as exc:
        raise TemporalError(str(exc)) from None
    if seconds is None and declaration.convention is TimestampConvention.INTERVAL_START:
        raise TemporalError(
            f"{code} bars have no fixed length, so a bar stamped at the start of one cannot be "
            "moved to its end. Re-export with interval-end timestamps."
        )
    return replace(declaration, bar_interval=code)


@dataclass(frozen=True, slots=True)
class InstrumentSpacing:
    """What one instrument's bar times showed about the declared interval."""

    bars: int
    #: Consecutive bars exactly one declared interval apart.
    one_interval_pairs: int
    #: Consecutive bars a whole number (> 1) of intervals apart: missing bars,
    #: overnight, weekend or holiday gaps. Not told apart -- no calendar is assumed.
    longer_gaps: int
    #: True when nothing in this instrument's own timestamps shows the declared
    #: interval and it was accepted only on the missing-observation confirmation.
    accepted_on_confirmation: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "bars": self.bars,
            "one_interval_pairs": self.one_interval_pairs,
            "longer_gaps": self.longer_gaps,
            "accepted_on_confirmation": self.accepted_on_confirmation,
        }


@dataclass(frozen=True, slots=True)
class SpacingReport:
    """What the observed spacing showed about the declared interval. Recorded."""

    #: How gaps were measured: ``wall_clock`` (dates and zoned wall-clock times,
    #: in the declared zone), ``absolute`` (offset-aware and epoch instants) or
    #: ``calendar_months``.
    arithmetic: str
    instruments: dict[str, InstrumentSpacing]
    #: Instruments whose own bars never sit one interval apart: the interval is
    #: the user's declaration for them, not something their bars show.
    ambiguous: tuple[str, ...]
    missing_observations_confirmed_for: frozenset[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "arithmetic": self.arithmetic,
            "instruments": {name: spacing.to_dict() for name, spacing in self.instruments.items()},
            "instrument_count": len(self.instruments),
            "one_interval_pairs": sum(i.one_interval_pairs for i in self.instruments.values()),
            "longer_gaps": sum(i.longer_gaps for i in self.instruments.values()),
            "accepted_on_confirmation": sorted(
                name for name, i in self.instruments.items() if i.accepted_on_confirmation
            ),
            "ambiguous": list(self.ambiguous),
            "missing_observations_confirmed_for": sorted(self.missing_observations_confirmed_for),
        }


_DAY = 86_400.0
#: Equal timestamps differ in float arithmetic by far less than this.
_EPSILON = 1e-6
_ZONED_KINDS = frozenset({TimestampKind.DATE, TimestampKind.NAIVE})
_WALL_EPOCH = datetime(1970, 1, 1)


def _wall(instant_: float, zone: ZoneInfo) -> datetime:
    """The naive wall-clock time ``instant_`` shows in ``zone``."""

    return datetime.fromtimestamp(instant_, zone).replace(tzinfo=None)


def check_spacing(
    timestamps_by_symbol: dict[str, list[float]],
    declaration: TemporalDeclaration,
    kind: TimestampKind,
    *,
    enforce_confirmation: bool = True,
) -> SpacingReport:
    """Validate the declared interval against **each instrument's** bar times, or refuse.

    The observed spacing can only *refuse* a declaration, never replace it, and
    each instrument answers for itself -- one instrument's bars never vouch for
    another's:

    * a gap shorter than one interval means bars overlap: the file mixes bar
      frequencies (a dataset has one bar interval) or the interval is wrong;
    * a gap that is not a whole number of intervals is irregular;
    * an instrument with no two consecutive bars one interval apart (or only one
      bar) is **ambiguous**: its timestamps fit bars of the declared interval with
      observations missing *and* longer bars equally well, and no timestamp-only
      rule can tell which. It is refused unless the declaration names it in
      ``missing_observations_confirmed_for`` -- the user's statement that its gaps
      are missing observations. The confirmation must name exactly the ambiguous
      instruments: an instrument not in the file, or one whose own bars show the
      interval, is refused, so a confirmation never becomes a blanket waiver.

    ``enforce_confirmation=False`` (the upload preview) reports the ambiguous
    instruments instead of refusing them; every other refusal still applies.

    Gaps are measured in the units the timestamps actually carry. Dates and zoned
    wall-clock times are compared on the declared zone's wall clock, so a
    daylight-saving change neither breaks nor excuses a daily series; offset-aware
    and epoch instants are compared in absolute seconds, exactly; monthly bars are
    counted in calendar months and must be labelled on a consistent day.
    ``timestamps_by_symbol`` holds each bar's *label* instant, before any move to
    the interval's end.
    """

    assert declaration.bar_interval is not None
    code, seconds = interval(declaration.bar_interval)
    zone = ZoneInfo(declaration.timezone) if declaration.timezone else None
    zoned = kind in _ZONED_KINDS
    if zoned and zone is None:  # pragma: no cover - validate() requires the zone
        raise TemporalError("A zone is required to measure these timestamps.")

    instruments: dict[str, InstrumentSpacing] = {}
    ambiguous: list[str] = []
    for symbol, stamps in sorted(timestamps_by_symbol.items()):
        ordered = sorted(stamps)
        if seconds is None:
            assert zone is not None  # validate() refuses months without a zone
            counts = _month_steps(symbol, [_wall(t, zone) for t in ordered], code)
        else:
            if zoned:
                assert zone is not None
                positions = [(_wall(t, zone) - _WALL_EPOCH).total_seconds() for t in ordered]
            else:
                positions = list(ordered)
            counts = _fixed_steps(symbol, positions, seconds, code, absolute=not zoned)
            if (
                zoned
                and kind is TimestampKind.NAIVE
                and declaration.convention is TimestampConvention.INTERVAL_START
            ):
                assert zone is not None
                _refuse_bars_spanning_an_offset_change(symbol, ordered, seconds, zone)
        exact, longer = counts
        unshown = exact == 0
        if unshown:
            ambiguous.append(symbol)
        instruments[symbol] = InstrumentSpacing(
            bars=len(ordered),
            one_interval_pairs=exact,
            longer_gaps=longer,
            accepted_on_confirmation=unshown
            and symbol in declaration.missing_observations_confirmed_for,
        )
    confirmed = declaration.missing_observations_confirmed_for
    if enforce_confirmation:
        _check_confirmation(code, set(instruments), set(ambiguous), confirmed)
    return SpacingReport(
        arithmetic=(
            "calendar_months" if seconds is None else ("wall_clock" if zoned else "absolute")
        ),
        instruments=instruments,
        ambiguous=tuple(ambiguous),
        missing_observations_confirmed_for=confirmed,
    )


def _named(symbols: set[str]) -> str:
    ordered = sorted(symbols)
    return ", ".join(ordered[:10]) + (f" and {len(ordered) - 10} more" if len(ordered) > 10 else "")


def _check_confirmation(
    code: str, present: set[str], ambiguous: set[str], confirmed: frozenset[str]
) -> None:
    """Refuse unless the confirmation names exactly the ambiguous instruments."""

    if unknown := set(confirmed) - present:
        raise TemporalError(
            f"Missing observations were confirmed for {_named(unknown)}, which this file has no "
            "bars for. A confirmation names instruments in the file being imported."
        )
    if unneeded := set(confirmed) - ambiguous:
        raise TemporalError(
            f"Missing observations were confirmed for {_named(unneeded)}, whose own bars already "
            f"sit one {code} interval apart. The confirmation is only for instruments whose "
            "timestamps cannot show the interval; remove these from it."
        )
    if unconfirmed := ambiguous - set(confirmed):
        raise TemporalError(
            f"{_named(unconfirmed)}: no two consecutive bars are one {code} interval apart, so "
            f"these timestamps cannot show whether they are {code} bars with observations "
            f"missing or longer bars (for example hourly bars) with the same times. {code} is "
            "your declaration for them, not something their bars show. If you know they are "
            f"{code} bars with observations missing, confirm missing observations for exactly "
            f"these instruments; if their bars are longer, they do not belong in a dataset of "
            f"{code} bars -- declared wrongly, start-stamped bars would be moved to the wrong "
            "instant and a backtest would see closes before they happened."
        )


def _fixed_steps(
    symbol: str, positions: list[float], unit: float, code: str, *, absolute: bool
) -> tuple[int, int]:
    """Count one-interval and longer gaps between consecutive positions, or refuse.

    No tolerance: an instant that misses the interval by a millisecond, or by a
    daylight-saving hour, is not on it. (A blanket hour of slack for daily bars
    used to accept a 23-hour gap between UTC instants as one day.)
    """

    exact = longer = 0
    for previous, current in pairwise(positions):
        gap = current - previous
        if gap < unit - _EPSILON:
            raise TemporalError(
                f"Two {symbol} bars are {_span(gap)} apart, less than the declared {code}: "
                "the file mixes bar frequencies (a dataset has one bar interval), or the "
                "interval is declared wrong."
            )
        whole = round(gap / unit)
        if abs(gap - whole * unit) > _EPSILON:
            raise TemporalError(
                f"Two {symbol} bars are {_span(gap)} apart, which is not a whole number of "
                f"{code} intervals: the spacing is irregular for the declared interval. "
                "Timestamps must fall on the interval exactly; none is rounded."
                + (
                    " Offset-aware and epoch timestamps are compared as absolute instants; if "
                    "these are local times whose UTC offset changes with daylight saving, "
                    "upload them as wall-clock times and declare their zone."
                    if absolute and unit >= _DAY
                    else ""
                )
            )
        if whole == 1:
            exact += 1
        else:
            longer += 1
    return exact, longer


def _month_steps(symbol: str, walls: list[datetime], code: str) -> tuple[int, int]:
    """Count calendar-month steps between consecutive monthly labels, or refuse.

    Every label must sit on the same day of the month at the same time of day,
    or on the last day of its month at the same time of day; a step is the
    difference in month number, which must be a whole multiple of the interval.
    """

    count = int(code[:-1])
    if not walls:
        return 0, 0
    times = {w.time() for w in walls}
    month_ends = all(w.day == calendar.monthrange(w.year, w.month)[1] for w in walls)
    same_day = len({w.day for w in walls}) == 1
    if len(times) != 1 or not (month_ends or same_day):
        raise TemporalError(
            f"{symbol} monthly bars are not labelled on a consistent day (one day of the month, "
            "or the last day of each month, at one time of day), so calendar months between "
            "them cannot be counted."
        )
    exact = longer = 0
    for previous, current in pairwise(walls):
        months = (current.year - previous.year) * 12 + (current.month - previous.month)
        if months < count:
            raise TemporalError(
                f"Two {symbol} bars are {months} month(s) apart, less than the declared {code}."
            )
        if months % count:
            raise TemporalError(
                f"Two {symbol} bars are {months} months apart, not a whole number of {code} "
                "intervals."
            )
        if months == count:
            exact += 1
        else:
            longer += 1
    return exact, longer


def _refuse_bars_spanning_an_offset_change(
    symbol: str, starts: list[float], seconds: float, zone: ZoneInfo
) -> None:
    """Refuse a start-stamped wall-clock bar whose interval crosses a UTC-offset change.

    Its end is placed by adding the interval's fixed length; across a
    daylight-saving change the wall-clock interval is an hour longer or shorter
    than that, and the close could be stamped before it happened.
    """

    for start in starts:
        before = datetime.fromtimestamp(start, zone).utcoffset()
        after = datetime.fromtimestamp(start + seconds, zone).utcoffset()
        if before != after:
            raise TemporalError(
                f"A {symbol} bar starting {_wall(start, zone).isoformat()} in {zone.key} spans a "
                "daylight-saving change, so adding its interval would not find its end. Use "
                "interval-end timestamps, or offset-aware ones, for this series."
            )


def _span(seconds: float) -> str:
    if seconds >= _DAY and seconds % _DAY == 0:
        return f"{int(seconds // _DAY)}d"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{int(seconds // 60)}m"
    return f"{seconds:g}s"


def _alphalab_text(raw: str, fmt: TimestampFormat, kind: TimestampKind) -> str:
    """The value in a form AlphaLab's reader accepts, without changing what it says.

    Only slash dates need rewriting: AlphaLab reads ISO-8601, so ``02/01/2024``
    (day-first, as detection established and the user saw) becomes
    ``2024-01-02``. Everything else is passed as written.
    """

    text = raw.strip()
    if fmt not in _SLASH_FORMATS:
        return text
    provisional = parse_timestamp(text, fmt)  # the wall clock, read as UTC
    if provisional is None:
        raise TemporalError(f"{text!r} is not a valid {fmt.value} value.")
    moment = datetime.fromtimestamp(provisional, UTC).replace(tzinfo=None)
    return moment.date().isoformat() if kind is TimestampKind.DATE else moment.isoformat()


def label_instant(raw: str, fmt: TimestampFormat, declaration: TemporalDeclaration) -> float:
    """The instant one raw timestamp names under ``declaration``, read by AlphaLab.

    For a bare date that is the end of the day it names; for an interval-start
    time it is still the start -- the move to the end needs the interval, which
    is only known once the whole file has been read (:func:`end_of_interval_for`).
    """

    kind = kind_of(raw, fmt)
    if kind is None:
        raise TemporalError(f"{raw.strip()!r} is not a valid {fmt.value} value.")
    text = _alphalab_text(raw, fmt, kind)
    if kind is TimestampKind.NAIVE and declaration.timezone:
        _refuse_unplaceable_wall_time(text, declaration.timezone)
    try:
        return instant(text, kind, declaration.timezone)
    except TemporalInterpretationError as exc:
        raise TemporalError(str(exc)) from None


def _refuse_unplaceable_wall_time(text: str, timezone: str) -> None:
    """Refuse a wall-clock time the zone shows twice, or never.

    At a daylight-saving change one wall-clock hour happens twice (autumn) and
    one not at all (spring). Neither names a single instant, so neither is read.
    """

    try:
        wall = datetime.fromisoformat(text)
    except ValueError:
        return  # AlphaLab's reader reports the malformed value
    zone = ZoneInfo(timezone)
    first, second = wall.replace(tzinfo=zone, fold=0), wall.replace(tzinfo=zone, fold=1)
    if first.utcoffset() == second.utcoffset():
        return
    round_trip = first.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    problem = "does not exist" if round_trip != wall else "happens twice"
    raise TemporalError(
        f"{text} {problem} in {timezone} (a daylight-saving change), so it names no single instant."
    )


def end_of_interval_for(row: Any, frequency: str) -> float:
    """Where a start-stamped canonical ``row`` of ``frequency`` ends, by AlphaLab's rule."""

    try:
        return end_of_interval(
            row.timestamp,
            frequency,
            symbol=row.symbol,
            open_=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
        )
    except TemporalInterpretationError as exc:
        raise TemporalError(str(exc)) from None


def declaration_from_record(record: dict[str, Any]) -> TemporalDeclaration | None:
    """The declaration a dataset version recorded, or ``None`` for one that predates them."""

    convention = record.get("convention")
    if (
        not convention
        or not record.get("bar_interval")
        or int(record.get("record_version") or 0) < RECORD_VERSION
    ):
        # No declaration (before 0006); a bar interval inferred from the median
        # gap (record 1); or one validated across the whole file at once, where
        # one instrument's bars could vouch for another's (record 2); or one
        # whose missing-observation confirmation was a dataset-wide flag (3).
        return None
    return TemporalDeclaration(
        TimestampConvention(convention),
        record.get("timezone"),
        record["bar_interval"],
        frozenset(record.get("missing_observations_confirmed_for") or ()),
    )
