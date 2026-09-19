"""Invitations: the only way an organization gets a second member.

The property under test throughout is that **the token is not the whole story**.
An invitation names an organization, a role and an email address, and every one
of those is fixed at creation and read from the stored row rather than from the
request that redeems it. A leaked token is therefore not a membership, and an
accepted invitation cannot grant more than the inviter could.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.conftest import register

pytestmark = pytest.mark.security

PASSWORD = "correct-horse-battery-staple"


def _invite(client, headers, email: str, role: str = "trader") -> dict:
    response = client.post(
        "/api/v1/organizations/invitations",
        json={"email": email, "role": role},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


# --- the happy path ---------------------------------------------------------


def test_an_invitation_turns_a_workspace_into_a_team(app, headers) -> None:
    """End to end: invite, accept, switch, and act in the new workspace."""

    from fastapi.testclient import TestClient

    with TestClient(app) as owner, TestClient(app) as guest:
        owner_user = register(owner, "owner@example.com", name="Owner")
        register(guest, "guest@example.com", name="Guest")

        issued = _invite(owner, headers, "guest@example.com", "trader")
        assert issued["invitation"]["status"] == "pending"
        assert issued["invitation"]["role"] == "trader"

        joined = guest.post(
            "/api/v1/organizations/invitations/accept",
            json={"token": issued["token"]},
            headers=headers,
        )
        assert joined.status_code == 200, joined.text
        assert joined.json()["role"] == "trader"

        # Accepting does not move the guest out of their own workspace.
        assert (
            guest.get("/api/v1/auth/me").json()["organization_id"]
            != (owner_user["organization_id"])
        )

        workspaces = guest.get("/api/v1/auth/organizations").json()
        assert {w["organization_id"] for w in workspaces} == {
            guest.get("/api/v1/auth/me").json()["organization_id"],
            owner_user["organization_id"],
        }

        switched = guest.post(
            "/api/v1/auth/switch-organization",
            json={"organization_id": owner_user["organization_id"]},
            headers=headers,
        )
        assert switched.status_code == 200, switched.text
        assert switched.json()["user"]["organization_id"] == owner_user["organization_id"]
        assert switched.json()["user"]["role"] == "trader"

        # And the new session really acts there.
        members = guest.get("/api/v1/organizations/members").json()
        assert {m["email"] for m in members} == {"owner@example.com", "guest@example.com"}


def test_the_owner_sees_the_invitation_as_accepted(app, headers) -> None:
    from fastapi.testclient import TestClient

    with TestClient(app) as owner, TestClient(app) as guest:
        register(owner, "o2@example.com")
        register(guest, "g2@example.com")
        issued = _invite(owner, headers, "g2@example.com")
        guest.post(
            "/api/v1/organizations/invitations/accept",
            json={"token": issued["token"]},
            headers=headers,
        )
        listed = owner.get("/api/v1/organizations/invitations").json()
        assert [i["status"] for i in listed] == ["accepted"]


# --- what must not work -----------------------------------------------------


def test_an_expired_invitation_is_refused(app, headers, db) -> None:
    from fastapi.testclient import TestClient

    from iluvtrade.db.base import utcnow
    from iluvtrade.db.models.platform import Invitation
    from iluvtrade.db.session import session_scope

    with TestClient(app) as owner, TestClient(app) as guest:
        register(owner, "o3@example.com")
        register(guest, "g3@example.com")
        issued = _invite(owner, headers, "g3@example.com")

        with session_scope() as session:
            row = session.get(Invitation, issued["invitation"]["id"])
            row.expires_at = utcnow() - timedelta(seconds=1)

        refused = guest.post(
            "/api/v1/organizations/invitations/accept",
            json={"token": issued["token"]},
            headers=headers,
        )
        assert refused.status_code == 400
        assert "not valid" in refused.json()["error"]["message"]


def test_a_revoked_invitation_is_refused(app, headers) -> None:
    from fastapi.testclient import TestClient

    with TestClient(app) as owner, TestClient(app) as guest:
        register(owner, "o4@example.com")
        register(guest, "g4@example.com")
        issued = _invite(owner, headers, "g4@example.com")

        revoked = owner.post(
            f"/api/v1/organizations/invitations/{issued['invitation']['id']}/revoke",
            headers=headers,
        )
        assert revoked.status_code == 200
        assert revoked.json()["status"] == "revoked"

        refused = guest.post(
            "/api/v1/organizations/invitations/accept",
            json={"token": issued["token"]},
            headers=headers,
        )
        assert refused.status_code == 400


def test_an_invitation_cannot_be_used_twice(app, headers) -> None:
    """Single use, even when the second caller is a different person."""

    from fastapi.testclient import TestClient

    with TestClient(app) as owner, TestClient(app) as guest, TestClient(app) as other:
        register(owner, "o5@example.com")
        register(guest, "g5@example.com")
        register(other, "g5-other@example.com")
        issued = _invite(owner, headers, "g5@example.com")

        first = guest.post(
            "/api/v1/organizations/invitations/accept",
            json={"token": issued["token"]},
            headers=headers,
        )
        assert first.status_code == 200

        for client in (guest, other):
            again = client.post(
                "/api/v1/organizations/invitations/accept",
                json={"token": issued["token"]},
                headers=headers,
            )
            assert again.status_code == 400


def test_a_token_is_inert_in_the_wrong_hands(app, headers) -> None:
    """The invited *address* must match, so a forwarded token does nothing.

    This is the control that matters most in a deployment with no email: the
    inviter passes the token through some channel, and that channel can leak.
    """

    from fastapi.testclient import TestClient

    with TestClient(app) as owner, TestClient(app) as wrong:
        register(owner, "o6@example.com")
        register(wrong, "someone-else@example.com")
        issued = _invite(owner, headers, "intended@example.com")

        refused = wrong.post(
            "/api/v1/organizations/invitations/accept",
            json={"token": issued["token"]},
            headers=headers,
        )
        assert refused.status_code == 400
        # And the refusal reads the same as an unknown token: whose workspace
        # this is, and whether the invitation exists, are not the caller's
        # business.
        unknown = wrong.post(
            "/api/v1/organizations/invitations/accept",
            json={"token": "x" * 44},
            headers=headers,
        )
        assert refused.json()["error"] == unknown.json()["error"]


def test_a_role_cannot_be_escalated_by_the_person_accepting(app, headers) -> None:
    """There is no field on the accept request that could carry a role."""

    from fastapi.testclient import TestClient

    with TestClient(app) as owner, TestClient(app) as guest:
        register(owner, "o7@example.com")
        register(guest, "g7@example.com")
        issued = _invite(owner, headers, "g7@example.com", "viewer")

        smuggled = guest.post(
            "/api/v1/organizations/invitations/accept",
            json={"token": issued["token"], "role": "owner"},
            headers=headers,
        )
        assert smuggled.status_code == 422, "an unknown field must be refused outright"

        accepted = guest.post(
            "/api/v1/organizations/invitations/accept",
            json={"token": issued["token"]},
            headers=headers,
        )
        assert accepted.json()["role"] == "viewer"


def test_an_inviter_cannot_grant_more_than_they_hold(app, headers, db) -> None:
    """An admin minting an owner, then being promoted by them, is the attack."""

    from fastapi.testclient import TestClient

    from iluvtrade.db.models.platform import Membership, Role
    from iluvtrade.db.session import session_scope

    with TestClient(app) as owner, TestClient(app) as admin:
        owner_user = register(owner, "o8@example.com")
        admin_user = register(admin, "a8@example.com")
        issued = _invite(owner, headers, "a8@example.com", "admin")
        admin.post(
            "/api/v1/organizations/invitations/accept",
            json={"token": issued["token"]},
            headers=headers,
        )
        admin.post(
            "/api/v1/auth/switch-organization",
            json={"organization_id": owner_user["organization_id"]},
            headers=headers,
        )
        with session_scope() as session:
            membership = (
                session.query(Membership)
                .filter_by(user_id=admin_user["id"], organization_id=owner_user["organization_id"])
                .one()
            )
            assert membership.role is Role.ADMIN

        refused = admin.post(
            "/api/v1/organizations/invitations",
            json={"email": "puppet@example.com", "role": "owner"},
            headers=headers,
        )
        assert refused.status_code == 400
        assert "never grant more than the inviter holds" in refused.json()["error"]["message"]


def test_a_non_admin_cannot_invite(app, headers) -> None:
    from fastapi.testclient import TestClient

    with TestClient(app) as owner, TestClient(app) as viewer:
        owner_user = register(owner, "o9@example.com")
        register(viewer, "v9@example.com")
        issued = _invite(owner, headers, "v9@example.com", "viewer")
        viewer.post(
            "/api/v1/organizations/invitations/accept",
            json={"token": issued["token"]},
            headers=headers,
        )
        viewer.post(
            "/api/v1/auth/switch-organization",
            json={"organization_id": owner_user["organization_id"]},
            headers=headers,
        )
        refused = viewer.post(
            "/api/v1/organizations/invitations",
            json={"email": "x@example.com", "role": "viewer"},
            headers=headers,
        )
        assert refused.status_code == 403
        assert viewer.get("/api/v1/organizations/invitations").status_code == 403


# --- tenancy ----------------------------------------------------------------


def test_one_workspace_cannot_see_or_revoke_anothers_invitations(app, headers) -> None:
    from fastapi.testclient import TestClient

    with TestClient(app) as alice, TestClient(app) as bob:
        register(alice, "alice-inv@example.com")
        register(bob, "bob-inv@example.com")
        issued = _invite(alice, headers, "target@example.com")

        assert bob.get("/api/v1/organizations/invitations").json() == []
        stolen = bob.post(
            f"/api/v1/organizations/invitations/{issued['invitation']['id']}/revoke",
            headers=headers,
        )
        assert stolen.status_code == 404, "a foreign id must be indistinguishable from absent"


def test_members_of_one_workspace_are_invisible_to_another(app, headers) -> None:
    from fastapi.testclient import TestClient

    with TestClient(app) as alice, TestClient(app) as bob:
        register(alice, "alice-mem@example.com")
        register(bob, "bob-mem@example.com")
        assert [m["email"] for m in bob.get("/api/v1/organizations/members").json()] == [
            "bob-mem@example.com"
        ]


def test_switching_to_a_workspace_you_do_not_belong_to_is_refused(app, headers) -> None:
    from fastapi.testclient import TestClient

    with TestClient(app) as alice, TestClient(app) as bob:
        alice_user = register(alice, "alice-sw@example.com")
        register(bob, "bob-sw@example.com")
        refused = bob.post(
            "/api/v1/auth/switch-organization",
            json={"organization_id": alice_user["organization_id"]},
            headers=headers,
        )
        assert refused.status_code == 401
        assert bob.get("/api/v1/auth/me").json()["email"] == "bob-sw@example.com"


# --- storage ----------------------------------------------------------------


def test_the_token_is_never_stored_in_readable_form(app, headers) -> None:
    from fastapi.testclient import TestClient

    from iluvtrade.db.models.platform import Invitation
    from iluvtrade.db.session import session_scope

    with TestClient(app) as owner:
        register(owner, "store@example.com")
        issued = _invite(owner, headers, "target-store@example.com")

        with session_scope() as session:
            row = session.get(Invitation, issued["invitation"]["id"])
            assert row.token_hash != issued["token"]
            assert issued["token"] not in row.token_hash
            assert len(row.token_hash) == 64


def test_the_token_is_never_served_again(app, headers) -> None:
    from fastapi.testclient import TestClient

    with TestClient(app) as owner:
        register(owner, "once-inv@example.com")
        issued = _invite(owner, headers, "target-once@example.com")
        token = issued["token"]

        for path in ("/api/v1/organizations/invitations", "/api/v1/organizations/members"):
            assert token not in owner.get(path).text


def test_inviting_the_same_address_twice_supersedes_the_first_token(app, headers) -> None:
    """Two live tokens for one seat doubles the window a leak is useful in."""

    from fastapi.testclient import TestClient

    with TestClient(app) as owner, TestClient(app) as guest:
        register(owner, "super@example.com")
        register(guest, "g-super@example.com")
        first = _invite(owner, headers, "g-super@example.com")
        second = _invite(owner, headers, "g-super@example.com")

        stale = guest.post(
            "/api/v1/organizations/invitations/accept",
            json={"token": first["token"]},
            headers=headers,
        )
        assert stale.status_code == 400

        fresh = guest.post(
            "/api/v1/organizations/invitations/accept",
            json={"token": second["token"]},
            headers=headers,
        )
        assert fresh.status_code == 200


def test_inviting_an_existing_member_is_refused(app, headers) -> None:
    from fastapi.testclient import TestClient

    with TestClient(app) as owner:
        register(owner, "dup@example.com")
        refused = owner.post(
            "/api/v1/organizations/invitations",
            json={"email": "dup@example.com", "role": "viewer"},
            headers=headers,
        )
        assert refused.status_code == 400
        assert "already a member" in refused.json()["error"]["message"]


def test_every_invitation_action_is_audited(app, headers) -> None:
    from fastapi.testclient import TestClient

    with TestClient(app) as owner, TestClient(app) as guest:
        register(owner, "audit-inv@example.com")
        register(guest, "g-audit@example.com")
        issued = _invite(owner, headers, "g-audit@example.com")
        guest.post(
            "/api/v1/organizations/invitations/accept",
            json={"token": issued["token"]},
            headers=headers,
        )

        actions = [event["action"] for event in owner.get("/api/v1/audit?limit=100").json()]
        assert "organization.invitation_created" in actions
        assert "organization.invitation_accepted" in actions

        # And no audit payload carries the token.
        assert issued["token"] not in owner.get("/api/v1/audit?limit=100").text


def test_a_second_invitation_to_an_existing_member_is_refused_at_issue(app, headers) -> None:
    """The outer guard: you cannot even mint a token for someone already in."""

    from fastapi.testclient import TestClient

    with TestClient(app) as owner, TestClient(app) as guest:
        register(owner, "dup-owner@example.com")
        register(guest, "dup-guest@example.com")

        first = _invite(owner, headers, "dup-guest@example.com", "viewer")
        assert (
            guest.post(
                "/api/v1/organizations/invitations/accept",
                json={"token": first["token"]},
                headers=headers,
            ).status_code
            == 200
        )

        refused = owner.post(
            "/api/v1/organizations/invitations",
            json={"email": "dup-guest@example.com", "role": "admin"},
            headers=headers,
        )
        assert refused.status_code == 400
        assert "already a member" in refused.json()["error"]["message"]


def test_accepting_when_already_a_member_cannot_add_a_second_membership(app, headers) -> None:
    """The inner guard, as defence in depth.

    The check above means this branch is not reachable through the API today.
    It is still the one that would matter if a membership ever arrived by
    another route, so the membership is created directly here to exercise it —
    and the assertion is that the role does **not** change, because silently
    upgrading someone on a replayed token is the interesting failure.
    """

    from fastapi.testclient import TestClient

    from iluvtrade.db.models.platform import Membership, Role
    from iluvtrade.db.session import session_scope

    with TestClient(app) as owner, TestClient(app) as guest:
        owner_user = register(owner, "inner-owner@example.com")
        guest_user = register(guest, "inner-guest@example.com")

        issued = _invite(owner, headers, "inner-guest@example.com", "admin")

        # The membership arrives by another route, at a lower role.
        with session_scope() as session:
            session.add(
                Membership(
                    organization_id=owner_user["organization_id"],
                    user_id=guest_user["id"],
                    role=Role.VIEWER,
                )
            )

        again = guest.post(
            "/api/v1/organizations/invitations/accept",
            json={"token": issued["token"]},
            headers=headers,
        )
        assert again.status_code == 400
        assert "already a member" in again.json()["error"]["message"]

        with session_scope() as session:
            memberships = (
                session.query(Membership)
                .filter_by(
                    user_id=guest_user["id"],
                    organization_id=owner_user["organization_id"],
                )
                .all()
            )
            assert len(memberships) == 1
            assert memberships[0].role is Role.VIEWER, (
                "a replayed invitation upgraded an existing member's role"
            )


def test_an_invitation_is_bound_to_the_organization_that_issued_it(app, headers) -> None:
    """A token cannot be redeemed into a different workspace."""

    from fastapi.testclient import TestClient

    with TestClient(app) as alice, TestClient(app) as bob, TestClient(app) as guest:
        alice_user = register(alice, "alice-bind@example.com")
        bob_user = register(bob, "bob-bind@example.com")
        register(guest, "guest-bind@example.com")

        issued = _invite(alice, headers, "guest-bind@example.com", "trader")
        guest.post(
            "/api/v1/organizations/invitations/accept",
            json={"token": issued["token"]},
            headers=headers,
        )

        workspaces = {w["organization_id"] for w in guest.get("/api/v1/auth/organizations").json()}
        assert alice_user["organization_id"] in workspaces
        assert bob_user["organization_id"] not in workspaces, (
            "the invitation put the guest in a workspace it did not name"
        )
