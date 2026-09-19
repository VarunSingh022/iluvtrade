"""In-app notifications, and the boundary a real channel would plug into.

PHASE 16 names the events that must reach an operator: a broker disconnecting, a
strategy stopping, a backtest failing, an order rejected, a risk limit breached,
stale market data, a reconciliation mismatch. All of them are written here as
rows, and the ones that matter are also written to the audit trail — two records
with different retention and different audiences.

Email and push are not implemented. :class:`NotificationChannel` is the seam
they would attach to, and ``docs/SECURITY.md`` records that as an external
dependency rather than pretending a delivery guarantee exists.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session as DbSession

from iluvtrade.db.base import utcnow
from iluvtrade.db.models.platform import Notification, NotificationSeverity
from iluvtrade.platform.tenancy import require_owned, scoped

__all__ = ["NotificationChannel", "mark_read", "notify", "recent", "unread_count"]


class NotificationChannel(Protocol):
    """Where a notification goes besides the database.

    Nothing implements this yet. It exists so that adding email does not mean
    editing every call site of :func:`notify`.
    """

    def deliver(self, notification: Notification) -> None: ...


def notify(
    session: DbSession,
    *,
    organization_id: str,
    kind: str,
    title: str,
    body: str = "",
    severity: str = "info",
    user_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
) -> Notification:
    """Record a notification. The caller still commits."""

    notification = Notification(
        organization_id=organization_id,
        user_id=user_id,
        severity=NotificationSeverity(severity),
        kind=kind,
        title=title[:200],
        body=body[:4000],
        resource_type=resource_type,
        resource_id=resource_id,
    )
    session.add(notification)
    return notification


def recent(
    session: DbSession, organization_id: str, user_id: str, *, limit: int = 50
) -> list[Notification]:
    """This user's notifications, newest first, including org-wide ones."""

    query = (
        scoped(Notification, organization_id)
        .where((Notification.user_id == user_id) | (Notification.user_id.is_(None)))
        .order_by(Notification.created_at.desc())
        .limit(min(limit, 200))
    )
    return list(session.execute(query).scalars())


def unread_count(session: DbSession, organization_id: str, user_id: str) -> int:
    rows = session.execute(
        scoped(Notification, organization_id).where(
            Notification.read_at.is_(None),
            (Notification.user_id == user_id) | (Notification.user_id.is_(None)),
        )
    ).scalars()
    return len(list(rows))


def mark_read(session: DbSession, organization_id: str, notification_id: str) -> Notification:
    notification = require_owned(session, Notification, notification_id, organization_id)
    if notification.read_at is None:
        notification.read_at = utcnow()
    return notification


def mark_all_read(session: DbSession, organization_id: str, user_id: str) -> int:
    rows = list(
        session.execute(
            scoped(Notification, organization_id).where(
                Notification.read_at.is_(None),
                (Notification.user_id == user_id) | (Notification.user_id.is_(None)),
            )
        ).scalars()
    )
    stamp = utcnow()
    for row in rows:
        row.read_at = stamp
    return len(rows)
