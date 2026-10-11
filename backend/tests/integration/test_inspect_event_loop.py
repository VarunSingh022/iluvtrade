"""Inspecting an upload never holds the event loop.

``POST /datasets/inspect`` is ``async`` (it streams the upload), and schema
detection parses the whole file. Run on the event loop, a large file stalled
every other request on the worker for as long as detection took. The test makes
detection block until released and requires another request to complete in the
meantime -- a statement about the event loop, not about timing: if detection
held the loop, the health request could not finish at all.
"""

from __future__ import annotations

import threading
from typing import Any

from tests.conftest import make_csv, register


def test_another_request_completes_while_an_inspection_is_running(
    client, headers, monkeypatch
) -> None:
    from iluvtrade.data import ingest

    register(client, "inspect-loop@example.com")
    real = ingest.inspect_bytes
    started, release = threading.Event(), threading.Event()

    def slow_inspect(payload: bytes, *settings: Any) -> dict[str, Any]:
        started.set()
        assert release.wait(timeout=30), "never released"
        return real(payload, *settings)

    monkeypatch.setattr(ingest, "inspect_bytes", slow_inspect)
    answers: dict[str, Any] = {}

    def inspect() -> None:
        answers["inspect"] = client.post(
            "/api/v1/datasets/inspect",
            files={"file": ("bars.csv", make_csv(bars=20), "text/csv")},
            headers=headers,
        )

    inspecting = threading.Thread(target=inspect)
    inspecting.start()
    try:
        assert started.wait(timeout=10), "the inspection never began"
        health = threading.Thread(target=lambda: answers.update(health=client.get("/api/health")))
        health.start()
        health.join(timeout=10)
        held = health.is_alive()
    finally:
        release.set()
        inspecting.join(timeout=30)
    assert not held, "a health check could not complete while an inspection ran"
    assert answers["health"].status_code == 200
    assert answers["inspect"].status_code == 200
    assert answers["inspect"].json()["timestamp_format"]
