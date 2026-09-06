"""Endpoint tests for gateway usage summaries."""

from datetime import UTC, datetime, timedelta

from preloop.models.crud import (
    crud_ai_model,
    crud_api_usage,
    crud_flow,
    crud_flow_execution,
    crud_runtime_session,
    crud_runtime_session_activity,
)
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.gateway_usage_search import GatewayUsageSearchService


def test_account_gateway_usage_summary_endpoint(client, db_session, test_user):
    """Account usage summary endpoint should return aggregated session-aware usage."""
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Gateway Session Flow",
            prompt_template="Test",
            trigger_event_source="github",
            trigger_event_types=["test"],
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )
    execution = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(
            flow_id=flow.id,
            status="SUCCEEDED",
            agent_session_reference="session-123",
        ),
    )
    runtime_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="flow_execution",
        session_source_id=str(execution.id),
        session_reference="session-123",
        runtime_principal_type="flow_execution",
        runtime_principal_id=str(execution.id),
        runtime_principal_name=flow.name,
        started_at=execution.start_time,
        last_activity_at=execution.start_time,
    )
    db_session.commit()
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        flow_id=str(flow.id),
        flow_execution_id=str(execution.id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=12,
        completion_tokens=8,
        total_tokens=20,
        estimated_cost=0.05,
    )

    response = client.get("/api/v1/account/gateway-usage/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["total_requests"] == 1
    assert body["token_usage"]["total_tokens"] == 20
    assert body["estimated_cost"] == 0.05
    assert body["usage_by_model"][0]["model_alias"] == "openai/gpt-5"
    assert body["usage_by_session"][0]["runtime_session_id"] == str(runtime_session.id)
    assert body["usage_by_session"][0]["session_source_type"] == "flow_execution"
    assert body["usage_by_session"][0]["flow_execution_id"] == str(execution.id)
    assert body["usage_by_session"][0]["flow_name"] == "Gateway Session Flow"
    assert body["usage_by_session"][0]["session_reference"] == "session-123"


def test_gateway_usage_by_session_includes_managed_agent_identity(
    client, db_session, test_user
):
    """Usage-by-session rows should expose managed agent identity and titles."""
    from preloop.models.crud import crud_managed_agent

    runtime_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="researcher-principal",
        session_reference="claude-session-researcher",
        runtime_principal_type="claude_code",
        runtime_principal_id="researcher-principal",
        runtime_principal_name="Researcher Principal",
        started_at=datetime.now(UTC),
        last_activity_at=datetime.now(UTC),
    )
    runtime_session.title = "Satya Nadella Investment Data Verification"
    agent = crud_managed_agent.upsert_from_runtime_session(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=runtime_session.id,
        session_source_type="claude_code",
        session_source_id="researcher-principal",
        display_name="Researcher",
        session_reference="claude-session-researcher",
    )
    db_session.commit()
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
        prompt_tokens=12,
        completion_tokens=8,
        total_tokens=20,
        estimated_cost=0.05,
    )

    response = client.get("/api/v1/account/gateway-usage/summary")

    assert response.status_code == 200
    row = response.json()["usage_by_session"][0]
    assert row["runtime_session_id"] == str(runtime_session.id)
    assert row["agent_id"] == str(agent.id)
    assert row["agent_name"] == "Researcher"
    assert row["title"] == "Satya Nadella Investment Data Verification"


def test_runtime_session_summary_and_optimization_endpoints(
    client, db_session, test_user
):
    """Runtime session observer endpoints should expose zero-spend insights."""
    now = datetime.now(UTC)
    runtime_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="workspace-42",
        session_reference="claude-session-42",
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-42",
        runtime_principal_name="Claude Workspace",
        started_at=now,
        last_activity_at=now,
    )
    db_session.commit()
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/anthropic/v1/messages",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="anthropic/claude-sonnet-4",
        provider_name="anthropic",
        prompt_tokens=900,
        completion_tokens=100,
        total_tokens=1000,
        estimated_cost=0.5,
    )

    summary_response = client.post(
        f"/api/v1/runtime-sessions/{runtime_session.id}/summaries"
    )

    assert summary_response.status_code == 200
    summary_body = summary_response.json()
    assert summary_body["generated_by"] == "local"
    assert summary_body["estimated_summary_cost"] == 0
    assert "1000 total tokens" in summary_body["highlights"]


def test_runtime_session_gateway_events_include_preview_without_raw_payloads(
    client, db_session, test_user
):
    """Gateway event list should include small previews and strip massive payloads."""
    now = datetime.now(UTC)
    runtime_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="workspace-preview",
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-preview",
        runtime_principal_name="Claude Workspace",
        started_at=now,
        last_activity_at=now,
    )
    db_session.commit()
    crud_runtime_session_activity.log_model_gateway_call(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=runtime_session.id,
        status="success",
        timestamp=now,
        metadata={
            "model_alias": "openai/gpt-5.5",
            "request": {"messages": ["massive raw request"]},
            "response": {"choices": ["massive raw response"]},
            "messages": ["raw messages"],
            "conversation_preview": {
                "messages": [
                    {
                        "role": "user",
                        "text": "Summarize the staging timeline issue",
                    }
                ]
            },
        },
    )
    for index in range(2):
        crud_runtime_session_activity.log_model_gateway_call(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=runtime_session.id,
            status="success",
            timestamp=now + timedelta(seconds=index + 1),
            metadata={
                "model_alias": "openai/gpt-5.5",
                "request": {"messages": [f"massive raw request {index}"]},
                "conversation_preview": {
                    "messages": [{"role": "user", "text": f"Preview {index}"}]
                },
            },
        )

    response = client.get(
        f"/api/v1/runtime-sessions/{runtime_session.id}/gateway-events?limit=2"
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["logs"]) == 2
    assert body["pagination"]["total"] == 3
    assert body["pagination"]["next_offset"] == 2
    assert body["pagination"]["has_more"] is True
    payloads = [event["payload"] for event in body["logs"]]
    assert [
        payload["conversation_preview"]["messages"][0]["text"] for payload in payloads
    ] == [
        "Preview 1",
        "Preview 0",
    ]
    for payload in payloads:
        assert "request" not in payload
        assert "response" not in payload
        assert "messages" not in payload

    metadata_response = client.get(
        f"/api/v1/runtime-sessions/{runtime_session.id}/gateway-events"
        "?limit=3&metadata_only=true"
    )
    assert metadata_response.status_code == 200
    metadata_body = metadata_response.json()
    assert [event["payload"]["metadata_only"] for event in metadata_body["logs"]] == [
        True,
        True,
        True,
    ]
    assert all(
        "conversation_preview" not in event["payload"]
        for event in metadata_body["logs"]
    )


def test_runtime_session_gateway_event_summary_endpoint_returns_local_fallback(
    client, db_session, test_user, monkeypatch
):
    """Interaction summary endpoint should be opt-in and useful without a model."""
    now = datetime.now(UTC)
    runtime_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="workspace-summary",
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-summary",
        runtime_principal_name="Claude Workspace",
        started_at=now,
        last_activity_at=now,
    )
    db_session.commit()
    activity = crud_runtime_session_activity.log_model_gateway_call(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=runtime_session.id,
        status="success",
        metadata={
            "model_alias": "openai/gpt-5.5",
            "endpoint_kind": "chat_completions",
            "outcome": "success",
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "request": {
                "messages": [
                    {
                        "role": "system",
                        "content": "Long personality file",
                    },
                    {
                        "role": "user",
                        "content": "Please debug why session replay is unreadable.",
                    },
                ]
            },
        },
    )
    monkeypatch.setattr(
        "preloop.services.runtime_session_explorer.crud_ai_model.get_default_active_model",
        lambda *args, **kwargs: None,
    )

    response = client.post(
        f"/api/v1/runtime-sessions/{runtime_session.id}/gateway-events/{activity.id}/summary"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["event_id"] == str(activity.id)
    assert body["generated_by"] == "local"
    assert "Please debug" in body["summary"]
    assert "120 tokens" in body["key_points"]


def test_flow_gateway_usage_summary_endpoint(client, db_session, test_user):
    """Flow usage summary endpoint should scope to one flow."""
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Gateway Summary Flow",
            prompt_template="Test",
            trigger_event_source="github",
            trigger_event_types=["test"],
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )
    execution = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED"),
    )
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        flow_id=str(flow.id),
        flow_execution_id=str(execution.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=40,
        completion_tokens=10,
        total_tokens=50,
        estimated_cost=0.2,
    )

    response = client.get(f"/api/v1/flows/{flow.id}/gateway-usage/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["flow_id"] == str(flow.id)
    assert body["total_requests"] == 1
    assert body["token_usage"]["total_tokens"] == 50
    assert body["usage_by_execution"][0]["flow_execution_id"] == str(execution.id)


def test_account_gateway_usage_search_endpoint(client, db_session, test_user):
    """Account gateway search should return indexed interaction hits."""
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Gateway Search Flow",
            prompt_template="Test",
            trigger_event_source="github",
            trigger_event_types=["test"],
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )
    execution = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(
            flow_id=flow.id,
            status="RUNNING",
            agent_session_reference="session-search-123",
        ),
    )
    runtime_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="flow_execution",
        session_source_id=str(execution.id),
        session_reference="session-search-123",
        runtime_principal_type="flow_execution",
        runtime_principal_id=str(execution.id),
        runtime_principal_name=flow.name,
        started_at=execution.start_time,
        last_activity_at=execution.start_time,
    )
    db_session.commit()
    usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        flow_id=str(flow.id),
        flow_execution_id=str(execution.id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=21,
        completion_tokens=13,
        total_tokens=34,
        estimated_cost=0.11,
        runtime_principal_type="flow_execution",
        runtime_principal_id=str(execution.id),
        runtime_principal_name=flow.name,
    )
    GatewayUsageSearchService(db_session).index_interaction(
        usage=usage,
        request_payload={
            "input": "Please review the production rollback checklist",
            "metadata": {"environment": "production"},
        },
        response_payload={
            "output_text": "Rollback checklist reviewed successfully",
        },
    )

    response = client.get(
        "/api/v1/account/gateway-usage/search",
        params={"query": "rollback checklist"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["api_usage_id"] == str(usage.id)
    assert body["items"][0]["flow_name"] == "Gateway Search Flow"
    assert body["items"][0]["runtime_session_id"] == str(runtime_session.id)
    assert body["items"][0]["session_reference"] == "session-search-123"
    assert body["items"][0]["token_usage"]["total_tokens"] == 34
    assert "rollback checklist" in body["items"][0]["excerpt"].lower()


def test_account_gateway_usage_search_lists_recent_documents_without_query(
    client, db_session, test_user
):
    """Account gateway search should list recent indexed interactions without a query."""
    first_usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/anthropic/v1/messages",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        model_alias="anthropic/claude-sonnet-4",
        provider_name="anthropic",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        estimated_cost=0.02,
    )
    second_usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=500,
        duration=0.2,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=18,
        completion_tokens=0,
        total_tokens=18,
        estimated_cost=0.03,
    )
    search_service = GatewayUsageSearchService(db_session)
    search_service.index_interaction(
        usage=first_usage,
        request_payload={"input": "Summarize onboarding status"},
        response_payload={"output_text": "Onboarding status summary"},
    )
    search_service.index_interaction(
        usage=second_usage,
        request_payload={"input": "Diagnose failed deployment"},
        response_payload={"error": "provider timeout"},
    )

    response = client.get(
        "/api/v1/account/gateway-usage/search",
        params={"limit": 1, "provider_name": "openai"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["limit"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["api_usage_id"] == str(second_usage.id)
    assert body["items"][0]["provider_name"] == "openai"
    assert body["items"][0]["outcome"] == "error"


def test_account_runtime_sessions_endpoints(client, db_session, test_user):
    """Runtime session explorer endpoints should list and drill into sessions."""
    runtime_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="workspace-42",
        session_reference="claude-session-42",
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-42",
        runtime_principal_name="Claude Workspace",
        started_at=test_user.created_at,
        last_activity_at=test_user.created_at,
    )
    db_session.commit()

    usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/anthropic/v1/messages",
        method="POST",
        status_code=200,
        duration=0.2,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="anthropic/claude-sonnet-4",
        provider_name="anthropic",
        prompt_tokens=55,
        completion_tokens=34,
        total_tokens=89,
        estimated_cost=0.14,
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-42",
        runtime_principal_name="Claude Workspace",
    )
    GatewayUsageSearchService(db_session).index_interaction(
        usage=usage,
        request_payload={"input": "Summarize the deployment risk review"},
        response_payload={"output_text": "Deployment risk review summarized"},
    )

    list_response = client.get(
        "/api/v1/runtime-sessions",
        params={"session_source_type": "claude_code"},
    )

    assert list_response.status_code == 200
    list_body = list_response.json()
    assert list_body["total"] == 1
    assert list_body["items"][0]["id"] == str(runtime_session.id)
    assert list_body["items"][0]["runtime_principal_name"] == "Claude Workspace"
    assert list_body["items"][0]["token_usage"]["total_tokens"] == 89

    detail_response = client.get(f"/api/v1/runtime-sessions/{runtime_session.id}")

    interactions_response = client.get(
        f"/api/v1/runtime-sessions/{runtime_session.id}/interactions",
        params={"interaction_query": "deployment risk"},
    )

    assert detail_response.status_code == 200
    assert interactions_response.status_code == 200
    detail_body = detail_response.json()
    interactions_body = interactions_response.json()
    assert detail_body["session"]["id"] == str(runtime_session.id)
    assert detail_body["session"]["session_source_type"] == "claude_code"
    assert (
        detail_body["usage_by_model"][0]["model_alias"] == "anthropic/claude-sonnet-4"
    )
    assert interactions_body["total"] == 1
    assert "deployment risk" in interactions_body["items"][0]["excerpt"].lower()


def test_account_runtime_sessions_use_most_recent_model_alias(
    client, db_session, test_user
):
    """Runtime session summaries should use the newest usage row, not max(alias)."""
    runtime_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="workspace-latest",
        session_reference="claude-session-latest",
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-latest",
        runtime_principal_name="Claude Workspace",
        started_at=test_user.created_at,
        last_activity_at=test_user.created_at,
    )
    db_session.commit()

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
        prompt_tokens=20,
        completion_tokens=5,
        total_tokens=25,
        estimated_cost=0.02,
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-latest",
        runtime_principal_name="Claude Workspace",
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
        prompt_tokens=10,
        completion_tokens=4,
        total_tokens=14,
        estimated_cost=0.01,
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-latest",
        runtime_principal_name="Claude Workspace",
    )
    now = datetime.now(UTC)
    older_usage.timestamp = now - timedelta(minutes=10)
    newer_usage.timestamp = now - timedelta(minutes=5)
    db_session.add(older_usage)
    db_session.add(newer_usage)
    db_session.commit()

    list_response = client.get(
        "/api/v1/runtime-sessions",
        params={"query": "claude-session-latest"},
    )

    assert list_response.status_code == 200
    list_body = list_response.json()
    list_item = next(
        item for item in list_body["items"] if item["id"] == str(runtime_session.id)
    )
    assert list_item["latest_model_alias"] == "openai/gpt-5.4"

    detail_response = client.get(f"/api/v1/runtime-sessions/{runtime_session.id}")

    assert detail_response.status_code == 200
    detail_body = detail_response.json()
    assert detail_body["session"]["latest_model_alias"] == "openai/gpt-5.4"


def test_ai_model_detail_endpoints_scope_by_durable_model_id(
    client, db_session, test_user
):
    """AI model detail endpoints should scope by ai_model_id, not alias alone."""
    primary_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Primary GPT-5",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "is_default": False,
        },
        account_id=test_user.account_id,
    )
    secondary_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Secondary GPT-5",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "is_default": False,
        },
        account_id=test_user.account_id,
    )

    primary_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="workspace-primary",
        session_reference="claude-primary",
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-primary",
        runtime_principal_name="Primary Workspace",
        started_at=test_user.created_at,
        last_activity_at=test_user.created_at,
    )
    secondary_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="workspace-secondary",
        session_reference="claude-secondary",
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-secondary",
        runtime_principal_name="Secondary Workspace",
        started_at=test_user.created_at,
        last_activity_at=test_user.created_at,
    )
    db_session.commit()

    primary_usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        ai_model_id=str(primary_model.id),
        runtime_session_id=str(primary_session.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=15,
        completion_tokens=5,
        total_tokens=20,
        estimated_cost=0.04,
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-primary",
        runtime_principal_name="Primary Workspace",
    )
    secondary_usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=500,
        duration=0.2,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        ai_model_id=str(secondary_model.id),
        runtime_session_id=str(secondary_session.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=9,
        completion_tokens=0,
        total_tokens=9,
        estimated_cost=0.02,
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-secondary",
        runtime_principal_name="Secondary Workspace",
    )
    search_service = GatewayUsageSearchService(db_session)
    search_service.index_interaction(
        usage=primary_usage,
        request_payload={"input": "Review the rollout checklist"},
        response_payload={"output_text": "Rollout checklist reviewed"},
    )
    search_service.index_interaction(
        usage=secondary_usage,
        request_payload={"input": "Review the incident checklist"},
        response_payload={"error": "provider timeout"},
    )

    summary_response = client.get(f"/api/v1/ai-models/{primary_model.id}/summary")
    sessions_response = client.get(
        f"/api/v1/ai-models/{primary_model.id}/runtime-sessions"
    )
    interactions_response = client.get(
        f"/api/v1/ai-models/{primary_model.id}/interactions",
        params={"query": "rollout checklist"},
    )

    assert summary_response.status_code == 200
    summary_body = summary_response.json()
    assert summary_body["ai_model_id"] == str(primary_model.id)
    assert summary_body["total_requests"] == 1
    assert summary_body["failed_requests"] == 0
    assert summary_body["token_usage"]["total_tokens"] == 20
    assert summary_body["estimated_cost"] == 0.04
    assert len(summary_body["requests_by_day"]) == 1
    assert summary_body["usage_by_session"][0]["runtime_session_id"] == str(
        primary_session.id
    )

    assert sessions_response.status_code == 200
    sessions_body = sessions_response.json()
    assert sessions_body["total"] == 1
    assert sessions_body["items"][0]["id"] == str(primary_session.id)
    assert sessions_body["items"][0]["runtime_principal_name"] == "Primary Workspace"
    assert sessions_body["items"][0]["token_usage"]["total_tokens"] == 20

    assert interactions_response.status_code == 200
    interactions_body = interactions_response.json()
    assert interactions_body["total"] == 1
    assert interactions_body["items"][0]["api_usage_id"] == str(primary_usage.id)
    assert interactions_body["items"][0]["ai_model_id"] == str(primary_model.id)
    assert interactions_body["items"][0]["runtime_session_id"] == str(
        primary_session.id
    )
    assert "rollout checklist" in interactions_body["items"][0]["excerpt"].lower()


def test_runtime_session_detail_includes_flow_activity_timeline(
    client, db_session, test_user
):
    """Flow-backed runtime sessions should include tool and model activity."""
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Timeline Flow",
            prompt_template="Test",
            trigger_event_source="github",
            trigger_event_types=["test"],
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )
    execution = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(
            flow_id=flow.id,
            status="SUCCEEDED",
            agent_session_reference="timeline-session-1",
            mcp_usage_logs=[
                {
                    "timestamp": "2026-03-10T10:00:01Z",
                    "server_name": "preloop-mcp",
                    "tool_name": "search_issues",
                    "status": "success",
                    "result_summary": "Found similar issues",
                }
            ],
        ),
    )
    runtime_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="flow_execution",
        session_source_id=str(execution.id),
        session_reference="timeline-session-1",
        runtime_principal_type="flow_execution",
        runtime_principal_id=str(execution.id),
        runtime_principal_name=flow.name,
        started_at=execution.start_time,
        last_activity_at=execution.start_time,
    )
    db_session.commit()
    api_usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        flow_id=str(flow.id),
        flow_execution_id=str(execution.id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=20,
        completion_tokens=10,
        total_tokens=30,
        estimated_cost=0.08,
        auth_subject_type="api_key",
    )
    GatewayUsageSearchService(db_session).index_interaction(
        usage=api_usage,
        request_payload={"input": "Summarize recent issue history"},
        response_payload={"output_text": "Issue history summarized"},
    )

    activity_response = client.get(
        f"/api/v1/runtime-sessions/{runtime_session.id}/activity"
    )

    assert activity_response.status_code == 200
    activity_body = activity_response.json()
    assert len(activity_body["items"]) == 3
    activity_types = {item["activity_type"] for item in activity_body["items"]}
    assert activity_types == {
        "session_started",
        "tool_call",
        "model_interaction",
    }
    assert any(
        item["activity_type"] == "session_started"
        and item["title"] == "Session started"
        for item in activity_body["items"]
    )
    assert any(
        item["activity_type"] == "tool_call" and item["title"] == "search_issues"
        for item in activity_body["items"]
    )
    assert any(
        item["activity_type"] == "model_interaction"
        and item["api_usage_id"] == str(api_usage.id)
        and item["auth_subject_type"] == "api_key"
        for item in activity_body["items"]
    )


def test_runtime_session_activity_dedupes_gateway_activity_rows(
    client, db_session, test_user
):
    """Session logs should show each gateway request once while preserving retries."""
    runtime_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="managed_agent",
        session_source_id="agent-hermes-test",
        session_reference="hermes-test-session",
        runtime_principal_type="managed_agent",
        runtime_principal_id="agent-hermes-test",
        runtime_principal_name="Hermes",
        started_at=datetime.now(UTC),
        last_activity_at=datetime.now(UTC),
    )
    db_session.commit()
    api_usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/anthropic/v1/messages",
        method="POST",
        status_code=502,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="anthropic/claude-sonnet-4",
        provider_name="anthropic",
        prompt_tokens=20,
        completion_tokens=0,
        total_tokens=20,
        estimated_cost=0.02,
        auth_subject_type="api_key",
        meta_data={
            "request_fingerprint": "fingerprint-1",
            "gateway_attempt": 2,
            "is_retry": True,
            "retry_of_api_usage_id": "first-usage-id",
        },
    )
    GatewayUsageSearchService(db_session).index_interaction(
        usage=api_usage,
        request_payload={"messages": [{"role": "user", "content": "hello"}]},
        response_payload={"error": "provider timeout"},
    )
    crud_runtime_session_activity.log_model_gateway_call(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=runtime_session.id,
        status="error",
        summary="anthropic/claude-sonnet-4 error",
        metadata={
            "api_usage_id": str(api_usage.id),
            "request_fingerprint": "fingerprint-1",
            "gateway_attempt": 2,
            "is_retry": True,
            "retry_of_api_usage_id": "first-usage-id",
            "total_tokens": 20,
            "estimated_cost": 0.02,
        },
    )

    activity_response = client.get(
        f"/api/v1/runtime-sessions/{runtime_session.id}/activity"
    )

    assert activity_response.status_code == 200
    activity_body = activity_response.json()
    model_items = [
        item
        for item in activity_body["items"]
        if item["activity_type"] in {"model_interaction", "model_gateway_call"}
    ]
    assert len(model_items) == 1
    assert model_items[0]["activity_type"] == "model_interaction"
    assert model_items[0]["api_usage_id"] == str(api_usage.id)
    assert model_items[0]["gateway_attempt"] == 2
    assert model_items[0]["is_retry"] is True
    assert model_items[0]["retry_of_api_usage_id"] == "first-usage-id"


def test_runtime_session_detail_falls_back_to_flow_execution_gateway_usage(
    client, db_session, test_user
):
    """Flow-backed sessions should inherit execution-scoped gateway usage."""
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Legacy Attribution Flow",
            prompt_template="Test",
            trigger_event_source="github",
            trigger_event_types=["test"],
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )
    execution = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(
            flow_id=flow.id,
            status="SUCCEEDED",
            agent_session_reference="legacy-runtime-session",
        ),
    )
    runtime_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="flow_execution",
        session_source_id=str(execution.id),
        session_reference="legacy-runtime-session",
        runtime_principal_type="flow_execution",
        runtime_principal_id=str(execution.id),
        runtime_principal_name=flow.name,
        started_at=execution.start_time,
        last_activity_at=execution.start_time,
    )
    db_session.commit()
    api_usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        flow_id=str(flow.id),
        flow_execution_id=str(execution.id),
        runtime_session_id=None,
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=30,
        completion_tokens=20,
        total_tokens=50,
        estimated_cost=0.15,
        auth_subject_type="api_key",
    )
    GatewayUsageSearchService(db_session).index_interaction(
        usage=api_usage,
        request_payload={"input": "Summarize the migration plan"},
        response_payload={"output_text": "Migration plan summarized"},
    )

    list_response = client.get("/api/v1/runtime-sessions")
    detail_response = client.get(f"/api/v1/runtime-sessions/{runtime_session.id}")
    interactions_response = client.get(
        f"/api/v1/runtime-sessions/{runtime_session.id}/interactions"
    )

    assert list_response.status_code == 200
    list_body = list_response.json()
    listed_session = next(
        item for item in list_body["items"] if item["id"] == str(runtime_session.id)
    )
    assert listed_session["total_requests"] == 1
    assert listed_session["token_usage"]["total_tokens"] == 50
    assert listed_session["estimated_cost"] == 0.15

    assert detail_response.status_code == 200
    assert interactions_response.status_code == 200
    detail_body = detail_response.json()
    interactions_body = interactions_response.json()
    assert detail_body["session"]["total_requests"] == 1
    assert detail_body["session"]["token_usage"]["total_tokens"] == 50
    assert detail_body["session"]["estimated_cost"] == 0.15
    assert detail_body["usage_by_model"][0]["model_alias"] == "openai/gpt-5"
    assert detail_body["usage_by_model"][0]["request_count"] == 1
    assert interactions_body["total"] == 1
    assert interactions_body["items"][0]["flow_execution_id"] == str(execution.id)
    assert interactions_body["items"][0]["runtime_session_id"] is None


def test_runtime_session_detail_includes_normalized_tool_activity_for_non_flow_session(
    client, db_session, test_user
):
    """Non-flow runtime sessions should surface normalized tool activity."""
    runtime_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="workspace-tools-1",
        session_reference="claude-tools-1",
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-tools-1",
        runtime_principal_name="Claude Tools Workspace",
        started_at=test_user.created_at,
        last_activity_at=test_user.created_at,
    )
    db_session.commit()
    crud_runtime_session_activity.log_tool_call(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=runtime_session.id,
        server_name="github",
        tool_name="search_issues",
        status="success",
    )
    activity_response = client.get(
        f"/api/v1/runtime-sessions/{runtime_session.id}/activity"
    )

    assert activity_response.status_code == 200
    activity_body = activity_response.json()
    assert any(
        item["activity_type"] == "tool_call"
        and item["title"] == "search_issues"
        and item["server_name"] == "github"
        and item["status"] == "success"
        for item in activity_body["items"]
    )


def test_gateway_usage_summary_splits_input_output_and_cache(
    client, db_session, test_user
):
    """Every usage aggregate should expose direction and cache splits, not just a total."""
    runtime_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="split-principal",
        session_reference="claude-split",
        runtime_principal_type="claude_code",
        runtime_principal_id="split-principal",
        runtime_principal_name="Split Principal",
        started_at=datetime.now(UTC),
        last_activity_at=datetime.now(UTC),
    )
    db_session.commit()
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/anthropic/v1/messages",
        method="POST",
        status_code=200,
        duration=0.2,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="anthropic/claude-sonnet-4",
        provider_name="anthropic",
        prompt_tokens=12_100,
        completion_tokens=3_100,
        total_tokens=15_200,
        cache_read_tokens=8_200,
        cache_creation_tokens=1_000,
        estimated_cost=0.42,
    )

    response = client.get("/api/v1/account/gateway-usage/summary")

    assert response.status_code == 200
    body = response.json()
    totals = body["token_usage"]
    assert totals["input_tokens"] == 12_100
    assert totals["output_tokens"] == 3_100
    assert totals["prompt_tokens"] == totals["input_tokens"]
    assert totals["completion_tokens"] == totals["output_tokens"]
    assert totals["cache_read_tokens"] == 8_200
    assert totals["cache_write_tokens"] == 1_000
    # Input totals are cache inclusive, so misses are prompt minus read minus write.
    assert totals["uncached_input_tokens"] == 2_900
    assert totals["cache_hit_ratio"] == 0.7387

    by_model = body["usage_by_model"][0]["token_usage"]
    assert by_model["input_tokens"] == 12_100
    assert by_model["output_tokens"] == 3_100
    assert by_model["cache_read_tokens"] == 8_200
    assert by_model["cache_write_tokens"] == 1_000

    by_session = body["usage_by_session"][0]["token_usage"]
    assert by_session["input_tokens"] == 12_100
    assert by_session["output_tokens"] == 3_100
    assert by_session["cache_read_tokens"] == 8_200
    assert by_session["cache_write_tokens"] == 1_000


def test_gateway_usage_summary_reports_unknown_cache_hit_ratio(
    client, db_session, test_user
):
    """Providers that report no cache fields leave the ratio unknown, not zero."""
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=400,
        completion_tokens=100,
        total_tokens=500,
        estimated_cost=0.01,
    )

    response = client.get("/api/v1/account/gateway-usage/summary")

    assert response.status_code == 200
    totals = response.json()["token_usage"]
    assert totals["input_tokens"] == 400
    assert totals["output_tokens"] == 100
    assert totals["cache_read_tokens"] == 0
    assert totals["cache_write_tokens"] == 0
    assert totals["cache_hit_ratio"] is None
