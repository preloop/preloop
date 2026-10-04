"""Webhook signing secret: shown once, hidden on reads, rotatable.

Also covers the decision channel recorded on the timeline: a decision is
labelled with the surface it really came through, not always ``console``.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from preloop.api.endpoints import approval_requests
from preloop.models.models import ApprovalRequest
from preloop.models.models.tool_configuration import ToolConfiguration

HOOK = "https://receiver.example.com/approvals"
BASE = "/api/v1/approval-workflows"


@pytest.fixture(autouse=True)
def _owner(db_session, test_user):
    """Managing workflows needs the account owner (OSS permission fallback)."""
    from preloop.models.crud import crud_account

    account = crud_account.get(db_session, id=test_user.account_id)
    account.primary_user_id = test_user.id
    db_session.flush()


def _create(client: TestClient, name: str = "Consent hook") -> dict:
    response = client.post(
        BASE,
        json={"name": name, "channel_configs": {"webhook": {"url": HOOK}}},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_create_returns_the_secret_once(client: TestClient):
    created = _create(client)
    secret = created["webhook_secret"]
    assert secret
    assert created["webhook_secret_hint"] == secret[-4:]

    fetched = client.get(f"{BASE}/{created['id']}").json()
    assert fetched["webhook_secret"] is None
    assert fetched["webhook_secret_hint"] == secret[-4:]
    assert secret not in str(fetched)
    listed = client.get(BASE).json()
    assert secret not in str(listed)


def test_update_round_trip_keeps_the_secret(client: TestClient):
    created = _create(client, "Round trip")
    fetched = client.get(f"{BASE}/{created['id']}").json()

    updated = client.put(
        f"{BASE}/{created['id']}",
        json={"approval_config": fetched["approval_config"] or {}},
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["webhook_secret"] is None
    assert updated.json()["webhook_secret_hint"] == created["webhook_secret"][-4:]


def test_rotate_returns_a_new_secret(client: TestClient):
    created = _create(client, "Rotate me")

    rotated = client.post(f"{BASE}/{created['id']}/webhook-secret/rotate")

    assert rotated.status_code == 200, rotated.text
    new_secret = rotated.json()["webhook_secret"]
    assert new_secret and new_secret != created["webhook_secret"]
    assert (
        client.get(f"{BASE}/{created['id']}").json()["webhook_secret_hint"]
        == (new_secret[-4:])
    )


def test_rotate_without_a_webhook_is_rejected(client: TestClient):
    response = client.post(BASE, json={"name": "No hook", "approval_type": "manual"})
    assert response.status_code == 201
    assert response.json()["webhook_secret"] is None

    rotated = client.post(f"{BASE}/{response.json()['id']}/webhook-secret/rotate")
    assert rotated.status_code == 400


def _user(api_key=None):
    user = SimpleNamespace(id=uuid.uuid4())
    if api_key is not None:
        user._auth_api_key = api_key
    return user


def _http(headers=None):
    return SimpleNamespace(headers=headers or {})


def test_api_key_decision_is_recorded_as_api():
    channel = approval_requests._decision_channel(
        _http({"x-preloop-client": "console"}), _user(api_key=object())
    )
    assert channel == "api"
    # The credential decides the channel before the surface hint: a CLI
    # authenticated with an API key is still "api", not "cli".
    cli = "preloop-cli/0.16.0 (darwin; arm64)"
    assert (
        approval_requests._decision_channel(
            _http({"user-agent": cli}), _user(api_key=object())
        )
        == "api"
    )


def test_cli_session_decision_is_recorded_as_cli():
    """A user-session CLI is not the browser console."""
    cli = "preloop-cli/0.16.0 (darwin; arm64)"
    assert (
        approval_requests._decision_channel(_http({"user-agent": cli}), _user())
        == "cli"
    )
    # The marker check is case-insensitive, like the mobile one.
    upper = "PRELOOP-CLI/0.16.0 (darwin; arm64)"
    assert (
        approval_requests._decision_channel(_http({"user-agent": upper}), _user())
        == "cli"
    )


def test_session_decision_channels():
    assert approval_requests._decision_channel(_http(), _user()) == "console"
    ios = "PreloopAI/412 CFNetwork/1568.100.1 Darwin/24.0.0"
    assert (
        approval_requests._decision_channel(_http({"user-agent": ios}), _user())
        == "mobile"
    )
    browser = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Safari/604.1"
    assert (
        approval_requests._decision_channel(_http({"user-agent": browser}), _user())
        == "console"
    )


def test_a_self_declared_client_header_is_not_trusted():
    """A session cannot relabel its own decision by sending a header."""
    assert (
        approval_requests._decision_channel(
            _http({"x-preloop-client": "slack"}), _user()
        )
        == "console"
    )


def test_token_url_decision_is_recorded_as_token_url(
    client: TestClient, db_session, test_user
):
    created = _create(client, "Token decline")
    tool_config = ToolConfiguration(
        tool_name="deploy",
        tool_source="mcp",
        account_id=test_user.account_id,
        approval_workflow_id=uuid.UUID(created["id"]),
    )
    db_session.add(tool_config)
    db_session.flush()
    request = ApprovalRequest(
        account_id=test_user.account_id,
        tool_configuration_id=tool_config.id,
        approval_workflow_id=uuid.UUID(created["id"]),
        execution_id="exec-token-url",
        tool_name="deploy",
        tool_args={"env": "prod"},
        status="pending",
        requested_at=datetime.now(UTC),
        approval_token="token-url-decline",
    )
    db_session.add(request)
    db_session.flush()

    updated = MagicMock()
    updated.id = request.id
    updated.tool_name = "deploy"
    updated.tool_args = {"env": "prod"}
    updated.agent_reasoning = None
    updated.status = "declined"
    updated.requested_at = request.requested_at
    updated.expires_at = None
    updated.resolved_at = datetime.now(UTC)

    with patch(
        "preloop.api.endpoints.public_approval.get_async_db_session"
    ) as mock_get_session:
        mock_get_session.return_value.__aenter__.return_value = AsyncMock()
        with patch(
            "preloop.api.endpoints.public_approval.ApprovalService"
        ) as mock_service_cls:
            mock_service = AsyncMock()
            mock_service.decline_request = AsyncMock(return_value=updated)
            mock_service_cls.return_value = mock_service
            response = client.post(
                f"/approval/{request.id}/decide",
                params={"token": "token-url-decline"},
                json={"action": "decline", "comment": "no"},
            )

    assert response.status_code == 200, response.text
    assert mock_service.decline_request.await_args.kwargs["channel"] == "token_url"
