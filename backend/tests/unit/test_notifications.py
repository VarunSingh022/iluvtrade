"""The notification contract.

The important tests are the two directions of catalogue agreement: nothing
undeclared can be sent, and nothing declared goes unsent. Together they make the
catalogue an accurate description of the product rather than a wish list.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from iluvtrade.platform import notifications
from iluvtrade.platform.events import (
    CATALOGUE,
    Audience,
    Severity,
    UnknownNotificationKind,
    kind_of,
)
from tests.conftest import make_principal

PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "iluvtrade"


def _emitted_kinds() -> dict[str, str]:
    """Every literal kind passed to ``notify()`` anywhere in the application."""

    found: dict[str, str] = {}
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = (
                function.attr
                if isinstance(function, ast.Attribute)
                else function.id
                if isinstance(function, ast.Name)
                else ""
            )
            if name != "notify":
                continue
            for keyword in node.keywords:
                if keyword.arg != "kind":
                    continue
                where = f"{path.relative_to(PACKAGE)}:{node.lineno}"
                # ``kind=`` is sometimes a conditional — one call site chooses
                # between "dataset.ready" and "dataset.failed" — so both
                # branches count as emitted.
                for constant in _constants(keyword.value):
                    found.setdefault(constant, where)
    return found


def _constants(node: ast.expr) -> list[str]:
    """Every string literal an expression can evaluate to, where that is knowable."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _constants(node.body) + _constants(node.orelse)
    return []


# --- the catalogue, both directions -----------------------------------------


def test_nothing_undeclared_is_ever_sent() -> None:
    undeclared = {k: where for k, where in _emitted_kinds().items() if k not in CATALOGUE}
    assert not undeclared, (
        f"These kinds reach notify() but are not in the catalogue: {undeclared}. "
        "Declare them in iluvtrade.platform.events.CATALOGUE."
    )


def test_nothing_declared_goes_unsent() -> None:
    """A catalogue promising notifications the product never sends is a lie.

    ``account.mfa_*`` are exempt only while MFA is unimplemented; the exemption
    list is deliberately explicit so it cannot quietly grow.
    """

    pending = {"account.mfa_enabled", "account.mfa_disabled"}
    never_emitted = sorted(set(CATALOGUE) - set(_emitted_kinds()) - pending)
    assert not never_emitted, (
        f"Declared but never emitted: {never_emitted}. Either wire them or remove "
        "them from the catalogue."
    )


def test_notify_refuses_an_undeclared_kind(db) -> None:
    principal = make_principal(db)
    with pytest.raises(UnknownNotificationKind, match="not a declared notification kind"):
        notifications.notify(
            db,
            organization_id=principal.organization_id,
            kind="something.invented",
            title="x",
        )


# --- catalogue shape ---------------------------------------------------------


def test_every_entry_is_well_formed() -> None:
    for key, entry in CATALOGUE.items():
        assert entry.key == key
        assert isinstance(entry.severity, Severity)
        assert isinstance(entry.audience, Audience)
        assert entry.summary.endswith("."), f"{key}: the summary is product copy"
        assert len(entry.summary) > 20, f"{key}: the summary says too little"


def test_failures_are_at_least_warnings() -> None:
    """A failure recorded as `info` is a failure nobody notices."""

    for key, entry in CATALOGUE.items():
        if key.endswith((".failed", ".revoked", "_revoked", "_expired")) or "kill" in key:
            assert entry.severity in {Severity.WARNING, Severity.ERROR, Severity.CRITICAL}, key


def test_the_kill_switch_is_critical_and_externally_deliverable() -> None:
    entry = kind_of("trading.kill_switch")
    assert entry.severity is Severity.CRITICAL
    assert entry.audience is Audience.ORGANIZATION
    assert entry.deliverable_externally


# --- severity and audience come from the catalogue ---------------------------


def test_severity_defaults_to_the_declared_one(db) -> None:
    principal = make_principal(db)
    row = notifications.notify(
        db,
        organization_id=principal.organization_id,
        kind="backtest.failed",
        title="x",
    )
    assert row.severity.value == "error"


def test_an_organization_wide_kind_is_never_addressed_to_one_person(db) -> None:
    """A colleague who needs to act must be able to see it."""

    principal = make_principal(db)
    row = notifications.notify(
        db,
        organization_id=principal.organization_id,
        kind="trading.kill_switch",
        title="halted",
        user_id=principal.user_id,
    )
    assert row.user_id is None


def test_an_actor_kind_keeps_its_recipient(db) -> None:
    principal = make_principal(db)
    row = notifications.notify(
        db,
        organization_id=principal.organization_id,
        kind="backtest.completed",
        title="done",
        user_id=principal.user_id,
    )
    assert row.user_id == principal.user_id


# --- the delivery seam --------------------------------------------------------


def test_no_external_channel_is_registered_in_this_build() -> None:
    """Email and push do not exist, and the code says so rather than implying."""

    assert notifications.registered_channels() == ()


def test_a_registered_channel_receives_only_externally_deliverable_kinds(db) -> None:
    delivered: list[str] = []

    class Recording:
        @property
        def name(self) -> str:
            return "recording"

        def deliver(self, notification) -> None:
            delivered.append(notification.kind)

    notifications.register_channel(Recording())
    try:
        principal = make_principal(db)
        notifications.notify(
            db, organization_id=principal.organization_id, kind="backtest.completed", title="x"
        )
        notifications.notify(
            db, organization_id=principal.organization_id, kind="backtest.failed", title="x"
        )
    finally:
        notifications._CHANNELS.clear()

    assert delivered == ["backtest.failed"], (
        "only kinds marked deliverable_externally should leave the application"
    )


def test_a_failing_channel_does_not_fail_the_request(db) -> None:
    """A transport failure is not an application failure."""

    class Broken:
        @property
        def name(self) -> str:
            return "broken"

        def deliver(self, notification) -> None:
            raise RuntimeError("smtp is down")

    notifications.register_channel(Broken())
    try:
        principal = make_principal(db)
        row = notifications.notify(
            db, organization_id=principal.organization_id, kind="backtest.failed", title="x"
        )
        db.flush()
        assert row.id, "the notification is still persisted despite the transport failing"
    finally:
        notifications._CHANNELS.clear()


# --- end to end ---------------------------------------------------------------


def test_the_product_flow_produces_the_notifications_it_should(client, headers) -> None:
    """A user who uploads and backtests is actually told what happened."""

    from iluvtrade.backtests.worker import BacktestWorkerPool
    from tests.conftest import make_csv, register

    register(client, "n@example.com")
    version = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", make_csv(bars=50), "text/csv")},
        headers=headers,
    ).json()
    kinds = {n["kind"] for n in client.get("/api/v1/notifications").json()}
    assert "dataset.ready" in kinds, "a processed dataset must announce itself"

    client.post(f"/api/v1/datasets/versions/{version['id']}/approve", headers=headers)
    strategy = client.post("/api/v1/strategies", json={"name": "S"}, headers=headers).json()
    draft = client.post(
        f"/api/v1/strategies/{strategy['id']}/versions",
        json={"implementation_key": "buy_and_hold", "parameters": {"quantity": 1}},
        headers=headers,
    ).json()
    client.post(f"/api/v1/strategies/versions/{draft['id']}/publish", headers=headers)
    client.post(
        "/api/v1/backtests",
        json={"dataset_version_id": version["id"], "strategy_version_id": draft["id"]},
        headers=headers,
    )
    BacktestWorkerPool(size=1).drain(timeout=120)

    kinds = {n["kind"] for n in client.get("/api/v1/notifications").json()}
    assert "backtest.completed" in kinds
