"""The notification catalogue: every event this application can tell someone about.

Why a catalogue
---------------

Before this, notification kinds were bare strings written at each call site, and
they had drifted into two different things sharing one namespace: *session
events* (``risk_rejected``, ``record_skipped``, ``strategy_log`` — an operational
log belonging to one run) and *notifications* (``backtest.completed`` — a message
addressed to a person). Nothing enumerated either set, so nobody could answer
"what can this system tell me about?" without grepping.

This module answers that. :data:`CATALOGUE` is the complete set of notification
kinds, each with its default severity, whether it is addressed to one user or to
the whole organization, and a one-line description that is the actual product
copy. A kind not in here cannot be sent — :func:`iluvtrade.platform.notifications.notify`
refuses it — so the catalogue cannot silently fall out of date.

Session events are deliberately **not** here. They live in
``session_events`` and are a per-run log; conflating the two is what produced the
mixed namespace in the first place.

Delivery
--------

Only the in-app channel exists. :class:`~iluvtrade.platform.notifications.NotificationChannel`
is the seam an email or push provider implements, and
:data:`NotificationKind.deliverable_externally` says which kinds would be worth
sending outside the app when one exists — because "everything" is the wrong
answer and deciding it per-kind is a product decision, not a transport one.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

__all__ = ["CATALOGUE", "Audience", "NotificationKind", "Severity", "kind_of"]


class Severity(enum.StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class Audience(enum.StrEnum):
    """Who a notification is for."""

    #: The person who caused it. Nobody else in the organization sees it.
    ACTOR = "actor"
    #: Everyone in the organization — an operational fact, not a personal one.
    ORGANIZATION = "organization"


@dataclass(frozen=True, slots=True)
class NotificationKind:
    """One thing the product can tell somebody about."""

    key: str
    severity: Severity
    audience: Audience
    summary: str
    #: Whether this would be worth sending by email or push once a channel
    #: exists. A failed backtest is; a completed one, probably not.
    deliverable_externally: bool = False


def _kind(
    key: str,
    severity: Severity,
    audience: Audience,
    summary: str,
    *,
    external: bool = False,
) -> NotificationKind:
    return NotificationKind(key, severity, audience, summary, external)


#: Every notification this application can emit. Adding one means adding a row
#: here first; ``notify()`` refuses an unknown key.
CATALOGUE: dict[str, NotificationKind] = {
    entry.key: entry
    for entry in (
        # --- research ---------------------------------------------------
        _kind(
            "backtest.completed",
            Severity.INFO,
            Audience.ACTOR,
            "A backtest finished and its results are available.",
        ),
        _kind(
            "backtest.failed",
            Severity.ERROR,
            Audience.ACTOR,
            "A backtest failed. The reason is on the job.",
            external=True,
        ),
        # --- data -------------------------------------------------------
        _kind(
            "dataset.ready",
            Severity.INFO,
            Audience.ACTOR,
            "A dataset finished processing and is awaiting review.",
        ),
        _kind(
            "dataset.failed",
            Severity.ERROR,
            Audience.ACTOR,
            "A dataset could not be processed into usable bars.",
            external=True,
        ),
        # --- trading ----------------------------------------------------
        _kind(
            "trading.session.started",
            Severity.INFO,
            Audience.ORGANIZATION,
            "A trading session started.",
        ),
        _kind(
            "trading.session.finished",
            Severity.INFO,
            Audience.ACTOR,
            "A trading session stopped.",
        ),
        _kind(
            "trading.session.failed",
            Severity.ERROR,
            Audience.ORGANIZATION,
            "A trading session failed and is no longer running.",
            external=True,
        ),
        _kind(
            "trading.kill_switch",
            Severity.CRITICAL,
            Audience.ORGANIZATION,
            "A session was halted by the kill switch and cannot be resumed.",
            external=True,
        ),
        _kind(
            "trading.risk_rejected",
            Severity.WARNING,
            Audience.ORGANIZATION,
            "Risk controls refused one or more orders during a session.",
        ),
        # --- brokers ----------------------------------------------------
        _kind(
            "broker.connected",
            Severity.INFO,
            Audience.ORGANIZATION,
            "A broker account was connected.",
        ),
        _kind(
            "broker.disconnected",
            Severity.WARNING,
            Audience.ORGANIZATION,
            "A broker connection was revoked or lost.",
            external=True,
        ),
        _kind(
            "broker.token_expired",
            Severity.WARNING,
            Audience.ORGANIZATION,
            "A broker session expired and must be reauthorized.",
            external=True,
        ),
        # --- marketplace -------------------------------------------------
        _kind(
            "reddesk.sale",
            Severity.INFO,
            Audience.ORGANIZATION,
            "One of this workspace's listings was purchased.",
            external=True,
        ),
        _kind(
            "reddesk.purchased",
            Severity.INFO,
            Audience.ACTOR,
            "A strategy licence was acquired and an entitlement granted.",
        ),
        _kind(
            "reddesk.refunded",
            Severity.WARNING,
            Audience.ORGANIZATION,
            "A purchase was refunded and its entitlement revoked.",
            external=True,
        ),
        _kind(
            "reddesk.entitlement_revoked",
            Severity.WARNING,
            Audience.ORGANIZATION,
            "An entitlement was revoked; strategies under it can no longer run.",
            external=True,
        ),
        # --- account ------------------------------------------------------
        _kind(
            "account.mfa_enabled",
            Severity.INFO,
            Audience.ACTOR,
            "Two-factor authentication was enabled on an account.",
            external=True,
        ),
        _kind(
            "account.mfa_disabled",
            Severity.WARNING,
            Audience.ACTOR,
            "Two-factor authentication was disabled on an account.",
            external=True,
        ),
    )
}


class UnknownNotificationKind(KeyError):
    """A notification kind that is not in the catalogue."""


def kind_of(key: str) -> NotificationKind:
    """The catalogue entry for ``key``, or refuse.

    Refusing rather than inventing a default is what keeps the catalogue
    complete: a new kind has to be declared before it can be sent, so the
    documented set and the emitted set cannot diverge.
    """

    try:
        return CATALOGUE[key]
    except KeyError as exc:
        raise UnknownNotificationKind(
            f"{key!r} is not a declared notification kind. Add it to "
            f"iluvtrade.platform.events.CATALOGUE. Known: {', '.join(sorted(CATALOGUE))}."
        ) from exc
