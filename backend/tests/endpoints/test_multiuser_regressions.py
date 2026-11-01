"""Regression coverage for removals, destructive batches and tenant metadata."""

import logging
from typing import Any
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

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


@pytest.fixture(autouse=True)
def enabled_permissions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every regression request with permission checks enabled."""
    from preloop.config import settings
    from preloop.utils import permissions

    monkeypatch.setenv("DISABLE_RBAC", "false")
    monkeypatch.setattr(settings, "disable_rbac", False)
    monkeypatch.setattr(permissions, "_plugin_require_permission", None)


def colleague(
    db: Session, owner: models.User, name: str, role_name: str | None = None
) -> models.User:
    """Create an account colleague with an optional genuine system role."""
    user = crud_user.create(
        db,
        obj_in={
            "account_id": owner.account_id,
            "username": name,
            "email": f"{name}@example.com",
            "is_active": True,
        },
    )
    if role_name:
        role = crud_role.get_by_name(db, name=role_name)
        assert role is not None
        crud_user_role.assign_role(db, user_id=user.id, role_id=role.id)
    return user


def other_account(db: Session) -> tuple[models.Account, models.User]:
    """Create a separate tenant and its only user."""
    account = crud_account.create(db, obj_in={"organization_name": "Other account"})
    user = crud_user.create(
        db,
        obj_in={
            "account_id": account.id,
            "username": "other_primary",
            "email": "other_primary@example.com",
            "is_active": True,
        },
    )
    return account, user


def remove_request(client: TestClient, target: models.User, action: str) -> Any:
    """Exercise each route that can remove or deactivate a membership."""
    if action == "patch":
        return client.patch(f"/api/v1/users/{target.id}", json={"is_active": False})
    if action == "deactivate":
        return client.post(f"/api/v1/users/{target.id}/deactivate")
    return client.post(
        "/api/v1/users/bulk-delete",
        json={"user_ids": [str(target.id)], "hard_delete": action == "hard"},
    )


@pytest.mark.parametrize("kind", ["primary", "owner"])
@pytest.mark.parametrize("action", ["patch", "deactivate", "soft", "hard"])
def test_any_member_can_remove_owners_in_core(
    client: TestClient,
    app: Any,
    db_session: Session,
    test_user: models.User,
    kind: str,
    action: str,
) -> None:
    """Core has no owner protection; only the account's own integrity rules."""
    target = colleague(
        db_session, test_user, "protected", "owner" if kind == "owner" else None
    )
    if kind == "primary":
        account = crud_account.get(db_session, id=test_user.account_id)
        crud_account.update(
            db_session, db_obj=account, obj_in={"primary_user_id": target.id}
        )
    peer = colleague(db_session, test_user, "peer")
    app.dependency_overrides[get_current_active_user] = lambda: peer
    response = remove_request(client, target, action)
    assert response.status_code == (204 if action == "deactivate" else 200), (
        response.text
    )
    retained = crud_user.get(db_session, id=target.id)
    if kind == "primary" and action == "hard":
        # An account cannot lose its primary user while others remain.
        assert response.json()["results"][0]["status"] == "skipped"
        assert retained is not None and retained.is_active
    elif action == "hard":
        assert retained is None
    else:
        assert retained is not None and not retained.is_active


@pytest.mark.parametrize("hard", [False, True])
def test_bulk_delete_scopes_skips_and_deduplicates(
    client: TestClient, db_session: Session, test_user: models.User, hard: bool
) -> None:
    """A mixed batch changes each local target once and preserves other tenants."""
    target = colleague(db_session, test_user, "departing_member")
    _, foreign = other_account(db_session)
    missing = uuid4()
    response = client.post(
        "/api/v1/users/bulk-delete",
        json={
            "user_ids": [
                str(test_user.id),
                str(target.id),
                str(target.id),
                str(foreign.id),
                str(missing),
            ],
            "hard_delete": hard,
        },
    )
    assert response.status_code == 200, response.text
    rows = response.json()["results"]
    assert len(rows) == 4
    results = {row["user_id"]: row for row in rows}
    assert results[str(target.id)]["status"] == ("deleted" if hard else "deactivated")
    for skipped in (test_user.id, foreign.id, missing):
        assert results[str(skipped)]["status"] == "skipped"
    retained = crud_user.get(db_session, id=target.id)
    assert retained is None if hard else retained is not None and not retained.is_active
    assert crud_user.get(db_session, id=test_user.id).is_active
    assert crud_user.get(db_session, id=foreign.id).is_active


def test_bulk_hard_delete_skips_primary_in_multiuser_account(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """Hard deletion requires explicit ownership transfer for a shared account."""
    target = colleague(db_session, test_user, "primary_member")
    account = crud_account.get(db_session, id=test_user.account_id)
    crud_account.update(
        db_session, db_obj=account, obj_in={"primary_user_id": target.id}
    )
    response = remove_request(client, target, "hard")
    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["status"] == "skipped"
    assert "transfer ownership" in result["reason"]
    assert crud_user.get(db_session, id=target.id) is not None


@pytest.mark.parametrize("with_billing", [False, True])
def test_superuser_can_purge_a_solo_account(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    with_billing: bool,
) -> None:
    """Solo-account purges use optional billing cleanup or the real core purge."""
    from preloop.api.endpoints import users

    crud_user.update(db_session, db_obj=test_user, obj_in={"is_superuser": True})
    account, target = other_account(db_session)
    crud_account.update(
        db_session, db_obj=account, obj_in={"primary_user_id": target.id}
    )
    account_id = str(account.id)
    billing = Mock()
    billing.delete_account.side_effect = lambda **kwargs: crud_account.purge(
        db_session, account_id=kwargs["account_id"]
    )
    if with_billing:
        monkeypatch.setattr(users, "_get_billing_service", lambda db: billing)
    else:
        assert users._get_billing_service(db_session) is None
    response = remove_request(client, target, "hard")
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["status"] == "account_purged", response.text
    assert crud_account.get(db_session, id=account_id) is None
    assert crud_user.get(db_session, id=target.id) is None
    assert crud_user.get(db_session, id=test_user.id).is_active
    if with_billing:
        billing.delete_account.assert_called_once_with(
            account_id=account_id,
            cancel_stripe=True,
            hard_delete=True,
            deleted_by=test_user.email,
        )
        billing.update_subscription_quantity.assert_not_called()


@pytest.mark.parametrize("hard", [False, True])
def test_bulk_delete_rolls_back_one_error_and_continues(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    hard: bool,
) -> None:
    """A failed target is reported while a later target can still be changed."""
    bad = colleague(db_session, test_user, "failed_member")
    good = colleague(db_session, test_user, "valid_member")
    # Soft removal goes through the offboarding service, hard removal
    # through crud_user.hard_delete.
    from preloop.api.endpoints import users as users_endpoint

    target = crud_user if hard else users_endpoint
    method = "hard_delete" if hard else "offboard_membership"
    original = getattr(target, method)
    rollback = Mock(wraps=db_session.rollback)
    monkeypatch.setattr(db_session, "rollback", rollback)

    def fail_one(db: Session, *, user_id: Any, **kwargs: Any) -> Any:
        if user_id == bad.id:
            raise RuntimeError("Synthetic deletion failure")
        return original(db, user_id=user_id, **kwargs)

    monkeypatch.setattr(target, method, fail_one)
    response = client.post(
        "/api/v1/users/bulk-delete",
        json={"user_ids": [str(bad.id), str(good.id)], "hard_delete": hard},
    )
    assert response.status_code == 200, response.text
    assert [row["status"] for row in response.json()["results"]] == [
        "error",
        "deleted" if hard else "deactivated",
    ]
    assert "Synthetic deletion failure" in response.json()["results"][0]["reason"]
    rollback.assert_called_once()
    assert crud_user.get(db_session, id=bad.id).is_active


def test_public_invitation_details_hide_foreign_metadata(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """A legacy invitation token cannot reveal another tenant's role or team."""
    foreign, _ = other_account(db_session)
    own_team = crud_team.create(
        db_session, obj_in={"account_id": test_user.account_id, "name": "Visible team"}
    )
    foreign_team = crud_team.create(
        db_session, obj_in={"account_id": foreign.id, "name": "Hidden team"}
    )
    foreign_role = crud_role.create(
        db_session, obj_in={"account_id": foreign.id, "name": "Hidden role"}
    )
    admin = crud_role.get_by_name(db_session, name="admin")
    invitation = crud_user_invitation.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "email": "invited@example.com",
            "invited_by": test_user.id,
            "role_ids": f"{admin.id},{foreign_role.id}",
            "team_ids": f"{own_team.id},{foreign_team.id}",
        },
    )
    response = client.get(f"/api/v1/invitations/public/{invitation.token}")
    assert response.status_code == 200, response.text
    assert response.json()["role_names"] == []
    assert response.json()["team_names"] == ["Visible team"]


def test_duplicate_team_member_returns_conflict(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """A retried team assignment has a defined result and cannot change its role."""
    target = colleague(db_session, test_user, "team_member")
    team = crud_team.create(
        db_session, obj_in={"account_id": test_user.account_id, "name": "Retry team"}
    )
    path = f"/api/v1/teams/{team.id}/members"
    assert (
        client.post(
            path, json={"user_id": str(target.id), "role": "member"}
        ).status_code
        == 201
    )
    duplicate = client.post(path, json={"user_id": str(target.id), "role": "admin"})
    assert duplicate.status_code == 409, duplicate.text
    assert (
        crud_team.get_membership(db_session, team_id=team.id, user_id=target.id).role
        == "member"
    )


def test_team_member_removal_is_logged(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Successful removals record the same operational log as other mutations."""
    target = colleague(db_session, test_user, "removed_member")
    team = crud_team.create(
        db_session, obj_in={"account_id": test_user.account_id, "name": "Removal team"}
    )
    crud_team.add_member(db_session, team_id=team.id, user_id=target.id)
    with caplog.at_level(logging.INFO, logger="preloop.api.endpoints.teams"):
        response = client.delete(f"/api/v1/teams/{team.id}/members/{target.id}")
    assert response.status_code == 204, response.text
    assert f"User {target.id} removed from team {team.id}" in caplog.text


def test_owner_cannot_deactivate_self_through_admin_update(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """The alternate update route preserves the explicit self-removal guard."""
    response = client.patch(f"/api/v1/users/{test_user.id}", json={"is_active": False})
    assert response.status_code == 400, response.text
    assert crud_user.get(db_session, id=test_user.id).is_active


def test_invitation_passes_email_to_optional_seat_policy(
    client: TestClient, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An overlay can recognize an existing colleague before reserving a seat."""
    from preloop.plugins.base import get_plugin_manager

    claims: list[str | None] = []

    def gate(db: Session, account_id: UUID, **context: Any) -> None:
        assert account_id == test_user.account_id
        claims.append(context.get("invitation_email"))

    monkeypatch.setitem(get_plugin_manager()._services, "multiuser_seat_gate", gate)
    with patch("preloop.api.endpoints.invitations.send_invitation_email"):
        response = client.post(
            "/api/v1/invitations", json={"email": "colleague@example.com"}
        )
    assert response.status_code == 201, response.text
    assert claims == ["colleague@example.com"]
