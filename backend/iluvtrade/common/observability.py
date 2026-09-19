"""Correlation identifiers and structured logging.

The question this exists to answer
----------------------------------

"A user says their backtest produced the wrong number — show me everything that
happened." Answering it means joining an HTTP request to the service call it
made, to the job that call queued, to the AlphaLab run that job executed, to the
row that stored the result. Before this, each of those logged independently and
nothing tied them together.

A **correlation id** does. It is minted per request (or adopted from an inbound
``X-Request-ID``), carried in a :class:`contextvars.ContextVar` so no function
has to thread it through its signature, propagated explicitly into background
threads, and attached to every log record.

Why a ContextVar and not a parameter
------------------------------------

Threading an id through every call signature is the more explicit design and it
is what this does *not* do, for one reason: it would have to pass through
``alphalab``, and this application does not add parameters to the engine's API.
A ContextVar stops at the boundary. The cost is that a background thread must
adopt it deliberately — :func:`correlated` is that adoption, and it is the only
place the propagation is not automatic.

Secrets
-------

:class:`SecretSafeFormatter` redacts credential-shaped values before a record is
written. It is a **backstop, not the control**: the control is that credentials
are `SecretString` and never reach a log line in the first place. A formatter
that was the only defence would be one regex away from failing.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

__all__ = [
    "SecretSafeFormatter",
    "configure_logging",
    "correlated",
    "correlation_id",
    "current_context",
    "new_correlation_id",
    "set_context",
]

#: The identifier tying one request's work together, across threads.
_CORRELATION: ContextVar[str | None] = ContextVar("correlation_id", default=None)
#: Who and where, when known. Never contains a credential.
#:
#: The default is ``None`` rather than ``{}`` because a mutable default on a
#: ContextVar is shared across every context that never set one — so one
#: request's fields would leak into another's.
_CONTEXT: ContextVar[dict[str, str] | None] = ContextVar("log_context", default=None)

#: Words that make the value after them a secret.
_SECRET_WORDS = (
    "access_token",
    "refresh_token",
    "request_token",
    "api_secret",
    "api_key",
    "secret_key",
    "token_hash",
    "password",
    "passwd",
    "authorization",
    "credential",
    "signature",
    "checksum",
    "token",
    "secret",
)

#: Values that must never reach a log line even by accident.
#:
#: Order matters. The auth-header pattern runs **first**, because the
#: ``word=value`` pattern would otherwise match ``Authorization: Bearer`` and
#: redact the word "Bearer" while leaving the token after it intact.
#:
#: The value group handles three spellings — ``key=value``, ``key: 'value'`` and
#: ``key: "value"`` — because a dict, an f-string and a query string each render
#: one of them, and a pattern that only understood the unquoted form missed the
#: commonest case of all: a `repr`'d dictionary. The *key* may be quoted too
#: (``{'password': ...}``), which is why a closing quote is allowed between the
#: word and the separator.
_SECRET_WORDS = (
    "access_token",
    "refresh_token",
    "request_token",
    "api_secret",
    "api_key",
    "secret_key",
    "token_hash",
    "password",
    "passwd",
    "authorization",
    "credential",
    "signature",
    "checksum",
    "token",
    "secret",
)

_AUTH_HEADER = re.compile(r"(?i)\b(Bearer|Basic|token)\s+([A-Za-z0-9._\-+/=]{8,})")
_KEY_VALUE = re.compile(
    r"(?i)\b(" + "|".join(_SECRET_WORDS) + r")\b"  # the word
    r"['\"]?\s*[=:]\s*"  # optional closing quote, then separator
    r"(?:"
    r"'([^']{3,})'"  # 'single quoted'
    r'|"([^"]{3,})"'  # "double quoted"
    r"|([^\s'\",;}\])]{3,})"  # bare
    r")"
)

REDACTED = "[redacted]"


def new_correlation_id() -> str:
    """A fresh identifier. Short enough to paste into a support ticket."""

    return uuid.uuid4().hex[:16]


def correlation_id() -> str | None:
    """The current identifier, or ``None`` outside a correlated scope."""

    return _CORRELATION.get()


def current_context() -> dict[str, str]:
    return dict(_CONTEXT.get() or {})


def set_context(**fields: str) -> None:
    """Add fields to every subsequent log record in this scope."""

    _CONTEXT.set({**current_context(), **{k: str(v) for k, v in fields.items() if v}})


@contextmanager
def correlated(identifier: str | None = None, **fields: str) -> Iterator[str]:
    """Run a block under a correlation id, restoring what was there before.

    A background thread calls this with the id of the request that queued its
    work — which is the one place propagation is deliberate rather than
    automatic, because a ``ContextVar`` does not cross a thread boundary.
    """

    value = identifier or new_correlation_id()
    correlation_token = _CORRELATION.set(value)
    context_token = _CONTEXT.set(
        {**current_context(), **{k: str(v) for k, v in fields.items() if v}}
    )
    try:
        yield value
    finally:
        _CORRELATION.reset(correlation_token)
        _CONTEXT.reset(context_token)


def _redact(text: str) -> str:
    """Replace credential-shaped values. A backstop, never the only control."""

    text = _AUTH_HEADER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    return _KEY_VALUE.sub(lambda m: f"{m.group(1)}={REDACTED}", text)


class SecretSafeFormatter(logging.Formatter):
    """JSON log records with the correlation id attached and secrets redacted.

    JSON because these lines are meant to be aggregated and queried, and a
    human-readable format that a log shipper then has to re-parse loses the
    structure twice.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact(record.getMessage()),
        }
        identifier = correlation_id()
        if identifier:
            payload["correlation_id"] = identifier
        payload.update(current_context())

        if record.exc_info:
            payload["exception"] = _redact(self.formatException(record.exc_info))

        # Anything a caller attached with ``extra=``.
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in logging.LogRecord("", 0, "", 0, "", (), None).__dict__:
                continue
            if key in payload or key in {"exc_info", "exc_text", "stack_info", "args", "msg"}:
                continue
            payload[key] = _redact(str(value))

        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", *, structured: bool = True) -> None:
    """Install the formatter on the root logger.

    Called once from the application factory. Idempotent, because a test that
    builds several apps must not stack handlers.
    """

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setFormatter(
        SecretSafeFormatter()
        if structured
        else logging.Formatter("%(levelname)s %(name)s %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn installs its own handlers; let them propagate to ours instead so
    # access logs carry the correlation id too.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


def with_correlation(function: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator form of :func:`correlated`, for a thread target."""

    def wrapper(*args: Any, correlation: str | None = None, **kwargs: Any) -> Any:
        with correlated(correlation):
            return function(*args, **kwargs)

    return wrapper
