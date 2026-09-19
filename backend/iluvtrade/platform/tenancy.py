"""Tenant isolation, enforced in one place.

Every read of tenant-scoped data goes through :func:`scoped`, which refuses to
build a query for a model that does not declare
:class:`~iluvtrade.db.base.OrgScopedMixin`. The refusal is the point: a new table
holding user data either carries the column and is filtered, or it cannot be
queried through this helper at all — there is no third outcome where it is
queried unfiltered.

:func:`require_owned` is the object-level half. Fetching a row by id and *then*
comparing its ``organization_id`` is what closes the insecure-direct-object-
reference hole, and it raises :class:`NotFoundError` rather than a
"forbidden" — telling an attacker that an id exists but belongs to someone else
is itself a disclosure.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from iluvtrade.db.base import OrgScopedMixin


class NotFoundError(LookupError):
    """The resource does not exist, or does not belong to this organization.

    Deliberately one exception for both. See the module docstring.
    """


class TenancyError(RuntimeError):
    """A query was attempted against a model that declares no tenant column."""


def scoped[T](model: type[T], organization_id: str) -> Select[tuple[T]]:
    """A select over ``model`` restricted to one organization."""

    if not (isinstance(model, type) and issubclass(model, OrgScopedMixin)):
        raise TenancyError(
            f"{model.__name__} does not declare OrgScopedMixin, so it cannot be "
            "queried per-organization. Either it holds no tenant data — in which "
            "case query it directly and say why — or the column is missing."
        )
    # The guard above narrows ``model`` to a subclass of the mixin, so the
    # column is statically known here. The cast is only needed because
    # ``select`` cannot carry the row type through that narrowing.
    column = model.organization_id
    return cast("Select[tuple[T]]", select(model).where(column == organization_id))


def require_owned[T](session: Session, model: type[T], resource_id: str, organization_id: str) -> T:
    """Load one row by id, or raise if it is absent or another tenant's."""

    if not resource_id:
        raise NotFoundError(f"{model.__name__} not found")
    # Every model this is called with has a UUID primary key named ``id``
    # (:class:`~iluvtrade.db.base.IdMixin`), which the bare ``type[T]`` cannot say.
    identity = model.id  # type: ignore[attr-defined]
    row = session.execute(
        scoped(model, organization_id).where(identity == resource_id)
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"{model.__name__} {resource_id} not found")
    return row
