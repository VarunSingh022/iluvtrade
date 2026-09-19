"""The audit writer.

One function, because an audit trail with several writers grows several
redaction policies. :func:`record` is the only thing in the application that
inserts into ``audit_events``.

**Redaction happens here.** Any key whose name looks like a credential is
replaced before the payload is serialized, so a caller that carelessly passes a
whole request body cannot write a secret into the permanent record.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from iluvtrade.db.base import utcnow
from iluvtrade.db.models.platform import AuditEvent

#: Substrings that make a key a secret. Matched case-insensitively against the
#: whole key, at every depth.
_SECRET_KEY_MARKERS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "credential",
    "authorization",
    "signature",
    "private",
    "checksum",
)

REDACTED = "[redacted]"


def redact(value: Any, _depth: int = 0) -> Any:
    """Return ``value`` with anything credential-shaped replaced."""

    if _depth > 8:
        return "[truncated]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if any(marker in name.lower() for marker in _SECRET_KEY_MARKERS):
                out[name] = REDACTED
            else:
                out[name] = redact(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth + 1) for item in value]
    return value


def record(
    session: Session,
    *,
    organization_id: str,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    actor_user_id: str | None = None,
    outcome: str = "success",
    ip_address: str | None = None,
    payload: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append one audit event. The caller must still commit the session."""

    event = AuditEvent(
        organization_id=organization_id,
        created_at=utcnow(),
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        ip_address=ip_address,
        payload_json=json.dumps(redact(payload or {}), default=str, sort_keys=True),
    )
    session.add(event)
    return event
