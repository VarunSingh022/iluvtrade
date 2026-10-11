"""A missing-observation confirmation is bound to the exact bytes and settings previewed.

The final review reproduced the UI reusing file A's interval check and BBB
confirmation for a different file. The server now makes that impossible to
smuggle through: ``POST /datasets/inspect`` (or ``/inspect-url``) returns a
``preview_id`` -- a SHA-256 over the raw bytes' SHA-256, the timestamp
declaration and the effective cleaning policy -- and an import that confirms
missing observations must send it. The import recomputes it from the bytes it
is actually ingesting and refuses a mismatch before storing anything, then
recomputes the ambiguity itself; the preview's result is never trusted.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from zoneinfo import ZoneInfo

from tests.conftest import register

IST = ZoneInfo("Asia/Kolkata")
OPEN = dt.datetime(2024, 1, 2, 9, 15, tzinfo=IST)
FIFTEEN = dt.timedelta(minutes=15)
DECLARED = {"timestamp_convention": "interval_start", "bar_interval": "15m"}


def _csv(close: int = 10, *, hourly_bbb: bool = True) -> bytes:
    rows = [("AAA", OPEN + FIFTEEN * i) for i in range(12)]
    step = dt.timedelta(hours=1) if hourly_bbb else FIFTEEN
    rows += [("BBB", OPEN + step * i) for i in range(4)]
    rows.sort(key=lambda r: (r[1], r[0]))
    lines = ["Timestamp,Symbol,Open,High,Low,Close,Volume"]
    lines += [f"{m.isoformat()},{s},{close},{close + 1},{close - 1},{close},100" for s, m in rows]
    return ("\n".join(lines) + "\n").encode()


def _inspect(client: Any, headers: dict[str, str], payload: bytes, **fields: str) -> dict:
    response = client.post(
        "/api/v1/datasets/inspect",
        files={"file": ("bars.csv", payload, "text/csv")},
        data={**DECLARED, **fields},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return dict(response.json()["interval_check"])


def _upload(client: Any, headers: dict[str, str], payload: bytes, **fields: Any) -> Any:
    return client.post(
        "/api/v1/datasets/upload",
        files={"file": ("bars.csv", payload, "text/csv")},
        data={"dataset_name": "bound", **DECLARED, **fields},
        headers=headers,
    )


def _sources() -> int:
    from sqlalchemy import func, select

    from iluvtrade.db.models.data import DataSource
    from iluvtrade.db.session import session_scope

    with session_scope() as session:
        return int(session.execute(select(func.count()).select_from(DataSource)).scalar_one())


def test_the_preview_identity_covers_bytes_declaration_and_policy(client, headers) -> None:
    """Documented composition, recomputed here from first principles."""

    import hashlib

    register(client, "identity-doc@example.com")
    payload = _csv()
    check = _inspect(client, headers, payload)
    content = hashlib.sha256(payload).hexdigest()
    document = {
        "preview_identity_version": 1,
        "content_sha256": content,
        "convention": "interval_start",
        "timezone": None,
        "bar_interval": "15m",
        "cleaning_policy": {
            "duplicate_policy": "keep_first",
            "drop_out_of_order": True,
            "fill_ohlc_from_close": True,
            "default_volume_to_zero": True,
            "enforce_ohlc_consistency": True,
            "require_positive_prices": True,
            "default_symbol": "INSTRUMENT",
            "timeframe": "1d",
        },
    }
    expected = hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert check["content_sha256"] == content
    assert check["preview_id"] == expected
    assert _sources() == 0  # inspection stores nothing


def test_a_confirmation_needs_a_preview_of_these_bytes(client, headers) -> None:
    register(client, "needs-preview@example.com")
    payload = _csv()
    refused = _upload(client, headers, payload, missing_observations_confirmed_for=["BBB"])
    assert refused.status_code == 400
    assert "without a preview_id" in refused.json()["error"]["message"]
    assert _sources() == 0

    check = _inspect(client, headers, payload)
    accepted = _upload(
        client,
        headers,
        payload,
        missing_observations_confirmed_for=["BBB"],
        preview_id=check["preview_id"],
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["accepted_on_confirmation"] == ["BBB"]


def test_another_files_preview_cannot_authorize_this_one(client, headers) -> None:
    """Same filename, same ambiguous instrument name, different bytes: refused."""

    register(client, "other-file@example.com")
    file_a, file_b = _csv(close=10), _csv(close=20)
    check_a = _inspect(client, headers, file_a)
    assert _inspect(client, headers, file_b)["ambiguous"] == check_a["ambiguous"] == ["BBB"]
    reused = _upload(
        client,
        headers,
        file_b,
        missing_observations_confirmed_for=["BBB"],
        preview_id=check_a["preview_id"],
    )
    assert reused.status_code == 400
    assert "does not match this file and these settings" in reused.json()["error"]["message"]
    assert _sources() == 0


def test_a_different_declaration_or_policy_voids_the_preview(client, headers) -> None:
    register(client, "settings-changed@example.com")
    payload = _csv()
    check = _inspect(client, headers, payload)
    for changed in (
        {"timestamp_convention": "interval_end"},
        {"policy_json": json.dumps({"drop_out_of_order": False})},
    ):
        response = _upload(
            client,
            headers,
            payload,
            missing_observations_confirmed_for=["BBB"],
            preview_id=check["preview_id"],
            **changed,
        )
        assert response.status_code == 400, changed
        assert "does not match" in response.json()["error"]["message"]
    # The policy the upload will use, previewed with it, matches.
    policy = json.dumps({"drop_out_of_order": False})
    with_policy = _inspect(client, headers, payload, policy_json=policy)
    assert with_policy["preview_id"] != check["preview_id"]
    ok = _upload(
        client,
        headers,
        payload,
        missing_observations_confirmed_for=["BBB"],
        preview_id=with_policy["preview_id"],
        policy_json=policy,
    )
    assert ok.status_code == 201, ok.text


def test_the_import_recomputes_ambiguity_rather_than_trusting_the_preview(client, headers) -> None:
    """A valid preview_id with names the import does not find ambiguous is refused."""

    register(client, "recompute@example.com")
    payload = _csv(hourly_bbb=False)  # nothing ambiguous
    check = _inspect(client, headers, payload)
    assert check["ambiguous"] == []
    response = _upload(
        client,
        headers,
        payload,
        missing_observations_confirmed_for=["BBB"],
        preview_id=check["preview_id"],
    )
    assert response.status_code == 201  # stored as a FAILED version naming why
    assert response.json()["status"] == "failed"
    assert "whose own bars already sit one 15m interval apart" in response.json()["failure_reason"]


def test_no_ambiguity_needs_no_preview_round_trip(client, headers) -> None:
    register(client, "no-round-trip@example.com")
    response = _upload(client, headers, _csv(hourly_bbb=False))
    assert response.status_code == 201
    assert response.json()["interval_status"] == "validated"
    # A supplied preview_id is still checked.
    stale = _upload(client, headers, _csv(hourly_bbb=False), preview_id="0" * 64)
    assert stale.status_code == 400


def test_the_retired_dataset_wide_flag_is_refused_on_every_path(
    client, headers, monkeypatch
) -> None:
    from iluvtrade.data import ingest

    monkeypatch.setattr(ingest, "fetch_csv", _fetcher([_csv()]))
    register(client, "retired-flag@example.com")
    upload = _upload(client, headers, _csv(), missing_observations_confirmed="true")
    assert upload.status_code == 422
    fetch = client.post(
        "/api/v1/datasets/fetch",
        json={"url": URL, **DECLARED, "missing_observations_confirmed": True},
        headers=headers,
    )
    assert fetch.status_code == 422
    assert _sources() == 0


# --- remote fetch ------------------------------------------------------------------

URL = "https://data.example.com/bars.csv"


def _fetcher(contents: list[bytes]) -> Any:
    """A fetch stub serving each content in turn: the resource changing between calls."""

    from iluvtrade.data.fetch import FetchResult

    served = iter(contents)

    def fake_fetch(url: str, settings: object) -> FetchResult:
        return FetchResult(
            content=next(served),
            final_url=url,
            requested_url=url,
            content_type="text/csv",
            retrieved_at=dt.datetime.now(dt.UTC),
            redirect_chain=(),
        )

    return fake_fetch


def test_a_fetched_resource_that_changed_since_its_preview_is_refused(
    client, headers, monkeypatch
) -> None:
    from iluvtrade.data import ingest

    register(client, "fetch-changed@example.com")
    monkeypatch.setattr(ingest, "fetch_csv", _fetcher([_csv(close=10), _csv(close=20)]))
    check = client.post(
        "/api/v1/datasets/inspect-url", json={"url": URL, **DECLARED}, headers=headers
    ).json()["interval_check"]
    assert check["ambiguous"] == ["BBB"]
    changed = client.post(
        "/api/v1/datasets/fetch",
        json={
            "url": URL,
            **DECLARED,
            "missing_observations_confirmed_for": ["BBB"],
            "preview_id": check["preview_id"],
        },
        headers=headers,
    )
    assert changed.status_code == 400
    assert "does not match this file" in changed.json()["error"]["message"]
    assert _sources() == 0


def test_an_unchanged_fetched_resource_is_imported_on_its_preview(
    client, headers, monkeypatch
) -> None:
    from iluvtrade.data import ingest

    register(client, "fetch-same@example.com")
    monkeypatch.setattr(ingest, "fetch_csv", _fetcher([_csv(), _csv(), _csv()]))
    check = client.post(
        "/api/v1/datasets/inspect-url", json={"url": URL, **DECLARED}, headers=headers
    ).json()["interval_check"]
    without = client.post(
        "/api/v1/datasets/fetch",
        json={"url": URL, **DECLARED, "missing_observations_confirmed_for": ["BBB"]},
        headers=headers,
    )
    assert without.status_code == 400 and "without a preview_id" in without.text
    fetched = client.post(
        "/api/v1/datasets/fetch",
        json={
            "url": URL,
            **DECLARED,
            "missing_observations_confirmed_for": ["BBB"],
            "preview_id": check["preview_id"],
        },
        headers=headers,
    )
    assert fetched.status_code == 201, fetched.text
    assert fetched.json()["accepted_on_confirmation"] == ["BBB"]
