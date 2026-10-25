"""Role visibility through the mounted route and real tenant-aware CRUD."""

from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.models import crud, models
from preloop.plugins import account_hooks
from preloop.services import multiuser


@pytest.fixture
def scoped_roles(db_session: Session, test_user: models.User) -> dict[str, Any]:
    other = crud.crud_account.create(
        db_session, obj_in={"organization_name": "Other Example", "is_active": True}
    )
    owners = {
        "own": (test_user.account_id, False),
        "foreign": (other.id, False),
        "foreign_system_marker": (other.id, True),
        "global": (None, True),
        "unowned_custom": (None, False),
    }
    roles = {}
    for label, (account_id, is_system) in owners.items():
        role = models.Role(
            id=uuid4(),
            account_id=account_id,
            name=f"example_{label}",
            description=f"{label} role details",
            is_system_role=is_system,
        )
        permission = models.Permission(
            id=uuid4(),
            name=f"example_{label}",
            description=f"{label} permission details",
            category="example",
        )
        role.role_permissions = [models.RolePermission(permission=permission)]
        db_session.add(role)
        roles[label] = role
    db_session.flush()
    return {"roles": roles, "other_account": other.id}


def test_account_roles_crud_rejects_foreign_system_marker(
    db_session: Session, test_user: models.User, scoped_roles: dict[str, Any]
) -> None:
    rows = crud.crud_role.get_all_for_account(
        db_session, account_id=test_user.account_id
    )
    ids = {row.id for row in rows}
    roles = scoped_roles["roles"]
    assert {roles["own"].id, roles["global"].id} <= ids
    assert ids.isdisjoint(
        {
            roles[key].id
            for key in ("foreign", "foreign_system_marker", "unowned_custom")
        }
    )


@pytest.mark.parametrize("other_account", [False, True])
def test_mounted_roles_route_scopes_projection_and_total(
    client: TestClient,
    app: FastAPI,
    test_user: models.User,
    scoped_roles: dict[str, Any],
    other_account: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Account custom roles are listed only when an overlay supports them.
    monkeypatch.setattr(multiuser, "advanced_roles_enabled", lambda: True)
    if other_account:
        caller = models.User(id=uuid4(), account_id=scoped_roles["other_account"])
        app.dependency_overrides[get_current_active_user] = lambda: caller
    response = client.get("/api/v1/roles")
    assert response.status_code == 200
    data = response.json()
    roles = scoped_roles["roles"]
    expected = (
        {"foreign", "foreign_system_marker", "global"}
        if other_account
        else {"own", "global"}
    )
    selected = [row for row in data["roles"] if row["name"].startswith("example_")]
    assert {row["id"] for row in selected} == {str(roles[key].id) for key in expected}
    for row in selected:
        key = row["name"].removeprefix("example_")
        assert row["description"] == f"{key} role details"
        assert row["permissions"] == [
            {
                "id": str(roles[key].role_permissions[0].permission.id),
                "name": f"example_{key}",
                "description": f"{key} permission details",
            }
        ]
    assert data["total"] == len(data["roles"])
    for key in set(roles) - expected:
        assert f"{key} role details" not in response.text
        assert f"{key} permission details" not in response.text


def test_oss_roles_route_lists_only_basic_system_roles(
    client: TestClient, scoped_roles: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(multiuser, "advanced_roles_enabled", lambda: False)
    response = client.get("/api/v1/roles")
    assert response.status_code == 200
    data = response.json()
    assert not [row for row in data["roles"] if row["name"].startswith("example_")]
    assert {row["name"] for row in data["roles"]} <= {"owner", "admin", "member"}
    assert data["total"] == len(data["roles"])
    for label in scoped_roles["roles"]:
        assert f"{label} role details" not in response.text


def test_roles_route_requires_authentication(client: TestClient, app: FastAPI) -> None:
    app.dependency_overrides.pop(get_current_active_user)
    assert client.get("/api/v1/roles").status_code == 401


def test_roles_route_honors_permission_denial(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny(ctx: Any, action: str, resource: Any) -> account_hooks.Decision:
        assert action == "view_users"
        return account_hooks.Decision("deny", reason="Permission denied")

    monkeypatch.setattr(account_hooks, "_authorizer", deny)
    assert client.get("/api/v1/roles").status_code == 403
