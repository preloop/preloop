"""Team projections use a bounded number of reads as result pages grow."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from preloop.api.endpoints import teams
from preloop.models import models
from preloop.models.crud import crud_role, crud_team, crud_team_role, crud_user


@contextmanager
def statements(db: Session) -> Iterator[list[str]]:
    captured: list[str] = []

    def record(conn: Any, cursor: Any, sql: str, *args: Any) -> None:
        if sql.lstrip().upper().startswith("SELECT"):
            captured.append(sql)

    bind = db.get_bind()
    event.listen(bind, "before_cursor_execute", record)
    try:
        yield captured
    finally:
        event.remove(bind, "before_cursor_execute", record)


@pytest.mark.parametrize("size", [1, 6])
def test_team_member_projection_query_budget(
    db_session: Session, test_user: models.User, size: int
) -> None:
    team = crud_team.create(
        db_session, obj_in={"account_id": test_user.account_id, "name": "Query team"}
    )
    team_id = team.id
    for i in range(size):
        member = crud_user.create(
            db_session,
            obj_in={
                "account_id": test_user.account_id,
                "username": f"query_member_{i}",
                "email": f"query_member_{i}@example.com",
            },
        )
        crud_team.add_member(
            db_session, team_id=team_id, user_id=member.id, role="member"
        )
    members = crud_team.get_members(db_session, team_id=team_id)
    with statements(db_session) as captured:
        result = teams._team_member_responses(db_session, team_id, members)
    assert len(result) == size
    assert {row.username for row in result} == {
        f"query_member_{i}" for i in range(size)
    }
    assert all(row.role == "member" for row in result)
    # Three canonical identity/role-context reads plus one membership batch.
    assert len(captured) <= 4, captured


@pytest.mark.parametrize("size", [1, 6])
def test_team_role_projection_query_budget(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    size: int,
) -> None:
    role = crud_role.get_by_name(db_session, name="admin")
    assert role is not None
    role_id = role.id
    for i in range(size):
        team = crud_team.create(
            db_session,
            obj_in={"account_id": test_user.account_id, "name": f"Roles team {i}"},
        )
        crud_team_role.assign_role(db_session, team_id=team.id, role_id=role_id)
    monkeypatch.setattr(teams.multiuser, "access_control", lambda: object())
    # Load caller identity outside the measured page projection.
    assert test_user.account_id is not None
    with statements(db_session) as captured:
        result = teams.list_teams(current_user=test_user, db=db_session)
    assert result["total"] == size
    assert all(row["roles"][0]["id"] == str(role_id) for row in result["teams"])
    # One team page, one count, one role batch, independent of page size.
    assert len(captured) <= 3, captured


def test_batched_team_roles_exclude_other_accounts(
    db_session: Session, test_user: models.User
) -> None:
    """Even explicitly supplied foreign team IDs stay outside the batch result."""
    from preloop.models.crud import crud_account

    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other example"}
    )
    team = crud_team.create(
        db_session, obj_in={"account_id": other.id, "name": "Foreign team"}
    )
    role = crud_role.get_by_name(db_session, name="admin")
    assert role is not None
    crud_team_role.assign_role(db_session, team_id=team.id, role_id=role.id)
    assert (
        crud_team_role.get_roles_for_teams(
            db_session, account_id=test_user.account_id, team_ids=[team.id]
        )
        == {}
    )


def test_batched_team_roles_exclude_invalid_legacy_grants(
    db_session: Session, test_user: models.User
) -> None:
    """A stored cross-account or unowned grant cannot leak through the batch."""
    from preloop.models.crud import crud_account

    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other role owner"}
    )
    team = crud_team.create(
        db_session, obj_in={"account_id": test_user.account_id, "name": "Legacy grants"}
    )
    team_id = team.id
    for role_account in [other.id, None]:
        role = crud_role.create(
            db_session,
            obj_in={
                "account_id": role_account,
                "name": f"Legacy {role_account}",
                "is_system_role": False,
            },
        )
        crud_team_role.assign_role(db_session, team_id=team_id, role_id=role.id)
    assert (
        crud_team_role.get_roles_for_teams(
            db_session, account_id=test_user.account_id, team_ids=[team_id]
        )
        == {}
    )
