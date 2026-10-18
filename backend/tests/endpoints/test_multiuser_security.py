"""Real OSS authorization checks with RBAC enabled and no proprietary plugins."""

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_role,
    crud_team,
    crud_user,
    crud_user_invitation,
    crud_user_role,
)


@pytest.fixture
def member(db_session: Session, test_user: models.User) -> models.User:
    """Persist an ordinary colleague with no administrative role."""
    user = crud_user.create(
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
    role = crud_role.get_by_name(db_session, name="member")
    assert role is not None
    crud_user_role.assign_role(db_session, user_id=user.id, role_id=role.id)
    return user


@pytest.fixture
def secured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the suite's permission bypass for security-sensitive requests."""
    from preloop.config import settings
    from preloop.utils import permissions

    monkeypatch.setenv("DISABLE_PROPRIETARY_PLUGINS", "true")
    monkeypatch.setenv("DISABLE_RBAC", "false")
    monkeypatch.setattr(settings, "disable_rbac", False)
    monkeypatch.setattr(permissions, "_plugin_require_permission", None)


@pytest.mark.parametrize(
    "method,path,payload",
    [
        (
            "post",
            "/users",
            {
                "username": "new_member",
                "email": "new@example.com",
                "password": "password123",
            },
        ),
        ("patch", "/users/{user}", {"is_active": False}),
        ("post", "/users/{user}/deactivate", None),
        ("post", "/users/bulk-delete", {"user_ids": []}),
        ("post", "/users/{user}/roles", {"role_id": "{role}"}),
        ("delete", "/users/{user}/roles/{role}", None),
        ("post", "/invitations", {"email": "new@example.com"}),
        ("delete", "/invitations/{invitation}", None),
        ("post", "/invitations/{invitation}/resend", None),
        ("post", "/teams", {"name": "Escalated team"}),
        ("patch", "/teams/{team}", {"name": "Escalated"}),
        ("delete", "/teams/{team}", None),
        ("post", "/teams/{team}/members", {"user_id": "{user}", "role": "admin"}),
        ("patch", "/teams/{team}/members/{user}", {"role": "admin"}),
        ("delete", "/teams/{team}/members/{user}", None),
    ],
)
def test_member_cannot_administer_account(
    client: TestClient,
    app: Any,
    db_session: Session,
    test_user: models.User,
    member: models.User,
    secured: None,
    method: str,
    path: str,
    payload: Any,
) -> None:
    """Members cannot escalate directly, by invite or through team membership."""
    app.dependency_overrides[get_current_active_user] = lambda: member
    admin = crud_role.get_by_name(db_session, name="admin")
    ids = {
        "user": str(test_user.id),
        "role": str(admin.id),
        "team": str(uuid4()),
        "invitation": str(uuid4()),
    }
    data = (
        {
            key: value.format(**ids) if isinstance(value, str) else value
            for key, value in payload.items()
        }
        if payload is not None
        else None
    )
    response = client.request(method.upper(), "/api/v1" + path.format(**ids), json=data)
    assert response.status_code == 403, response.text
    assert (
        not crud_user_role.get_by_user(db_session, user_id=member.id)[0].role_id
        == admin.id
    )


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
    if resource in {"users", "invitations"}:
        # Tokens grant membership, so ordinary colleagues cannot enumerate them.
        assert listing.status_code == 403, listing.text
        return
    assert listing.status_code == 200, listing.text
    assert str(target.id) not in [row["id"] for row in listing.json()[resource]]


def test_admin_can_assign_basic_roles_but_not_custom_roles(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    member: models.User,
    secured: None,
) -> None:
    """OSS supports its basic roles and rejects advanced grant requests."""
    admin = crud_role.get_by_name(db_session, name="admin")
    response = client.post(
        f"/api/v1/users/{member.id}/roles", json={"role_id": str(admin.id)}
    )
    assert response.status_code == 201, response.text
    custom = crud_role.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "name": "custom",
            "is_system_role": False,
        },
    )
    denied = client.post(
        f"/api/v1/users/{member.id}/roles", json={"role_id": str(custom.id)}
    )
    assert denied.status_code == 402, denied.text
    assert denied.json()["detail"]["code"] == "upgrade_required"


def test_role_listing_cannot_leak_other_accounts(
    client: TestClient,
    db_session: Session,
    secured: None,
) -> None:
    """The role selector never exposes another tenant's custom roles."""
    account = crud_account.create(
        db_session, obj_in={"organization_name": "Private account"}
    )
    role = crud_role.create(
        db_session, obj_in={"account_id": account.id, "name": "private_role"}
    )
    response = client.get("/api/v1/roles")
    assert response.status_code == 200, response.text
    assert str(role.id) not in [item["id"] for item in response.json()["roles"]]
    assert {item["name"] for item in response.json()["roles"]} <= {
        "owner",
        "admin",
        "member",
    }


def test_admin_cannot_bind_another_accounts_role(
    client: TestClient,
    db_session: Session,
    member: models.User,
    secured: None,
) -> None:
    """Even an administrator cannot attach a foreign account's role."""
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other account"}
    )
    role = crud_role.create(
        db_session, obj_in={"account_id": other.id, "name": "Foreign administrator"}
    )
    response = client.post(
        f"/api/v1/users/{member.id}/roles", json={"role_id": str(role.id)}
    )
    assert response.status_code == 404, response.text
    invite = client.post(
        "/api/v1/invitations",
        json={"email": "foreign_role@example.com", "role_ids": [str(role.id)]},
    )
    assert invite.status_code == 403, invite.text


def test_new_oss_user_defaults_to_member(
    client: TestClient,
    db_session: Session,
    secured: None,
) -> None:
    """Directly added colleagues get an effective basic role."""
    response = client.post(
        "/api/v1/users",
        json={
            "username": "default_member",
            "email": "default@example.com",
            "password": "password123",
        },
    )
    assert response.status_code == 201, response.text
    assert [role["name"] for role in response.json()["roles"]] == ["member"]


def test_member_profile_exposes_enforced_permissions(
    client: TestClient,
    app: Any,
    member: models.User,
    secured: None,
) -> None:
    """The console sees explicit grants instead of treating OSS as unrestricted."""
    app.dependency_overrides[get_current_active_user] = lambda: member
    response = client.get("/api/v1/auth/users/me")
    assert response.status_code == 200, response.text
    grants = response.json()["permissions"]
    assert isinstance(grants, list)
    assert "view_teams" in grants
    assert not set(grants) & {
        "view_users",
        "manage_users",
        "invite_users",
        "assign_roles",
        "manage_teams",
        "create_teams",
        "edit_teams",
        "delete_teams",
    }


def test_stored_advanced_invitation_cannot_grant_roles_in_oss(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    secured: None,
) -> None:
    """An invitation issued by an advanced edition cannot enable custom OSS roles."""
    role = crud_role.create(
        db_session,
        obj_in={"account_id": test_user.account_id, "name": "Stored custom role"},
    )
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
    assert response.status_code == 402, response.text
    assert crud_user.get_by_username(db_session, username="stored_invitee") is None
    db_session.refresh(invitation)
    assert invitation.status == models.UserInvitationStatus.PENDING


def test_basic_admin_can_manage_colleagues_but_cannot_grant_ownership(
    client: TestClient,
    app: Any,
    db_session: Session,
    test_user: models.User,
    member: models.User,
    secured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-owner administrator can manage basic teams and invitations."""
    from preloop.api.endpoints import invitations

    monkeypatch.setattr(invitations, "send_invitation_email", lambda **kwargs: None)
    admin = crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "username": "basic_admin",
            "email": "admin@example.com",
            "is_active": True,
        },
    )
    admin_role = crud_role.get_by_name(db_session, name="admin")
    owner_role = crud_role.get_by_name(db_session, name="owner")
    crud_user_role.assign_role(db_session, user_id=admin.id, role_id=admin_role.id)
    app.dependency_overrides[get_current_active_user] = lambda: admin
    team = client.post("/api/v1/teams", json={"name": "Admin managed team"})
    assert team.status_code == 201, team.text
    added = client.post(
        f"/api/v1/teams/{team.json()['id']}/members",
        json={"user_id": str(member.id), "role": "member"},
    )
    assert added.status_code == 201, added.text
    invited = client.post(
        "/api/v1/invitations",
        json={"email": "colleague@example.com", "role_ids": [str(admin_role.id)]},
    )
    assert invited.status_code == 201, invited.text
    denied = client.post(
        f"/api/v1/users/{admin.id}/roles", json={"role_id": str(owner_role.id)}
    )
    assert denied.status_code == 403, denied.text
    proxy = client.post(
        "/api/v1/invitations",
        json={"email": "proxy@example.com", "role_ids": [str(owner_role.id)]},
    )
    assert proxy.status_code == 403, proxy.text
