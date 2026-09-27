"""Both permission checks resolve roles the same way (issue #983).

``has_permission`` (the OSS fallback in ``preloop.api.auth.permissions``) and
``user_holds_permission`` (``preloop.utils.permissions``) must agree on which
roles a user holds in their account, including roles granted through a team.
"""

import uuid

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from preloop.api.auth.permissions import has_permission
from preloop.models.crud import (
    crud_account,
    crud_permission,
    crud_role,
    crud_team,
    crud_team_role,
    crud_user,
    crud_user_role,
)
from preloop.models.models.user import User
from preloop.utils.permissions import user_holds_permission

PERMISSION = "manage_kill_switch"


def _both(db: Session, user: User, permission: str = PERMISSION) -> tuple:
    return (
        has_permission(user, permission, db),
        user_holds_permission(db, user, permission),
    )


def _make_account(db: Session):
    return crud_account.create(
        db, obj_in={"organization_name": "Perm Org", "is_active": True}
    )


def _make_user(db: Session, account_id, *, is_active: bool = True) -> User:
    suffix = uuid.uuid4().hex[:10]
    user = crud_user.create(
        db,
        obj_in={
            "account_id": account_id,
            "email": f"perm{suffix}@example.com",
            "username": f"perm{suffix}",
            "full_name": "Perm User",
            "is_active": is_active,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )
    db.flush()
    return user


def _make_role(db: Session, account_id, *, name: str, permission: str | None):
    role = crud_role.create(
        db,
        obj_in={"name": name, "account_id": account_id, "is_system_role": False},
    )
    if permission is not None:
        perm = crud_permission.get_by_name(db, name=permission)
        assert perm is not None, f"seeded permission {permission} missing"
        crud_role.assign_permission(db, role_id=role.id, permission_id=perm.id)
    return role


def _make_team_with_role(db: Session, account_id, role, *, name: str = "responders"):
    team = crud_team.create(db, obj_in={"name": name, "account_id": account_id})
    crud_team_role.assign_role(db, team_id=team.id, role_id=role.id)
    return team


@pytest.fixture
def roleless_user(db_session: Session) -> User:
    """A user in a fresh account with no roles and no teams."""
    account = _make_account(db_session)
    return _make_user(db_session, account.id)


def test_user_without_roles_holds_nothing(db_session, roleless_user):
    assert _both(db_session, roleless_user) == (False, False)


def test_team_role_is_the_only_path_both_checks_grant(db_session, roleless_user):
    role = _make_role(
        db_session, roleless_user.account_id, name="responder", permission=PERMISSION
    )
    team = _make_team_with_role(db_session, roleless_user.account_id, role)
    crud_team.add_member(db_session, team_id=team.id, user_id=roleless_user.id)

    assert crud_user_role.get_by_user(db_session, user_id=roleless_user.id) == []
    assert _both(db_session, roleless_user) == (True, True)
    # A permission the team role does not carry is still denied.
    assert _both(db_session, roleless_user, "manage_billing") == (False, False)


def test_removing_team_membership_revokes_on_next_check(db_session, roleless_user):
    role = _make_role(
        db_session, roleless_user.account_id, name="responder", permission=PERMISSION
    )
    team = _make_team_with_role(db_session, roleless_user.account_id, role)
    crud_team.add_member(db_session, team_id=team.id, user_id=roleless_user.id)
    assert _both(db_session, roleless_user) == (True, True)

    assert crud_team.remove_member(
        db_session, team_id=team.id, user_id=roleless_user.id
    )
    assert _both(db_session, roleless_user) == (False, False)


def test_direct_custom_role_grants_in_both_checks(db_session, roleless_user):
    role = _make_role(
        db_session, roleless_user.account_id, name="direct", permission=PERMISSION
    )
    crud_user_role.assign_role(db_session, user_id=roleless_user.id, role_id=role.id)

    assert _both(db_session, roleless_user) == (True, True)
    assert _both(db_session, roleless_user, "manage_billing") == (False, False)


def test_system_owner_role_grants_everything_through_a_team(db_session, roleless_user):
    owner = crud_role.get_by_name(db_session, name="owner")
    assert owner is not None and owner.is_system_role
    _team = _make_team_with_role(db_session, roleless_user.account_id, owner)
    crud_team.add_member(db_session, team_id=_team.id, user_id=roleless_user.id)

    assert _both(db_session, roleless_user, "manage_billing") == (True, True)


def test_custom_role_named_owner_is_not_all_powerful(db_session, roleless_user):
    fake_owner = _make_role(
        db_session, roleless_user.account_id, name="owner", permission=None
    )
    crud_user_role.assign_role(
        db_session, user_id=roleless_user.id, role_id=fake_owner.id
    )

    assert _both(db_session, roleless_user, "manage_billing") == (False, False)


def test_team_in_another_account_grants_nothing(db_session, roleless_user):
    other = _make_account(db_session)
    role = _make_role(db_session, other.id, name="responder", permission=PERMISSION)
    team = _make_team_with_role(db_session, other.id, role)
    crud_team.add_member(db_session, team_id=team.id, user_id=roleless_user.id)

    assert _both(db_session, roleless_user) == (False, False)


def test_inactive_user_fails_has_permission(db_session):
    account = _make_account(db_session)
    user = _make_user(db_session, account.id, is_active=False)
    role = _make_role(db_session, account.id, name="responder", permission=PERMISSION)
    team = _make_team_with_role(db_session, account.id, role)
    crud_team.add_member(db_session, team_id=team.id, user_id=user.id)

    assert has_permission(user, PERMISSION, db_session) is False


def test_has_permission_resolves_in_one_query(db_session, roleless_user):
    """The fallback runs per decorated request, so it stays a single query."""
    role = _make_role(
        db_session, roleless_user.account_id, name="responder", permission=PERMISSION
    )
    for index in range(3):
        team = _make_team_with_role(
            db_session, roleless_user.account_id, role, name=f"team{index}"
        )
        crud_team.add_member(db_session, team_id=team.id, user_id=roleless_user.id)
    db_session.flush()
    # Load attributes up front so lazy refreshes are not counted.
    _ = (roleless_user.id, roleless_user.account_id, roleless_user.is_active)

    statements: list[str] = []
    bind = db_session.get_bind()

    def _count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(bind, "before_cursor_execute", _count)
    try:
        granted = has_permission(roleless_user, "manage_billing", db_session)
    finally:
        event.remove(bind, "before_cursor_execute", _count)

    assert granted is False
    assert len(statements) == 1, statements
