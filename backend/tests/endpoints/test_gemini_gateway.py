"""Endpoint tests for the Gemini-compatible gateway."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from preloop.models.crud import crud_ai_model
from preloop.services.model_gateway_auth import ModelGatewayAuthContext


def _create_gateway_model(db_session, account_id, model_alias: str = "gemini-2.5-pro"):
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": model_alias,
                    "provider_adapter": "preloop",
                }
            },
            "is_default": True,
        },
        account_id=account_id,
    )


def _parse_sse_payloads(body: str) -> list[dict]:
    payloads: list[dict] = []
    for chunk in body.split("\n\n"):
        if not chunk.startswith("data: "):
            continue
        raw_payload = chunk[6:]
        if raw_payload == "[DONE]":
            continue
        payloads.append(json.loads(raw_payload))
    return payloads


def test_list_models_returns_gemini_shape(app, client, db_session, test_user):
    """GET /gemini/v1beta/models should return Gemini-shaped model metadata."""
    from preloop.api.endpoints.gemini_gateway import get_gemini_gateway_auth_context

    _create_gateway_model(db_session, test_user.account_id)
    app.dependency_overrides[get_gemini_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="gateway-token", user=test_user)
    )

    response = client.get(
        "/gemini/v1beta/models", headers={"x-goog-api-key": "ignored"}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["models"][0]["name"] == "models/gemini-2.5-pro"
    assert payload["models"][0]["displayName"] == "gemini-2.5-pro"
    assert payload["models"][0]["supportedGenerationMethods"] == [
        "generateContent",
        "streamGenerateContent",
    ]


def test_list_models_strips_provider_prefix_from_gateway_alias(
    app, client, db_session, test_user
):
    """Gemini model listing should expose the client-facing short model name."""
    from preloop.api.endpoints.gemini_gateway import get_gemini_gateway_auth_context

    _create_gateway_model(
        db_session,
        test_user.account_id,
        model_alias="google/gemini-2.5-pro",
    )
    app.dependency_overrides[get_gemini_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="gateway-token", user=test_user)
    )

    response = client.get(
        "/gemini/v1beta/models", headers={"x-goog-api-key": "ignored"}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["models"][0]["name"] == "models/gemini-2.5-pro"
    assert payload["models"][0]["displayName"] == "gemini-2.5-pro"


def test_generate_content_translates_text_request_to_gateway(
    app, client, db_session, test_user
):
    """Gemini text requests should be translated onto the OpenAI Responses path."""
    from preloop.api.endpoints.gemini_gateway import get_gemini_gateway_auth_context

    _create_gateway_model(db_session, test_user.account_id)
    app.dependency_overrides[get_gemini_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="gateway-token", user=test_user)
    )

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value={
            "id": "chatcmpl_123",
            "created": 1710000000,
            "choices": [
                {"message": {"role": "assistant", "content": "Hello from Preloop"}}
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        },
    ) as mock_completion:
        response = client.post(
            "/gemini/v1beta/models/gemini-2.5-pro:generateContent",
            headers={"x-goog-api-key": "ignored"},
            json={
                "systemInstruction": {"parts": [{"text": "Be brief"}]},
                "contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
                "generationConfig": {
                    "temperature": 0.25,
                    "maxOutputTokens": 64,
                    "stopSequences": ["DONE"],
                },
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["candidates"][0]["content"]["role"] == "model"
    assert payload["candidates"][0]["content"]["parts"] == [
        {"text": "Hello from Preloop"}
    ]
    assert payload["usageMetadata"] == {
        "promptTokenCount": 5,
        "candidatesTokenCount": 7,
        "totalTokenCount": 12,
    }

    kwargs = mock_completion.call_args.kwargs
    assert kwargs["model"] == "openai/gpt-5"
    assert kwargs["messages"] == [
        {"role": "system", "content": "Be brief"},
        {"role": "user", "content": "Hello"},
    ]
    assert kwargs["temperature"] == 0.25
    assert kwargs["max_tokens"] == 64
    assert kwargs["stop"] == ["DONE"]


def test_generate_content_resolves_provider_prefixed_gateway_alias(
    app, client, db_session, test_user
):
    """Gemini requests should match stored gateway aliases even when they omit provider prefixes."""
    from preloop.api.endpoints.gemini_gateway import get_gemini_gateway_auth_context

    _create_gateway_model(
        db_session,
        test_user.account_id,
        model_alias="google/gemini-2.5-pro",
    )
    app.dependency_overrides[get_gemini_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="gateway-token", user=test_user)
    )

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value={
            "id": "chatcmpl_123",
            "created": 1710000000,
            "choices": [
                {"message": {"role": "assistant", "content": "Hello from Preloop"}}
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        },
    ) as mock_completion:
        response = client.post(
            "/gemini/v1beta/models/gemini-2.5-pro:generateContent",
            headers={"x-goog-api-key": "ignored"},
            json={"contents": [{"role": "user", "parts": [{"text": "Hello"}]}]},
        )

    assert response.status_code == 200
    kwargs = mock_completion.call_args.kwargs
    assert kwargs["model"] == "openai/gpt-5"


def test_generate_content_accepts_provider_prefixed_model_path(
    app, client, db_session, test_user
):
    """Gemini routes must accept aliases like google/gemini-* in the URL path."""
    from preloop.api.endpoints.gemini_gateway import get_gemini_gateway_auth_context

    _create_gateway_model(
        db_session,
        test_user.account_id,
        model_alias="google/gemini-2.5-pro",
    )
    app.dependency_overrides[get_gemini_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="gateway-token", user=test_user)
    )

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value={
            "id": "chatcmpl_123",
            "created": 1710000000,
            "choices": [
                {"message": {"role": "assistant", "content": "Hello from Preloop"}}
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        },
    ) as mock_completion:
        response = client.post(
            "/gemini/v1beta/models/google/gemini-2.5-pro:generateContent",
            headers={"x-goog-api-key": "ignored"},
            json={"contents": [{"role": "user", "parts": [{"text": "Hello"}]}]},
        )

    assert response.status_code == 200
    kwargs = mock_completion.call_args.kwargs
    assert kwargs["model"] == "openai/gpt-5"


def test_generate_content_translates_tools_and_function_call_parts(
    app, client, db_session, test_user
):
    """Gemini tool declarations and function-call responses should round-trip."""
    from preloop.api.endpoints.gemini_gateway import get_gemini_gateway_auth_context

    _create_gateway_model(db_session, test_user.account_id)
    app.dependency_overrides[get_gemini_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="gateway-token", user=test_user)
    )

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value={
            "id": "chatcmpl_123",
            "created": 1710000000,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call_weather",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city":"Athens"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
        },
    ) as mock_completion:
        response = client.post(
            "/gemini/v1beta/models/gemini-2.5-pro:generateContent",
            headers={"x-goog-api-key": "ignored"},
            json={
                "contents": [
                    {"role": "user", "parts": [{"text": "Weather in Athens?"}]},
                    {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "id": "call_previous",
                                    "name": "lookup_city",
                                    "args": {"city": "Athens"},
                                }
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "id": "call_previous",
                                    "name": "lookup_city",
                                    "response": {"result": {"city": "Athens"}},
                                }
                            }
                        ],
                    },
                ],
                "tools": [
                    {
                        "functionDeclarations": [
                            {
                                "name": "get_weather",
                                "description": "Get current weather",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"city": {"type": "string"}},
                                    "required": ["city"],
                                },
                            }
                        ]
                    }
                ],
                "toolConfig": {
                    "functionCallingConfig": {
                        "mode": "ANY",
                        "allowedFunctionNames": ["get_weather"],
                    }
                },
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["candidates"][0]["content"]["parts"] == [
        {
            "functionCall": {
                "id": "call_weather",
                "name": "get_weather",
                "args": {"city": "Athens"},
            }
        }
    ]

    kwargs = mock_completion.call_args.kwargs
    assert kwargs["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get current weather",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
    ]
    assert kwargs["tool_choice"] == {
        "type": "function",
        "function": {"name": "get_weather"},
    }
    assert kwargs["messages"] == [
        {"role": "user", "content": "Weather in Athens?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_previous",
                    "type": "function",
                    "function": {
                        "name": "lookup_city",
                        "arguments": '{"city": "Athens"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_previous",
            "content": '{"result": {"city": "Athens"}}',
        },
    ]


def test_generate_content_accepts_gemini_auth_variants(client, db_session, test_user):
    """Gemini ingress should accept the default header, query key, and bearer auth."""
    _create_gateway_model(db_session, test_user.account_id)
    auth_context = ModelGatewayAuthContext(token="gateway-token", user=test_user)

    with (
        patch(
            "preloop.api.endpoints.gemini_gateway.authenticate_bearer_token",
            new=AsyncMock(return_value=auth_context),
        ) as mock_auth,
        patch(
            "preloop.services.openai_gateway.litellm.completion",
            return_value={
                "id": "chatcmpl_123",
                "created": 1710000000,
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        ),
    ):
        header_response = client.post(
            "/gemini/v1beta/models/gemini-2.5-pro:generateContent",
            headers={"x-goog-api-key": "header-token"},
            json={"contents": [{"role": "user", "parts": [{"text": "Hello"}]}]},
        )
        query_response = client.post(
            "/gemini/v1beta/models/gemini-2.5-pro:generateContent?key=query-token",
            json={"contents": [{"role": "user", "parts": [{"text": "Hello"}]}]},
        )
        bearer_response = client.post(
            "/gemini/v1beta/models/gemini-2.5-pro:generateContent",
            headers={"Authorization": "Bearer bearer-token"},
            json={"contents": [{"role": "user", "parts": [{"text": "Hello"}]}]},
        )

    assert header_response.status_code == 200
    assert query_response.status_code == 200
    assert bearer_response.status_code == 200
    assert [call.args[0] for call in mock_auth.await_args_list] == [
        "header-token",
        "query-token",
        "bearer-token",
    ]


def test_generate_content_returns_gemini_error_for_missing_auth(client):
    """Missing API credentials should use Gemini's Google-style error envelope."""
    response = client.post(
        "/gemini/v1beta/models/gemini-2.5-pro:generateContent",
        json={"contents": [{"role": "user", "parts": [{"text": "Hello"}]}]},
    )

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": 401,
            "message": "Missing API key",
            "status": "UNAUTHENTICATED",
        }
    }


def test_stream_generate_content_streams_gemini_sse(app, client, db_session, test_user):
    """Gemini streaming should emit Gemini-shaped SSE chunks."""
    from preloop.api.endpoints.gemini_gateway import get_gemini_gateway_auth_context

    _create_gateway_model(db_session, test_user.account_id)
    app.dependency_overrides[get_gemini_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="gateway-token", user=test_user)
    )

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=iter(
            [
                {
                    "choices": [{"delta": {"content": "Hello"}}],
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_123",
                                        "type": "function",
                                        "function": {"name": "get_weather"},
                                    }
                                ]
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {"arguments": '{"city":"Athens"}'},
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 4,
                        "total_tokens": 7,
                    },
                },
            ]
        ),
    ):
        response = client.post(
            "/gemini/v1beta/models/gemini-2.5-pro:streamGenerateContent?alt=sse",
            headers={"x-goog-api-key": "ignored"},
            json={
                "contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
                "tools": [
                    {
                        "functionDeclarations": [
                            {
                                "name": "get_weather",
                                "parameters": {"type": "object", "properties": {}},
                            }
                        ]
                    }
                ],
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    payloads = _parse_sse_payloads(response.text)
    assert payloads[0]["candidates"][0]["content"]["parts"] == [{"text": "Hello"}]
    assert payloads[1]["candidates"][0]["content"]["parts"] == [
        {
            "functionCall": {
                "id": "call_123",
                "name": "get_weather",
                "args": {"city": "Athens"},
            }
        }
    ]
    assert payloads[-1]["usageMetadata"] == {
        "promptTokenCount": 3,
        "candidatesTokenCount": 4,
        "totalTokenCount": 7,
    }


def test_stream_generate_content_midstream_failure_emits_gemini_error_event(
    app, client, db_session, test_user
):
    """Regression for issue #109: mid-stream upstream failures must surface as
    a Gemini-native SSE error event, not a silently truncated stream."""
    from preloop.api.endpoints.gemini_gateway import get_gemini_gateway_auth_context

    _create_gateway_model(db_session, test_user.account_id)
    app.dependency_overrides[get_gemini_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="gateway-token", user=test_user)
    )

    def _exploding_stream():
        yield {"choices": [{"delta": {"content": "Hel"}}]}
        raise Exception("upstream connection reset")

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=_exploding_stream(),
    ):
        response = client.post(
            "/gemini/v1beta/models/gemini-2.5-pro:streamGenerateContent?alt=sse",
            headers={"x-goog-api-key": "ignored"},
            json={
                "contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
            },
        )

    assert response.status_code == 200
    payloads = _parse_sse_payloads(response.text)
    assert payloads, f"expected SSE payloads, got: {response.text!r}"
    error_payloads = [p for p in payloads if "error" in p]
    assert error_payloads, f"expected a Gemini error event, got: {payloads!r}"
    assert "upstream connection reset" in error_payloads[-1]["error"]["message"]
    # The final STOP candidate must not be emitted after a failure.
    assert not any(
        candidate.get("finishReason") == "STOP"
        for p in payloads
        for candidate in p.get("candidates", [])
    )
