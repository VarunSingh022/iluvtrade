"""Creating strategies and publishing immutable versions.

The invariant this module exists to hold: **a published version never changes.**
It is what makes a backtest reproducible, an entitlement meaningful and a
marketplace listing honest. Three things enforce it together:

1. :func:`publish` sets ``frozen`` and computes ``content_hash`` over exactly
   the fields that decide behaviour.
2. :func:`update_draft` refuses to write to a frozen row.
3. :func:`verify_integrity` recomputes the hash, so a mutation that bypassed
   this module entirely — a stray ``UPDATE``, a migration bug — is detectable
   rather than invisible. ``tests/security/test_strategy_version_immutability.py``
   exercises all three.

A change to a published strategy is a *new version*, which is why
:func:`create_version` exists and ``update_draft`` does not accept a published id.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from iluvtrade.alphalab_bridge import strategies as implementations
from iluvtrade.db.base import utcnow
from iluvtrade.db.models.platform import Role
from iluvtrade.db.models.strategy import (
    CertificationStatus,
    Strategy,
    StrategyVersion,
    StrategyVersionStatus,
    StrategyVisibility,
)
from iluvtrade.platform import audit
from iluvtrade.platform.accounts import Principal, slugify
from iluvtrade.platform.tenancy import NotFoundError, require_owned, scoped

__all__ = [
    "StrategyError",
    "VersionFrozenError",
    "create_strategy",
    "create_version",
    "publish",
    "resolve_runnable",
    "update_draft",
    "verify_integrity",
]


class StrategyError(ValueError):
    """The strategy operation was refused."""


class VersionFrozenError(StrategyError):
    """A published version cannot be modified. Create a new version instead."""


#: The fields a version's hash covers. Deliberately only the ones that change
#: what a run *does*: a changelog edit is not a behaviour change, and making it
#: break every prior run's integrity check would train people to ignore the check.
_HASHED_FIELDS = ("implementation_key", "parameters_schema_json", "default_parameters_json")


def content_hash_of(version: StrategyVersion) -> str:
    """The hash over a version's behaviour-deciding fields."""

    payload = {field: getattr(version, field) for field in _HASHED_FIELDS}
    payload["strategy_id"] = version.strategy_id
    payload["version"] = version.version
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_strategy(
    session: DbSession,
    principal: Principal,
    *,
    name: str,
    description: str = "",
    visibility: StrategyVisibility = StrategyVisibility.PRIVATE,
) -> Strategy:
    """Create a strategy identity owned by the caller's organization."""

    principal.require(Role.TRADER)
    slug = slugify(name, fallback="strategy")
    if session.execute(
        scoped(Strategy, principal.organization_id).where(Strategy.slug == slug)
    ).scalar_one_or_none():
        raise StrategyError(f"A strategy named {name!r} already exists in this workspace.")

    strategy = Strategy(
        organization_id=principal.organization_id,
        slug=slug,
        name=name.strip(),
        description=description,
        visibility=visibility,
        owner_user_id=principal.user_id,
    )
    session.add(strategy)
    session.flush()
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="strategy.created",
        resource_type="strategy",
        resource_id=strategy.id,
        actor_user_id=principal.user_id,
        payload={"name": name, "visibility": visibility.value},
    )
    return strategy


def create_version(
    session: DbSession,
    principal: Principal,
    *,
    strategy_id: str,
    implementation_key: str,
    parameters: dict[str, Any] | None = None,
    changelog: str = "",
) -> StrategyVersion:
    """Add a new draft version to a strategy.

    Parameters are validated against the implementation's schema *now*, so a
    version that cannot run is refused at creation rather than at the first
    backtest — where the failure would look like a strategy that found no signals.
    """

    strategy = require_owned(session, Strategy, strategy_id, principal.organization_id)
    try:
        resolved = implementations.validate_parameters(implementation_key, parameters)
    except (implementations.UnknownImplementationError, implementations.ParameterError) as exc:
        raise StrategyError(str(exc)) from exc

    highest = session.execute(
        select(func.max(StrategyVersion.version)).where(StrategyVersion.strategy_id == strategy.id)
    ).scalar_one_or_none()

    version = StrategyVersion(
        organization_id=principal.organization_id,
        strategy_id=strategy.id,
        version=int(highest or 0) + 1,
        status=StrategyVersionStatus.DRAFT,
        implementation_key=implementation_key,
        parameters_schema_json=json.dumps(
            implementations.parameter_schema(implementation_key), sort_keys=True
        ),
        default_parameters_json=json.dumps(resolved, sort_keys=True),
        changelog=changelog,
    )
    session.add(version)
    session.flush()
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="strategy.version.created",
        resource_type="strategy_version",
        resource_id=version.id,
        actor_user_id=principal.user_id,
        payload={
            "strategy_id": strategy.id,
            "version": version.version,
            "implementation": implementation_key,
        },
    )
    return version


def update_draft(
    session: DbSession,
    principal: Principal,
    *,
    version_id: str,
    implementation_key: str | None = None,
    parameters: dict[str, Any] | None = None,
    changelog: str | None = None,
) -> StrategyVersion:
    """Modify a draft. Refuses anything already published."""

    version = require_owned(session, StrategyVersion, version_id, principal.organization_id)
    if version.frozen or version.status is not StrategyVersionStatus.DRAFT:
        raise VersionFrozenError(
            f"Version {version.version} is {version.status.value} and cannot be edited. "
            "Publishing froze it; create a new version to change behaviour."
        )

    key = implementation_key or version.implementation_key
    if implementation_key is not None or parameters is not None:
        try:
            resolved = implementations.validate_parameters(key, parameters)
        except (implementations.UnknownImplementationError, implementations.ParameterError) as exc:
            raise StrategyError(str(exc)) from exc
        version.implementation_key = key
        version.parameters_schema_json = json.dumps(
            implementations.parameter_schema(key), sort_keys=True
        )
        version.default_parameters_json = json.dumps(resolved, sort_keys=True)
    if changelog is not None:
        version.changelog = changelog
    session.flush()
    return version


def publish(session: DbSession, principal: Principal, version_id: str) -> StrategyVersion:
    """Freeze a draft. After this the version's behaviour can never change."""

    version = require_owned(session, StrategyVersion, version_id, principal.organization_id)
    if version.status is StrategyVersionStatus.PUBLISHED:
        return version
    if version.status is not StrategyVersionStatus.DRAFT:
        raise StrategyError(f"A {version.status.value} version cannot be published.")

    version.status = StrategyVersionStatus.PUBLISHED
    version.frozen = True
    version.published_at = utcnow()
    version.published_by_user_id = principal.user_id
    version.content_hash = content_hash_of(version)
    session.flush()
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="strategy.version.published",
        resource_type="strategy_version",
        resource_id=version.id,
        actor_user_id=principal.user_id,
        payload={"version": version.version, "content_hash": version.content_hash},
    )
    return version


def verify_integrity(version: StrategyVersion) -> bool:
    """Whether a published version still hashes to what it did at publication.

    ``True`` for an unpublished version: there is nothing frozen to verify.
    """

    if not version.frozen or version.content_hash is None:
        return True
    return content_hash_of(version) == version.content_hash


def certify(
    session: DbSession,
    principal: Principal,
    *,
    version_id: str,
    status: CertificationStatus,
    notes: str = "",
) -> StrategyVersion:
    """Record a certification decision against a published version.

    Certification is a statement about review having happened, and this
    application performs no automated review — a human sets it. Storing it
    without pretending to have earned it is the honest form; see
    ``docs/REDDESK.md``.
    """

    principal.require(Role.ADMIN)
    version = require_owned(session, StrategyVersion, version_id, principal.organization_id)
    version.certification_status = status
    version.certification_notes = notes
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="strategy.version.certified",
        resource_type="strategy_version",
        resource_id=version.id,
        actor_user_id=principal.user_id,
        payload={"status": status.value, "notes": notes},
    )
    return version


def resolve_runnable(session: DbSession, organization_id: str, version_id: str) -> StrategyVersion:
    """Load a version that may actually be run, or refuse.

    Two refusals, and they are different facts. A version belonging to another
    organization is *not found* — saying "forbidden" would confirm the id
    exists. A version that is merely unpublished is named as such, because the
    caller owns it and can publish it.
    """

    version = require_owned(session, StrategyVersion, version_id, organization_id)
    if version.status is StrategyVersionStatus.DRAFT:
        raise StrategyError(
            f"Version {version.version} is a draft. Publish it before running it, so the "
            "run records an identity that cannot change afterwards."
        )
    if not verify_integrity(version):
        raise StrategyError(
            f"Version {version.version} no longer matches the content hash recorded when it "
            "was published. It has been modified outside the application and will not run."
        )
    return version


def latest_published(session: DbSession, organization_id: str, strategy_id: str) -> StrategyVersion:
    """The newest published version of a strategy.

    Used only where a *human* is choosing — creating a listing, picking a
    default in a form. Never by a run: PHASE 8 forbids executing "latest"
    implicitly, so every execution path takes a concrete version id.
    """

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
        raise NotFoundError(f"Strategy {strategy_id} has no published version.")
    return version
