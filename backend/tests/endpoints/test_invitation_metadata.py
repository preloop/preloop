"""Invitation metadata and delivery use separate response boundaries."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.endpoints import invitations
from preloop.models import models
from preloop.plugins import account_hooks


@pytest.fixture
def invitation_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    account_id = uuid4()
    user = SimpleNamespace(
        id=uuid4(), account_id=account_id, username="member", email="member@example.com"
    )
    row = models.UserInvitation(
        id=uuid4(),
        account_id=account_id,
        email="invited@example.com",
        token="synthetic-delivery-value",
        status="pending",
        invited_by=user.id,
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    db = MagicMock()
    crud = MagicMock()
    crud.get.return_value = row
    crud.get_by_account.return_value = [row]
    crud.count_by_account.return_value = 1
    crud.get_by_email.return_value = None
    crud.create.return_value = row
    monkeypatch.setattr(invitations, "crud_user_invitation", crud)
    monkeypatch.setattr(invitations, "email_exists", lambda **kwargs: False)
    monkeypatch.setattr(invitations, "enforce_seat_available", MagicMock())
    monkeypatch.setattr(invitations, "validate_invitation_roles", lambda *args: [])
    monkeypatch.setattr(
        invitations.crud_account,
        "get",
        lambda *args, **kwargs: SimpleNamespace(organization_name="Example"),
    )
    send_email = MagicMock()
    monkeypatch.setattr(invitations, "send_invitation_email", send_email)
    allowed = {"view_users"}
    actions = []

    def authorize(
        ctx: Any, action: str, resource: Any = None
    ) -> account_hooks.Decision:
        actions.append(action)
        return account_hooks.Decision("allow" if action in allowed else "deny")

    previous = account_hooks.get_authorizer()
    account_hooks.register_authorizer(authorize)
    app = FastAPI()
    app.include_router(invitations.router)
    app.dependency_overrides[invitations.get_current_active_user] = lambda: user
    app.dependency_overrides[invitations.get_db_session] = lambda: db
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(
                client=client,
                row=row,
                crud=crud,
                allowed=allowed,
                actions=actions,
                send_email=send_email,
            )
    finally:
        account_hooks.register_authorizer(previous)


@pytest.mark.parametrize("detail", [False, True])
def test_directory_reader_receives_metadata(invitation_client, detail: bool) -> None:
    case = invitation_client
    path = f"/invitations/{case.row.id}" if detail else "/invitations"
    response = case.client.get(path)
    assert response.status_code == 200
    data = response.json() if detail else response.json()["invitations"][0]
    assert data["email"] == case.row.email
    assert data["status"] == "pending"
    assert "token" not in data
    assert case.row.token not in response.text
    assert case.actions == ["view_users"]


@pytest.mark.parametrize("detail", [False, True])
def test_directory_read_requires_permission(invitation_client, detail: bool) -> None:
    case = invitation_client
    case.allowed.clear()
    path = f"/invitations/{case.row.id}" if detail else "/invitations"
    assert case.client.get(path).status_code == 403
    case.crud.get.assert_not_called()
    case.crud.get_by_account.assert_not_called()


def test_detail_retains_account_boundary(invitation_client) -> None:
    case = invitation_client
    case.row.account_id = uuid4()
    assert case.client.get(f"/invitations/{case.row.id}").status_code == 403


@pytest.mark.parametrize("resend", [False, True])
def test_delivery_retains_redemption_value(invitation_client, resend: bool) -> None:
    case = invitation_client
    case.allowed.add("invite_users")
    if resend:
        response = case.client.post(f"/invitations/{case.row.id}/resend")
        assert response.status_code == 204
    else:
        response = case.client.post("/invitations", json={"email": case.row.email})
        assert response.status_code == 201
        assert "token" not in response.json()
    assert case.send_email.call_args.kwargs["token"] == case.row.token
    assert case.actions == ["invite_users"]


def test_response_schema_documents_metadata(invitation_client) -> None:
    schema = invitation_client.client.get("/openapi.json").json()
    assert (
        "token"
        not in schema["components"]["schemas"]["InvitationResponse"]["properties"]
    )
