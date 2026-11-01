"""OSS multi-user boundaries with RBAC enabled and no proprietary plugins.

Core applies no access control between users of the same account: every
member can do everything in it. Accounts stay isolated from each other, roles
are a commercial overlay, and the overlay hooks are honoured when registered.
"""

import sys
from types import ModuleType
from typing import Any
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_role,
    crud_team,
    crud_team_role,
    crud_user,
    crud_user_invitation,
    crud_user_role,
)
from preloop.services import multiuser


@pytest.fixture
def member(db_session: Session, test_user: models.User) -> models.User:
    """Persist an ordinary colleague who holds no role at all."""
    return crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "email": "member@example.com",
            "username": "basic_member",
            "hashed_password": "unused",
            "is_active": True,
            "email_verified": True,
        },
    )


@pytest.fixture
def secured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the suite's permission bypass so the core edition runs as shipped."""
    from preloop.config import settings
    from preloop.utils import permissions

    monkeypatch.setenv("DISABLE_PROPRIETARY_PLUGINS", "true")
    monkeypatch.setenv("DISABLE_RBAC", "false")
    monkeypatch.setattr(settings, "disable_rbac", False)
    monkeypatch.setattr(permissions, "_plugin_require_permission", None)


@pytest.fixture
def no_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep invitation emails off the network."""
    from preloop.api.endpoints import invitations

    monkeypatch.setattr(invitations, "send_invitation_email", lambda **kwargs: None)


def test_any_member_can_administer_the_account(
    client: TestClient,
    app: Any,
    db_session: Session,
    test_user: models.User,
    member: models.User,
    secured: None,
    no_email: None,
) -> None:
    """A colleague without roles manages users, invitations and teams."""
    app.dependency_overrides[get_current_active_user] = lambda: member

    created = client.post(
        "/api/v1/users",
        json={
            "username": "added_by_member",
            "email": "added@example.com",
            "password": "password123",
        },
    )
    assert created.status_code == 201, created.text
    assert (
        crud_user_role.get_by_user(db_session, user_id=UUID(created.json()["id"])) == []
    )
    assert created.json()["roles"] == []
    assert client.get("/api/v1/users").status_code == 200

    invited = client.post("/api/v1/invitations", json={"email": "new@example.com"})
    assert invited.status_code == 201, invited.text
    assert client.get("/api/v1/invitations").status_code == 200
    resent = client.post(f"/api/v1/invitations/{invited.json()['id']}/resend")
    assert resent.status_code in (200, 204), resent.text
    cancelled = client.delete(f"/api/v1/invitations/{invited.json()['id']}")
    assert cancelled.status_code == 204, cancelled.text

    team = client.post("/api/v1/teams", json={"name": "Member team"})
    assert team.status_code == 201, team.text
    team_id = team.json()["id"]
    added = client.post(
        f"/api/v1/teams/{team_id}/members",
        json={"user_id": created.json()["id"], "role": "lead"},
    )
    assert added.status_code == 201, added.text
    relabelled = client.patch(
        f"/api/v1/teams/{team_id}/members/{created.json()['id']}",
        json={"role": "member"},
    )
    assert relabelled.status_code == 200, relabelled.text
    renamed = client.patch(f"/api/v1/teams/{team_id}", json={"name": "Renamed"})
    assert renamed.status_code == 200, renamed.text
    assert client.delete(f"/api/v1/teams/{team_id}").status_code == 204

    # No owner protection in core: the primary user is an ordinary member.
    deactivated = client.post(f"/api/v1/users/{test_user.id}/deactivate")
    assert deactivated.status_code == 204, deactivated.text


@pytest.mark.parametrize("resource", ["users", "teams", "invitations"])
def test_member_cannot_read_other_account(
    client: TestClient,
    app: Any,
    db_session: Session,
    member: models.User,
    secured: None,
    resource: str,
) -> None:
    """Lists and direct ids stay confined to the authenticated account."""
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other account"}
    )
    user = crud_user.create(
        db_session,
        obj_in={
            "account_id": other.id,
            "username": "other_member",
            "email": "other@example.com",
            "is_active": True,
        },
    )
    team = crud_team.create(
        db_session, obj_in={"account_id": other.id, "name": "Other team"}
    )
    invitation = crud_user_invitation.create(
        db_session,
        obj_in={
            "account_id": other.id,
            "email": "invited@example.com",
            "invited_by": user.id,
        },
    )
    target = {"users": user, "teams": team, "invitations": invitation}[resource]
    app.dependency_overrides[get_current_active_user] = lambda: member
    response = client.get(f"/api/v1/{resource}/{target.id}")
    assert response.status_code in (403, 404), response.text
    listing = client.get(f"/api/v1/{resource}")
    assert listing.status_code == 200, listing.text
    body = listing.json()
    rows = body[resource] if isinstance(body, dict) else body
    assert str(target.id) not in [row["id"] for row in rows]


def test_member_cannot_remove_another_accounts_user(
    client: TestClient,
    app: Any,
    db_session: Session,
    member: models.User,
    secured: None,
) -> None:
    """Full access inside an account never reaches into another account."""
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other account"}
    )
    foreign = crud_user.create(
        db_session,
        obj_in={
            "account_id": other.id,
            "username": "foreign_member",
            "email": "foreign@example.com",
            "is_active": True,
        },
    )
    app.dependency_overrides[get_current_active_user] = lambda: member
    response = client.post(f"/api/v1/users/{foreign.id}/deactivate")
    assert response.status_code == 403, response.text
    db_session.refresh(foreign)
    assert foreign.is_active


def test_role_assignment_routes_are_not_part_of_core(
    client: TestClient,
    db_session: Session,
    member: models.User,
    secured: None,
) -> None:
    """Role grants are served by the commercial overlay only."""
    admin = crud_role.get_by_name(db_session, name="admin")
    assert admin is not None
    assigned = client.post(
        f"/api/v1/users/{member.id}/roles", json={"role_id": str(admin.id)}
    )
    assert assigned.status_code in (404, 405), assigned.text
    removed = client.delete(f"/api/v1/users/{member.id}/roles/{admin.id}")
    assert removed.status_code in (404, 405), removed.text
    assert crud_user_role.get_by_user(db_session, user_id=member.id) == []


def test_invitation_roles_require_an_upgrade(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    secured: None,
    no_email: None,
) -> None:
    """Asking core to grant a role answers 402 and stores nothing."""
    admin = crud_role.get_by_name(db_session, name="admin")
    assert admin is not None
    response = client.post(
        "/api/v1/invitations",
        json={"email": "role@example.com", "role_ids": [str(admin.id)]},
    )
    assert response.status_code == 402, response.text
    assert response.json()["detail"] == {
        "code": "upgrade_required",
        "feature": "rbac",
    }
    assert (
        crud_user_invitation.get_by_email(
            db_session, email="role@example.com", account_id=test_user.account_id
        )
        is None
    )


def test_stored_invitation_roles_are_not_granted_in_oss(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    secured: None,
) -> None:
    """An invitation issued by a commercial edition still joins, without roles."""
    role = crud_role.get_by_name(db_session, name="admin")
    assert role is not None
    invitation = crud_user_invitation.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "email": "stored@example.com",
            "invited_by": test_user.id,
            "role_ids": str(role.id),
        },
    )
    response = client.post(
        "/api/v1/invitations/accept",
        json={
            "token": invitation.token,
            "username": "stored_invitee",
            "password": "password123",
        },
    )
    assert response.status_code == 201, response.text
    user = crud_user.get_by_username(db_session, username="stored_invitee")
    assert user is not None
    assert crud_user_role.get_by_user(db_session, user_id=user.id) == []


def test_member_profile_reports_unrestricted_access(
    client: TestClient,
    app: Any,
    member: models.User,
    secured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An installed but disabled plugin cannot restrict the core profile."""
    plugin = ModuleType("preloop.plugins.proprietary.rbac.permissions")
    permission_lookup = Mock(return_value=[])
    plugin.get_user_permissions = permission_lookup  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, plugin.__name__, plugin)
    app.dependency_overrides[get_current_active_user] = lambda: member
    response = client.get("/api/v1/auth/users/me")
    assert response.status_code == 200, response.text
    assert response.json()["permissions"] is None
    permission_lookup.assert_not_called()


def test_role_helpers_grant_every_member_everything(
    db_session: Session, member: models.User, secured: None
) -> None:
    """Shared role helpers treat a roleless colleague as fully permitted."""
    from preloop.utils.permissions import (
        ensure_permission_in_oss,
        members_have_full_access,
        user_holds_permission,
        user_permission_names,
    )

    assert members_have_full_access()
    assert user_holds_permission(db_session, member, "manage_kill_switch")
    assert "manage_users" in user_permission_names(db_session, member)
    ensure_permission_in_oss(db_session, member, "decide_approvals")


def test_core_has_no_role_catalog(
    client: TestClient,
    db_session: Session,
    secured: None,
) -> None:
    """Core cannot expose role metadata, including other accounts' roles."""
    account = crud_account.create(
        db_session, obj_in={"organization_name": "Private account"}
    )
    role = crud_role.create(
        db_session, obj_in={"account_id": account.id, "name": "private_role"}
    )
    response = client.get("/api/v1/roles")
    assert response.status_code == 404, response.text
    assert str(role.id) not in response.text


class _RecordingAccessControl:
    """Stand-in for the commercial overlay registered through PluginManager."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def validate_invitation_roles(
        self, db: Session, current_user: models.User, role_ids: list[UUID]
    ) -> list[UUID]:
        self.calls.append("validate")
        return role_ids

    def apply_invitation_roles(
        self, db: Session, user: models.User, invitation: models.UserInvitation
    ) -> None:
        self.calls.append("apply")

    def ensure_can_remove_member(
        self, db: Session, current_user: models.User, user: models.User
    ) -> None:
        self.calls.append("remove")
        raise HTTPException(403, "Only an account owner can remove an owner")


def test_access_control_overlay_is_consulted(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    member: models.User,
    secured: None,
    no_email: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An installed overlay validates roles, grants them and guards removals."""
    overlay = _RecordingAccessControl()
    monkeypatch.setattr(multiuser, "access_control", lambda: overlay)
    role_id = str(uuid4())

    invited = client.post(
        "/api/v1/invitations",
        json={"email": "overlay@example.com", "role_ids": [role_id]},
    )
    assert invited.status_code == 201, invited.text
    assert invited.json()["role_ids"] == role_id
    invitation = crud_user_invitation.get(db_session, id=invited.json()["id"])
    accepted = client.post(
        "/api/v1/invitations/accept",
        json={
            "token": invitation.token,
            "username": "overlay_invitee",
            "password": "password123",
        },
    )
    assert accepted.status_code == 201, accepted.text

    for method, path, payload in (
        ("POST", f"/api/v1/users/{member.id}/deactivate", None),
        ("PATCH", f"/api/v1/users/{member.id}", {"is_active": False}),
    ):
        blocked = client.request(method, path, json=payload)
        assert blocked.status_code == 403, blocked.text
    bulk = client.post("/api/v1/users/bulk-delete", json={"user_ids": [str(member.id)]})
    assert bulk.json()["results"][0]["status"] == "skipped"
    db_session.refresh(member)
    assert member.is_active
    assert overlay.calls == ["validate", "apply", "remove", "remove", "remove"]


def test_core_responses_do_not_advertise_stored_commercial_roles(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    secured: None,
) -> None:
    """Edition changes preserve role storage without exposing active core roles."""
    owner = crud_role.get_by_name(db_session, name="owner")
    assert owner is not None
    team = crud_team.create(
        db_session, obj_in={"account_id": test_user.account_id, "name": "Example"}
    )
    crud_team_role.assign_role(db_session, team_id=team.id, role_id=owner.id)
    profile = client.get("/api/v1/users/me").json()
    assert profile["roles"] == []
    assert profile["inherited_roles"] == []
    listed = client.get("/api/v1/users").json()["users"]
    assert all(row["roles"] == [] and row["inherited_roles"] == [] for row in listed)
    teams = client.get("/api/v1/teams").json()["teams"]
    assert teams[0]["roles"] == []
    assert crud_team_role.get_team_roles(db_session, team_id=team.id) == [owner]
