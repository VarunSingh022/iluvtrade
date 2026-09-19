"""User A must never reach user B's data.

The first test is the important one: it enumerates every tenant-scoped table
from the metadata rather than from a hand-written list, so a table added later
is covered without anyone remembering to add it here.
"""

from __future__ import annotations

import pytest

from iluvtrade.db.base import OrgScopedMixin
from iluvtrade.db.models import Base
from iluvtrade.platform.tenancy import NotFoundError, TenancyError, scoped
from tests.conftest import make_csv, register

pytestmark = pytest.mark.security

#: Tables that legitimately hold no tenant column, each with the reason.
_GLOBAL_TABLES = {
    "organizations": "is the tenant",
    "users": "a person is global; their data is not",
    "memberships": "the user↔org edge; carries the column via OrgScopedMixin",
    "sessions": "carries organization_id directly; it is the auth record",
    "listing_versions": "belongs to a listing, which is tenant-scoped",
    "password_reset_tokens": (
        "a password belongs to a person, not to a workspace. A user may be a "
        "member of several, and scoping this would force a meaningless choice "
        "of tenant and let one workspace reason about an account in another."
    ),
}


def test_every_user_data_table_is_tenant_scoped() -> None:
    """No table holding user data may lack the tenant column."""

    unscoped = []
    for name, table in Base.metadata.tables.items():
        if name in _GLOBAL_TABLES:
            continue
        if "organization_id" not in table.columns:
            unscoped.append(name)
    assert not unscoped, (
        f"These tables hold user data with no organization_id: {unscoped}. Either add "
        "OrgScopedMixin, or add them to _GLOBAL_TABLES with the reason they are global."
    )


def test_scoped_refuses_a_model_without_the_mixin() -> None:
    """The helper refuses rather than silently returning an unfiltered query."""

    from iluvtrade.db.models.platform import User

    with pytest.raises(TenancyError, match="does not declare OrgScopedMixin"):
        scoped(User, "any-org")


def test_scoped_accepts_a_model_with_the_mixin() -> None:
    from iluvtrade.db.models.data import Dataset

    assert issubclass(Dataset, OrgScopedMixin)
    assert "organization_id" in str(scoped(Dataset, "org-1"))


def test_a_user_cannot_read_another_users_dataset(app, headers) -> None:
    """The core invariant, through the real HTTP API."""

    from fastapi.testclient import TestClient

    with TestClient(app) as alice, TestClient(app) as bob:
        register(alice, "alice@example.com")
        register(bob, "bob@example.com")

        created = alice.post(
            "/api/v1/datasets/upload",
            files={"file": ("a.csv", make_csv(), "text/csv")},
            data={"dataset_name": "Alice data"},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        version_id = created.json()["id"]

        # Alice can read it.
        assert alice.get(f"/api/v1/datasets/versions/{version_id}").status_code == 200

        # Bob cannot — and is told "not found", not "forbidden", so the id's
        # existence is not confirmed to him.
        denied = bob.get(f"/api/v1/datasets/versions/{version_id}")
        assert denied.status_code == 404
        assert denied.json()["error"]["code"] == "NotFoundError"

        # Nor can Bob approve it, preview it, or see it in his listing.
        assert (
            bob.post(f"/api/v1/datasets/versions/{version_id}/approve", headers=headers).status_code
            == 404
        )
        assert bob.get(f"/api/v1/datasets/versions/{version_id}/preview").status_code == 404
        assert bob.get("/api/v1/datasets").json() == []


def test_a_user_cannot_read_another_users_backtest(app, headers) -> None:
    from fastapi.testclient import TestClient

    with TestClient(app) as alice, TestClient(app) as bob:
        register(alice, "alice@example.com")
        register(bob, "bob@example.com")

        version = alice.post(
            "/api/v1/datasets/upload",
            files={"file": ("a.csv", make_csv(), "text/csv")},
            headers=headers,
        ).json()
        alice.post(f"/api/v1/datasets/versions/{version['id']}/approve", headers=headers)
        strategy = alice.post("/api/v1/strategies", json={"name": "S"}, headers=headers).json()
        draft = alice.post(
            f"/api/v1/strategies/{strategy['id']}/versions",
            json={"implementation_key": "buy_and_hold", "parameters": {"quantity": 5}},
            headers=headers,
        ).json()
        alice.post(f"/api/v1/strategies/versions/{draft['id']}/publish", headers=headers)
        job = alice.post(
            "/api/v1/backtests",
            json={
                "dataset_version_id": version["id"],
                "strategy_version_id": draft["id"],
            },
            headers=headers,
        ).json()

        assert bob.get(f"/api/v1/backtests/{job['id']}").status_code == 404
        assert bob.get(f"/api/v1/backtests/{job['id']}/result").status_code == 404
        assert bob.get("/api/v1/backtests").json() == []


def test_require_owned_refuses_another_tenants_row(db) -> None:
    from iluvtrade.data import ingest
    from iluvtrade.db.models.data import DatasetVersion
    from iluvtrade.platform.tenancy import require_owned
    from tests.conftest import make_principal

    alice = make_principal(db, "alice@example.com")
    bob = make_principal(db, "bob@example.com")
    outcome = ingest.ingest_upload(
        db, alice, filename="a.csv", payload=make_csv(), dataset_name="A"
    )

    assert (
        require_owned(db, DatasetVersion, outcome.version.id, alice.organization_id).id
        == outcome.version.id
    )
    with pytest.raises(NotFoundError):
        require_owned(db, DatasetVersion, outcome.version.id, bob.organization_id)
