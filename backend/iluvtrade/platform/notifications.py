"""In-app notifications, and the seam an external channel would attach to.

Every notification is a declared kind from :mod:`iluvtrade.platform.events`.
:func:`notify` refuses an undeclared one, which is what keeps the catalogue an
accurate description of the product rather than an aspirational list.

Severity and audience come from the catalogue by default, so the same kind
cannot be `info` in one call site and `error` in another.

**Email and push do not exist.** :class:`NotificationChannel` is the interface
they would implement and :func:`register_channel` is where they would attach.
Nothing is registered, `deliver()` is never called, and
``NotificationKind.deliverable_externally`` records which kinds would be worth
sending when a channel exists — a per-kind product decision rather than a
transport one.
"""

from __future__ import annotations

import logging
from typing import Protocol

from sqlalchemy.orm import Session as DbSession

from iluvtrade.db.base import utcnow
from iluvtrade.db.models.platform import Notification, NotificationSeverity
from iluvtrade.platform.events import Audience, kind_of
from iluvtrade.platform.tenancy import require_owned, scoped

logger = logging.getLogger("iluvtrade.notifications")

__all__ = [
    "NotificationChannel",
    "mark_all_read",
    "mark_read",
    "notify",
    "recent",
    "register_channel",
    "registered_channels",
    "unread_count",
]


class NotificationChannel(Protocol):
    """Somewhere a notification goes besides the database.

    Nothing implements this. It exists so that adding email means writing one
    class and registering it, rather than editing every call site of
    :func:`notify`.
    """

    @property
    def name(self) -> str:
        """Identifies the channel in logs and in the health endpoint."""
        ...

    def deliver(self, notification: Notification) -> None:
        """Send it. Must not raise — a transport failure is not an application failure."""
        ...


_CHANNELS: list[NotificationChannel] = []


def register_channel(channel: NotificationChannel) -> None:
    """Attach an external channel. None is registered in this build."""

    _CHANNELS.append(channel)


def registered_channels() -> tuple[str, ...]:
    """Which external channels are attached. Empty in this build, and said so."""

    return tuple(channel.name for channel in _CHANNELS)


def notify(
    session: DbSession,
    *,
    organization_id: str,
    kind: str,
    title: str,
    body: str = "",
    severity: str | None = None,
    user_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
) -> Notification:
    """Record a notification. The caller still commits.

    ``kind`` must be declared in the catalogue. ``severity`` and the audience
    default to what the catalogue says, so one kind cannot be ``info`` at one
    call site and ``error`` at another; passing ``severity`` overrides it for a
    case that genuinely differs.
    """

    declared = kind_of(kind)
    if declared.audience is Audience.ORGANIZATION:
        # An organization-wide fact addressed to one person would be invisible
        # to the colleague who needs to act on it.
        user_id = None

    notification = Notification(
        organization_id=organization_id,
        user_id=user_id,
        severity=NotificationSeverity(severity or declared.severity.value),
        kind=kind,
        title=title[:200],
        body=body[:4000],
        resource_type=resource_type,
        resource_id=resource_id,
    )
    session.add(notification)

    for channel in _CHANNELS:
        if not declared.deliverable_externally:
            continue
        try:
            channel.deliver(notification)
        except Exception:
            logger.exception("Notification channel %s failed", channel.name)

    return notification


def recent(
    session: DbSession, organization_id: str, user_id: str, *, limit: int = 50
) -> list[Notification]:
    """This user's notifications, newest first, including organization-wide ones."""

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
