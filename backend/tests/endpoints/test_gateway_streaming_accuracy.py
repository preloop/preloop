"""Streaming token-accounting accuracy tests for the model gateway.

Covers the include_usage injection, client-facing suppression of the
synthetic usage chunk, cross-chunk usage merging, local fallback estimation,
first-class token detail columns, and subscription-covered pricing.
"""

import json
from unittest.mock import patch

from preloop.api.endpoints.openai_gateway import get_model_gateway_auth_context
from preloop.models.crud import crud_ai_model
from preloop.models.models.api_usage import ApiUsage
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService

LITELLM_COMPLETION = "preloop.services.openai_gateway.litellm.completion"


def _create_gateway_model(db_session, test_user, **overrides):
    obj_in = {
        "name": "Gateway Model",
        "provider_name": "openai",
        "model_identifier": "gpt-5",
        "api_key": "provider-secret",
        "meta_data": {
            "gateway": {
                "enabled": True,
                "model_alias": "openai/gpt-5",
                "provider_adapter": "preloop",
            },
            # Deterministic pricing so cost assertions don't depend on the
            # litellm price map.
            "pricing": {"input_price_per_1k": 0.01, "output_price_per_1k": 0.02},
        },
        "is_default": True,
    }
    obj_in.update(overrides)
    return crud_ai_model.create_with_account(
        db=db_session, obj_in=obj_in, account_id=test_user.account_id
    )


def _latest_usage(db_session, account_id) -> ApiUsage:
    row = (
        db_session.query(ApiUsage)
        .filter(
            ApiUsage.account_id == account_id,
            ApiUsage.action_type == "model_gateway",
        )
        .order_by(ApiUsage.created_at.desc())
        .first()
    )
    assert row is not None, "expected a gateway usage row"
    return row


def _sse_chunks(text: str) -> list[dict]:
    chunks = []
    for line in text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            chunks.append(json.loads(line[len("data: ") :]))
    return chunks


def _content_chunk(text: str) -> dict:
    return {
        "id": "chatcmpl_123",
        "created": 1710000000,
        "choices": [{"index": 0, "delta": {"content": text}}],
    }


def _finish_chunk() -> dict:
    return {
        "id": "chatcmpl_123",
        "created": 1710000000,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }


def _synthetic_usage_chunk() -> dict:
    # Shape litellm emits for the final include_usage chunk: one choice with
    # an all-null delta, no finish_reason, usage set.
    return {
        "id": "chatcmpl_123",
        "created": 1710000000,
        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
        "usage": {
            "prompt_tokens": 30,
            "completion_tokens": 5,
            "total_tokens": 35,
            "prompt_tokens_details": {
                "cached_tokens": 12,
                "cache_creation_tokens": 6,
            },
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
    }


def test_stream_injects_include_usage_and_records_tokens(
    app, client, db_session, test_user
):
    """Streaming without client stream_options must still record real usage."""
    _create_gateway_model(db_session, test_user)
    app.dependency_overrides[get_model_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="runtime-token", user=test_user)
    )

    with patch(
        LITELLM_COMPLETION,
        return_value=iter(
            [_content_chunk("Hello"), _finish_chunk(), _synthetic_usage_chunk()]
        ),
    ) as mock_completion:
        response = client.post(
            "/openai/v1/chat/completions",
            headers={"Authorization": "Bearer ignored"},
            json={
                "model": "openai/gpt-5",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
        )

    assert response.status_code == 200
    # include_usage is always requested upstream.
    assert mock_completion.call_args.kwargs["stream_options"] == {"include_usage": True}
    # The synthetic usage-only chunk must NOT leak to a client that did not
    # opt in.
    client_chunks = _sse_chunks(response.text)
    assert all(chunk.get("usage") is None for chunk in client_chunks)
    # ... but its tokens are recorded, including the cache/reasoning split.
    usage = _latest_usage(db_session, test_user.account_id)
    assert usage.prompt_tokens == 30
    assert usage.completion_tokens == 5
    assert usage.cache_read_tokens == 12
    assert usage.cache_creation_tokens == 6
    assert usage.reasoning_tokens == 2
    assert usage.usage_source == "provider"
    assert usage.cost_source == "model_config"
    assert usage.estimated_cost and usage.estimated_cost > 0


def test_stream_passes_usage_chunk_when_client_opted_in(
    app, client, db_session, test_user
):
    """Clients that request include_usage keep receiving the usage chunk."""
    _create_gateway_model(db_session, test_user)
    app.dependency_overrides[get_model_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="runtime-token", user=test_user)
    )

    with patch(
        LITELLM_COMPLETION,
        return_value=iter(
            [_content_chunk("Hello"), _finish_chunk(), _synthetic_usage_chunk()]
        ),
    ):
        response = client.post(
            "/openai/v1/chat/completions",
            headers={"Authorization": "Bearer ignored"},
            json={
                "model": "openai/gpt-5",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )

    assert response.status_code == 200
    client_chunks = _sse_chunks(response.text)
    usage_chunks = [c for c in client_chunks if c.get("usage")]
    assert len(usage_chunks) == 1
    assert usage_chunks[0]["usage"]["total_tokens"] == 35


def test_stream_merges_usage_across_chunks(app, client, db_session, test_user):
    """Sparse usage payloads on different chunks must merge, not overwrite."""
    _create_gateway_model(db_session, test_user)
    app.dependency_overrides[get_model_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="runtime-token", user=test_user)
    )

    first = _content_chunk("Hello")
    first["usage"] = {"prompt_tokens": 100, "completion_tokens": 0}
    second = _finish_chunk()
    second["usage"] = {"prompt_tokens": 0, "completion_tokens": 9}

    with patch(LITELLM_COMPLETION, return_value=iter([first, second])):
        response = client.post(
            "/openai/v1/chat/completions",
            headers={"Authorization": "Bearer ignored"},
            json={
                "model": "openai/gpt-5",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
        )

    assert response.status_code == 200
    usage = _latest_usage(db_session, test_user.account_id)
    assert usage.prompt_tokens == 100
    assert usage.completion_tokens == 9
    assert usage.total_tokens == 109


def test_stream_falls_back_to_local_estimation(app, client, db_session, test_user):
    """A stream with no usage chunk records locally estimated tokens."""
    _create_gateway_model(db_session, test_user)
    app.dependency_overrides[get_model_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="runtime-token", user=test_user)
    )

    with patch(
        LITELLM_COMPLETION,
        return_value=iter([_content_chunk("Hello world"), _finish_chunk()]),
    ):
        response = client.post(
            "/openai/v1/chat/completions",
            headers={"Authorization": "Bearer ignored"},
            json={
                "model": "openai/gpt-5",
                "messages": [{"role": "user", "content": "Count some tokens."}],
                "stream": True,
            },
        )

    assert response.status_code == 200
    usage = _latest_usage(db_session, test_user.account_id)
    assert usage.prompt_tokens and usage.prompt_tokens > 0
    assert usage.completion_tokens and usage.completion_tokens > 0
    assert usage.usage_source == "estimated"
    assert (usage.meta_data or {}).get("usage_estimated") is True


def test_non_streaming_records_token_detail_columns(app, client, db_session, test_user):
    """Cache/reasoning details from a non-streaming response become columns."""
    _create_gateway_model(db_session, test_user)
    app.dependency_overrides[get_model_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="runtime-token", user=test_user)
    )

    with patch(
        LITELLM_COMPLETION,
        return_value={
            "id": "chatcmpl_123",
            "created": 1710000000,
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hi"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 200,
                "completion_tokens": 10,
                "total_tokens": 210,
                "cache_read_input_tokens": 150,
                "cache_creation_input_tokens": 20,
            },
        },
    ):
        response = client.post(
            "/openai/v1/chat/completions",
            headers={"Authorization": "Bearer ignored"},
            json={
                "model": "openai/gpt-5",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    assert response.status_code == 200
    usage = _latest_usage(db_session, test_user.account_id)
    assert usage.cache_read_tokens == 150
    assert usage.cache_creation_tokens == 20
    assert usage.currency == "USD"


def test_subscription_covered_call_records_zero_cost(db_session, test_user):
    """OAuth-covered upstream calls record $0 with the API-equivalent kept."""
    ai_model = _create_gateway_model(db_session, test_user)
    auth_context = ModelGatewayAuthContext(token="runtime-token", user=test_user)
    service = OpenAIGatewayService(db_session, auth_context)
    service._last_upstream_credential_type = "oauth"

    service._record_gateway_request(
        endpoint="/anthropic/v1/messages",
        method="POST",
        status_code=200,
        duration=0.5,
        ai_model=ai_model,
        requested_model="openai/gpt-5",
        response_payload={
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "total_tokens": 1100,
            }
        },
        upstream_response={
            "id": "msg_1",
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "total_tokens": 1100,
            },
        },
        endpoint_kind="anthropic_messages",
        request_payload={"model": "openai/gpt-5", "messages": []},
    )

    usage = _latest_usage(db_session, test_user.account_id)
    assert usage.estimated_cost == 0.0
    assert usage.cost_source == "subscription"
    # input 1000 * 0.01/1k + output 100 * 0.02/1k = 0.012
    assert (usage.meta_data or {}).get("api_equivalent_cost") == 0.012


def test_merge_usage_dicts_semantics():
    """Unit: merge favors non-null, non-zero values and recurses into dicts."""
    merged = OpenAIGatewayService._merge_usage_dicts(
        {
            "prompt_tokens": 100,
            "completion_tokens": 0,
            "prompt_tokens_details": {"cached_tokens": 40},
        },
        {
            "prompt_tokens": 0,
            "completion_tokens": 9,
            "total_tokens": 109,
            "prompt_tokens_details": {"cache_creation_tokens": 10},
        },
    )
    assert merged["prompt_tokens"] == 100
    assert merged["completion_tokens"] == 9
    assert merged["total_tokens"] == 109
    assert merged["prompt_tokens_details"] == {
        "cached_tokens": 40,
        "cache_creation_tokens": 10,
    }
