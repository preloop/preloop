"""Endpoint tests for managed-agent registry surfaces."""

from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

from preloop.api.endpoints.account import (
    _managed_agent_control_fields,
    _managed_agent_control_config_flags,
    _managed_agent_onboarding_flags,
)
from preloop.models.crud import (
    crud_ai_model,
    crud_api_usage,
    crud_managed_agent,
    crud_managed_agent_enrollment,
    crud_runtime_session,
    crud_runtime_session_activity,
)
from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.mcp_server import MCPServer
from preloop.models.models.mcp_tool import MCPTool


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


def test_managed_agent_control_flags_read_openclaw_plugin_config():
    """OpenClaw control config lives under its runtime plugin entry."""
    validation_ready, managed_ready = _managed_agent_control_config_flags(
        {
            "validation_result": {
                "control_plugin_verified": True,
                "control_ws_url_ok": True,
                "control_bearer_token_ok": True,
            },
            "managed_config": {
                "plugins": {
                    "entries": {
                        "preloop-plugin": {
                            "config": {
                                "enabled": True,
                                "control_ws_url": (
                                    "wss://preloop.example/api/v1/agents/control/ws"
                                ),
                            }
                        }
                    }
                }
            },
        }
    )

    assert validation_ready is True
    assert managed_ready is True


def test_managed_agent_control_fields_merge_runtime_plugin_evidence():
    """A later CLI enrollment must not hide a live runtime control connection."""
    fields = _managed_agent_control_fields(
        {
            "agent_kind": "openclaw",
            "session_source_type": "openclaw",
            "lifecycle_state": "active",
            "runtime_session_id": "runtime-123",
            "ended_at": None,
        },
        {
            "validation_result": {},
            "managed_config": {
                "plugins": {
                    "entries": {
                        "preloop-plugin": {
                            "config": {
                                "enabled": True,
                                "control_ws_url": (
                                    "wss://preloop.example/api/v1/agents/control/ws"
                                ),
                            }
                        }
                    }
                }
            },
        },
        {
            "validation_result": {
                "control_channel_configured": True,
                "control_plugin_verified": True,
                "control_ws_url_ok": True,
                "control_bearer_token_ok": True,
            },
            "managed_config": {},
        },
    )

    assert fields["control_state"] == "plugin_connected"
    assert fields["control_enabled"] is True
    assert fields["control_online"] is True
    assert fields["control_capabilities"]


def test_onboarding_flags_downgrade_failed_live_gateway_to_mcp_only():
    """Failed gateway validation must not leave the UI showing fully onboarded."""
    mcp_configured, gateway_configured, state = _managed_agent_onboarding_flags(
        {
            "managed_config": {
                "mcpServers": {"preloop": {"url": "https://preloop.example/mcp/v1"}},
                "baseUrl": "https://preloop.example/openai/v1",
                "apiKey": "agt_managed",
                "model": {"name": "claude/haiku"},
            },
            "validation_result": {
                "preloop_server_present": True,
                "gateway_provider_ok": False,
                "gateway_base_url_ok": False,
                "gateway_token_ok": False,
                "model_provider_rewritten": False,
                "live_validation_supported": True,
                "live_validation_attempted": True,
                "live_validation_status": "failed",
                "live_validation_passed": False,
            },
        }
    )

    assert mcp_configured is True
    assert gateway_configured is False
    assert state == "mcp_proxy_only"


def test_onboarding_flags_keep_gateway_when_live_validation_throttled():
    """An upstream rate-limit is inconclusive: the probe authenticated at the
    gateway and reached the provider, so a throttled live validation must not
    downgrade a configured gateway onboarding to MCP-only."""
    mcp_configured, gateway_configured, state = _managed_agent_onboarding_flags(
        {
            "managed_config": {
                "mcpServers": {"preloop": {"url": "https://preloop.example/mcp/v1"}},
                "baseUrl": "https://preloop.example/openai/v1",
                "apiKey": "agt_managed",
                "model": {"name": "claude/haiku"},
            },
            "validation_result": {
                "preloop_server_present": True,
                "gateway_provider_ok": True,
                "gateway_base_url_ok": True,
                "gateway_token_ok": True,
                "model_provider_rewritten": True,
                "live_validation_supported": True,
                "live_validation_attempted": True,
                "live_validation_status": "throttled",
                "live_validation_passed": False,
            },
        }
    )

    assert mcp_configured is True
    assert gateway_configured is True
    assert state == "fully_onboarded"


def test_account_agents_endpoint_lists_onboarded_agents(client, db_session, test_user):
    """Account agents endpoint should expose registry entries created by onboarding."""
    github_server = _create_active_mcp_server(
        db_session, test_user.account_id, name="github"
    )
    _add_server_tool(db_session, github_server.id, tool_name="search_issues")

    token_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-123",
            "session_reference": "claude-session-abc",
            "runtime_principal_name": "Claude Code Workspace",
            "allowed_mcp_servers": ["github"],
        },
    )

    assert token_response.status_code == 201
    runtime_session = crud_runtime_session.get_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="workspace-123",
    )
    assert runtime_session is not None

    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=120,
        completion_tokens=40,
        total_tokens=160,
        estimated_cost=0.12,
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-123",
        runtime_principal_name="Claude Code Workspace",
    )

    response = client.get("/api/v1/agents")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["display_name"] == "Claude Code Workspace"
    assert item["agent_kind"] == "claude_code"
    assert item["session_source_type"] == "claude_code"
    assert item["session_source_id"] == "workspace-123"
    assert item["session_reference"] == "claude-session-abc"
    assert item["runtime_session_id"] == str(runtime_session.id)
    assert item["managed_mcp_servers"] == ["github"]
    assert item["total_requests"] == 1
    assert item["estimated_cost"] == 0.12
    assert item["latest_model_alias"] == "openai/gpt-5"
    assert item["latest_provider_name"] == "openai"
    assert item["onboarding_state"] == "incomplete"
    assert item["mcp_proxy_configured"] is False
    assert item["model_gateway_configured"] is False


def test_account_agents_activity_requires_traffic_not_registration(
    client, db_session, test_user
):
    """'Active now' means traffic recency: a freshly onboarded agent with zero
    gateway requests must list as idle, matching the dashboard's definition."""
    token_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-fresh",
            "session_reference": "claude-session-fresh",
            "runtime_principal_name": "Fresh Claude",
        },
    )
    assert token_response.status_code == 201

    response = client.get("/api/v1/agents")
    assert response.status_code == 200
    item = next(
        entry
        for entry in response.json()["items"]
        if entry["session_source_id"] == "workspace-fresh"
    )
    assert item["total_requests"] == 0
    assert item["activity_status"] == "idle"
    assert item["is_active_now"] is False

    # A recent gateway request flips it to active_now.
    runtime_session = crud_runtime_session.get_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="workspace-fresh",
    )
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        estimated_cost=0.01,
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-fresh",
        runtime_principal_name="Fresh Claude",
    )

    response = client.get("/api/v1/agents")
    item = next(
        entry
        for entry in response.json()["items"]
        if entry["session_source_id"] == "workspace-fresh"
    )
    assert item["total_requests"] == 1
    assert item["activity_status"] == "active_now"
    assert item["is_active_now"] is True


def test_account_agents_list_exposes_success_and_failure_counts(
    client, db_session, test_user
):
    """The list summary carries success/failure splits so the console can
    surface an all-requests-failing agent (red 'Model traffic failing' strip)."""
    token_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-fail",
            "session_reference": "claude-session-fail",
            "runtime_principal_name": "Failing Claude",
        },
    )
    assert token_response.status_code == 201
    runtime_session = crud_runtime_session.get_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="workspace-fail",
    )
    assert runtime_session is not None

    for _ in range(5):
        crud_api_usage.log_gateway_request(
            db_session,
            endpoint="/openai/v1/responses",
            method="POST",
            status_code=502,
            duration=0.1,
            user_id=str(test_user.id),
            account_id=str(test_user.account_id),
            runtime_session_id=str(runtime_session.id),
            model_alias="openai/gpt-5",
            provider_name="openai",
            prompt_tokens=10,
            completion_tokens=0,
            total_tokens=10,
            estimated_cost=0.0,
            runtime_principal_type="claude_code",
            runtime_principal_id="workspace-fail",
            runtime_principal_name="Failing Claude",
        )

    response = client.get("/api/v1/agents")
    assert response.status_code == 200
    body = response.json()
    item = next(
        entry
        for entry in body["items"]
        if entry["session_source_id"] == "workspace-fail"
    )
    assert item["total_requests"] == 5
    assert item["successful_requests"] == 0
    assert item["failed_requests"] == 5

    detail = client.get(f"/api/v1/agents/{item['id']}")
    assert detail.status_code == 200
    agent = detail.json()["agent"]
    assert agent["successful_requests"] == 0
    assert agent["failed_requests"] == 5


def test_account_agents_list_batches_usage_and_enrichment_for_multiple_agents(
    client, db_session, test_user
):
    """List enrichment should keep per-agent usage and enrollment fields for many agents."""
    from preloop.models.crud.managed_agent import (
        _usage_aggregate_for_principal,
        _usage_aggregates_for_principals,
    )

    for index, source_id in enumerate(("workspace-a", "workspace-b"), start=1):
        token_response = client.post(
            "/api/v1/auth/runtime-sessions/token",
            json={
                "session_source_type": "claude_code",
                "session_source_id": source_id,
                "session_reference": f"claude-session-{source_id}",
                "runtime_principal_name": f"Agent {source_id}",
                "allowed_mcp_servers": [],
            },
        )
        assert token_response.status_code == 201
        agent = crud_managed_agent.get_by_source(
            db_session,
            account_id=str(test_user.account_id),
            session_source_type="claude_code",
            session_source_id=source_id,
        )
        assert agent is not None
        runtime_session = crud_runtime_session.get_by_source(
            db_session,
            account_id=test_user.account_id,
            session_source_type="claude_code",
            session_source_id=source_id,
        )
        assert runtime_session is not None
        crud_api_usage.log_gateway_request(
            db_session,
            endpoint="/openai/v1/responses",
            method="POST",
            status_code=200,
            duration=0.1,
            user_id=str(test_user.id),
            account_id=str(test_user.account_id),
            runtime_session_id=str(runtime_session.id),
            model_alias=f"openai/gpt-{index}",
            provider_name="openai",
            prompt_tokens=10 * index,
            completion_tokens=5 * index,
            total_tokens=15 * index,
            estimated_cost=0.1 * index,
            runtime_principal_type="claude_code",
            runtime_principal_id=source_id,
            runtime_principal_name=f"Agent {source_id}",
        )
        crud_managed_agent_enrollment.create_for_agent(
            db_session,
            account_id=test_user.account_id,
            agent_id=agent.id,
            created_by_user_id=test_user.id,
            enrollment_type="cli_managed_config",
            adapter_key="claude_code",
            status="active",
            managed_config={
                "mcp": {
                    "preloop": {
                        "type": "remote",
                        "url": "https://preloop.example/mcp/v1",
                    }
                },
                "model": f"preloop/openai/gpt-{index}",
                "provider": {
                    "preloop": {
                        "options": {
                            "baseURL": "https://preloop.example/openai/v1",
                            "apiKey": "agt_secret",
                        }
                    }
                },
            },
            validation_result={
                "preloop_server_present": True,
                "gateway_provider_ok": True,
                "gateway_base_url_ok": True,
                "gateway_token_ok": True,
            },
            commit=False,
        )
    db_session.commit()

    principals = [
        ("claude_code", "workspace-a"),
        ("claude_code", "workspace-b"),
    ]
    batched = _usage_aggregates_for_principals(
        db_session,
        account_id=str(test_user.account_id),
        principals=principals,
    )
    for principal in principals:
        single = _usage_aggregate_for_principal(
            db_session,
            account_id=str(test_user.account_id),
            principal_type=principal[0],
            principal_id=principal[1],
        )
        assert batched[principal] == single

    response = client.get("/api/v1/agents")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    items_by_source = {item["session_source_id"]: item for item in body["items"]}
    assert items_by_source["workspace-a"]["total_requests"] == 1
    assert items_by_source["workspace-a"]["estimated_cost"] == 0.1
    assert items_by_source["workspace-a"]["latest_model_alias"] == "openai/gpt-1"
    assert items_by_source["workspace-a"]["onboarding_state"] == "fully_onboarded"
    assert items_by_source["workspace-a"]["configured_model_alias"] == "openai/gpt-1"
    assert items_by_source["workspace-b"]["total_requests"] == 1
    assert items_by_source["workspace-b"]["estimated_cost"] == 0.2
    assert items_by_source["workspace-b"]["latest_model_alias"] == "openai/gpt-2"
    assert items_by_source["workspace-b"]["onboarding_state"] == "fully_onboarded"
    assert items_by_source["workspace-b"]["configured_model_alias"] == "openai/gpt-2"
    assert "control_state" in items_by_source["workspace-a"]
    assert "mcp_proxy_configured" in items_by_source["workspace-a"]


def test_account_agents_endpoint_exposes_configured_model_alias(
    client, db_session, test_user
):
    """Managed agent summaries should expose the configured gateway model alias."""
    token_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-opencode",
            "session_reference": "claude-session-opencode",
            "runtime_principal_name": "Claude Code Workspace",
            "allowed_mcp_servers": [],
        },
    )

    assert token_response.status_code == 201

    agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="claude_code",
        session_source_id="workspace-opencode",
    )
    assert agent is not None

    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "OpenCode zai/glm-5-turbo",
            "provider_name": "openai",
            "model_identifier": "glm-5-turbo",
            "meta_data": {
                "gateway": {"enabled": True, "model_alias": "zai/glm-5-turbo"},
                "managed_agent_id": str(agent.id),
            },
        },
        account_id=test_user.account_id,
    )

    crud_managed_agent_enrollment.create_for_agent(
        db_session,
        account_id=test_user.account_id,
        agent_id=agent.id,
        created_by_user_id=test_user.id,
        enrollment_type="cli_managed_config",
        adapter_key="opencode",
        status="active",
        target_config_path="~/.config/opencode/config.json",
        managed_config={
            "mcp": {
                "preloop": {
                    "type": "remote",
                    "url": "https://preloop.example/mcp/v1",
                }
            },
            "model": "preloop/zai/glm-5-turbo",
            "provider": {
                "preloop": {
                    "options": {
                        "baseURL": "https://preloop.example/openai/v1",
                        "apiKey": "agt_secret",
                    }
                }
            },
        },
    )
    db_session.commit()

    response = client.get("/api/v1/agents")

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["configured_model_alias"] == "zai/glm-5-turbo"
    assert body["items"][0]["configured_model_id"] == str(ai_model.id)
    assert (
        body["items"][0]["configured_models"][0]["gateway_alias"] == "zai/glm-5-turbo"
    )
    assert body["items"][0]["configured_models"][0]["ai_model_id"] == str(ai_model.id)
    assert body["items"][0]["onboarding_state"] == "fully_onboarded"

    detail_response = client.get(f"/api/v1/agents/{body['items'][0]['id']}")
    assert detail_response.status_code == 200
    assert detail_response.json()["agent"]["configured_model_id"] == str(ai_model.id)
    assert (
        detail_response.json()["agent"]["configured_models"][0]["gateway_alias"]
        == "zai/glm-5-turbo"
    )


def test_account_agent_model_bindings_endpoint_replaces_bindings(
    client, db_session, test_user
):
    """Managed-agent bindings should be explicitly replaceable through the API."""
    token_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "opencode",
            "session_source_id": "workspace-bindings",
            "runtime_principal_name": "OpenCode Workspace",
        },
    )
    assert token_response.status_code == 201

    agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="opencode",
        session_source_id="workspace-bindings",
    )
    assert agent is not None

    primary_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "OpenAI GPT-5.4",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
        },
        account_id=test_user.account_id,
    )
    fallback_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "OpenAI GPT-5.4",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
        },
        account_id=test_user.account_id,
    )

    response = client.put(
        f"/api/v1/agents/{agent.id}/model-bindings",
        json={
            "bindings": [
                {
                    "ai_model_id": str(primary_model.id),
                    "binding_type": "configured",
                    "config_key": "model.primary",
                    "gateway_alias": "openai/gpt-5.4",
                    "is_primary": True,
                    "status": "gateway_ready",
                },
                {
                    "ai_model_id": str(fallback_model.id),
                    "binding_type": "configured",
                    "config_key": "model.fallbacks[0]",
                    "gateway_alias": "openai/gpt-5.4",
                    "is_primary": False,
                    "status": "gateway_ready",
                },
            ]
        },
    )

    assert response.status_code == 200
    bindings = response.json()
    assert [binding["gateway_alias"] for binding in bindings] == [
        "openai/gpt-5.4",
        "openai/gpt-5.4",
    ]

    detail_response = client.get(f"/api/v1/agents/{agent.id}")
    assert detail_response.status_code == 200
    body = detail_response.json()
    assert body["agent"]["configured_model_alias"] == "openai/gpt-5.4"
    assert body["agent"]["configured_model_id"] == str(primary_model.id)
    assert [
        binding["config_key"] for binding in body["agent"]["configured_models"]
    ] == [
        "model.primary",
        "model.fallbacks[0]",
    ]


def test_account_agents_endpoint_uses_most_recent_model_alias(
    client, db_session, test_user
):
    """Managed agent summaries should use the most recent usage row, not max(alias)."""
    token_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-latest-model",
            "session_reference": "claude-session-latest",
            "runtime_principal_name": "Claude Code Workspace",
            "allowed_mcp_servers": [],
        },
    )
    assert token_response.status_code == 201

    runtime_session = crud_runtime_session.get_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="workspace-latest-model",
    )
    assert runtime_session is not None

    older_usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="zai/glm-5-turbo",
        provider_name="openai",
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        estimated_cost=0.25,
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-latest-model",
        runtime_principal_name="Claude Code Workspace",
    )
    newer_usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-5.4",
        provider_name="openai",
        prompt_tokens=50,
        completion_tokens=10,
        total_tokens=60,
        estimated_cost=0.1,
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-latest-model",
        runtime_principal_name="Claude Code Workspace",
    )
    older_usage.timestamp = datetime(2026, 4, 3, 21, 0, tzinfo=UTC)
    newer_usage.timestamp = datetime(2026, 4, 3, 21, 5, tzinfo=UTC)
    db_session.add(older_usage)
    db_session.add(newer_usage)
    db_session.commit()

    response = client.get("/api/v1/agents")

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["latest_model_alias"] == "openai/gpt-5.4"


def test_managed_agent_onboarding_flags_supports_codex_gateway_config():
    """Codex custom-provider configs should count as full managed onboarding."""
    mcp_ok, gateway_ok, state = _managed_agent_onboarding_flags(
        {
            "managed_config": {
                "mcp": {
                    "servers": {
                        "preloop": {
                            "url": "https://preloop.example/mcp/v1",
                            "transport": "http",
                        }
                    }
                },
                "model_provider": "preloop",
                "model": "openai/gpt-5.4",
                "model_providers": {
                    "preloop": {
                        "base_url": "https://preloop.example/openai/v1",
                        "wire_api": "responses",
                    }
                },
            }
        }
    )
    assert mcp_ok is True
    assert gateway_ok is True
    assert state == "fully_onboarded"


def test_managed_agent_onboarding_flags_supports_opencode_gateway_config():
    """OpenCode provider configs should count as full managed onboarding."""
    mcp_ok, gateway_ok, state = _managed_agent_onboarding_flags(
        {
            "managed_config": {
                "mcp": {
                    "preloop": {
                        "type": "remote",
                        "url": "https://preloop.example/mcp/v1",
                    }
                },
                "model": "preloop/openai/gpt-5.4",
                "provider": {
                    "preloop": {
                        "options": {
                            "baseURL": "https://preloop.example/openai/v1",
                            "apiKey": "agt_secret",
                        }
                    }
                },
            }
        }
    )
    assert mcp_ok is True
    assert gateway_ok is True
    assert state == "fully_onboarded"


def test_managed_agent_onboarding_flags_supports_claude_gateway_config():
    """Claude settings env overrides should count as full managed onboarding."""
    mcp_ok, gateway_ok, state = _managed_agent_onboarding_flags(
        {
            "managed_config": {
                "mcpServers": {"preloop": {"url": "https://preloop.example/mcp/v1"}},
                "env": {
                    "ANTHROPIC_BASE_URL": "https://preloop.example/anthropic",
                    "ANTHROPIC_AUTH_TOKEN": "agt_secret",
                    "ANTHROPIC_MODEL": "openai/gpt-5.4",
                    "ANTHROPIC_CUSTOM_MODEL_OPTION": "openai/gpt-5.4",
                },
            }
        }
    )
    assert mcp_ok is True
    assert gateway_ok is True
    assert state == "fully_onboarded"


def test_managed_agent_onboarding_flags_supports_gemini_gateway_config():
    """Gemini custom endpoint configs should count as full managed onboarding."""
    mcp_ok, gateway_ok, state = _managed_agent_onboarding_flags(
        {
            "managed_config": {
                "mcpServers": {"preloop": {"url": "https://preloop.example/mcp/v1"}},
                "baseUrl": "https://preloop.example/gemini/v1beta",
                "apiKey": "agt_secret",
                "apiKeyHeader": "x-goog-api-key",
                "model": {"name": "google/gemini-3.1-pro-preview"},
            }
        }
    )
    assert mcp_ok is True
    assert gateway_ok is True
    assert state == "fully_onboarded"


def test_managed_agent_onboarding_flags_supports_hermes_gateway_config():
    """Hermes ``model.{provider,base_url,api_key}`` configs should count as fully
    onboarded.

    Hermes' managed config writes the gateway parameters under ``model:`` (not
    the top-level ``baseUrl`` / ``apiKey`` keys other adapters use), and its
    older validation payloads may omit both the legacy
    ``gateway_model_configured`` flag and the newer ``gateway_provider_ok`` /
    ``gateway_base_url_ok`` pair. The managed config shape itself is enough
    evidence that the model gateway is wired up.
    """
    mcp_ok, gateway_ok, state = _managed_agent_onboarding_flags(
        {
            "managed_config": {
                "mcp_servers": {
                    "preloop": {
                        "url": "https://preloop.example/mcp/v1",
                        "transport": "http-streaming",
                    }
                },
                "model": {
                    "provider": "custom",
                    "base_url": "https://preloop.example/openai/v1",
                    "api_key": "agt_secret",
                    "default": "openai/gpt-5.4",
                },
            },
            "validation_result": {
                "adapter_key": "hermes",
                "validation_passed": True,
                "transport_ok": True,
                "preloop_url_ok": True,
                "preloop_server_present": True,
                "gateway_model_alias": "openai/gpt-5.4",
            },
        }
    )
    assert mcp_ok is True
    assert gateway_ok is True
    assert state == "fully_onboarded"


def test_managed_agent_onboarding_flags_uses_cli_validation_flags():
    """``gateway_provider_ok`` + ``gateway_base_url_ok`` are sufficient.

    All CLI adapters emit these two flags in their validation result after
    they've successfully rewritten the agent's local config to route through
    Preloop's gateway. The backend should accept that signal even when none of
    the hard-coded ``managed_config`` shape patterns match — otherwise newly
    added agents would silently appear as ``mcp_proxy_only`` until the
    backend learns their bespoke nested config shape.
    """
    mcp_ok, gateway_ok, state = _managed_agent_onboarding_flags(
        {
            "managed_config": {
                "mcpServers": {"preloop": {"url": "https://preloop.example/mcp/v1"}},
                "some_future_provider_block": {"endpoint": "https://preloop.example"},
            },
            "validation_result": {
                "adapter_key": "future_agent",
                "validation_passed": True,
                "preloop_server_present": True,
                "gateway_provider_ok": True,
                "gateway_base_url_ok": True,
            },
        }
    )
    assert mcp_ok is True
    assert gateway_ok is True
    assert state == "fully_onboarded"


def test_account_agent_detail_endpoint_returns_one_agent(client, db_session, test_user):
    """Managed agent detail endpoint should return the scoped registry summary."""
    github_server = _create_active_mcp_server(
        db_session, test_user.account_id, name="github"
    )
    _add_server_tool(db_session, github_server.id, tool_name="search_issues")

    token_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-456",
            "session_reference": "claude-session-def",
            "runtime_principal_name": "Claude Code Workspace 2",
            "allowed_mcp_servers": ["github"],
        },
    )

    assert token_response.status_code == 201

    runtime_session = crud_runtime_session.get_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="workspace-456",
    )
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        estimated_cost=0.25,
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-456",
        runtime_principal_name="Claude Code Workspace 2",
    )

    list_response = client.get("/api/v1/agents")
    assert list_response.status_code == 200
    agent_id = list_response.json()["items"][0]["id"]

    detail_response = client.get(f"/api/v1/agents/{agent_id}")

    assert detail_response.status_code == 200
    body = detail_response.json()
    assert body["agent"]["id"] == agent_id
    assert body["agent"]["display_name"] == "Claude Code Workspace 2"
    assert body["agent"]["session_source_type"] == "claude_code"
    assert body["agent"]["session_source_id"] == "workspace-456"
    assert body["agent"]["managed_mcp_servers"] == ["github"]
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["session_source_id"] == "workspace-456"


def test_account_agent_detail_endpoint_includes_session_history(
    client, db_session, test_user
):
    """Managed agent detail should include multiple runtime sessions for one durable agent."""
    principal_id = "claude-code-agent-1"

    first_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-1",
            "runtime_principal_id": principal_id,
            "runtime_principal_name": "Claude Code Workspace",
        },
    )
    second_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-2",
            "runtime_principal_id": principal_id,
            "runtime_principal_name": "Claude Code Workspace",
        },
    )

    assert first_response.status_code == 201
    assert second_response.status_code == 201

    first_session = crud_runtime_session.get_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="workspace-1",
    )
    second_session = crud_runtime_session.get_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="workspace-2",
    )
    assert first_session is not None
    assert second_session is not None

    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(first_session.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        estimated_cost=0.25,
        runtime_principal_type="claude_code",
        runtime_principal_id=principal_id,
        runtime_principal_name="Claude Code Workspace",
    )
    crud_runtime_session_activity.log_tool_call(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=first_session.id,
        server_name="github",
        tool_name="search_issues",
        status="success",
        commit=False,
    )
    crud_runtime_session_activity.log_tool_call(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=second_session.id,
        server_name="github",
        tool_name="search_issues",
        status="failed",
        summary="rate limited",
        commit=False,
    )
    crud_runtime_session_activity.log_tool_call(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=second_session.id,
        server_name="jira",
        tool_name="get_issue",
        status="success",
    )
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=429,
        duration=0.2,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(second_session.id),
        model_alias="openai/gpt-5-mini",
        provider_name="openai",
        prompt_tokens=50,
        completion_tokens=10,
        total_tokens=60,
        estimated_cost=0.05,
        runtime_principal_type="claude_code",
        runtime_principal_id=principal_id,
        runtime_principal_name="Claude Code Workspace",
    )

    list_response = client.get("/api/v1/agents")
    assert list_response.status_code == 200
    body = list_response.json()
    assert body["total"] == 1

    agent_id = body["items"][0]["id"]
    detail_response = client.get(f"/api/v1/agents/{agent_id}")

    assert detail_response.status_code == 200
    detail_body = detail_response.json()
    assert detail_body["agent"]["session_source_id"] == principal_id
    assert detail_body["aggregate"]["session_count"] == 2
    assert detail_body["aggregate"]["total_requests"] == 2
    assert detail_body["aggregate"]["successful_requests"] == 1
    assert detail_body["aggregate"]["failed_requests"] == 1
    assert detail_body["aggregate"]["token_usage"]["prompt_tokens"] == 150
    assert detail_body["aggregate"]["token_usage"]["completion_tokens"] == 30
    assert detail_body["aggregate"]["token_usage"]["total_tokens"] == 180
    assert detail_body["aggregate"]["estimated_cost"] == 0.3
    assert len(detail_body["usage_by_model"]) == 2
    assert detail_body["usage_by_model"][0]["model_alias"] == "openai/gpt-5"
    assert detail_body["usage_by_model"][0]["provider_name"] == "openai"
    assert detail_body["usage_by_model"][0]["request_count"] == 1
    assert detail_body["usage_by_model"][0]["token_usage"]["total_tokens"] == 120
    assert detail_body["usage_by_model"][1]["model_alias"] == "openai/gpt-5-mini"
    assert detail_body["usage_by_model"][1]["request_count"] == 1
    assert len(detail_body["activity_by_server"]) == 2
    assert detail_body["activity_by_server"][0]["server_name"] == "github"
    assert detail_body["activity_by_server"][0]["call_count"] == 2
    assert detail_body["activity_by_server"][0]["successful_calls"] == 1
    assert detail_body["activity_by_server"][0]["failed_calls"] == 1
    assert detail_body["activity_by_tool"][0]["tool_name"] == "search_issues"
    assert detail_body["activity_by_tool"][0]["server_name"] == "github"
    assert detail_body["activity_by_tool"][0]["call_count"] == 2
    assert detail_body["activity_by_tool"][1]["tool_name"] == "get_issue"
    assert len(detail_body["sessions"]) == 2
    assert [session["session_source_id"] for session in detail_body["sessions"]] == [
        "workspace-2",
        "workspace-1",
    ]


def test_account_agent_update_endpoint_controls_lifecycle_and_owner(
    client, db_session, test_user
):
    """Managed agent update endpoint should support ownership and lifecycle controls."""
    token_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-lifecycle",
            "runtime_principal_name": "Lifecycle Agent",
        },
    )
    assert token_response.status_code == 201

    list_response = client.get("/api/v1/agents")
    assert list_response.status_code == 200
    agent_id = list_response.json()["items"][0]["id"]

    update_response = client.patch(
        f"/api/v1/agents/{agent_id}",
        json={
            "owner_user_id": str(test_user.id),
            "lifecycle_action": "suspend",
            "reason": "maintenance window",
        },
    )

    assert update_response.status_code == 200
    body = update_response.json()
    assert body["owner_user_id"] == str(test_user.id)
    assert body["owner_username"] == test_user.username
    assert body["lifecycle_state"] == "suspended"
    assert body["lifecycle_reason"] == "maintenance window"
    assert body["activity_status"] == "suspended"

    reenroll_response = client.patch(
        f"/api/v1/agents/{agent_id}",
        json={"lifecycle_action": "reenroll"},
    )
    assert reenroll_response.status_code == 200
    assert reenroll_response.json()["lifecycle_state"] == "active"


def _seed_pauseable_agent(client, db_session, test_user, *, source_id: str):
    """Onboard one managed agent and return ``(agent_id, durable_token)``.

    Mirrors the CLI onboarding shape: a runtime-session token mints the
    registry entry, then a durable credential is created and written into the
    local agent config as ``ANTHROPIC_API_KEY``.

    Args:
        client: FastAPI test client.
        db_session: Active database session.
        test_user: Authenticated account owner.
        source_id: Durable principal id for the simulated local agent.

    Returns:
        Tuple of the managed-agent id and its durable credential token.
    """
    token_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": source_id,
            "runtime_principal_name": "Claude Code",
        },
    )
    assert token_response.status_code == 201

    agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="claude_code",
        session_source_id=source_id,
    )
    assert agent is not None
    agent_id = str(agent.id)

    credential_response = client.post(
        f"/api/v1/agents/{agent_id}/credentials",
        json={
            "name": "durable-gateway-credential",
            "expires_in_days": 365,
            "scopes": ["mcp:read", "mcp:write"],
        },
    )
    assert credential_response.status_code == 201
    return agent_id, credential_response.json()["token"]


def _post_anthropic_message(client, token: str):
    """Drive one gateway request with a durable agent credential."""
    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value={
            "id": "msg_pause_resume",
            "created": 1710000000,
            "choices": [
                {
                    "message": {"role": "assistant", "content": "pong"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        },
    ):
        return client.post(
            "/anthropic/v1/messages",
            headers={"x-api-key": token, "anthropic-version": "2023-06-01"},
            json={
                "model": "anthropic/claude-pause-resume",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 64,
            },
        )


def _seed_anthropic_gateway_model(db_session, account_id) -> None:
    """Create the gateway-enabled model the pause/resume tests dispatch to."""
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Claude Pause Resume Model",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "anthropic/claude-pause-resume",
                    "provider_adapter": "preloop",
                }
            },
        },
        account_id=account_id,
    )


def _usage_count(db_session, account_id) -> int:
    """Count persisted Anthropic-gateway usage rows for one account."""
    return (
        db_session.query(ApiUsage)
        .filter(
            ApiUsage.account_id == account_id,
            ApiUsage.endpoint == "/anthropic/v1/messages",
        )
        .count()
    )


def test_account_agent_pause_then_resume_restores_gateway_traffic(
    client, db_session, test_user
):
    """Pause must block gateway traffic and resume must restore it.

    Regression for the halt/resume asymmetry: suspend deactivated every
    runtime API key while resume only flipped the lifecycle flag back, so a
    resumed agent 401'd forever with no usage row ever written.
    """
    _seed_anthropic_gateway_model(db_session, test_user.account_id)
    agent_id, durable_token = _seed_pauseable_agent(
        client, db_session, test_user, source_id="pause-resume-host"
    )

    before = _post_anthropic_message(client, durable_token)
    assert before.status_code == 200, before.text
    assert _usage_count(db_session, test_user.account_id) == 1

    pause_response = client.patch(
        f"/api/v1/agents/{agent_id}",
        json={"lifecycle_action": "suspend", "reason": "paused by admin"},
    )
    assert pause_response.status_code == 200
    assert pause_response.json()["lifecycle_state"] == "suspended"

    paused = _post_anthropic_message(client, durable_token)
    assert paused.status_code == 401
    assert _usage_count(db_session, test_user.account_id) == 1

    resume_response = client.patch(
        f"/api/v1/agents/{agent_id}",
        json={"lifecycle_action": "resume", "reason": "resumed by admin"},
    )
    assert resume_response.status_code == 200
    assert resume_response.json()["lifecycle_state"] == "active"

    after = _post_anthropic_message(client, durable_token)
    assert after.status_code == 200, after.text
    assert _usage_count(db_session, test_user.account_id) == 2


def test_account_agent_pause_then_reenroll_restores_gateway_traffic(
    client, db_session, test_user
):
    """The CLI's reenroll action must also restore gateway function."""
    _seed_anthropic_gateway_model(db_session, test_user.account_id)
    agent_id, durable_token = _seed_pauseable_agent(
        client, db_session, test_user, source_id="pause-reenroll-host"
    )

    pause_response = client.patch(
        f"/api/v1/agents/{agent_id}",
        json={"lifecycle_action": "suspend", "reason": "paused by admin"},
    )
    assert pause_response.status_code == 200
    assert _post_anthropic_message(client, durable_token).status_code == 401

    reenroll_response = client.patch(
        f"/api/v1/agents/{agent_id}",
        json={"lifecycle_action": "reenroll"},
    )
    assert reenroll_response.status_code == 200
    assert reenroll_response.json()["lifecycle_state"] == "active"

    after = _post_anthropic_message(client, durable_token)
    assert after.status_code == 200, after.text
    assert _usage_count(db_session, test_user.account_id) == 1


def test_account_agent_resume_heals_legacy_revoked_credentials(
    client, db_session, test_user
):
    """Resume must revive agents left dead by the old revoke-on-suspend release.

    Reproduces the state a shipped release leaves behind: lifecycle
    ``suspended``, durable key ``is_active = False`` and the bound session
    stamped ``ended_at``. Resume has to undo all three or the agent stays
    silently dead in production.
    """
    from preloop.models.crud import crud_api_key

    _seed_anthropic_gateway_model(db_session, test_user.account_id)
    agent_id, durable_token = _seed_pauseable_agent(
        client, db_session, test_user, source_id="legacy-halted-host"
    )
    agent = crud_managed_agent.get_for_account(
        db_session, account_id=str(test_user.account_id), agent_id=agent_id
    )
    assert agent is not None
    runtime_session_id = str(agent.runtime_session_id)

    # Recreate the pre-fix suspension exactly.
    agent.lifecycle_state = "suspended"
    agent.lifecycle_reason = "halted by the previous release"
    agent.runtime_session_id = None
    db_session.add(agent)
    crud_api_key.deactivate_runtime_keys_for_managed_agent(
        db_session,
        account_id=test_user.account_id,
        managed_agent_id=agent_id,
        commit=False,
    )
    crud_runtime_session.update_operator_state(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=runtime_session_id,
        ended_at=datetime.now(UTC),
        commit=False,
    )
    db_session.flush()
    assert _post_anthropic_message(client, durable_token).status_code == 401

    resume_response = client.patch(
        f"/api/v1/agents/{agent_id}",
        json={"lifecycle_action": "resume"},
    )
    assert resume_response.status_code == 200

    durable_key = crud_api_key.get_by_key(db_session, key=durable_token)
    assert durable_key is not None
    db_session.refresh(durable_key)
    assert durable_key.is_active is True

    runtime_session = crud_runtime_session.get_account_session(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=runtime_session_id,
    )
    assert runtime_session is not None
    assert runtime_session.ended_at is None

    after = _post_anthropic_message(client, durable_token)
    assert after.status_code == 200, after.text
    assert _usage_count(db_session, test_user.account_id) == 1


def test_account_agent_resume_does_not_revive_operator_revoked_credentials(
    client, db_session, test_user
):
    """Healing on resume must never resurrect a deliberately revoked credential."""
    from preloop.models.crud import crud_api_key

    agent_id, durable_token = _seed_pauseable_agent(
        client, db_session, test_user, source_id="revoked-credential-host"
    )
    detail_response = client.get(f"/api/v1/agents/{agent_id}")
    assert detail_response.status_code == 200
    credential_id = detail_response.json()["credentials"][0]["id"]

    revoke_response = client.delete(
        f"/api/v1/agents/{agent_id}/credentials/{credential_id}"
    )
    assert revoke_response.status_code == 200

    pause_response = client.patch(
        f"/api/v1/agents/{agent_id}",
        json={"lifecycle_action": "suspend", "reason": "paused by admin"},
    )
    assert pause_response.status_code == 200
    resume_response = client.patch(
        f"/api/v1/agents/{agent_id}",
        json={"lifecycle_action": "resume"},
    )
    assert resume_response.status_code == 200

    durable_key = crud_api_key.get_by_key(db_session, key=durable_token)
    assert durable_key is not None
    db_session.refresh(durable_key)
    assert durable_key.is_active is False


def test_account_agent_decommission_still_revokes_credentials(
    client, db_session, test_user
):
    """Decommission keeps hard credential revocation (offboard semantics)."""
    from preloop.models.crud import crud_api_key

    _agent_id, durable_token = _seed_pauseable_agent(
        client, db_session, test_user, source_id="decommission-revokes-host"
    )
    agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="claude_code",
        session_source_id="decommission-revokes-host",
    )
    assert agent is not None

    decommission_response = client.patch(
        f"/api/v1/agents/{agent.id}",
        json={"lifecycle_action": "decommission", "reason": "offboarded"},
    )
    assert decommission_response.status_code == 200

    durable_key = crud_api_key.get_by_key(db_session, key=durable_token)
    assert durable_key is not None
    db_session.refresh(durable_key)
    assert durable_key.is_active is False


def test_account_agent_detail_includes_credentials_and_enrollments(
    client, db_session, test_user
):
    """Managed agent detail should expose durable credentials and enrollment state."""
    token_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "openclaw",
            "session_source_id": "openclaw-workspace",
            "session_reference": "/Users/test/.openclaw/openclaw.json",
            "runtime_principal_name": "OpenClaw Workspace",
        },
    )
    assert token_response.status_code == 201

    list_response = client.get("/api/v1/agents")
    assert list_response.status_code == 200
    agent_id = list_response.json()["items"][0]["id"]

    credential_response = client.post(
        f"/api/v1/agents/{agent_id}/credentials",
        json={
            "name": "OpenClaw Durable Credential",
            "description": "Primary managed credential",
            "scopes": ["mcp:read", "mcp:write"],
        },
    )
    assert credential_response.status_code == 201
    credential_body = credential_response.json()
    assert credential_body["token"].startswith("agt_")
    assert credential_body["credential"]["name"] == "OpenClaw Durable Credential"

    enrollment_response = client.post(
        f"/api/v1/agents/{agent_id}/enrollments",
        json={
            "enrollment_type": "cli_managed_config",
            "adapter_key": "openclaw",
            "status": "pending",
            "target_config_path": "/Users/test/.openclaw/openclaw.json",
            "discovered_config": {
                "mcpServers": {"github": {"url": "https://example.com"}}
            },
            "managed_config": {
                "mcpServers": {"preloop": {"url": "https://preloop.test/mcp"}}
            },
            "backup_metadata": {"path": "/tmp/openclaw.backup.json"},
            "validation_result": {"dry_run": True},
            "restore_available": True,
        },
    )
    assert enrollment_response.status_code == 201

    detail_response = client.get(f"/api/v1/agents/{agent_id}")
    assert detail_response.status_code == 200
    body = detail_response.json()
    assert len(body["credentials"]) == 1
    assert body["credentials"][0]["name"] == "OpenClaw Durable Credential"
    assert body["credentials"][0]["status"] == "active"
    assert len(body["enrollments"]) == 2
    enrollment_types = {item["enrollment_type"] for item in body["enrollments"]}
    assert enrollment_types == {"cli_managed_config", "runtime_session_bootstrap"}
    cli_enrollment = next(
        item
        for item in body["enrollments"]
        if item["enrollment_type"] == "cli_managed_config"
    )
    bootstrap_enrollment = next(
        item
        for item in body["enrollments"]
        if item["enrollment_type"] == "runtime_session_bootstrap"
    )
    assert cli_enrollment["adapter_key"] == "openclaw"
    assert body["agent"]["onboarding_state"] == "mcp_proxy_only"
    assert body["agent"]["mcp_proxy_configured"] is True
    assert body["agent"]["model_gateway_configured"] is False
    assert (
        bootstrap_enrollment["managed_config"]["runtime_session_id"]
        == (token_response.json()["runtime_session_id"])
    )

    revoke_response = client.delete(
        f"/api/v1/agents/{agent_id}/credentials/{credential_body['credential']['id']}"
    )
    assert revoke_response.status_code == 200
    assert revoke_response.json()["status"] == "revoked"

    credentials_response = client.get(f"/api/v1/agents/{agent_id}/credentials")
    assert credentials_response.status_code == 200
    assert credentials_response.json()[0]["status"] == "revoked"


def test_register_custom_managed_agent_happy_path(client, db_session, test_user):
    """Registering a custom agent returns 201 with an active custom summary."""
    response = client.post(
        "/api/v1/agents",
        json={
            "display_name": "Customer LangGraph Agent",
            "description": "A LangGraph agent the discovery CLI cannot find",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["id"]
    assert body["display_name"] == "Customer LangGraph Agent"
    assert body["session_source_type"] == "custom"
    assert body["lifecycle_state"] == "active"
    assert body["session_source_id"].startswith("custom_")

    agent = crud_managed_agent.get_for_account(
        db_session, account_id=str(test_user.account_id), agent_id=body["id"]
    )
    assert agent is not None
    assert agent.session_source_type == "custom"
    assert agent.lifecycle_state == "active"
    assert agent.session_source_id.startswith("custom_")
    assert agent.enrolled_via == "operator_registration"
    assert agent.tags.get("description") == (
        "A LangGraph agent the discovery CLI cannot find"
    )


def test_register_custom_managed_agent_allows_duplicate_display_name(
    client, db_session
):
    """Two custom agents may share a display name with distinct source ids."""
    first = client.post("/api/v1/agents", json={"display_name": "Dup Agent"})
    second = client.post("/api/v1/agents", json={"display_name": "Dup Agent"})
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["session_source_id"] != second.json()["session_source_id"]


def test_register_custom_managed_agent_is_credential_eligible(client, db_session):
    """A freshly registered custom agent can immediately mint a credential."""
    register_response = client.post(
        "/api/v1/agents",
        json={"display_name": "Credentialable Agent"},
    )
    assert register_response.status_code == 201
    agent_id = register_response.json()["id"]
    assert register_response.json()["lifecycle_state"] == "active"

    credential_response = client.post(
        f"/api/v1/agents/{agent_id}/credentials",
        json={
            "name": "Custom Agent Credential",
            "scopes": ["mcp:read", "mcp:write"],
        },
    )
    assert credential_response.status_code == 201
    credential_body = credential_response.json()
    assert credential_body["token"].startswith("agt_")
    assert credential_body["credential"]["name"] == "Custom Agent Credential"


def test_register_custom_managed_agent_requires_authorized_account(app, test_user):
    """A caller without a resolvable account is rejected before any write."""
    from fastapi import HTTPException, status

    from preloop.api.common import get_account_for_user

    def _deny_account():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account not found",
        )

    app.dependency_overrides[get_account_for_user] = _deny_account
    try:
        from fastapi.testclient import TestClient

        with TestClient(app) as unauthorized_client:
            response = unauthorized_client.post(
                "/api/v1/agents",
                json={"display_name": "Should Not Persist"},
            )
        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(get_account_for_user, None)


def test_account_agent_delete_removes_registry_record(client, db_session, test_user):
    token_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-delete-agent",
            "runtime_principal_name": "Delete Me",
        },
    )
    assert token_response.status_code == 201

    list_response = client.get("/api/v1/agents")
    assert list_response.status_code == 200
    agent_id = list_response.json()["items"][0]["id"]

    delete_response = client.delete(f"/api/v1/agents/{agent_id}")
    assert delete_response.status_code == 200

    after_response = client.get("/api/v1/agents")
    assert after_response.status_code == 200
    assert after_response.json()["total"] == 0


def test_account_agent_delete_scopes_credential_revocation(
    client, db_session, test_user
):
    """Deleting one agent must not revoke sibling agents' durable credentials.

    Several registry entries can share one runtime principal (repeated
    onboards of the same local agent). A principal-wide sweep used to
    deactivate the *surviving* agent's durable gateway/MCP credential too,
    breaking an onboarded Claude Code the day after a console cleanup.
    """
    from preloop.models.crud import crud_api_key

    token_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "shared-principal-host",
            "runtime_principal_name": "Claude Code",
        },
    )
    assert token_response.status_code == 201

    list_response = client.get("/api/v1/agents")
    assert list_response.status_code == 200
    live_agent_id = list_response.json()["items"][0]["id"]

    credential_response = client.post(
        f"/api/v1/agents/{live_agent_id}/credentials",
        json={
            "name": "durable-gateway-credential",
            "expires_in_days": 365,
            "scopes": ["mcp:read", "mcp:write"],
        },
    )
    assert credential_response.status_code == 201
    durable_token = credential_response.json()["token"]

    # A second registry entry with its own principal (another enrollment).
    stale_agent = crud_managed_agent.upsert_from_runtime_session(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=None,
        session_source_type="claude_code",
        session_source_id="stale-duplicate-host",
        display_name="Claude Code (stale duplicate)",
    )
    db_session.flush()

    # Legacy key carrying only the stale agent's principal, with no
    # managed-agent binding (as minted by older CLI versions).
    legacy_key, _legacy_token = crud_api_key.create_runtime_key(
        db_session,
        name="legacy principal-only key",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={
            "runtime_principal": {
                "type": "claude_code",
                "id": "stale-duplicate-host",
            }
        },
    )

    delete_response = client.delete(f"/api/v1/agents/{stale_agent.id}")
    assert delete_response.status_code == 200

    # The live agent's durable credential must still authenticate.
    durable_key = crud_api_key.get_by_key(db_session, key=durable_token)
    assert durable_key is not None
    db_session.refresh(durable_key)
    assert durable_key.is_active is True

    # Legacy unbound keys carrying the deleted agent's principal are swept.
    db_session.refresh(legacy_key)
    assert legacy_key.is_active is False


def test_account_agent_delete_revokes_own_credentials(client, db_session, test_user):
    """Deleting an agent still revokes that agent's own durable credentials."""
    from preloop.models.crud import crud_api_key

    token_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "revoke-own-credentials-host",
            "runtime_principal_name": "Claude Code",
        },
    )
    assert token_response.status_code == 201

    list_response = client.get("/api/v1/agents")
    assert list_response.status_code == 200
    agent_id = list_response.json()["items"][0]["id"]

    credential_response = client.post(
        f"/api/v1/agents/{agent_id}/credentials",
        json={
            "name": "durable-gateway-credential",
            "expires_in_days": 365,
            "scopes": ["mcp:read", "mcp:write"],
        },
    )
    assert credential_response.status_code == 201
    durable_token = credential_response.json()["token"]

    delete_response = client.delete(f"/api/v1/agents/{agent_id}")
    assert delete_response.status_code == 200

    durable_key = crud_api_key.get_by_key(db_session, key=durable_token)
    assert durable_key is not None
    db_session.refresh(durable_key)
    assert durable_key.is_active is False


def test_account_agent_governance_round_trip(client, db_session, test_user):
    token_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "openclaw",
            "session_source_id": "openclaw-governance",
            "runtime_principal_name": "Governed Agent",
        },
    )
    assert token_response.status_code == 201

    list_response = client.get("/api/v1/agents")
    assert list_response.status_code == 200
    agent_id = list_response.json()["items"][0]["id"]

    get_response = client.get(f"/api/v1/agents/{agent_id}/governance")
    assert get_response.status_code == 200
    assert get_response.json()["config"]["allowed_models"] == []

    update_response = client.put(
        f"/api/v1/agents/{agent_id}/governance",
        json={
            "allowed_models": ["openai/gpt-5"],
            "model_budgets": {
                "openai/gpt-5": {"monthly_usd_limit": 25, "soft_limit_usd": 20}
            },
            "tool_rules": {
                "search_issues": [
                    {
                        "action": "require_approval",
                        "condition_type": "simple",
                        "condition_expression": "args.repo == 'private'",
                    }
                ]
            },
        },
    )
    assert update_response.status_code == 200
    updated = update_response.json()
    assert updated["config"]["allowed_models"] == ["openai/gpt-5"]
    assert updated["config"]["model_budgets"]["openai/gpt-5"]["monthly_usd_limit"] == 25
    assert updated["config"]["tool_rules"]["search_issues"][0]["action"] == (
        "require_approval"
    )

    verify_response = client.get(f"/api/v1/agents/{agent_id}/governance")
    assert verify_response.status_code == 200
    assert verify_response.json()["config"] == updated["config"]


def test_account_agent_governance_approval_workflow(client, db_session, test_user):
    """Operators can pin the approval workflow that governs an agent's native
    tool-call approvals; the id must reference a workflow in the account."""
    from preloop.models.crud import crud_approval_workflow

    token_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "claude-approvals-workflow",
            "runtime_principal_name": "Approvals Agent",
        },
    )
    assert token_response.status_code == 201

    list_response = client.get("/api/v1/agents")
    assert list_response.status_code == 200
    agent_id = list_response.json()["items"][0]["id"]

    workflow = crud_approval_workflow.create(
        db_session,
        obj_in={
            "name": "Agent-specific workflow",
            "approval_type": "standard",
            "approver_user_ids": [str(test_user.id)],
        },
        account_id=str(test_user.account_id),
    )

    # Unknown workflow ids are rejected.
    invalid_response = client.put(
        f"/api/v1/agents/{agent_id}/governance",
        json={"approval_workflow_id": str(uuid4())},
    )
    assert invalid_response.status_code == 400

    # Malformed ids are rejected.
    malformed_response = client.put(
        f"/api/v1/agents/{agent_id}/governance",
        json={"approval_workflow_id": "not-a-uuid"},
    )
    assert malformed_response.status_code == 400

    # A workflow belonging to the account is accepted and persisted.
    update_response = client.put(
        f"/api/v1/agents/{agent_id}/governance",
        json={"approval_workflow_id": str(workflow.id)},
    )
    assert update_response.status_code == 200
    assert update_response.json()["config"]["approval_workflow_id"] == str(workflow.id)

    verify_response = client.get(f"/api/v1/agents/{agent_id}/governance")
    assert verify_response.status_code == 200
    assert verify_response.json()["config"]["approval_workflow_id"] == str(workflow.id)

    # Clearing the field removes the per-agent pin.
    clear_response = client.put(
        f"/api/v1/agents/{agent_id}/governance",
        json={"approval_workflow_id": None},
    )
    assert clear_response.status_code == 200
    assert clear_response.json()["config"]["approval_workflow_id"] is None


def test_account_agent_governance_native_tool_approvals(client, db_session, test_user):
    """Operators can switch native tool-call approvals off per agent; absent
    means enforce and invalid values are rejected."""
    token_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "claude-native-approvals-toggle",
            "runtime_principal_name": "Toggle Agent",
        },
    )
    assert token_response.status_code == 201

    list_response = client.get("/api/v1/agents")
    assert list_response.status_code == 200
    agent_id = list_response.json()["items"][0]["id"]

    # Absent by default (approvals enforced).
    get_response = client.get(f"/api/v1/agents/{agent_id}/governance")
    assert get_response.status_code == 200
    assert get_response.json()["config"]["native_tool_approvals"] is None

    # Values outside enforce/off are rejected by schema validation.
    invalid_response = client.put(
        f"/api/v1/agents/{agent_id}/governance",
        json={"native_tool_approvals": "sometimes"},
    )
    assert invalid_response.status_code == 422

    # Switching approvals off persists.
    off_response = client.put(
        f"/api/v1/agents/{agent_id}/governance",
        json={"native_tool_approvals": "off"},
    )
    assert off_response.status_code == 200
    assert off_response.json()["config"]["native_tool_approvals"] == "off"

    verify_response = client.get(f"/api/v1/agents/{agent_id}/governance")
    assert verify_response.status_code == 200
    assert verify_response.json()["config"]["native_tool_approvals"] == "off"

    # Explicit enforce and clearing both re-enable approvals.
    enforce_response = client.put(
        f"/api/v1/agents/{agent_id}/governance",
        json={"native_tool_approvals": "enforce"},
    )
    assert enforce_response.status_code == 200
    assert enforce_response.json()["config"]["native_tool_approvals"] == "enforce"

    clear_response = client.put(
        f"/api/v1/agents/{agent_id}/governance",
        json={"native_tool_approvals": None},
    )
    assert clear_response.status_code == 200
    assert clear_response.json()["config"]["native_tool_approvals"] is None


def test_account_agent_enrollment_validate_and_restore(client, db_session, test_user):
    """Managed agent enrollments should support validation and restore actions."""
    token_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-validate-restore",
            "session_reference": "/Users/test/.claude/mcp-servers.json",
            "runtime_principal_name": "Claude Code Workspace",
        },
    )
    assert token_response.status_code == 201

    list_response = client.get("/api/v1/agents")
    assert list_response.status_code == 200
    agent_id = list_response.json()["items"][0]["id"]

    enrollment_response = client.post(
        f"/api/v1/agents/{agent_id}/enrollments",
        json={
            "enrollment_type": "cli_managed_config",
            "adapter_key": "claude_code",
            "status": "applied",
            "target_config_path": "/Users/test/.claude/mcp-servers.json",
            "discovered_config": {
                "servers": {"github": {"url": "https://example.com"}}
            },
            "managed_config": {
                "servers": {"preloop": {"url": "https://preloop.test/mcp/v1"}}
            },
            "backup_metadata": {"backup_path": "/tmp/claude.backup.json"},
            "restore_available": True,
        },
    )
    assert enrollment_response.status_code == 201
    enrollment_id = enrollment_response.json()["id"]

    validate_response = client.post(
        f"/api/v1/agents/{agent_id}/enrollments/{enrollment_id}/validate",
        json={
            "status": "validated",
            "validation_result": {
                "ok": True,
                "checked": ["config_parse", "preloop_server_present"],
            },
        },
    )
    assert validate_response.status_code == 200
    validate_body = validate_response.json()
    assert validate_body["status"] == "validated"
    assert validate_body["validation_result"]["ok"] is True
    assert validate_body["last_validated_at"] is not None
    assert validate_body["restore_available"] is True

    restore_response = client.post(
        f"/api/v1/agents/{agent_id}/enrollments/{enrollment_id}/restore",
        json={
            "backup_metadata": {
                "backup_path": "/tmp/claude.backup.json",
                "restored_by": "test",
            },
            "validation_result": {"restored": True},
        },
    )
    assert restore_response.status_code == 200
    restore_body = restore_response.json()
    assert restore_body["status"] == "restored"
    assert restore_body["restore_available"] is False
    assert restore_body["backup_metadata"]["restored_by"] == "test"
    assert restore_body["validation_result"]["restored"] is True
    assert restore_body["last_restored_at"] is not None
