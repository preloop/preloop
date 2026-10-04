"""Fresh local permission authority for restricted CI administrators."""

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.models.crud.base import CRUDBase
from tests.api.test_ci_principal import ci_resources as create_resources


@pytest.fixture
def authority(db_session: Session) -> tuple[Any, ...]:
    owner, project, flow, grant = create_resources.__wrapped__(db_session)
    permission = crud.crud_permission.create(
        db_session,
        obj_in={
            "name": f"synthetic_ci_{uuid4().hex}",
            "description": "Synthetic CI permission",
            "category": "ci",
            "is_active": True,
        },
    )
    return owner, permission


def role(
    db: Session,
    *,
    account_id: Any,
    permission: models.Permission,
    system: bool = False,
    name: str = "Synthetic administrator",
) -> models.Role:
    row = crud.crud_role.create(
        db,
        obj_in={
            "name": name,
            "account_id": account_id,
            "is_system_role": system,
        },
    )
    crud.crud_role.assign_permission(db, role_id=row.id, permission_id=permission.id)
    return row


def effective(db: Session, owner: models.User) -> set[str]:
    return crud.crud_role.get_effective_permissions(
        db, user_id=owner.id, account_id=owner.account_id
    )


def test_custom_role_listing_keeps_local_administration_roles(
    db_session: Session, authority: tuple[Any, ...]
) -> None:
    """Custom CI administration roles must remain discoverable and account-local."""
    owner, permission = authority
    local = role(db_session, account_id=owner.account_id, permission=permission)
    role(db_session, account_id=None, permission=permission, system=True)
    foreign = crud.crud_account.create(
        db_session, obj_in={"organization_name": "Synthetic foreign", "is_active": True}
    )
    role(db_session, account_id=foreign.id, permission=permission)
    assert [
        item.id
        for item in crud.crud_role.get_custom_roles(
            db_session, account_id=str(owner.account_id)
        )
    ] == [local.id]


def test_direct_permission_revocation_is_fresh(
    db_session: Session, authority: tuple[Any, ...]
) -> None:
    owner, permission = authority
    local = role(db_session, account_id=owner.account_id, permission=permission)
    crud.crud_user_role.assign_role(db_session, user_id=owner.id, role_id=local.id)
    assert effective(db_session, owner) == {permission.name}
    _ = local.role_permissions
    crud.crud_role.remove_permission(
        db_session, role_id=local.id, permission_id=permission.id
    )
    assert effective(db_session, owner) == set()


def test_foreign_roles_and_teams_do_not_grant(
    db_session: Session, authority: tuple[Any, ...]
) -> None:
    owner, permission = authority
    foreign = crud.crud_account.create(
        db_session, obj_in={"organization_name": "Synthetic foreign", "is_active": True}
    )
    foreign_role = role(db_session, account_id=foreign.id, permission=permission)
    crud.crud_user_role.assign_role(
        db_session, user_id=owner.id, role_id=foreign_role.id
    )
    team = crud.crud_team.create(
        db_session, obj_in={"name": "Synthetic foreign team", "account_id": foreign.id}
    )
    system = role(db_session, account_id=None, permission=permission, system=True)
    crud.crud_team.add_member(db_session, team_id=team.id, user_id=owner.id)
    crud.crud_team_role.assign_role(db_session, team_id=team.id, role_id=system.id)
    assert effective(db_session, owner) == set()


def test_local_team_permission_and_membership_revocation_are_fresh(
    db_session: Session, authority: tuple[Any, ...]
) -> None:
    owner, permission = authority
    team = crud.crud_team.create(
        db_session,
        obj_in={"name": "Synthetic local team", "account_id": owner.account_id},
    )
    system = role(db_session, account_id=None, permission=permission, system=True)
    crud.crud_team.add_member(db_session, team_id=team.id, user_id=owner.id)
    crud.crud_team_role.assign_role(db_session, team_id=team.id, role_id=system.id)
    assert effective(db_session, owner) == {permission.name}
    crud.crud_team.remove_member(db_session, team_id=team.id, user_id=owner.id)
    assert effective(db_session, owner) == set()


def test_custom_owner_name_and_forged_system_role_do_not_widen(
    db_session: Session, authority: tuple[Any, ...]
) -> None:
    owner, permission = authority
    custom = crud.crud_role.create(
        db_session,
        obj_in={
            "name": "owner",
            "account_id": owner.account_id,
            "is_system_role": False,
        },
    )
    crud.crud_user_role.assign_role(db_session, user_id=owner.id, role_id=custom.id)
    malformed = role(db_session, account_id=None, permission=permission, system=False)
    crud.crud_user_role.assign_role(db_session, user_id=owner.id, role_id=malformed.id)
    assert effective(db_session, owner) == set()


@pytest.mark.parametrize("disabled", ["permission", "user", "account"])
def test_inactive_authority_is_fresh(
    db_session: Session, authority: tuple[Any, ...], disabled: str
) -> None:
    owner, permission = authority
    local = role(db_session, account_id=owner.account_id, permission=permission)
    crud.crud_user_role.assign_role(db_session, user_id=owner.id, role_id=local.id)
    assert effective(db_session, owner) == {permission.name}
    if disabled == "permission":
        target = permission
    elif disabled == "user":
        target = owner
    else:
        target = crud.crud_account.get(db_session, id=owner.account_id)
    CRUDBase(type(target)).update(
        db_session, db_obj=target, obj_in={"is_active": False}
    )
    assert effective(db_session, owner) == set()
