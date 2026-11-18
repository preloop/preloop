"""An account never loses its last owner through a role or membership change.

All identities are synthetic. Concurrency uses two real PostgreSQL sessions.
"""

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_role, crud_user, crud_user_role
from preloop.models.crud.permission import LAST_OWNER_MESSAGE, LastOwnerError
from preloop.services.membership_lifecycle import offboard_membership
from tests.test_passkey_only import _user

from tests import test_passkey_only as passkey_support

api = passkey_support.api


def _role(db: Session, name: str) -> models.Role:
    role = crud_role.get_by_name(db, name=name)
    if role is None:
        role = models.Role(
            id=uuid.uuid4(),
            name=name,
            description=name,
            is_system_role=True,
            account_id=None,
        )
        db.add(role)
        db.flush()
    return role


def _member(db: Session, account_id: uuid.UUID) -> models.User:
    unique = uuid.uuid4().hex[:8]
    return crud_user.create(
        db,
        obj_in={
            "account_id": account_id,
            "username": f"m{unique}",
            "email": f"m{unique}@example.com",
            "full_name": "Jane Doe",
            "is_active": True,
            "email_verified": True,
            "user_source": "local",
        },
    )


def _grant(db: Session, user: models.User, role: models.Role) -> None:
    db.add(models.UserRole(id=uuid.uuid4(), user_id=user.id, role_id=role.id))
    db.flush()


def test_single_owner_cannot_lose_owner_role(db_session: Session) -> None:
    """protects=last owner; fails_when=role removal leaves no owner."""
    owner = _user(db_session)
    role = _role(db_session, "owner")
    _grant(db_session, owner, role)
    with pytest.raises(LastOwnerError) as refused:
        crud_user_role.remove_role(db_session, user_id=owner.id, role_id=role.id)
    assert str(refused.value) == LAST_OWNER_MESSAGE
    assert crud_user_role.get_by_user(db_session, user_id=owner.id)


def test_one_of_two_owners_can_go_and_non_owner_roles_are_unaffected(
    db_session: Session,
) -> None:
    """protects=ordinary role changes; fails_when=guard blocks a valid removal."""
    first = _user(db_session)
    second = _member(db_session, first.account_id)
    owner, admin = _role(db_session, "owner"), _role(db_session, "admin")
    for user in (first, second):
        _grant(db_session, user, owner)
    _grant(db_session, first, admin)
    assert crud_user_role.remove_role(db_session, user_id=first.id, role_id=admin.id)
    assert crud_user_role.remove_role(db_session, user_id=first.id, role_id=owner.id)
    with pytest.raises(LastOwnerError):
        crud_user_role.remove_role(db_session, user_id=second.id, role_id=owner.id)


def test_inactive_owner_does_not_count(db_session: Session) -> None:
    """protects=an inactive owner is not a remaining owner."""
    first = _user(db_session)
    second = _member(db_session, first.account_id)
    owner = _role(db_session, "owner")
    for user in (first, second):
        _grant(db_session, user, owner)
    second.is_active = False
    db_session.flush()
    with pytest.raises(LastOwnerError):
        crud_user_role.remove_role(db_session, user_id=first.id, role_id=owner.id)


def test_last_owner_membership_cannot_be_deactivated(db_session: Session) -> None:
    """protects=deactivate/delete/SCIM paths through offboard_membership."""
    owner = _user(db_session)
    _grant(db_session, owner, _role(db_session, "owner"))
    with pytest.raises(LastOwnerError):
        offboard_membership(
            db_session,
            account_id=owner.account_id,
            user_id=owner.id,
            reason="test removal",
        )
    db_session.refresh(owner)
    assert owner.is_active


def test_last_owner_error_is_a_409_with_stable_code(api: TestClient) -> None:
    """protects=every endpoint maps the guard to 409 last_owner."""

    def boom() -> None:
        raise LastOwnerError()

    api.app.add_api_route("/__last_owner_probe", boom)  # type: ignore[attr-defined]
    response = api.get("/__last_owner_probe")
    assert response.status_code == 409
    assert response.json() == {
        "detail": {"code": "last_owner", "message": LAST_OWNER_MESSAGE}
    }


def test_concurrent_removal_of_two_owners_leaves_one(db_engine: Engine) -> None:
    """protects=row lock; fails_when=two removals both pass and no owner remains."""
    with Session(db_engine) as db:
        first = _user(db)
        second = _member(db, first.account_id)
        owner = _role(db, "owner")
        for user in (first, second):
            _grant(db, user, owner)
        db.commit()
        ids, role_id, account_id = [first.id, second.id], owner.id, first.account_id
    start = Barrier(2)

    def remove(user_id: uuid.UUID) -> bool:
        with Session(db_engine) as db:
            start.wait(timeout=10)
            try:
                crud_user_role.remove_role(db, user_id=user_id, role_id=role_id)
                return True
            except LastOwnerError:
                db.rollback()
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(remove, ids))
    assert sorted(results) == [False, True]
    with Session(db_engine) as db:
        remaining = [
            row
            for user_id in ids
            for row in crud_user_role.get_by_user(db, user_id=user_id)
            if row.role_id == role_id
        ]
        assert len(remaining) == 1
        assert crud_user.get(db, id=ids[0]).account_id == account_id
