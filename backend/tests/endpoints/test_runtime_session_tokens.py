"""Tests for external runtime-session token onboarding."""

from datetime import datetime, UTC

import pytest
from unittest.mock import patch

from preloop.api.auth.jwt import get_user_from_token_if_valid
from preloop.models.crud import (
    crud_account,
    crud_api_key,
    crud_managed_agent,
    crud_managed_agent_enrollment,
    crud_runtime_session,
)
from preloop.models.models.mcp_server import MCPServer
from preloop.models.models.mcp_tool import MCPTool
from preloop.services.model_gateway_auth import authenticate_bearer_token


def _create_active_mcp_server(db_session, account_id, *, name: str) -> MCPServer:
    server = MCPServer(
        account_id=account_id,
        name=name,
        url=f"https://{name}.example.com/mcp",
        transport="http-streaming",
        auth_type="none",
        status="active",
    )
    db_session.add(server)
    db_session.flush()
    return server


def _add_server_tool(db_session, server_id, *, tool_name: str) -> None:
    db_session.add(
        MCPTool(
            mcp_server_id=server_id,
            name=tool_name,
            description=f"{tool_name} description",
            input_schema={"type": "object"},
            discovered_at=datetime.now(UTC).isoformat(),
        )
    )
    db_session.flush()


def test_create_runtime_session_token(client, db_session, test_user):
    """Runtime session token endpoint should mint a token and upsert the session."""
    github_server = _create_active_mcp_server(
        db_session, test_user.account_id, name="github"
    )
    _add_server_tool(db_session, github_server.id, tool_name="search_issues")

    response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-123",
            "session_reference": "claude-session-abc",
            "runtime_principal_name": "Claude Code Workspace",
            "expires_in_minutes": 30,
            "allowed_mcp_servers": ["github"],
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["session_source_type"] == "claude_code"
    assert body["session_source_id"] == "workspace-123"
    assert body["session_reference"] == "claude-session-abc"
    assert body["token"]

    runtime_session = crud_runtime_session.get_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="workspace-123",
    )
    assert runtime_session is not None
    assert runtime_session.session_reference == "claude-session-abc"
    assert runtime_session.runtime_principal_name == "Claude Code Workspace"

    api_key = crud_api_key.get_by_key(db_session, key=body["token"])
    assert api_key is not None
    assert api_key.user_id == test_user.id
    assert api_key.name == "Managed Agent Claude Code Workspace [workspace-123]"
    assert api_key.scopes == ["mcp:read", "mcp:write"]
    assert api_key.context_data["runtime_session_id"] == str(runtime_session.id)
    assert api_key.context_data["runtime_principal"]["type"] == "claude_code"
    assert api_key.context_data["runtime_principal"]["id"] == "workspace-123"
    assert api_key.context_data["allowed_mcp_servers"] == ["github"]
    assert api_key.context_data["allowed_mcp_tools"] == [{"tool_name": "search_issues"}]
    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="claude_code",
        session_source_id="workspace-123",
    )
    assert managed_agent is not None
    enrollments = crud_managed_agent_enrollment.list_for_agent(
        db_session, account_id=str(test_user.account_id), agent_id=str(managed_agent.id)
    )
    assert len(enrollments) == 1
    assert enrollments[0]["enrollment_type"] == "runtime_session_bootstrap"
    assert enrollments[0]["adapter_key"] == "claude_code"
    assert enrollments[0]["managed_config"]["managed_mcp_servers"] == ["github"]


def test_create_runtime_session_token_rejects_unknown_source_type(client):
    """Runtime session token endpoint should validate supported source types."""
    response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "totally_unknown_agent",
            "session_source_id": "workspace-123",
        },
    )

    assert response.status_code == 400
    assert "Unsupported session_source_type" in response.json()["detail"]


@pytest.mark.parametrize("source_type", ["gemini_cli", "opencode", "hermes"])
def test_create_runtime_session_token_accepts_managed_cli_source_types(
    client, db_session, test_user, source_type
):
    """Managed CLI integrations should be allowed as first-class source types."""
    response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": source_type,
            "session_source_id": "workspace-123",
            "runtime_principal_name": "Managed CLI Workspace",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["session_source_type"] == source_type
    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type=source_type,
        session_source_id="workspace-123",
    )
    assert managed_agent is not None
    enrollments = crud_managed_agent_enrollment.list_for_agent(
        db_session, account_id=str(test_user.account_id), agent_id=str(managed_agent.id)
    )
    assert len(enrollments) == 1
    assert enrollments[0]["adapter_key"] == source_type


def test_create_runtime_session_token_rejects_scope_escalation(client):
    """Runtime session tokens should reject scopes outside the runtime-safe set."""
    response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-123",
            "scopes": ["mcp:read", "admin:all"],
        },
    )

    assert response.status_code == 400
    assert "only support these scopes" in response.json()["detail"]


def test_create_runtime_session_token_ignores_unknown_allowed_servers(client):
    """Runtime session tokens should ignore active account MCP servers rather than rejecting."""
    response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-123",
            "allowed_mcp_servers": ["github"],
        },
    )

    assert response.status_code == 201
    assert "token" in response.json()


def test_runtime_session_identity_is_account_scoped(db_session):
    """Runtime session source identity should be isolated per account."""
    first_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Account One", "is_active": True},
    )
    second_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Account Two", "is_active": True},
    )
    now = datetime.now(UTC)

    first_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=first_account.id,
        session_source_type="claude_code",
        session_source_id="workspace-123",
        runtime_principal_name="Account One Agent",
        last_activity_at=now,
    )
    second_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=second_account.id,
        session_source_type="claude_code",
        session_source_id="workspace-123",
        runtime_principal_name="Account Two Agent",
        last_activity_at=now,
    )
    db_session.commit()

    assert first_session.id != second_session.id
    assert (
        crud_runtime_session.get_by_source(
            db_session,
            account_id=first_account.id,
            session_source_type="claude_code",
            session_source_id="workspace-123",
        ).id
        == first_session.id
    )
    assert (
        crud_runtime_session.get_by_source(
            db_session,
            account_id=second_account.id,
            session_source_type="claude_code",
            session_source_id="workspace-123",
        ).id
        == second_session.id
    )


def test_account_runtime_session_update_endpoint_ends_session_and_clears_agent_binding(
    client, db_session, test_user
):
    """Runtime session operators should be able to end a session cleanly."""
    response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-terminate",
            "runtime_principal_name": "Claude Workspace",
        },
    )
    assert response.status_code == 201
    runtime_session_id = response.json()["runtime_session_id"]

    runtime_session = crud_runtime_session.get_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="workspace-terminate",
    )
    assert runtime_session is not None

    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="claude_code",
        session_source_id="workspace-terminate",
    )
    assert managed_agent is not None
    assert str(managed_agent.runtime_session_id) == str(runtime_session.id)

    end_response = client.patch(
        f"/api/v1/runtime-sessions/{runtime_session_id}",
        json={"action": "end", "reason": "operator ended stale session"},
    )
    assert end_response.status_code == 200
    body = end_response.json()
    assert body["id"] == runtime_session_id
    assert body["ended_at"] is not None
    assert body["activity_status"] == "ended"
    assert body["is_active_now"] is False

    db_session.refresh(runtime_session)
    db_session.refresh(managed_agent)
    assert runtime_session.ended_at is not None
    assert managed_agent.runtime_session_id is None


@pytest.mark.asyncio
async def test_ending_runtime_session_deactivates_token_and_blocks_mcp_and_gateway_auth(
    client, db_session, test_user
):
    """Ended runtime sessions should revoke runtime tokens across auth paths."""
    response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-revoked",
            "runtime_principal_name": "Revoked Workspace",
        },
    )
    assert response.status_code == 201
    body = response.json()
    token = body["token"]
    runtime_session_id = body["runtime_session_id"]

    with patch(
        "preloop.api.auth.jwt.get_db_session",
        side_effect=lambda: iter([db_session]),
    ):
        assert await get_user_from_token_if_valid(token, db_session) is not None
        assert await authenticate_bearer_token(token, db_session) is not None

    end_response = client.patch(
        f"/api/v1/runtime-sessions/{runtime_session_id}",
        json={"action": "end", "reason": "operator revoked session"},
    )
    assert end_response.status_code == 200

    api_key = crud_api_key.get_by_key(db_session, key=token)
    assert api_key is not None
    assert api_key.is_active is False
    with patch(
        "preloop.api.auth.jwt.get_db_session",
        side_effect=lambda: iter([db_session]),
    ):
        assert await get_user_from_token_if_valid(token, db_session) is None
        assert await authenticate_bearer_token(token, db_session) is None


@pytest.mark.asyncio
async def test_ended_runtime_session_can_mint_fresh_token_for_same_source(
    client, db_session, test_user
):
    """Minting a new token for an ended source should reopen session auth successfully."""
    initial_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-restart",
            "runtime_principal_name": "Restarted Workspace",
        },
    )
    assert initial_response.status_code == 201
    initial_body = initial_response.json()
    old_token = initial_body["token"]
    runtime_session_id = initial_body["runtime_session_id"]

    end_response = client.patch(
        f"/api/v1/runtime-sessions/{runtime_session_id}",
        json={"action": "end", "reason": "operator ended previous workspace run"},
    )
    assert end_response.status_code == 200

    old_runtime_session = crud_runtime_session.get_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="workspace-restart",
    )
    assert old_runtime_session is not None
    ended_at = old_runtime_session.ended_at
    assert ended_at is not None

    replacement_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-restart",
            "runtime_principal_name": "Restarted Workspace",
        },
    )
    assert replacement_response.status_code == 201
    replacement_body = replacement_response.json()
    new_token = replacement_body["token"]

    reopened_runtime_session = crud_runtime_session.get_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="workspace-restart",
    )
    assert reopened_runtime_session is not None
    assert reopened_runtime_session.ended_at is None
    assert reopened_runtime_session.started_at is not None
    assert reopened_runtime_session.started_at >= ended_at

    old_api_key = crud_api_key.get_by_key(db_session, key=old_token)
    new_api_key = crud_api_key.get_by_key(db_session, key=new_token)
    assert old_api_key is not None
    assert new_api_key is not None
    assert old_api_key.is_active is False
    assert new_api_key.is_active is True

    with patch(
        "preloop.api.auth.jwt.get_db_session",
        side_effect=lambda: iter([db_session]),
    ):
        assert await get_user_from_token_if_valid(old_token, db_session) is None
        assert await authenticate_bearer_token(old_token, db_session) is None
        assert await get_user_from_token_if_valid(new_token, db_session) is not None
        assert await authenticate_bearer_token(new_token, db_session) is not None


@pytest.mark.parametrize("lifecycle_action", ["suspend", "decommission"])
def test_runtime_session_token_issuance_rejects_non_active_agents(
    client, db_session, test_user, lifecycle_action
):
    """Suspended or decommissioned agents should not be re-onboarded by minting a token."""
    initial_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-blocked",
            "runtime_principal_name": "Blocked Workspace",
        },
    )
    assert initial_response.status_code == 201

    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="claude_code",
        session_source_id="workspace-blocked",
    )
    assert managed_agent is not None

    update_response = client.patch(
        f"/api/v1/agents/{managed_agent.id}",
        json={
            "lifecycle_action": lifecycle_action,
            "reason": f"{lifecycle_action} for operator control",
        },
    )
    assert update_response.status_code == 200

    blocked_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-blocked",
            "runtime_principal_name": "Blocked Workspace",
        },
    )

    assert blocked_response.status_code == 403
    assert lifecycle_action in blocked_response.json()["detail"]

    db_session.refresh(managed_agent)
    assert managed_agent.lifecycle_state == (
        "suspended" if lifecycle_action == "suspend" else "decommissioned"
    )


def test_runtime_session_token_issuance_succeeds_after_reenroll(
    client, db_session, test_user
):
    """PATCH reenroll must unblock token issuance for an archived agent."""
    initial_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-reenroll",
            "runtime_principal_name": "Reenroll Workspace",
        },
    )
    assert initial_response.status_code == 201

    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="claude_code",
        session_source_id="workspace-reenroll",
    )
    assert managed_agent is not None
    agent_id = str(managed_agent.id)

    decommission_response = client.patch(
        f"/api/v1/agents/{agent_id}",
        json={
            "lifecycle_action": "decommission",
            "reason": "offboarded via preloop CLI",
        },
    )
    assert decommission_response.status_code == 200
    assert decommission_response.json()["lifecycle_state"] == "decommissioned"

    blocked_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-reenroll",
            "runtime_principal_name": "Reenroll Workspace",
        },
    )
    assert blocked_response.status_code == 403

    reenroll_response = client.patch(
        f"/api/v1/agents/{agent_id}",
        json={"lifecycle_action": "reenroll"},
    )
    assert reenroll_response.status_code == 200
    assert reenroll_response.json()["lifecycle_state"] == "active"

    token_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-reenroll",
            "runtime_principal_name": "Reenroll Workspace",
        },
    )
    assert token_response.status_code == 201
    assert token_response.json()["token"]

    db_session.refresh(managed_agent)
    assert managed_agent.lifecycle_state == "active"
    assert str(managed_agent.id) == agent_id


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle_action", ["suspend", "decommission"])
async def test_existing_runtime_session_token_is_revoked_for_non_active_agents(
    client, db_session, test_user, lifecycle_action
):
    """Suspending or decommissioning an agent should revoke its existing runtime token."""
    initial_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-lifecycle-revoke",
            "runtime_principal_name": "Lifecycle Revoke Workspace",
        },
    )
    assert initial_response.status_code == 201
    token = initial_response.json()["token"]
    runtime_session_id = initial_response.json()["runtime_session_id"]

    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="claude_code",
        session_source_id="workspace-lifecycle-revoke",
    )
    assert managed_agent is not None
    managed_agent_id = str(managed_agent.id)

    with patch(
        "preloop.api.auth.jwt.get_db_session",
        side_effect=lambda: iter([db_session]),
    ):
        assert await get_user_from_token_if_valid(token, db_session) is not None
        assert await authenticate_bearer_token(token, db_session) is not None

    update_response = client.patch(
        f"/api/v1/agents/{managed_agent_id}",
        json={
            "lifecycle_action": lifecycle_action,
            "reason": f"{lifecycle_action} for operator control",
        },
    )
    assert update_response.status_code == 200

    runtime_session = crud_runtime_session.get_account_session(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session_id),
    )
    assert runtime_session is not None
    assert runtime_session.ended_at is not None

    managed_agent = crud_managed_agent.get_for_account(
        db_session,
        account_id=str(test_user.account_id),
        agent_id=managed_agent_id,
    )
    assert managed_agent is not None
    assert managed_agent.runtime_session_id is None
    assert managed_agent.lifecycle_state == (
        "suspended" if lifecycle_action == "suspend" else "decommissioned"
    )

    api_key = crud_api_key.get_by_key(db_session, key=token)
    assert api_key is not None
    assert api_key.is_active is False

    with patch(
        "preloop.api.auth.jwt.get_db_session",
        side_effect=lambda: iter([db_session]),
    ):
        assert await get_user_from_token_if_valid(token, db_session) is None
        assert await authenticate_bearer_token(token, db_session) is None


@pytest.mark.asyncio
async def test_durable_managed_agent_credential_respects_agent_lifecycle(
    client, db_session, test_user
):
    """Durable agent credentials should be rejected when the agent is suspended."""
    initial_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-durable-auth",
            "runtime_principal_name": "Durable Credential Workspace",
        },
    )
    assert initial_response.status_code == 201

    list_response = client.get("/api/v1/agents")
    agent_id = list_response.json()["items"][0]["id"]

    credential_response = client.post(
        f"/api/v1/agents/{agent_id}/credentials",
        json={
            "name": "Primary Durable Credential",
            "scopes": ["mcp:read", "mcp:write"],
        },
    )
    assert credential_response.status_code == 201
    durable_token = credential_response.json()["token"]

    with patch(
        "preloop.api.auth.jwt.get_db_session",
        side_effect=lambda: iter([db_session]),
    ):
        assert await get_user_from_token_if_valid(durable_token, db_session) is not None
        assert await authenticate_bearer_token(durable_token, db_session) is not None

    suspend_response = client.patch(
        f"/api/v1/agents/{agent_id}",
        json={"lifecycle_action": "suspend", "reason": "operator suspension"},
    )
    assert suspend_response.status_code == 200

    with patch(
        "preloop.api.auth.jwt.get_db_session",
        side_effect=lambda: iter([db_session]),
    ):
        assert await get_user_from_token_if_valid(durable_token, db_session) is None
        assert await authenticate_bearer_token(durable_token, db_session) is None
