"""Symbol detection through the real import path: preview, upload, stored version.

The Apple file used to become a version of two instruments, ``Increasing`` and
``Decreasing``. It now imports as the one instrument its price headers name,
and a file whose instruments cannot be identified fails with the reason
instead of being split on a guess.
"""

from __future__ import annotations

from typing import Any

from tests.conftest import register
from tests.unit.test_symbol_detection import APPLE_CSV, DAYS

DECLARED = {"timestamp_convention": "session_date", "timezone": "UTC"}


def _upload(client: Any, headers: dict[str, str], text: str) -> dict[str, Any]:
    response = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("finance-charts-apple.csv", text.encode(), "text/csv")},
        data={"dataset_name": "symbols", **DECLARED},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def _stored_symbols(version_id: str) -> set[str]:
    from iluvtrade.data import ingest
    from iluvtrade.db.models.data import DatasetVersion
    from iluvtrade.db.session import session_scope

    with session_scope() as session:
        version = session.get(DatasetVersion, version_id)
        assert version is not None
        return {str(row["symbol"]) for row in ingest.canonical_rows(version)}


def test_the_apple_file_imports_as_one_instrument(client, headers) -> None:
    register(client, "apple-symbols@example.com")
    preview = client.post(
        "/api/v1/datasets/inspect",
        files={"file": ("finance-charts-apple.csv", APPLE_CSV.encode(), "text/csv")},
        headers=headers,
    ).json()
    assert preview["fields"]["symbol"]["column"] is None
    assert preview["header_symbol"] == "AAPL"
    assert preview["symbol_ambiguous"] is False

    version = _upload(client, headers, APPLE_CSV)
    assert version["status"] == "pending_approval", version
    assert version["symbol_count"] == 1
    assert version["row_count"] == 12
    assert _stored_symbols(version["id"]) == {"AAPL"}
    assert version["temporal"]["spacing"]["instruments"].keys() == {"AAPL"}


def test_a_file_whose_instruments_are_ambiguous_fails_with_the_reason(client, headers) -> None:
    register(client, "ambiguous-symbols@example.com")
    lines = ["Date,side,Open,High,Low,Close,Volume"]
    lines += [f"{d},{side},10,11,9,10,100" for d in DAYS for side in ("Buy", "Sell")]
    version = _upload(client, headers, "\n".join(lines) + "\n")
    assert version["status"] == "failed"
    assert version["row_count"] == 0 and version["canonical_hash"] is None
    assert "could not be identified unambiguously" in version["failure_reason"]
    assert "'side'" in version["failure_reason"]
