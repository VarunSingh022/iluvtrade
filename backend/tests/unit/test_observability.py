"""Correlation identifiers and secret-safe structured logging.

The redaction tests matter most. The formatter is a **backstop** — credentials
are `SecretString` and should never reach a log line at all — but a backstop
that does not fire is worse than none, because it is trusted.
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from iluvtrade.common.observability import (
    SecretSafeFormatter,
    configure_logging,
    correlated,
    correlation_id,
    current_context,
    new_correlation_id,
    set_context,
)


@pytest.fixture
def captured() -> io.StringIO:
    """A root logger writing JSON into a buffer."""

    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(SecretSafeFormatter())
    root = logging.getLogger()
    previous, previous_level = list(root.handlers), root.level
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    try:
        yield buffer
    finally:
        root.handlers, root.level = previous, previous_level


def _records(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


# --- correlation scope -------------------------------------------------------


def test_there_is_no_correlation_id_outside_a_scope() -> None:
    assert correlation_id() is None


def test_a_scope_mints_one_when_not_given() -> None:
    with correlated() as identifier:
        assert correlation_id() == identifier
        assert len(identifier) == 16
    assert correlation_id() is None


def test_a_scope_adopts_an_inbound_identifier() -> None:
    """A trace that began at a load balancer must stay one trace."""

    with correlated("from-the-edge") as identifier:
        assert identifier == "from-the-edge"
        assert correlation_id() == "from-the-edge"


def test_scopes_nest_and_restore() -> None:
    with correlated("outer"):
        with correlated("inner"):
            assert correlation_id() == "inner"
        assert correlation_id() == "outer"
    assert correlation_id() is None


def test_context_fields_accumulate_and_do_not_leak_out() -> None:
    with correlated("x", organization_id="org-1"):
        set_context(job_id="job-9")
        assert current_context() == {"organization_id": "org-1", "job_id": "job-9"}
    assert current_context() == {}


def test_context_does_not_leak_between_sibling_scopes() -> None:
    """A mutable ContextVar default would share one dict across every request."""

    with correlated("first"):
        set_context(user_id="alice")
    with correlated("second"):
        assert "user_id" not in current_context()


def test_identifiers_are_unique() -> None:
    assert len({new_correlation_id() for _ in range(500)}) == 500


# --- what reaches a log line -------------------------------------------------


def test_every_record_in_scope_carries_the_identifier(captured: io.StringIO) -> None:
    logger = logging.getLogger("test.scope")
    with correlated("abc123", organization_id="org-7"):
        logger.info("queued")
        logger.warning("slow")
    logger.info("outside")

    records = _records(captured)
    assert records[0]["correlation_id"] == "abc123"
    assert records[0]["organization_id"] == "org-7"
    assert records[1]["correlation_id"] == "abc123"
    assert "correlation_id" not in records[2]


def test_records_are_json_with_the_expected_shape(captured: io.StringIO) -> None:
    logging.getLogger("test.shape").info("hello")
    record = _records(captured)[0]
    assert set(record) >= {"ts", "level", "logger", "message"}
    assert record["level"] == "INFO"
    assert record["logger"] == "test.shape"


def test_an_exception_is_captured(captured: io.StringIO) -> None:
    logger = logging.getLogger("test.exc")
    try:
        raise ValueError("something broke")
    except ValueError:
        logger.exception("failed")

    record = _records(captured)[0]
    assert "something broke" in record["exception"]


# --- redaction ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        ("token=SUPERSECRETVALUE123", "SUPERSECRETVALUE123"),
        ('access_token: "REAL-TOKEN-abc"', "REAL-TOKEN-abc"),
        ("api_secret=hunter2hunter2", "hunter2hunter2"),
        ("request_token=xyz789abcdef", "xyz789abcdef"),
        ("checksum=abc123def456789", "abc123def456789"),
        ("password=correct-horse-battery", "correct-horse-battery"),
        ("{'password': 'correct-horse-battery'}", "correct-horse-battery"),
        ('{"api_secret": "hunter2hunter2"}', "hunter2hunter2"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9", "eyJhbGciOiJIUzI1NiJ9"),
        ("token_hash=deadbeefdeadbeef", "deadbeefdeadbeef"),
    ],
)
def test_credential_shaped_values_never_reach_a_log_line(
    captured: io.StringIO, message: str, secret: str
) -> None:
    logging.getLogger("test.redact").info(message)
    assert secret not in captured.getvalue(), f"{secret!r} leaked from {message!r}"
    assert "[redacted]" in captured.getvalue()


def test_a_secret_in_an_exception_is_redacted(captured: io.StringIO) -> None:
    logger = logging.getLogger("test.redact.exc")
    try:
        raise RuntimeError("connect failed: password=hunter2hunter2")
    except RuntimeError:
        logger.exception("boom")

    assert "hunter2hunter2" not in captured.getvalue()


def test_a_secret_in_an_extra_field_is_redacted(captured: io.StringIO) -> None:
    logging.getLogger("test.redact.extra").info(
        "connected", extra={"detail": "access_token=REAL-TOKEN-abc"}
    )
    assert "REAL-TOKEN-abc" not in captured.getvalue()


@pytest.mark.parametrize(
    "message",
    [
        "equity=2011110.34 orders=21 fills=21",
        "Executing backtest over 520 records",
        "dataset_version_id=68dfee66 rows=520",
        "Session stopped after 520 of 520 records",
    ],
)
def test_ordinary_operational_detail_survives(captured: io.StringIO, message: str) -> None:
    """Over-redaction destroys the logs' usefulness, which is its own failure."""

    logging.getLogger("test.keep").info(message)
    record = _records(captured)[0]
    assert record["message"] == message
    assert "[redacted]" not in record["message"]


# --- configuration ------------------------------------------------------------


def test_configure_logging_is_idempotent() -> None:
    """Tests build several apps; handlers must not stack."""

    configure_logging("INFO")
    first = len(logging.getLogger().handlers)
    configure_logging("INFO")
    assert len(logging.getLogger().handlers) == first == 1


def test_unstructured_mode_is_available_for_a_terminal() -> None:
    configure_logging("INFO", structured=False)
    handler = logging.getLogger().handlers[0]
    assert not isinstance(handler.formatter, SecretSafeFormatter)
    configure_logging("INFO")


# --- through the API ----------------------------------------------------------


def test_every_response_carries_a_request_id(client) -> None:
    response = client.get("/api/health")
    assert response.headers["X-Request-ID"]
    assert len(response.headers["X-Request-ID"]) == 16


def test_an_inbound_request_id_is_adopted(client) -> None:
    response = client.get("/api/health", headers={"X-Request-ID": "trace-from-lb-123"})
    assert response.headers["X-Request-ID"] == "trace-from-lb-123"


def test_two_requests_get_different_identifiers(client) -> None:
    first = client.get("/api/health").headers["X-Request-ID"]
    second = client.get("/api/health").headers["X-Request-ID"]
    assert first != second


@pytest.mark.parametrize(
    "hostile",
    ["../../etc/passwd", "a\nb: injected", "x" * 500, "<script>alert(1)</script>"],
)
def test_a_hostile_inbound_identifier_is_sanitised(client, hostile: str) -> None:
    """It is caller-controlled text that ends up in log lines and a header."""

    value = client.get("/api/health", headers={"X-Request-ID": hostile}).headers["X-Request-ID"]
    assert len(value) <= 64
    assert all(c.isalnum() or c in "-_" for c in value)
    assert "\n" not in value and "<" not in value


def test_a_backtest_job_records_the_submitting_requests_identifier(client, headers) -> None:
    """The join that makes a trace possible: HTTP request -> job -> engine run."""

    from iluvtrade.db.models.backtest import BacktestJob
    from iluvtrade.db.session import session_scope
    from tests.conftest import make_csv, register

    register(client, "trace@example.com")
    version = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=40), "text/csv")},
        headers=headers,
    ).json()
    client.post(f"/api/v1/datasets/versions/{version['id']}/approve", headers=headers)
    strategy = client.post("/api/v1/strategies", json={"name": "S"}, headers=headers).json()
    draft = client.post(
        f"/api/v1/strategies/{strategy['id']}/versions",
        json={"implementation_key": "buy_and_hold", "parameters": {"quantity": 1}},
        headers=headers,
    ).json()
    client.post(f"/api/v1/strategies/versions/{draft['id']}/publish", headers=headers)

    response = client.post(
        "/api/v1/backtests",
        json={"dataset_version_id": version["id"], "strategy_version_id": draft["id"]},
        headers={**headers, "X-Request-ID": "submit-trace-42"},
    )
    assert response.headers["X-Request-ID"] == "submit-trace-42"

    with session_scope() as session:
        job = session.get(BacktestJob, response.json()["id"])
        assert job is not None
        assert job.correlation_id == "submit-trace-42", (
            "the job must remember which request queued it"
        )
