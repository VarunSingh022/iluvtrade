"""The data-quality report a user approves or rejects a dataset on.

Every figure here is counted from the file, not estimated. The score at the end
is a weighted penalty over those counts and is deliberately *not* presented as a
verdict: it orders datasets for attention, and the findings underneath it are
what a user actually reads.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from statistics import median
from typing import Any

from iluvtrade.data.cleaning import CleaningResult
from iluvtrade.data.schema import DetectedSchema, FieldRole

__all__ = ["Finding", "QualityReport", "Severity", "analyse", "infer_frequency"]


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing worth telling the user about their data."""

    code: str
    severity: Severity
    message: str
    count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "count": self.count,
        }


#: Seconds → the frequency label AlphaLab's ``TimeFrame`` understands. Anything
#: not close to one of these is reported as irregular rather than rounded.
_FREQUENCY_TABLE: tuple[tuple[float, str], ...] = (
    (60.0, "1m"),
    (300.0, "5m"),
    (900.0, "15m"),
    (3600.0, "1h"),
    (14400.0, "4h"),
    (86400.0, "1d"),
    (604800.0, "1w"),
)


def infer_frequency(timestamps: list[float]) -> tuple[str | None, float | None]:
    """The dominant spacing between consecutive bars, as a label and in seconds.

    Uses the median gap, so a weekend or a trading halt does not decide the
    answer. Returns ``(None, median)`` when no standard timeframe is within 20%
    — an honest "irregular" beats snapping daily bars onto a 4-hour label.
    """

    if len(timestamps) < 3:
        return None, None
    ordered = sorted(set(timestamps))
    if len(ordered) < 3:
        return None, None
    gaps = [b - a for a, b in pairwise(ordered) if b > a]
    if not gaps:
        return None, None
    typical = median(gaps)
    for seconds, label in _FREQUENCY_TABLE:
        if abs(typical - seconds) <= seconds * 0.2:
            return label, typical
    return None, typical


@dataclass(slots=True)
class QualityReport:
    """Counts, findings and a score, all derived from one cleaning run."""

    total_input_rows: int = 0
    accepted_rows: int = 0
    rejected_rows: int = 0
    symbol_count: int = 0
    symbols: list[str] = field(default_factory=list)
    start_timestamp: float | None = None
    end_timestamp: float | None = None
    inferred_frequency: str | None = None
    median_gap_seconds: float | None = None
    duplicate_count: int = 0
    missing_value_count: int = 0
    malformed_row_count: int = 0
    invalid_timestamp_count: int = 0
    out_of_order_count: int = 0
    invalid_price_count: int = 0
    invalid_volume_count: int = 0
    gap_count: int = 0
    rejection_breakdown: dict[str, int] = field(default_factory=dict)
    transformation_breakdown: dict[str, int] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_input_rows": self.total_input_rows,
            "accepted_rows": self.accepted_rows,
            "rejected_rows": self.rejected_rows,
            "symbol_count": self.symbol_count,
            "symbols": self.symbols[:200],
            "start_timestamp": self.start_timestamp,
            "end_timestamp": self.end_timestamp,
            "inferred_frequency": self.inferred_frequency,
            "median_gap_seconds": self.median_gap_seconds,
            "duplicate_count": self.duplicate_count,
            "missing_value_count": self.missing_value_count,
            "malformed_row_count": self.malformed_row_count,
            "invalid_timestamp_count": self.invalid_timestamp_count,
            "out_of_order_count": self.out_of_order_count,
            "invalid_price_count": self.invalid_price_count,
            "invalid_volume_count": self.invalid_volume_count,
            "gap_count": self.gap_count,
            "rejection_breakdown": self.rejection_breakdown,
            "transformation_breakdown": self.transformation_breakdown,
            "findings": [f.to_dict() for f in self.findings],
            "score": self.score,
        }


def _count_gaps(timestamps: list[float], expected_gap: float | None) -> int:
    """How many times the series skips at least two expected intervals."""

    if expected_gap is None or expected_gap <= 0 or len(timestamps) < 3:
        return 0
    ordered = sorted(set(timestamps))
    return sum(1 for a, b in pairwise(ordered) if (b - a) > expected_gap * 1.9)


def analyse(result: CleaningResult, schema: DetectedSchema) -> QualityReport:
    """Build the report for one cleaning run."""

    report = QualityReport(
        total_input_rows=result.total_input_rows,
        accepted_rows=len(result.rows),
        rejected_rows=result.rejected_count,
        rejection_breakdown=dict(result.rejection_counts),
        transformation_breakdown=dict(result.transformation_counts),
    )

    counts: Counter[str] = result.rejection_counts
    report.duplicate_count = counts.get("duplicate", 0)
    report.malformed_row_count = counts.get("malformed_row", 0)
    report.invalid_timestamp_count = counts.get("invalid_timestamp", 0) + counts.get(
        "missing_timestamp", 0
    )
    report.out_of_order_count = counts.get("out_of_order", 0)
    report.invalid_price_count = (
        counts.get("invalid_price", 0)
        + counts.get("non_positive_price", 0)
        + counts.get("inconsistent_ohlc", 0)
    )
    report.invalid_volume_count = counts.get("negative_volume", 0)
    report.missing_value_count = counts.get("missing_price", 0) + counts.get("missing_timestamp", 0)

    if result.rows:
        symbols = sorted({row.symbol for row in result.rows})
        timestamps = [row.timestamp for row in result.rows]
        report.symbols = symbols
        report.symbol_count = len(symbols)
        report.start_timestamp = min(timestamps)
        report.end_timestamp = max(timestamps)
        label, gap = infer_frequency(timestamps)
        report.inferred_frequency = label
        report.median_gap_seconds = gap
        report.gap_count = _count_gaps(timestamps, gap)

    findings: list[Finding] = []

    if result.fatal:
        findings.append(Finding("fatal", Severity.ERROR, result.fatal))
    if not result.rows and not result.fatal:
        findings.append(
            Finding("no_usable_rows", Severity.ERROR, "No row in the file produced a usable bar.")
        )
    for warning in schema.warnings:
        findings.append(Finding("schema_warning", Severity.WARNING, warning))
    if schema.timestamp_ambiguous:
        findings.append(
            Finding(
                "ambiguous_dates",
                Severity.ERROR,
                "Day and month cannot be distinguished in the timestamp column.",
            )
        )
    if report.duplicate_count:
        findings.append(
            Finding(
                "duplicates",
                Severity.WARNING,
                f"{report.duplicate_count} row(s) repeated a symbol and timestamp already seen.",
                report.duplicate_count,
            )
        )
    if report.out_of_order_count:
        findings.append(
            Finding(
                "out_of_order",
                Severity.WARNING,
                f"{report.out_of_order_count} row(s) went backwards in time and were dropped. "
                "AlphaLab marks the book to the newest record it is given, so an "
                "out-of-order bar would revalue positions backwards.",
                report.out_of_order_count,
            )
        )
    if report.invalid_price_count:
        findings.append(
            Finding(
                "invalid_prices",
                Severity.WARNING,
                f"{report.invalid_price_count} row(s) had prices that were unparseable, "
                "non-positive, or inconsistent with their own high and low.",
                report.invalid_price_count,
            )
        )
    if report.malformed_row_count:
        findings.append(
            Finding(
                "malformed_rows",
                Severity.WARNING,
                f"{report.malformed_row_count} row(s) did not have the expected number of fields.",
                report.malformed_row_count,
            )
        )
    if report.gap_count:
        findings.append(
            Finding(
                "gaps",
                Severity.INFO,
                f"{report.gap_count} gap(s) of two or more intervals. Common in daily data "
                "(weekends, holidays) and worth checking in intraday data.",
                report.gap_count,
            )
        )
    if report.inferred_frequency is None and report.accepted_rows > 2:
        findings.append(
            Finding(
                "irregular_frequency",
                Severity.INFO,
                "Bar spacing does not match a standard timeframe within 20%; the dataset "
                "is treated as irregular.",
            )
        )
    filled = result.transformation_counts.get("filled_ohlc_from_close", 0)
    if filled:
        findings.append(
            Finding(
                "synthetic_ohlc",
                Severity.WARNING,
                f"{filled} open/high/low value(s) were set from the close because the file "
                "did not carry them. Intrabar behaviour in a backtest is therefore "
                "synthetic, not observed.",
                filled,
            )
        )
    if not schema.matches.get(FieldRole.VOLUME, None) or not schema.column_for(FieldRole.VOLUME):
        findings.append(
            Finding(
                "no_volume",
                Severity.INFO,
                "No volume column. Liquidity-aware fill policies cannot be used with this dataset.",
            )
        )

    report.findings = findings
    report.score = _score(report)
    return report


def _score(report: QualityReport) -> float:
    """A 0-100 figure ordering datasets by how much attention they need.

    A weighted penalty, not a verdict: a dataset scoring 70 is not "70% correct",
    it is one with findings worth reading. Errors floor the score at 0 because a
    dataset that cannot be built has no quality to grade.
    """

    if any(f.severity is Severity.ERROR for f in report.findings):
        return 0.0
    if report.total_input_rows == 0:
        return 0.0

    total = Decimal(report.total_input_rows)

    def ratio(count: int) -> float:
        return float(Decimal(count) / total)

    penalty = (
        ratio(report.rejected_rows) * 45.0
        + ratio(report.duplicate_count) * 15.0
        + ratio(report.invalid_price_count) * 20.0
        + ratio(report.malformed_row_count) * 15.0
        + ratio(report.out_of_order_count) * 10.0
        + min(ratio(report.gap_count) * 10.0, 5.0)
        + (3.0 if report.inferred_frequency is None else 0.0)
    )
    return round(max(0.0, 100.0 - penalty), 2)
