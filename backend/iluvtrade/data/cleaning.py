"""Validation and cleaning, with a written record of everything it did.

PHASE 3 is explicit: *do not silently mutate data; every cleaning transformation
must be observable*. This module satisfies that literally. Each row either

* becomes a :class:`CanonicalRow`, possibly after transformations that are each
  appended to the log with the row number and the before/after values, or
* becomes a :class:`RejectedRow` carrying its line number, the raw text and the
  reason it could not be used.

Nothing is dropped without a record and nothing is altered without one. The
:class:`CleaningPolicy` is stored with the dataset version, so "what did the
importer do to my data" is answerable from the database alone, and re-running
the same policy over the same bytes produces the same output.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from iluvtrade.data.schema import (
    DetectedSchema,
    FieldRole,
    TimestampFormat,
    parse_timestamp,
)

__all__ = [
    "CanonicalRow",
    "CleaningPolicy",
    "CleaningResult",
    "DuplicatePolicy",
    "RejectedRow",
    "RejectionReason",
    "Transformation",
    "TransformationKind",
    "clean",
]

#: Transformations are logged individually up to this many, then counted. A
#: 10-million-row file must not produce a 10-million-entry JSON document; the
#: counts stay exact either way.
MAX_LOGGED_TRANSFORMATIONS = 500
MAX_LOGGED_REJECTIONS = 500


class RejectionReason(StrEnum):
    MALFORMED_ROW = "malformed_row"
    MISSING_TIMESTAMP = "missing_timestamp"
    INVALID_TIMESTAMP = "invalid_timestamp"
    MISSING_PRICE = "missing_price"
    INVALID_PRICE = "invalid_price"
    NON_POSITIVE_PRICE = "non_positive_price"
    NEGATIVE_VOLUME = "negative_volume"
    INCONSISTENT_OHLC = "inconsistent_ohlc"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"


class TransformationKind(StrEnum):
    TRIMMED_WHITESPACE = "trimmed_whitespace"
    FILLED_OHLC_FROM_CLOSE = "filled_ohlc_from_close"
    DEFAULTED_VOLUME = "defaulted_volume"
    DERIVED_VWAP = "derived_vwap"
    DEFAULTED_SYMBOL = "defaulted_symbol"
    REORDERED = "reordered"
    COERCED_NUMERIC = "coerced_numeric"
    DROPPED_DUPLICATE = "dropped_duplicate"


class DuplicatePolicy(StrEnum):
    """What to do when two rows share a symbol and a timestamp."""

    #: Keep the first and reject the rest. The default: a later row is not
    #: automatically a correction.
    KEEP_FIRST = "keep_first"
    KEEP_LAST = "keep_last"
    #: Keep both and let validation refuse the dataset. Chosen when a user wants
    #: to see the duplicates rather than have them handled.
    REJECT_DATASET = "reject_dataset"


@dataclass(frozen=True, slots=True)
class CleaningPolicy:
    """Every decision cleaning is allowed to make, in one serializable object."""

    duplicate_policy: DuplicatePolicy = DuplicatePolicy.KEEP_FIRST
    #: Reject rows whose timestamp goes backwards. AlphaLab's market engine takes
    #: the newest record as current, so an out-of-order bar would mark the book
    #: backwards; ``MarketDataset`` refuses such a dataset outright.
    drop_out_of_order: bool = True
    #: Fill open/high/low from close when only a close exists.
    fill_ohlc_from_close: bool = True
    #: Use 0 when no volume column exists. Recorded, never assumed silently.
    default_volume_to_zero: bool = True
    #: Derive VWAP as the typical price when no VWAP column exists.
    derive_vwap: bool = True
    #: Refuse a bar whose high < low, or whose open/close sit outside [low, high].
    enforce_ohlc_consistency: bool = True
    #: Refuse zero or negative prices. A price of 0 is not a price.
    require_positive_prices: bool = True
    #: The instrument every row is attributed to when the file has no symbol.
    default_symbol: str = "INSTRUMENT"
    timeframe: str = "1d"

    def to_dict(self) -> dict[str, Any]:
        return {
            "duplicate_policy": self.duplicate_policy.value,
            "drop_out_of_order": self.drop_out_of_order,
            "fill_ohlc_from_close": self.fill_ohlc_from_close,
            "default_volume_to_zero": self.default_volume_to_zero,
            "derive_vwap": self.derive_vwap,
            "enforce_ohlc_consistency": self.enforce_ohlc_consistency,
            "require_positive_prices": self.require_positive_prices,
            "default_symbol": self.default_symbol,
            "timeframe": self.timeframe,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CleaningPolicy:
        known = set(cls.__dataclass_fields__)
        data = {k: v for k, v in payload.items() if k in known}
        if "duplicate_policy" in data:
            data["duplicate_policy"] = DuplicatePolicy(data["duplicate_policy"])
        return cls(**data)


@dataclass(frozen=True, slots=True)
class CanonicalRow:
    """One validated bar, ready to become an AlphaLab ``Bar``."""

    symbol: str
    timestamp: float
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    vwap: Decimal
    trade_count: int
    source_line: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "volume": str(self.volume),
            "vwap": str(self.vwap),
            "trade_count": self.trade_count,
        }


@dataclass(frozen=True, slots=True)
class RejectedRow:
    """A row that could not be used, and why."""

    line_number: int
    reason: RejectionReason
    detail: str
    raw: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"reason": self.reason.value}


@dataclass(frozen=True, slots=True)
class Transformation:
    """One change made to one row."""

    line_number: int
    kind: TransformationKind
    column: str
    before: str
    after: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"kind": self.kind.value}


@dataclass(slots=True)
class CleaningResult:
    """Canonical rows plus the complete record of how they were produced."""

    rows: list[CanonicalRow] = field(default_factory=list)
    rejected: list[RejectedRow] = field(default_factory=list)
    transformations: list[Transformation] = field(default_factory=list)
    rejection_counts: Counter[str] = field(default_factory=Counter)
    transformation_counts: Counter[str] = field(default_factory=Counter)
    total_input_rows: int = 0
    #: True when the dataset must be refused outright rather than cleaned, e.g.
    #: duplicates under ``REJECT_DATASET``.
    fatal: str | None = None

    def _reject(self, row: RejectedRow) -> None:
        self.rejection_counts[row.reason.value] += 1
        if len(self.rejected) < MAX_LOGGED_REJECTIONS:
            self.rejected.append(row)

    def _transform(self, transformation: Transformation) -> None:
        self.transformation_counts[transformation.kind.value] += 1
        if len(self.transformations) < MAX_LOGGED_TRANSFORMATIONS:
            self.transformations.append(transformation)

    @property
    def rejected_count(self) -> int:
        return sum(self.rejection_counts.values())

    @property
    def transformation_total(self) -> int:
        return sum(self.transformation_counts.values())


def _decimal(raw: str) -> Decimal | None:
    text = raw.strip().replace(",", "")
    if not text:
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _cell(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index]


def clean(text: str, schema: DetectedSchema, policy: CleaningPolicy) -> CleaningResult:
    """Turn raw CSV text into canonical rows under ``policy``."""

    result = CleaningResult()

    if schema.missing_required:
        missing = ", ".join(role.value for role in schema.missing_required)
        result.fatal = f"The file has no {missing} column, so no bar can be built from it."
        return result
    if schema.timestamp_ambiguous or schema.timestamp_format is TimestampFormat.UNKNOWN:
        result.fatal = (
            "The timestamp column could not be interpreted unambiguously. "
            + " ".join(schema.warnings)
        ).strip()
        return result

    idx = {role: schema.index_for(role) for role in FieldRole}
    reader = csv.reader(io.StringIO(text), delimiter=schema.delimiter)
    expected_width = len(schema.columns)

    seen: dict[tuple[str, float], int] = {}
    last_timestamp: float | None = None

    for line_number, raw_row in enumerate(reader, start=1):
        if line_number == 1 and schema.has_header:
            continue
        if not raw_row or all(not cell.strip() for cell in raw_row):
            continue
        result.total_input_rows += 1
        raw_text = schema.delimiter.join(raw_row)[:500]

        if len(raw_row) < expected_width:
            # Short rows are only fatal if a needed column is the missing one.
            needed = [i for i in (idx[FieldRole.TIMESTAMP], idx[FieldRole.CLOSE]) if i is not None]
            if any(i >= len(raw_row) for i in needed):
                result._reject(
                    RejectedRow(
                        line_number,
                        RejectionReason.MALFORMED_ROW,
                        f"Row has {len(raw_row)} fields; {expected_width} were expected.",
                        raw_text,
                    )
                )
                continue

        # --- timestamp ---------------------------------------------------
        raw_timestamp = _cell(raw_row, idx[FieldRole.TIMESTAMP])
        if not raw_timestamp.strip():
            result._reject(
                RejectedRow(
                    line_number, RejectionReason.MISSING_TIMESTAMP, "Empty timestamp.", raw_text
                )
            )
            continue
        if raw_timestamp != raw_timestamp.strip():
            result._transform(
                Transformation(
                    line_number,
                    TransformationKind.TRIMMED_WHITESPACE,
                    schema.column_for(FieldRole.TIMESTAMP) or "timestamp",
                    raw_timestamp,
                    raw_timestamp.strip(),
                )
            )
        timestamp = parse_timestamp(raw_timestamp, schema.timestamp_format)
        if timestamp is None:
            result._reject(
                RejectedRow(
                    line_number,
                    RejectionReason.INVALID_TIMESTAMP,
                    f"{raw_timestamp.strip()!r} is not a valid {schema.timestamp_format.value}.",
                    raw_text,
                )
            )
            continue

        # --- symbol ------------------------------------------------------
        raw_symbol = _cell(raw_row, idx[FieldRole.SYMBOL]).strip()
        if raw_symbol:
            symbol = raw_symbol
        else:
            symbol = policy.default_symbol
            result._transform(
                Transformation(
                    line_number, TransformationKind.DEFAULTED_SYMBOL, "symbol", "", symbol
                )
            )

        # --- close, the one price a bar cannot lack -----------------------
        raw_close = _cell(raw_row, idx[FieldRole.CLOSE])
        close = _decimal(raw_close)
        if close is None:
            reason = (
                RejectionReason.MISSING_PRICE
                if not raw_close.strip()
                else RejectionReason.INVALID_PRICE
            )
            result._reject(
                RejectedRow(line_number, reason, f"close={raw_close.strip()!r}", raw_text)
            )
            continue

        # --- open / high / low -------------------------------------------
        prices: dict[FieldRole, Decimal] = {FieldRole.CLOSE: close}
        filled_from_close: list[str] = []
        for role in (FieldRole.OPEN, FieldRole.HIGH, FieldRole.LOW):
            raw_value = _cell(raw_row, idx[role])
            value = _decimal(raw_value)
            if value is not None:
                prices[role] = value
                continue
            # An *absent* value may be filled from the close; a *present but
            # unparseable* one may not. Filling "notanumber" from the close
            # would record the transformation as "the file did not carry this
            # value", which is false for that row, and would turn a data error
            # into synthetic data that reads as observed.
            if raw_value.strip():
                result._reject(
                    RejectedRow(
                        line_number,
                        RejectionReason.INVALID_PRICE,
                        f"{role.value}={raw_value.strip()!r} is not a number.",
                        raw_text,
                    )
                )
                break
            if not policy.fill_ohlc_from_close:
                result._reject(
                    RejectedRow(
                        line_number,
                        RejectionReason.MISSING_PRICE,
                        f"{role.value} is empty and filling from close is off.",
                        raw_text,
                    )
                )
                break
            prices[role] = close
            filled_from_close.append(role.value)
        else:
            for role_name in filled_from_close:
                result._transform(
                    Transformation(
                        line_number,
                        TransformationKind.FILLED_OHLC_FROM_CLOSE,
                        role_name,
                        "",
                        str(close),
                    )
                )

            if policy.require_positive_prices and any(v <= 0 for v in prices.values()):
                offending = {r.value: str(v) for r, v in prices.items() if v <= 0}
                result._reject(
                    RejectedRow(
                        line_number, RejectionReason.NON_POSITIVE_PRICE, str(offending), raw_text
                    )
                )
                continue

            high, low = prices[FieldRole.HIGH], prices[FieldRole.LOW]
            open_price = prices[FieldRole.OPEN]
            if policy.enforce_ohlc_consistency and (
                high < low or open_price > high or open_price < low or close > high or close < low
            ):
                result._reject(
                    RejectedRow(
                        line_number,
                        RejectionReason.INCONSISTENT_OHLC,
                        f"o={open_price} h={high} l={low} c={close}",
                        raw_text,
                    )
                )
                continue

            # --- volume --------------------------------------------------
            raw_volume = _cell(raw_row, idx[FieldRole.VOLUME])
            volume = _decimal(raw_volume)
            if volume is None and raw_volume.strip():
                result._reject(
                    RejectedRow(
                        line_number,
                        RejectionReason.INVALID_PRICE,
                        f"volume={raw_volume.strip()!r} is not a number.",
                        raw_text,
                    )
                )
                continue
            if volume is None:
                if not policy.default_volume_to_zero:
                    result._reject(
                        RejectedRow(
                            line_number,
                            RejectionReason.MISSING_PRICE,
                            "No volume, and defaulting is off.",
                            raw_text,
                        )
                    )
                    continue
                volume = Decimal("0")
                result._transform(
                    Transformation(
                        line_number,
                        TransformationKind.DEFAULTED_VOLUME,
                        "volume",
                        raw_volume.strip(),
                        "0",
                    )
                )
            elif volume < 0:
                result._reject(
                    RejectedRow(line_number, RejectionReason.NEGATIVE_VOLUME, str(volume), raw_text)
                )
                continue

            # --- vwap ----------------------------------------------------
            raw_vwap = _cell(raw_row, idx[FieldRole.VWAP])
            vwap = _decimal(raw_vwap)
            if vwap is None:
                if policy.derive_vwap:
                    vwap = (high + low + close) / Decimal("3")
                    result._transform(
                        Transformation(
                            line_number,
                            TransformationKind.DERIVED_VWAP,
                            "vwap",
                            raw_vwap.strip(),
                            str(vwap),
                        )
                    )
                else:
                    vwap = close

            raw_trades = _cell(raw_row, idx[FieldRole.TRADE_COUNT])
            trades_value = _decimal(raw_trades)
            trade_count = int(trades_value) if trades_value is not None and trades_value >= 0 else 0

            # --- duplicates and ordering ---------------------------------
            key = (symbol, timestamp)
            if key in seen:
                if policy.duplicate_policy is DuplicatePolicy.REJECT_DATASET:
                    result.fatal = (
                        f"Duplicate row for {symbol} at {raw_timestamp.strip()} "
                        f"(lines {seen[key]} and {line_number}); the policy refuses "
                        "datasets containing duplicates."
                    )
                    # A refused dataset yields nothing. Returning the rows read
                    # before the duplicate would offer a partial dataset the
                    # user never asked for and did not approve.
                    result.rows = []
                    return result
                if policy.duplicate_policy is DuplicatePolicy.KEEP_FIRST:
                    result._reject(
                        RejectedRow(
                            line_number,
                            RejectionReason.DUPLICATE,
                            f"Already seen at line {seen[key]}.",
                            raw_text,
                        )
                    )
                    continue
                # KEEP_LAST: drop the earlier row and record it.
                for position, existing in enumerate(result.rows):
                    if existing.symbol == symbol and existing.timestamp == timestamp:
                        result.rows.pop(position)
                        result._transform(
                            Transformation(
                                seen[key],
                                TransformationKind.DROPPED_DUPLICATE,
                                "row",
                                f"line {seen[key]}",
                                f"superseded by line {line_number}",
                            )
                        )
                        break

            if (
                last_timestamp is not None
                and timestamp < last_timestamp
                and policy.drop_out_of_order
            ):
                result._reject(
                    RejectedRow(
                        line_number,
                        RejectionReason.OUT_OF_ORDER,
                        f"{timestamp} follows {last_timestamp}.",
                        raw_text,
                    )
                )
                continue

            seen[key] = line_number
            last_timestamp = timestamp if last_timestamp is None else max(last_timestamp, timestamp)
            result.rows.append(
                CanonicalRow(
                    symbol=symbol,
                    timestamp=timestamp,
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    vwap=vwap,
                    trade_count=trade_count,
                    source_line=line_number,
                )
            )

    # AlphaLab requires chronological records. Rows arriving interleaved across
    # symbols are legitimate and common, so sort rather than reject — and record
    # having done so, because a reordered file is not the file that was uploaded.
    if result.rows:
        ordered = sorted(result.rows, key=lambda r: (r.timestamp, r.symbol))
        if [r.source_line for r in ordered] != [r.source_line for r in result.rows]:
            result._transform(
                Transformation(
                    0,
                    TransformationKind.REORDERED,
                    "dataset",
                    "file order",
                    "chronological order",
                )
            )
            result.rows = ordered

    return result


def iter_canonical(result: CleaningResult) -> Iterator[dict[str, Any]]:
    """The canonical rows as plain dictionaries, for serialization."""

    for row in result.rows:
        yield row.to_dict()
