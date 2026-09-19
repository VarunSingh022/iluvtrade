"""A published strategy version must never change."""

from __future__ import annotations

import json

import pytest

from iluvtrade.strategies import service
from tests.conftest import make_principal

pytestmark = pytest.mark.security


def _published(db):
    principal = make_principal(db)
    strategy = service.create_strategy(db, principal, name="Frozen")
    version = service.create_version(
        db,
        principal,
        strategy_id=strategy.id,
        implementation_key="moving_average_crossover",
        parameters={"fast": 5, "slow": 20, "quantity": 10},
    )
    return principal, service.publish(db, principal, version.id)


def test_publishing_freezes_and_hashes(db) -> None:
    _, version = _published(db)
    assert version.frozen is True
    assert version.content_hash is not None
    assert service.verify_integrity(version) is True


def test_the_service_refuses_to_edit_a_published_version(db) -> None:
    principal, version = _published(db)
    for kwargs in (
        {"changelog": "sneaky"},
        {"parameters": {"fast": 2, "slow": 3, "quantity": 1}},
        {"implementation_key": "buy_and_hold"},
    ):
        with pytest.raises(service.VersionFrozenError):
            service.update_draft(db, principal, version_id=version.id, **kwargs)


def test_a_mutation_bypassing_the_service_is_detected(db) -> None:
    """The hash is recomputed, so a stray UPDATE does not go unnoticed.

    This is the case the service-level refusal cannot cover: a migration bug, a
    direct SQL edit, or a future code path that writes the row without asking.
    """

    _, version = _published(db)
    assert service.verify_integrity(version) is True

    version.default_parameters_json = json.dumps({"fast": 1, "slow": 2, "quantity": 999})
    db.flush()

    assert service.verify_integrity(version) is False


def test_a_tampered_version_will_not_run(db) -> None:
    principal, version = _published(db)
    version.implementation_key = "buy_and_hold"
    db.flush()

    with pytest.raises(service.StrategyError, match="content hash"):
        service.resolve_runnable(db, principal.organization_id, version.id)


def test_a_draft_cannot_be_run(db) -> None:
    principal = make_principal(db)
    strategy = service.create_strategy(db, principal, name="Draft only")
    draft = service.create_version(
        db, principal, strategy_id=strategy.id, implementation_key="buy_and_hold"
    )
    with pytest.raises(service.StrategyError, match="draft"):
        service.resolve_runnable(db, principal.organization_id, draft.id)


def test_a_changelog_edit_on_a_draft_does_not_change_the_hash_after_publish(db) -> None:
    """The hash covers behaviour, not prose — stated in the service, asserted here."""

    principal = make_principal(db)
    strategy = service.create_strategy(db, principal, name="Notes")
    version = service.create_version(
        db,
        principal,
        strategy_id=strategy.id,
        implementation_key="buy_and_hold",
        parameters={"quantity": 3},
    )
    service.update_draft(db, principal, version_id=version.id, changelog="new notes")
    published = service.publish(db, principal, version.id)

    before = published.content_hash
    published.changelog = "edited after the fact"
    db.flush()
    assert service.content_hash_of(published) == before
    assert service.verify_integrity(published) is True
