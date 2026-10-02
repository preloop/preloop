"""Regression tests: MCP server auth_config secrets are write-only (#1133)."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from preloop.api.endpoints import policies
from preloop.models.models.mcp_server import MCPServer
from preloop.models.models.policy_snapshot import PolicySnapshot
from preloop.models.schemas.mcp_server import merge_auth_config, redact_auth_config
from preloop.utils.redaction import REDACTED_STRING

SECRET = "example-bearer-secret"


@pytest.fixture(autouse=True)
def _no_network():
    """Keep the endpoint from connecting, scanning or publishing."""
    client = AsyncMock()
    with (
        patch(
            "preloop.sync.services.event_bus.EventBus.connect", new_callable=AsyncMock
        ),
        patch("preloop.services.mcp_client_pool.MCPClient", return_value=client),
        patch(
            "preloop.api.endpoints.mcp_servers.scan_mcp_server_tools",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        yield


def _bearer_server(db_session, test_user, auth_config=None):
    server = MCPServer(
        name=f"srv-{uuid.uuid4().hex[:8]}",
        url="https://mcp.example.com/mcp",
        transport="http-streaming",
        auth_type="bearer",
        auth_config=auth_config or {"token": SECRET},
        account_id=test_user.account_id,
        status="active",
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


def test_create_response_masks_token(client: TestClient, db_session):
    resp = client.post(
        "/api/v1/mcp-servers",
        json={
            "name": "bearer-server",
            "url": "https://mcp.example.com/mcp",
            "auth_type": "bearer",
            "auth_config": {"token": SECRET},
        },
    )
    assert resp.status_code == 201
    assert SECRET not in resp.text
    assert resp.json()["auth_config"] == {"token": REDACTED_STRING}
    stored = db_session.get(MCPServer, uuid.UUID(resp.json()["id"]))
    assert stored.auth_config == {"token": SECRET}


def test_get_and_list_mask_token(client: TestClient, db_session, test_user):
    server = _bearer_server(db_session, test_user)
    one = client.get(f"/api/v1/mcp-servers/{server.id}")
    many = client.get("/api/v1/mcp-servers")
    assert one.status_code == 200 and many.status_code == 200
    assert SECRET not in one.text
    assert SECRET not in many.text
    assert one.json()["auth_config"] == {"token": REDACTED_STRING}


def test_oauth_secrets_masked_but_metadata_kept(
    client: TestClient, db_session, test_user
):
    server = _bearer_server(
        db_session,
        test_user,
        {
            "client_id": "public-client",
            "client_secret": "cs-value",
            "access_token": "at-value",
            "refresh_token": "rt-value",
            "token_endpoint": "https://auth.example.com/token",
            "token_type": "Bearer",
            "expires_at": 123,
        },
    )
    body = client.get(f"/api/v1/mcp-servers/{server.id}").json()["auth_config"]
    assert body["client_secret"] == REDACTED_STRING
    assert body["access_token"] == REDACTED_STRING
    assert body["refresh_token"] == REDACTED_STRING
    assert body["client_id"] == "public-client"
    assert body["token_endpoint"] == "https://auth.example.com/token"
    assert body["token_type"] == "Bearer"
    assert body["expires_at"] == 123


def test_update_echoing_marker_keeps_stored_token(
    client: TestClient, db_session, test_user
):
    server = _bearer_server(db_session, test_user)
    read = client.get(f"/api/v1/mcp-servers/{server.id}").json()
    resp = client.put(
        f"/api/v1/mcp-servers/{server.id}",
        json={"name": "renamed", "auth_config": read["auth_config"]},
    )
    assert resp.status_code == 200
    assert SECRET not in resp.text
    db_session.refresh(server)
    assert server.auth_config == {"token": SECRET}


def test_update_with_whole_marker_or_omitted_keeps_token(
    client: TestClient, db_session, test_user
):
    server = _bearer_server(db_session, test_user)
    client.put(
        f"/api/v1/mcp-servers/{server.id}", json={"auth_config": {"redacted": True}}
    )
    client.put(f"/api/v1/mcp-servers/{server.id}", json={"name": "renamed"})
    db_session.refresh(server)
    assert server.auth_config == {"token": SECRET}


def test_update_with_new_token_replaces(client: TestClient, db_session, test_user):
    server = _bearer_server(db_session, test_user)
    resp = client.put(
        f"/api/v1/mcp-servers/{server.id}", json={"auth_config": {"token": "new"}}
    )
    assert resp.status_code == 200
    db_session.refresh(server)
    assert server.auth_config == {"token": "new"}


def test_nested_header_secrets_redacted_and_merged():
    stored = {"headers": {"Authorization": "Bearer x", "X-Trace": "on"}}
    redacted = redact_auth_config(stored)
    assert redacted == {"headers": {"Authorization": REDACTED_STRING, "X-Trace": "on"}}
    assert merge_auth_config(redacted, stored) == stored
    assert merge_auth_config({"token": REDACTED_STRING}, None) == {}


def test_policy_version_response_masks_snapshot_credentials():
    snapshot = MagicMock(spec=PolicySnapshot)
    snapshot.id = uuid.uuid4()
    snapshot.version_number = 1
    snapshot.tag = None
    snapshot.description = None
    snapshot.is_active = True
    snapshot.mcp_servers_count = 1
    snapshot.policies_count = 0
    snapshot.tools_count = 0
    snapshot.created_at = datetime.now(timezone.utc)
    snapshot.created_by_user_id = None
    snapshot.snapshot_data = {
        "version": "1.0",
        "mcp_servers": [
            {
                "name": "s",
                "url": "https://mcp.example.com",
                "auth_config": {"token": SECRET},
            }
        ],
    }
    full = policies._snapshot_to_full(snapshot)
    assert SECRET not in str(full.model_dump())
    assert full.snapshot_data["mcp_servers"][0]["auth_config"] == {
        "token": REDACTED_STRING
    }
    # Stored snapshot keeps credentials so rollback still works.
    assert snapshot.snapshot_data["mcp_servers"][0]["auth_config"] == {"token": SECRET}


def test_api_key_header_name_stays_readable():
    redacted = redact_auth_config({"api_key": "k-value", "key_name": "X-API-Key"})
    assert redacted == {"api_key": REDACTED_STRING, "key_name": "X-API-Key"}


def test_rollback_diff_masks_snapshot_credentials(db_session, test_user):
    from preloop.services.policy_version_service import PolicyVersionService

    server = _bearer_server(db_session, test_user)
    service = PolicyVersionService(db_session, str(test_user.account_id))
    snap = service.create_snapshot(set_active=False)
    # Drop the server so the rollback diff carries the snapshot definition.
    db_session.delete(server)
    db_session.commit()

    with patch("preloop.services.policy_version_service.PolicyApplier") as applier:
        applier.return_value.apply.return_value = MagicMock(success=True, errors=[])
        preview, ok, _ = service.rollback_to_snapshot(snap.id, preview_only=True)
        assert ok is True
        assert SECRET not in preview.model_dump_json()

        applied, ok, _ = service.rollback_to_snapshot(snap.id)
        assert ok is True
        assert SECRET not in applied.model_dump_json()
        # The rollback itself still applies the stored credentials.
        applied_policy = applier.return_value.apply.call_args.args[0]
        assert applied_policy.mcp_servers[0].auth_config == {"token": SECRET}
