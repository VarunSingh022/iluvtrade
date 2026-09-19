"""Resolving what an organization may run, to a concrete strategy version.

This is the security-relevant half of RedDesk. Every path that deploys a
strategy — backtest, paper, live — calls :func:`resolve_version`, which answers
with a **strategy version id** or raises. There is no "allowed: true" boolean
anywhere: a check that answers yes/no invites a caller to then pick a version
itself, and picking "the latest" is exactly what PHASE 8 forbids.

Two ways an organization may run a strategy:

* It **owns** the strategy. Ownership is the ``organization_id`` on the strategy
  row, and the version must be published.
* It **holds an entitlement** granting a concrete version. The entitlement names
  that version; under a ``ROLLING`` licence it can be advanced, but only by
  :func:`advance_rolling`, which writes an audit event. Nothing advances at read
  time, so "which version did I run, and when did that change" always has an
  answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from iluvtrade.db.base import utcnow
from iluvtrade.db.models.reddesk import (
    Entitlement,
    EntitlementStatus,
    Listing,
    ListingVersion,
    VersionAccessPolicy,
)
from iluvtrade.db.models.strategy import Strategy, StrategyVersion, StrategyVersionStatus
from iluvtrade.platform import audit, notifications
from iluvtrade.platform.tenancy import scoped

__all__ = [
    "EntitlementError",
    "Grant",
    "active_entitlements",
    "advance_rolling",
    "grant",
    "resolve_version",
    "revoke",
]


class EntitlementError(PermissionError):
    """The organization may not run this strategy. The message says why."""


@dataclass(frozen=True, slots=True)
class Grant:
    """The answer to "may this organization run this, and as what exactly?"."""

    strategy_version_id: str
    strategy_id: str
    #: ``owner`` or ``entitlement``.
    basis: str
    entitlement_id: str | None
    expires_at: datetime | None


def _is_live(entitlement: Entitlement, now: datetime) -> bool:
    if entitlement.status is not EntitlementStatus.ACTIVE:
        return False
    if entitlement.valid_from > now:
        return False
    return not (entitlement.valid_until is not None and entitlement.valid_until <= now)


def resolve_version(
    session: DbSession,
    organization_id: str,
    *,
    strategy_version_id: str | None = None,
    strategy_id: str | None = None,
) -> Grant:
    """Resolve to one concrete, runnable strategy version, or refuse.

    Pass ``strategy_version_id`` to run a specific version, or ``strategy_id`` to
    run whichever version this organization is entitled to. Passing neither is a
    programming error; passing both means the named version must be the one the
    entitlement grants.
    """

    if strategy_version_id is None and strategy_id is None:
        raise EntitlementError("A strategy or a strategy version must be named.")

    now = utcnow()

    if strategy_version_id is not None:
        version = session.get(StrategyVersion, strategy_version_id)
        if version is None:
            raise EntitlementError("That strategy version does not exist.")
        strategy = session.get(Strategy, version.strategy_id)
        if strategy is None:
            raise EntitlementError("That strategy no longer exists.")

        if strategy.organization_id == organization_id:
            if version.status is StrategyVersionStatus.DRAFT:
                raise EntitlementError(
                    f"Version {version.version} is a draft. Publish it before running it."
                )
            return Grant(version.id, strategy.id, "owner", None, None)

        entitlement = (
            session.execute(
                scoped(Entitlement, organization_id).where(
                    Entitlement.granted_strategy_version_id == version.id
                )
            )
            .scalars()
            .first()
        )
        if entitlement is None or not _is_live(entitlement, now):
            # Deliberately identical for "no entitlement", "expired" and "revoked"
            # when the strategy is someone else's: distinguishing them would leak
            # which versions exist and who holds what.
            raise EntitlementError(
                "This workspace holds no active entitlement for that strategy version."
            )
        return Grant(
            version.id, strategy.id, "entitlement", entitlement.id, entitlement.valid_until
        )

    assert strategy_id is not None
    strategy = session.get(Strategy, strategy_id)
    if strategy is None:
        raise EntitlementError("That strategy does not exist.")

    if strategy.organization_id == organization_id:
        version = (
            session.execute(
                scoped(StrategyVersion, organization_id)
                .where(
                    StrategyVersion.strategy_id == strategy_id,
                    StrategyVersion.status == StrategyVersionStatus.PUBLISHED,
                )
                .order_by(StrategyVersion.version.desc())
            )
            .scalars()
            .first()
        )
        if version is None:
            raise EntitlementError("That strategy has no published version to run.")
        return Grant(version.id, strategy.id, "owner", None, None)

    entitlement = (
        session.execute(
            scoped(Entitlement, organization_id)
            .where(Entitlement.strategy_id == strategy_id)
            .order_by(Entitlement.created_at.desc())
        )
        .scalars()
        .first()
    )
    if entitlement is None or not _is_live(entitlement, now):
        raise EntitlementError("This workspace holds no active entitlement for that strategy.")
    return Grant(
        entitlement.granted_strategy_version_id,
        strategy_id,
        "entitlement",
        entitlement.id,
        entitlement.valid_until,
    )


def grant(
    session: DbSession,
    *,
    organization_id: str,
    listing: Listing,
    strategy_version_id: str,
    purchase_id: str | None,
    valid_until: datetime | None = None,
    actor_user_id: str | None = None,
) -> Entitlement:
    """Create an entitlement. Called by the purchase flow, not by an API route."""

    entitlement = Entitlement(
        organization_id=organization_id,
        listing_id=listing.id,
        strategy_id=listing.strategy_id,
        granted_strategy_version_id=strategy_version_id,
        version_access_policy=listing.version_access_policy,
        status=EntitlementStatus.ACTIVE,
        source_purchase_id=purchase_id,
        valid_from=utcnow(),
        valid_until=valid_until,
    )
    session.add(entitlement)
    session.flush()
    audit.record(
        session,
        organization_id=organization_id,
        action="reddesk.entitlement.granted",
        resource_type="entitlement",
        resource_id=entitlement.id,
        actor_user_id=actor_user_id,
        payload={
            "listing_id": listing.id,
            "strategy_version_id": strategy_version_id,
            "policy": listing.version_access_policy.value,
            "valid_until": valid_until.isoformat() if valid_until else None,
        },
    )
    return entitlement


def advance_rolling(
    session: DbSession, entitlement_id: str, *, actor_user_id: str | None = None
) -> Entitlement:
    """Move a ``ROLLING`` entitlement to the listing's current version.

    Explicit, audited, and refused for a ``PINNED`` licence. A buyer whose
    entitlement silently followed the creator's newest publish would be running
    code they never agreed to — which is the same defect as executing "latest".
    """

    entitlement = session.get(Entitlement, entitlement_id)
    if entitlement is None:
        raise EntitlementError("That entitlement does not exist.")
    if entitlement.version_access_policy is not VersionAccessPolicy.ROLLING:
        raise EntitlementError(
            "This licence is pinned to the version purchased. Newer versions require a "
            "new purchase."
        )
    if entitlement.status is not EntitlementStatus.ACTIVE:
        raise EntitlementError("This entitlement is not active.")

    current = (
        session.execute(
            select(ListingVersion)
            .where(ListingVersion.listing_id == entitlement.listing_id, ListingVersion.is_current)
            .order_by(ListingVersion.created_at.desc())
        )
        .scalars()
        .first()
    )
    if current is None:
        raise EntitlementError("That listing offers no current version.")

    previous = entitlement.granted_strategy_version_id
    if previous == current.strategy_version_id:
        return entitlement

    entitlement.granted_strategy_version_id = current.strategy_version_id
    audit.record(
        session,
        organization_id=entitlement.organization_id,
        action="reddesk.entitlement.advanced",
        resource_type="entitlement",
        resource_id=entitlement.id,
        actor_user_id=actor_user_id,
        payload={"from_version_id": previous, "to_version_id": current.strategy_version_id},
    )
    return entitlement


def revoke(
    session: DbSession, entitlement_id: str, *, reason: str, actor_user_id: str | None = None
) -> Entitlement:
    entitlement = session.get(Entitlement, entitlement_id)
    if entitlement is None:
        raise EntitlementError("That entitlement does not exist.")
    entitlement.status = EntitlementStatus.REVOKED
    entitlement.revoked_reason = reason[:400]
    notifications.notify(
        session,
        organization_id=entitlement.organization_id,
        kind="reddesk.entitlement_revoked",
        title="Strategy licence revoked",
        body=(
            f"Version {entitlement.granted_strategy_version_id[:8]} can no longer be run "
            f"in this workspace. Reason: {reason}"
        ),
        resource_type="entitlement",
        resource_id=entitlement.id,
    )
    audit.record(
        session,
        organization_id=entitlement.organization_id,
        action="reddesk.entitlement.revoked",
        resource_type="entitlement",
        resource_id=entitlement.id,
        actor_user_id=actor_user_id,
        outcome="success",
        payload={"reason": reason},
    )
    return entitlement


def active_entitlements(session: DbSession, organization_id: str) -> list[Entitlement]:
    now = utcnow()
    rows = session.execute(
        scoped(Entitlement, organization_id).order_by(Entitlement.created_at.desc())
    ).scalars()
    return [row for row in rows if _is_live(row, now)]


def expire_due(session: DbSession) -> int:
    """Mark entitlements whose term has ended. Idempotent; safe to run often."""

    now = utcnow()
    rows = session.execute(
        select(Entitlement).where(Entitlement.status == EntitlementStatus.ACTIVE)
    ).scalars()
    expired = 0
    for row in rows:
        if row.valid_until is not None and row.valid_until <= now:
            row.status = EntitlementStatus.EXPIRED
            expired += 1
    return expired
