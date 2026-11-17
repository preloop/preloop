"""Single-endpoint read route for outbound event webhooks."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.models.models.webhook_endpoint import SOURCE_ACCOUNT, WebhookEndpoint
from preloop.plugins import account_hooks
from preloop.plugins.account_hooks import ALLOW, AuthorizationContext, Decision
from preloop.services.event_webhooks.signing import generate_secret
from preloop.utils.encryption import encrypt_value

BASE = "/api/v1/event-webhooks/endpoints"


def _endpoint(db: Session, account_id: object, secret: str) -> WebhookEndpoint:
    """Persist an account webhook endpoint with an encrypted secret.

    Args:
        db: Database session.
        account_id: Owning account id.
        secret: Plaintext signing secret.

    Returns:
        The flushed endpoint row.
    """
    row = WebhookEndpoint(
        account_id=account_id,
        url="https://hooks.example.com/receiver",
        description="Example receiver",
        secret_encrypted=encrypt_value(secret),
        secret_hint=secret[-6:],
        event_types=["approval.decided"],
        source=SOURCE_ACCOUNT,
    )
    db.add(row)
    db.flush()
    return row


def test_get_returns_the_safe_projection(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """The owner reads one endpoint by id; no secret material is returned."""
    secret = generate_secret()
    row = _endpoint(db_session, test_user.account_id, secret)

    response = client.get(f"{BASE}/{row.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(row.id)
    assert body["url"] == "https://hooks.example.com/receiver"
    assert body["description"] == "Example receiver"
    assert body["event_types"] == ["approval.decided"]
    assert body["secret_hint"] == secret[-6:]
    assert "secret" not in body
    assert secret not in response.text
    assert row.secret_encrypted not in response.text


def test_get_unknown_and_cross_account_are_404(
    client: TestClient, db_session: Session
) -> None:
    """Another account's endpoint is indistinguishable from an unknown id."""
    other = crud.crud_account.create(
        db_session, obj_in={"organization_name": "Other", "is_active": True}
    )
    foreign = _endpoint(db_session, other.id, generate_secret())

    assert client.get(f"{BASE}/{foreign.id}").status_code == 404
    assert client.get(f"{BASE}/{uuid4()}").status_code == 404


def test_get_requires_view_permission(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An actor denied view_policies is refused before any read."""
    row = _endpoint(db_session, test_user.account_id, generate_secret())
    actions: list[str] = []

    def deny(_ctx: AuthorizationContext, action: str, _resource: object) -> Decision:
        """Deny the endpoint-level view permission."""
        actions.append(action)
        return Decision("deny") if action == "view_policies" else ALLOW

    monkeypatch.setattr(account_hooks, "_authorizer", deny)

    response = client.get(f"{BASE}/{row.id}")

    assert response.status_code == 403, response.text
    assert "view_policies" in response.json()["detail"]
    assert "view_policies" in actions
    assert str(row.id) not in response.text
