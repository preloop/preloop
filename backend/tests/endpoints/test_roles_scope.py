"""Role visibility through the mounted route and real tenant-aware CRUD."""

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models import crud, models


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


@pytest.mark.parametrize("limit", [100, None])
def test_account_roles_crud_rejects_foreign_system_marker(
    db_session: Session,
    test_user: models.User,
    scoped_roles: dict[str, Any],
    limit: int | None,
) -> None:
    rows = crud.crud_role.get_all_for_account(
        db_session, account_id=test_user.account_id, limit=limit
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
