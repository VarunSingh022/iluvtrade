"""The canonical on-disk form of a dataset, and the keys it lives under.

One JSON-Lines file per dataset version, one canonical bar per line, Decimals
written as strings so no value is rounded on the way to disk. This is the *only*
format anything downstream reads, which is what makes the upload path and the
fetch path genuinely the same pipeline rather than two that agree.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "artifact_key",
    "canonical_key",
    "raw_key",
    "rejected_key",
]


def raw_key(organization_id: str, source_id: str, suffix: str = "csv") -> str:
    """Where the bytes exactly as received are kept, forever."""

    return f"org/{organization_id}/raw/{source_id}.{suffix}"


def canonical_key(organization_id: str, dataset_version_id: str) -> str:
    """Where one version's canonical bars live."""

    return f"org/{organization_id}/datasets/{dataset_version_id}/canonical.jsonl"


def rejected_key(organization_id: str, dataset_version_id: str) -> str:
    """Where the rows that did not make it are kept, with their reasons."""

    return f"org/{organization_id}/datasets/{dataset_version_id}/rejected.json"


def artifact_key(organization_id: str, run_id: str) -> str:
    """Where a backtest run's full result document lives."""

    return f"org/{organization_id}/backtests/{run_id}/result.json"


def session_artifact_key(organization_id: str, session_id: str, name: str) -> str:
    """Where a trading session's captured artifacts live."""

    return f"org/{organization_id}/sessions/{session_id}/{name}"


def summarise_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """A small header describing a canonical file, for quick display."""

    if not rows:
        return {"rows": 0, "symbols": [], "start": None, "end": None}
    symbols = sorted({str(row["symbol"]) for row in rows})
    timestamps = [float(row["timestamp"]) for row in rows]
    return {
        "rows": len(rows),
        "symbols": symbols,
        "start": min(timestamps),
        "end": max(timestamps),
    }
