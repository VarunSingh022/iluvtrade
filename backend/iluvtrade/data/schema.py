"""Schema detection: what the columns in this CSV appear to mean.

Detection is a *proposal*, not a decision. Every field resolves to a
:class:`FieldMatch` carrying the column chosen, how it was chosen, and a
confidence; the user sees all of it before approving a dataset. Where the data
is genuinely ambiguous — ``03/04/2024`` is the third of April or the fourth of
March and the file does not say — this module reports the ambiguity instead of
picking, because a silently wrong date order produces a backtest that is wrong
in a way no metric reveals.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

__all__ = [
    "DetectedSchema",
    "FieldMatch",
    "FieldRole",
    "TimestampFormat",
    "detect_schema",
    "parse_timestamp",
    "read_csv_preview",
    "sniff_dialect",
]

#: How many data rows detection reads before deciding. Enough to be confident,
#: small enough that a 2 GB file is inspected in milliseconds.
SAMPLE_ROWS = 500


class FieldRole(StrEnum):
    """A role the canonical model needs filled."""

    TIMESTAMP = "timestamp"
    SYMBOL = "symbol"
    OPEN = "open"
    HIGH = "high"
    LOW = "low"
    CLOSE = "close"
    VOLUME = "volume"
    VWAP = "vwap"
    TRADE_COUNT = "trade_count"


#: Header spellings seen in the wild, per role, in preference order. Matched
#: after normalising to lowercase alphanumerics, so ``Adj. Close`` and
#: ``adj_close`` are the same key.
_HEADER_ALIASES: dict[FieldRole, tuple[str, ...]] = {
    FieldRole.TIMESTAMP: (
        "timestamp",
        "datetime",
        "date",
        "time",
        "dt",
        "tradedate",
        "tradingdate",
        "bardatetime",
        "bartime",
        "epoch",
        "unixtime",
        "candledate",
    ),
    FieldRole.SYMBOL: (
        "symbol",
        "ticker",
        "instrument",
        "asset",
        "assetid",
        "tradingsymbol",
        "scrip",
        "scripcode",
        "name",
        "security",
        "contract",
        "instrumenttoken",
    ),
    FieldRole.OPEN: ("open", "o", "openprice", "opn", "firstprice"),
    FieldRole.HIGH: ("high", "h", "highprice", "hi", "maxprice"),
    FieldRole.LOW: ("low", "l", "lowprice", "lo", "minprice"),
    FieldRole.CLOSE: (
        "close",
        "c",
        "closeprice",
        "last",
        "lastprice",
        "ltp",
        "settle",
        "adjclose",
        "adjustedclose",
    ),
    FieldRole.VOLUME: ("volume", "v", "vol", "qty", "quantity", "tradedqty", "totalvolume"),
    FieldRole.VWAP: ("vwap", "avgprice", "averageprice", "vwapprice"),
    FieldRole.TRADE_COUNT: ("trades", "tradecount", "numtrades", "ntrades", "count"),
}

#: Roles a bar cannot be built without.
REQUIRED_ROLES = (FieldRole.TIMESTAMP, FieldRole.CLOSE)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalise_header(value: str) -> str:
    return _NON_ALNUM.sub("", value.strip().lower())


class TimestampFormat(StrEnum):
    """How the timestamp column is encoded."""

    ISO_8601 = "iso_8601"
    DATE_ONLY = "date_only"
    DATETIME_SPACE = "datetime_space"
    EPOCH_SECONDS = "epoch_seconds"
    EPOCH_MILLIS = "epoch_millis"
    SLASH_DMY = "slash_dmy"
    SLASH_MDY = "slash_mdy"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FieldMatch:
    """One role, the column filling it, and how sure detection is."""

    role: FieldRole
    column: str | None
    column_index: int | None
    confidence: float
    method: str
    note: str = ""

    @property
    def found(self) -> bool:
        return self.column is not None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"role": self.role.value}


@dataclass(frozen=True, slots=True)
class DetectedSchema:
    """Everything detection concluded, including what it could not conclude."""

    columns: tuple[str, ...]
    delimiter: str
    has_header: bool
    matches: dict[FieldRole, FieldMatch]
    timestamp_format: TimestampFormat
    timestamp_ambiguous: bool
    sample_rows: tuple[tuple[str, ...], ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def column_for(self, role: FieldRole) -> str | None:
        match = self.matches.get(role)
        return match.column if match else None

    def index_for(self, role: FieldRole) -> int | None:
        match = self.matches.get(role)
        return match.column_index if match else None

    @property
    def missing_required(self) -> tuple[FieldRole, ...]:
        return tuple(role for role in REQUIRED_ROLES if self.column_for(role) is None)

    @property
    def is_ohlc_complete(self) -> bool:
        return all(
            self.column_for(role) is not None
            for role in (FieldRole.OPEN, FieldRole.HIGH, FieldRole.LOW, FieldRole.CLOSE)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": list(self.columns),
            "delimiter": self.delimiter,
            "has_header": self.has_header,
            "fields": {role.value: match.to_dict() for role, match in self.matches.items()},
            "timestamp_format": self.timestamp_format.value,
            "timestamp_ambiguous": self.timestamp_ambiguous,
            "missing_required": [role.value for role in self.missing_required],
            "ohlc_complete": self.is_ohlc_complete,
            "sample_rows": [list(row) for row in self.sample_rows[:10]],
            "warnings": list(self.warnings),
        }


#: Every alias, flattened, for the header test below.
_ALL_ALIASES: frozenset[str] = frozenset(
    alias for aliases in _HEADER_ALIASES.values() for alias in aliases
)


def _looks_like_header(first: list[str], second: list[str] | None) -> bool:
    """Decide whether the first row names the columns.

    ``csv.Sniffer.has_header`` is a heuristic over column *types* and it is
    unreliable on the short, narrow files people actually upload: a two-column
    file headed ``candledate,close`` is reported as having no header, after
    which every column is named ``column_0`` and detection fails completely —
    for a file that is perfectly well formed.

    Two much stronger signals are available here, because this module already
    knows what a market-data header looks like:

    1. **A cell is a known field name.** ``close``, ``tradingsymbol`` and
       ``timestamp`` are not values.
    2. **The first row is entirely non-numeric while the second is not.** A
       header of words above a row of prices is the ordinary case.

    The sniffer is consulted only when neither signal fires.
    """

    if not first:
        return False
    normalised = [_normalise_header(cell) for cell in first]
    if any(cell in _ALL_ALIASES for cell in normalised if cell):
        return True
    if second:
        first_numeric = sum(1 for cell in first if _try_decimal(cell) is not None)
        second_numeric = sum(1 for cell in second if _try_decimal(cell) is not None)
        if first_numeric == 0 and second_numeric > 0:
            return True
    return False


def sniff_dialect(text: str) -> tuple[str, bool]:
    """Guess the delimiter and whether the first line is a header.

    Falls back to a comma rather than raising: a file the sniffer cannot read is
    still worth showing the user, and the columns it produces will simply fail
    role detection with a warning they can act on.
    """

    sample = text[:16384]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in (",", ";", "\t", "|")}
        delimiter = max(counts, key=lambda d: counts[d]) if any(counts.values()) else ","

    rows = list(csv.reader(io.StringIO(sample), delimiter=delimiter))
    if _looks_like_header(rows[0] if rows else [], rows[1] if len(rows) > 1 else None):
        return delimiter, True
    try:
        return delimiter, csv.Sniffer().has_header(sample)
    except csv.Error:
        return delimiter, True


def read_csv_preview(
    text: str, *, limit: int = SAMPLE_ROWS
) -> tuple[list[str], list[list[str]], str, bool]:
    """Read headers and up to ``limit`` data rows."""

    delimiter, has_header = sniff_dialect(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: list[list[str]] = []
    headers: list[str] = []
    for index, row in enumerate(reader):
        if index == 0 and has_header:
            headers = [cell.strip() for cell in row]
            continue
        rows.append(row)
        if len(rows) >= limit:
            break
    if not has_header:
        width = max((len(r) for r in rows), default=0)
        headers = [f"column_{i}" for i in range(width)]
    return headers, rows, delimiter, has_header


# --------------------------------------------------------------------------
# Timestamp parsing
# --------------------------------------------------------------------------

_SLASH_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})")
_DASH_YMD_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})")


def _try_decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value.strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None


def parse_timestamp(raw: str, fmt: TimestampFormat) -> float | None:
    """Parse one cell to epoch seconds under ``fmt``, or return ``None``.

    Naive values are read as UTC. That is a *stated* assumption rather than a
    guess: the canonical model is epoch seconds, a CSV with no offset carries no
    timezone, and the alternative — inventing the uploader's local zone — would
    shift every bar by an amount nothing records. It is surfaced in the quality
    report so a user whose data is exchange-local can say so.
    """

    text = raw.strip()
    if not text:
        return None
    try:
        match fmt:
            case TimestampFormat.EPOCH_SECONDS:
                value = _try_decimal(text)
                return float(value) if value is not None else None
            case TimestampFormat.EPOCH_MILLIS:
                value = _try_decimal(text)
                return float(value) / 1000.0 if value is not None else None
            case (
                TimestampFormat.ISO_8601
                | TimestampFormat.DATETIME_SPACE
                | TimestampFormat.DATE_ONLY
            ):
                normalised = text.replace("Z", "+00:00")
                parsed = datetime.fromisoformat(normalised)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return parsed.timestamp()
            case TimestampFormat.SLASH_DMY | TimestampFormat.SLASH_MDY:
                match = _SLASH_RE.match(text)
                if match is None:
                    return None
                first, second, year_text = (
                    int(match.group(1)),
                    int(match.group(2)),
                    match.group(3),
                )
                day, month = (
                    (first, second) if fmt is TimestampFormat.SLASH_DMY else (second, first)
                )
                year = int(year_text)
                if year < 100:
                    year += 2000 if year < 70 else 1900
                remainder = text[match.end() :].strip()
                hour = minute = second_of = 0
                if remainder:
                    parts = remainder.split(":")
                    hour = int(parts[0]) if parts and parts[0].isdigit() else 0
                    minute = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                    second_of = int(float(parts[2])) if len(parts) > 2 else 0
                return datetime(year, month, day, hour, minute, second_of, tzinfo=UTC).timestamp()
            case _:
                return None
    except (ValueError, OverflowError, ArithmeticError):
        return None


def _detect_timestamp_format(samples: Sequence[str]) -> tuple[TimestampFormat, bool, str]:
    """Decide the timestamp encoding, and whether day/month order is ambiguous."""

    values = [s.strip() for s in samples if s and s.strip()]
    if not values:
        return TimestampFormat.UNKNOWN, False, "No non-empty timestamp values were sampled."

    slash_hits = sum(1 for v in values if _SLASH_RE.match(v))
    if slash_hits >= len(values) * 0.8:
        firsts, seconds = [], []
        for value in values:
            match = _SLASH_RE.match(value)
            if match:
                firsts.append(int(match.group(1)))
                seconds.append(int(match.group(2)))
        first_exceeds_12 = any(v > 12 for v in firsts)
        second_exceeds_12 = any(v > 12 for v in seconds)
        if first_exceeds_12 and not second_exceeds_12:
            return TimestampFormat.SLASH_DMY, False, "First component exceeds 12, so it is the day."
        if second_exceeds_12 and not first_exceeds_12:
            return (
                TimestampFormat.SLASH_MDY,
                False,
                "Second component exceeds 12, so it is the day.",
            )
        if first_exceeds_12 and second_exceeds_12:
            return (
                TimestampFormat.UNKNOWN,
                True,
                "Both components exceed 12 somewhere in the sample; the column is not a "
                "consistent date.",
            )
        # Neither component ever exceeds 12: nothing in the data distinguishes
        # d/m/Y from m/d/Y. Refusing is the honest answer.
        return (
            TimestampFormat.UNKNOWN,
            True,
            "Slash-separated dates whose day and month cannot be told apart "
            "(no component exceeds 12). Re-export with ISO-8601 dates, or state "
            "the order explicitly.",
        )

    if all(_DASH_YMD_RE.match(v) for v in values):
        has_time = any(("T" in v) or (" " in v.strip() and len(v.strip()) > 10) for v in values)
        if any("T" in v for v in values):
            return TimestampFormat.ISO_8601, False, ""
        return (
            (TimestampFormat.DATETIME_SPACE if has_time else TimestampFormat.DATE_ONLY),
            False,
            "",
        )

    numeric = [_try_decimal(v) for v in values]
    if all(value is not None for value in numeric):
        magnitudes = [abs(v) for v in numeric if v is not None]
        # 1e11 sits between plausible epoch seconds (~1.7e9 today, ~3e9 far
        # future) and epoch milliseconds (~1.7e12), so the split is not a
        # coincidence of today's date.
        if magnitudes and max(magnitudes) > Decimal("100000000000"):
            return TimestampFormat.EPOCH_MILLIS, False, ""
        return TimestampFormat.EPOCH_SECONDS, False, ""

    if all(parse_timestamp(v, TimestampFormat.ISO_8601) is not None for v in values):
        return TimestampFormat.ISO_8601, False, ""

    return (
        TimestampFormat.UNKNOWN,
        False,
        "The timestamp column is in a format this importer does not recognise.",
    )


# --------------------------------------------------------------------------
# Role detection
# --------------------------------------------------------------------------


def _numeric_ratio(column: Sequence[str]) -> float:
    values = [c for c in column if c.strip()]
    if not values:
        return 0.0
    return sum(1 for c in values if _try_decimal(c) is not None) / len(values)


def _match_by_header(role: FieldRole, headers: Sequence[str]) -> tuple[str, int, float, str] | None:
    normalised = [_normalise_header(h) for h in headers]
    for rank, alias in enumerate(_HEADER_ALIASES[role]):
        for index, header in enumerate(normalised):
            if header == alias:
                # An exact hit on the first alias is as good as it gets; later
                # aliases are progressively less canonical spellings.
                confidence = max(0.75, 1.0 - rank * 0.03)
                return headers[index], index, confidence, "header-exact"
    for rank, alias in enumerate(_HEADER_ALIASES[role]):
        # Short aliases match exactly or not at all. ``"l"`` is a real spelling
        # of *low*, but prefix-matching it claims every column starting with l —
        # ``ltp`` (last traded price, a *close*) was being read as a low price,
        # which produces a bar whose low is its close and a backtest that is
        # quietly wrong. Three characters is the shortest that is discriminating.
        if len(alias) < 3:
            continue
        for index, header in enumerate(normalised):
            if header.startswith(alias) or header.endswith(alias):
                return headers[index], index, max(0.55, 0.8 - rank * 0.03), "header-prefix"
    return None


def detect_schema(text: str) -> DetectedSchema:
    """Inspect a CSV and propose what each column means."""

    headers, rows, delimiter, has_header = read_csv_preview(text)
    warnings: list[str] = []
    if not headers:
        return DetectedSchema(
            columns=(),
            delimiter=delimiter,
            has_header=has_header,
            matches={},
            timestamp_format=TimestampFormat.UNKNOWN,
            timestamp_ambiguous=False,
            sample_rows=(),
            warnings=("The file has no readable header row.",),
        )

    columns: list[list[str]] = [[] for _ in headers]
    for row in rows:
        for index in range(len(headers)):
            columns[index].append(row[index] if index < len(row) else "")

    matches: dict[FieldRole, FieldMatch] = {}
    taken: set[int] = set()

    for role in FieldRole:
        hit = _match_by_header(role, headers)
        if hit is not None and hit[1] not in taken:
            column, index, confidence, method = hit
            matches[role] = FieldMatch(role, column, index, confidence, method)
            taken.add(index)
        else:
            matches[role] = FieldMatch(role, None, None, 0.0, "unmatched")

    # A timestamp column with no recognisable header, found by content: a column
    # nothing else claimed whose values all parse as a date or an epoch.
    if not matches[FieldRole.TIMESTAMP].found:
        for index, header in enumerate(headers):
            if index in taken:
                continue
            sample = [c for c in columns[index][:50] if c.strip()]
            if not sample:
                continue
            fmt, ambiguous, _ = _detect_timestamp_format(sample)
            if fmt is not TimestampFormat.UNKNOWN and not ambiguous:
                matches[FieldRole.TIMESTAMP] = FieldMatch(
                    FieldRole.TIMESTAMP,
                    header,
                    index,
                    0.5,
                    "content-timestamp",
                    "Matched on the values, not the column name.",
                )
                taken.add(index)
                break

    # Likewise a symbol column: non-numeric, low cardinality relative to rows.
    if not matches[FieldRole.SYMBOL].found:
        for index, header in enumerate(headers):
            if index in taken:
                continue
            sample = [c.strip() for c in columns[index] if c.strip()]
            if len(sample) < 3 or _numeric_ratio(sample) > 0.2:
                continue
            distinct = len(set(sample))
            if distinct <= max(1, len(sample) // 2):
                matches[FieldRole.SYMBOL] = FieldMatch(
                    FieldRole.SYMBOL,
                    header,
                    index,
                    0.45,
                    "content-symbol",
                    f"{distinct} distinct values across {len(sample)} sampled rows.",
                )
                taken.add(index)
                break

    timestamp_index = matches[FieldRole.TIMESTAMP].column_index
    if timestamp_index is None:
        timestamp_format, ambiguous = TimestampFormat.UNKNOWN, False
        warnings.append("No timestamp column was found. A dataset cannot be built without one.")
    else:
        timestamp_format, ambiguous, note = _detect_timestamp_format(columns[timestamp_index])
        if note:
            warnings.append(note)

    for role in (FieldRole.OPEN, FieldRole.HIGH, FieldRole.LOW, FieldRole.CLOSE, FieldRole.VOLUME):
        match = matches[role]
        if match.column_index is None:
            continue
        ratio = _numeric_ratio(columns[match.column_index])
        if ratio < 0.9:
            warnings.append(
                f"Column {match.column!r} was matched to {role.value} but only "
                f"{ratio:.0%} of sampled values are numeric."
            )

    if matches[FieldRole.CLOSE].found and not all(
        matches[r].found for r in (FieldRole.OPEN, FieldRole.HIGH, FieldRole.LOW)
    ):
        warnings.append(
            "Only a close price was found. Bars will be built with open, high and "
            "low set to the close, which is recorded as a transformation."
        )
    if not matches[FieldRole.SYMBOL].found:
        warnings.append(
            "No symbol column was found. Every row will be attributed to a single "
            "instrument named after the dataset."
        )

    return DetectedSchema(
        columns=tuple(headers),
        delimiter=delimiter,
        has_header=has_header,
        matches=matches,
        timestamp_format=timestamp_format,
        timestamp_ambiguous=ambiguous,
        sample_rows=tuple(tuple(row) for row in rows[:10]),
        warnings=tuple(warnings),
    )
