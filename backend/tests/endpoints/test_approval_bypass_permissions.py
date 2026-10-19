"""Approval-bypass endpoints apply their permissions on every build."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pytest_mock import MockerFixture

from preloop.api.endpoints import approval_bypass
from preloop.models.models.approval_bypass import ApprovalBypassMode
from preloop.schemas.approval_bypass import ApprovalBypassCreate

MODULE = "preloop.api.endpoints.approval_bypass"


def make_user() -> SimpleNamespace:
    """Build a minimal authenticated user."""
    return SimpleNamespace(id=uuid4(), account_id=uuid4())


def payload() -> ApprovalBypassCreate:
    """Build a short auto-approve bypass request."""
    return ApprovalBypassCreate(
        mode=ApprovalBypassMode.AUTO_APPROVE, duration_minutes=15
    )


def deny(*_: Any) -> None:
    """Stand in for a user without the permission."""
    raise HTTPException(status_code=403, detail="Permission denied")


def closure_strings(func: Any, seen: set[int] | None = None) -> set[str]:
    """Collect string constants captured by a decorated handler's closures."""
    seen = seen if seen is not None else set()
    if id(func) in seen:
        return set()
    seen.add(id(func))
    found: set[str] = set()
    for cell in getattr(func, "__closure__", None) or ():
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if isinstance(value, str):
            found.add(value)
        elif callable(value):
            found |= closure_strings(value, seen)
    return found


def test_create_requires_decide_approvals_and_writes_nothing_when_denied(
    mocker: MockerFixture,
) -> None:
    """A user without decide_approvals cannot open a bypass."""
    check = mocker.patch(f"{MODULE}.ensure_permission_in_oss", side_effect=deny)
    db, user = MagicMock(), make_user()
    with pytest.raises(HTTPException) as exc:
        approval_bypass.create_bypass(payload=payload(), current_user=user, db=db)
    assert exc.value.status_code == 403
    check.assert_called_once_with(db, user, "decide_approvals")
    db.add.assert_not_called()
    db.commit.assert_not_called()


def test_create_succeeds_when_permission_held(mocker: MockerFixture) -> None:
    """A user holding decide_approvals opens a bypass for themselves."""
    mocker.patch(f"{MODULE}.ensure_permission_in_oss")
    db, user = MagicMock(), make_user()
    bypass = approval_bypass.create_bypass(payload=payload(), current_user=user, db=db)
    db.add.assert_called_once_with(bypass)
    db.commit.assert_called_once()
    assert bypass.user_id == user.id
    assert bypass.account_id == user.account_id


@pytest.mark.parametrize("handler", ["list_bypasses", "revoke_all_bypasses"])
def test_list_and_revoke_all_check_view_approvals(
    handler: str, mocker: MockerFixture
) -> None:
    """Listing and revoking everything check view_approvals."""
    check = mocker.patch(f"{MODULE}.ensure_permission_in_oss")
    mocker.patch(
        f"{MODULE}.crud_approval_bypass.list_active_for_account", return_value=[]
    )
    db, user = MagicMock(), make_user()
    assert getattr(approval_bypass, handler)(current_user=user, db=db) == []
    check.assert_called_once_with(db, user, "view_approvals")


def test_revoke_checks_view_approvals(mocker: MockerFixture) -> None:
    """Revoking one bypass checks view_approvals, not ownership."""
    check = mocker.patch(f"{MODULE}.ensure_permission_in_oss")
    revoked = SimpleNamespace(
        id=uuid4(), mode=ApprovalBypassMode.AUTO_APPROVE, auto_approved_count=0
    )
    mocker.patch(f"{MODULE}.crud_approval_bypass.get", return_value=revoked)
    mocker.patch(f"{MODULE}.crud_approval_bypass.revoke", return_value=revoked)
    db, user = MagicMock(), make_user()
    result = approval_bypass.revoke_bypass(
        bypass_id=revoked.id, current_user=user, db=db
    )
    assert result is revoked
    check.assert_called_once_with(db, user, "view_approvals")


def test_revoke_denied_before_any_crud_call(mocker: MockerFixture) -> None:
    """A denied revoke never reaches the CRUD layer."""
    mocker.patch(f"{MODULE}.ensure_permission_in_oss", side_effect=deny)
    get = mocker.patch(f"{MODULE}.crud_approval_bypass.get")
    revoke = mocker.patch(f"{MODULE}.crud_approval_bypass.revoke")
    with pytest.raises(HTTPException) as exc:
        approval_bypass.revoke_bypass(
            bypass_id=uuid4(), current_user=make_user(), db=MagicMock()
        )
    assert exc.value.status_code == 403
    get.assert_not_called()
    revoke.assert_not_called()


@pytest.mark.parametrize(
    "handler,permission",
    [
        ("create_bypass", "decide_approvals"),
        ("list_bypasses", "view_approvals"),
        ("revoke_bypass", "view_approvals"),
        ("revoke_all_bypasses", "view_approvals"),
    ],
)
def test_decorator_declares_permission(handler: str, permission: str) -> None:
    """The route decorator declares the same permission the handler enforces."""
    captured = closure_strings(getattr(approval_bypass, handler))
    assert permission in captured
    assert captured & {"decide_approvals", "view_approvals"} == {permission}


@pytest.mark.parametrize("handler", ["list_bypasses", "revoke_all_bypasses"])
def test_list_and_revoke_all_denied_before_any_crud_call(
    handler: str, mocker: MockerFixture
) -> None:
    """A denied list or revoke-all never reaches the CRUD layer or commits."""
    mocker.patch(f"{MODULE}.ensure_permission_in_oss", side_effect=deny)
    listed = mocker.patch(f"{MODULE}.crud_approval_bypass.list_active_for_account")
    revoke = mocker.patch(f"{MODULE}.crud_approval_bypass.revoke")
    db = MagicMock()
    with pytest.raises(HTTPException) as exc:
        getattr(approval_bypass, handler)(current_user=make_user(), db=db)
    assert exc.value.status_code == 403
    listed.assert_not_called()
    revoke.assert_not_called()
    db.commit.assert_not_called()
