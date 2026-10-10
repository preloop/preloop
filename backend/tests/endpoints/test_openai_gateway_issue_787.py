"""Issue 787: VS Code Copilot Chat Custom Endpoint request shapes.

VS Code's Custom Endpoint provider (``vendor: customendpoint``) sends
OpenAI Chat Completions with ``tools`` and ``stream: true``, and the
Responses API shape, with the Preloop agent credential as a bearer key.
These tests replay those bodies through the gateway test client and a
fake upstream. They do not open VS Code.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

import pytest

from preloop.api.endpoints.openai_gateway import get_model_gateway_auth_context
from preloop.models import models
from preloop.models.crud import crud_ai_model, crud_api_key
from preloop.services.model_gateway_auth import ModelGatewayAuthContext

LITELLM_COMPLETION = "preloop.services.openai_gateway.litellm.completion"
GATEWAY_ALIAS = "openai/gpt-5"

# Chat Completions body VS Code sends when apiType is chat-completions,
# toolCalling is true, and streaming is on (the provider default).
CHAT_COMPLETIONS_BODY = {
    "model": GATEWAY_ALIAS,
    "messages": [
        {
            "role": "user",
            "content": "List the files in the workspace",
        }
    ],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a workspace file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ],
    "stream": True,
    "stream_options": {"include_usage": True},
}

# Responses body for the same provider when apiType is responses.
# Function tools use the Responses shape (name beside type).
RESPONSES_BODY = {
    "model": GATEWAY_ALIAS,
    "input": [
        {
            "role": "user",
            "content": "List the files in the workspace",
        }
    ],
    "tools": [
        {
            "type": "function",
            "name": "read_file",
            "description": "Read a workspace file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ],
    "stream": True,
}


def _stream_chunks(*, prompt_tokens: int, completion_tokens: int) -> list[dict]:
    total = prompt_tokens + completion_tokens
    return [
        {
            "id": "chatcmpl_787",
            "created": 1710000000,
            "choices": [{"index": 0, "delta": {"content": "workspace"}}],
        },
        {
            "id": "chatcmpl_787",
            "created": 1710000000,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total,
            },
        },
    ]


def _gateway_model(db_session, test_user) -> None:
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": GATEWAY_ALIAS,
                    "provider_adapter": "preloop",
                    # Mocked litellm.completion is the transcode path.
                    # Native /responses would call the dummy key upstream.
                    "responses_api": "transcode",
                },
                "pricing": {
                    "input_price_per_1k": 0.01,
                    "output_price_per_1k": 0.02,
                },
            },
            "is_default": True,
        },
        account_id=test_user.account_id,
    )


def _vscode_agent_credential(db_session, test_user):
    """Mint the durable credential onboarding would hand to VS Code."""
    now = datetime.now(UTC).replace(tzinfo=None)
    source_id = f"vscode-copilot-{uuid4()}"
    agent = models.ManagedAgent(
        id=uuid4(),
        account_id=test_user.account_id,
        owner_user_id=test_user.id,
        runtime_session_id=None,
        agent_kind="vscode",
        session_source_type="vscode",
        session_source_id=source_id,
        display_name="VSCode / Copilot",
        enrolled_via="cli",
        lifecycle_state="active",
        lifecycle_updated_at=now,
        last_seen_at=now,
    )
    db_session.add(agent)
    db_session.flush()
    api_key, token = crud_api_key.create_runtime_key(
        db_session,
        name=f"VSCode / Copilot credential {uuid4()}",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={
            "managed_agent_id": str(agent.id),
            "runtime_principal": {
                "type": "vscode",
                "id": source_id,
                "name": "VSCode / Copilot",
            },
        },
    )
    return agent, api_key, token


def _usage_for(db_session, api_key_id, endpoint: str) -> models.ApiUsage:
    row = (
        db_session.query(models.ApiUsage)
        .filter(
            models.ApiUsage.api_key_id == api_key_id,
            models.ApiUsage.endpoint == endpoint,
            models.ApiUsage.action_type == "model_gateway",
        )
        .one()
    )
    return row


def test_issue_787_vscode_custom_endpoint_attributes_usage_to_agent(
    app, client, db_session, test_user
):
    """Chat Completions and Responses usage land on the VS Code agent.

    Both calls use the agent credential. Tokens and a positive cost are
    stored on that key's usage rows, and the managed-agent budget bucket
    receives the same cost.
    """
    _gateway_model(db_session, test_user)
    agent, api_key, token = _vscode_agent_credential(db_session, test_user)
    app.dependency_overrides[get_model_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token=token, user=test_user, api_key=api_key)
    )
    headers = {"Authorization": f"Bearer {token}"}

    with patch(
        LITELLM_COMPLETION,
        return_value=iter(_stream_chunks(prompt_tokens=30, completion_tokens=5)),
    ) as chat_completion:
        chat_response = client.post(
            "/openai/v1/chat/completions",
            headers=headers,
            json=CHAT_COMPLETIONS_BODY,
        )

    assert chat_response.status_code == 200
    assert chat_response.headers["content-type"].startswith("text/event-stream")
    assert "workspace" in chat_response.text
    # call_args is the last litellm call. A later session-summary probe can
    # follow the client stream, so the gateway request is the first call.
    chat_call = chat_completion.call_args_list[0]
    assert chat_call.kwargs["tools"][0]["function"]["name"] == "read_file"
    assert chat_call.kwargs["stream"] is True

    with patch(
        LITELLM_COMPLETION,
        return_value=iter(_stream_chunks(prompt_tokens=12, completion_tokens=4)),
    ) as responses_completion:
        responses_response = client.post(
            "/openai/v1/responses",
            headers=headers,
            json=RESPONSES_BODY,
        )

    assert responses_response.status_code == 200
    assert responses_response.headers["content-type"].startswith("text/event-stream")
    assert "response.completed" in responses_response.text
    forwarded = responses_completion.call_args_list[0].kwargs["tools"]
    assert forwarded[0]["type"] == "function"
    assert forwarded[0]["function"]["name"] == "read_file"

    chat_usage = _usage_for(db_session, api_key.id, "/openai/v1/chat/completions")
    responses_usage = _usage_for(db_session, api_key.id, "/openai/v1/responses")
    for usage, prompt_tokens, completion_tokens in (
        (chat_usage, 30, 5),
        (responses_usage, 12, 4),
    ):
        assert usage.prompt_tokens == prompt_tokens
        assert usage.completion_tokens == completion_tokens
        assert usage.total_tokens == prompt_tokens + completion_tokens
        assert usage.estimated_cost is not None and usage.estimated_cost > 0
        assert usage.auth_subject_type == "api_key"
        assert usage.runtime_principal_type == "vscode"
        assert usage.runtime_principal_id == agent.session_source_id
        assert usage.runtime_principal_name == "VSCode / Copilot"

    bucket = (
        db_session.query(models.BudgetSpendActivity)
        .filter(
            models.BudgetSpendActivity.account_id == test_user.account_id,
            models.BudgetSpendActivity.subject_type == "managed_agent",
            models.BudgetSpendActivity.subject_id == agent.id,
            models.BudgetSpendActivity.model_alias == GATEWAY_ALIAS,
            models.BudgetSpendActivity.period == models.BudgetPeriod.all_time,
        )
        .one()
    )
    assert bucket.spend_usd == pytest.approx(
        chat_usage.estimated_cost + responses_usage.estimated_cost
    )
