"""Routing policy and effective request evidence use mocked upstreams only."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session
from preloop.models import models

from preloop.schemas.ai_model import AIModelCreate, AIModelUpdate
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService


def service() -> OpenAIGatewayService:
    """Build an isolated gateway with a mocked upstream."""
    return OpenAIGatewayService(
        MagicMock(),
        ModelGatewayAuthContext(
            token="test-token", user=SimpleNamespace(id="user", account_id="account")
        ),
        upstream_backend=MagicMock(),
    )


def model(**overrides: object) -> SimpleNamespace:
    """Build a synthetic registered model."""
    return SimpleNamespace(
        **{
            "id": "model",
            "provider_name": "openrouter",
            "model_identifier": "example/model",
            "api_endpoint": None,
            "model_parameters": {
                "provider": {
                    "only": ["host-a"],
                    "zdr": True,
                    "data_collection": "deny",
                    "allow_fallbacks": False,
                }
            },
            "meta_data": {},
            "account_id": "account",
            **overrides,
        }
    )


@pytest.mark.parametrize("schema", [AIModelCreate, AIModelUpdate])
@pytest.mark.parametrize(
    "preferences",
    [
        {"typo": True},
        {"zdr": "true"},
        {"only": "host-a"},
        {"only": [""]},
        {"data_collection": "never"},
        {"max_price": {"secret": 1}},
        {"sort": {"by": "price", "typo": True}},
        {"preferred_max_latency": {"p95": 1}},
        {"preferred_min_throughput": -1},
    ],
)
def test_save_rejects_invalid_provider_preferences(
    schema: type[AIModelCreate] | type[AIModelUpdate],
    preferences: dict,
) -> None:
    with pytest.raises(ValidationError, match="provider"):
        schema(
            name="Example",
            provider_name="openrouter",
            model_identifier="example/model",
            model_parameters={"provider": preferences},
        )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "provider,endpoint",
    [
        ("openrouter", None),
        ("openai-compatible", "https://openrouter.ai/api/v1"),
    ],
)
def test_saved_policy_wins_over_client_and_extra_body(
    stream: bool,
    provider: str,
    endpoint: str | None,
) -> None:
    gateway = service()
    configured = model(provider_name=provider, api_endpoint=endpoint)
    with patch("preloop.services.openai_gateway.get_secret_service") as secret:
        secret.return_value.resolve_ai_model_credentials.return_value = SimpleNamespace(
            credential_type="api_key", value="synthetic-secret"
        )
        kwargs = gateway._build_completion_kwargs(
            configured,
            messages=[],
            stream=stream,
            provider="openai",
            payload={
                "provider": {"only": ["host-b"], "zdr": False},
                "extra_body": {"provider": {"data_collection": "allow"}},
            },
        )
    assert kwargs["extra_body"]["provider"] == configured.model_parameters["provider"]
    assert (
        gateway._last_upstream_parameters["provider"]
        == kwargs["extra_body"]["provider"]
    )


def test_summary_records_translated_schema_without_content() -> None:
    gateway = service()
    with (
        patch("preloop.services.openai_gateway.get_secret_service") as secret,
        patch(
            "preloop.services.openai_gateway.litellm.get_supported_openai_params",
            return_value=["response_format"],
        ),
        patch(
            "preloop.services.openai_gateway._upstream_supports_json_object_only",
            return_value=True,
        ),
    ):
        secret.return_value.resolve_ai_model_credentials.return_value = SimpleNamespace(
            credential_type="api_key", value="synthetic-secret"
        )
        gateway._build_completion_kwargs(
            model(),
            messages=[{"role": "user", "content": "private content"}],
            stream=False,
            provider="openai",
            payload={
                "max_tokens": 123,
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "private_tool", "parameters": {}},
                    }
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "private_schema",
                        "strict": True,
                        "schema": {"type": "object"},
                    },
                },
            },
        )
    summary = gateway._last_upstream_parameters
    assert summary["output_format"] == {"type": "json_object"}
    assert summary["schema_in_instructions"] is True
    assert summary["tool_count"] == 1
    assert summary["max_tokens"] == 123
    assert summary["model"] == "example/model"
    assert "private" not in str(summary)
    assert "secret" not in str(summary)


def test_openrouter_serving_provider_survives_litellm_hidden_metadata() -> None:
    gateway = service()
    response = SimpleNamespace(
        model_dump=lambda: {"id": "gen-example", "choices": []},
        _hidden_params={"upstream_provider": "Host A"},
    )
    gateway._response_to_dict(response)
    assert gateway._last_upstream_provider == "Host A"
    gateway._begin_request_accounting()
    assert gateway._last_upstream_provider is None


@pytest.mark.parametrize("path", ["chat", "responses", "messages"])
@pytest.mark.parametrize("stream", [False, True])
def test_routing_reaches_all_gateway_paths(path: str, stream: bool) -> None:
    gateway = service()
    configured = model()
    response = {
        "id": "gen-example",
        "provider": "Host A",
        "model": "example/model",
        "choices": [
            {
                "message": {"role": "assistant", "content": "Done"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    chunk = {
        **response,
        "choices": [{"delta": {"content": "Done"}, "finish_reason": "stop"}],
    }
    gateway.upstream_backend.completion.return_value = (
        iter([chunk]) if stream else response
    )
    with (
        patch.object(gateway, "_resolve_requested_model", return_value=configured),
        patch.object(gateway, "_check_budget", return_value=None),
        patch.object(gateway, "_record_gateway_request"),
        patch("preloop.services.openai_gateway.get_secret_service") as secret,
    ):
        secret.return_value.resolve_ai_model_credentials.return_value = SimpleNamespace(
            credential_type="api_key", value="synthetic-secret"
        )
        payload = {
            "model": "example/model",
            "provider": {"zdr": False},
            "max_tokens": 32,
        }
        if path == "responses":
            payload["input"] = "Hello"
            result = (
                gateway.stream_response(payload)
                if stream
                else gateway.create_response(payload)
            )
        elif path == "chat":
            payload["messages"] = [{"role": "user", "content": "Hello"}]
            result = (
                gateway.stream_chat_completion(payload)
                if stream
                else gateway.create_chat_completion(payload)
            )
        else:
            payload["messages"] = [{"role": "user", "content": "Hello"}]
            result = (
                gateway.stream_message(payload)
                if stream
                else gateway.create_message(payload)
            )
        if stream:
            list(result)
            gateway.flush_deferred_stream_record()
    assert (
        gateway.upstream_backend.completion.call_args.kwargs["extra_body"]["provider"]
        == configured.model_parameters["provider"]
    )
    assert gateway._last_upstream_provider == "Host A"


def test_usage_csv_contains_serving_identity_and_safe_summary() -> None:
    from preloop.services.gateway_usage_export import gateway_usage_csv

    row = {
        "id": "usage-1",
        "timestamp": "2026-11-17T12:00:00Z",
        "model_alias": "example/model",
        "provider_name": "openrouter",
        "upstream_request_id": "gen-example",
        "upstream_provider": "Host A",
        "upstream_parameters": {
            "model": "example/model",
            "output_format": {"type": "none"},
        },
        "request": {"messages": "private text"},
        "api_key": "synthetic-secret",
    }
    exported = gateway_usage_csv([row])
    assert "upstream_provider" in exported
    assert "Host A" in exported
    assert "gen-example" in exported
    assert "output_format" in exported
    assert "private text" not in exported
    assert "synthetic-secret" not in exported


def test_current_openrouter_price_and_sort_fields() -> None:
    parameters = {
        "provider": {
            "max_price": {"prompt": "0.25", "audio": "0.1"},
            "sort": {"by": "exacto", "partition": "none"},
        }
    }
    assert AIModelUpdate(model_parameters=parameters).model_parameters == parameters


@pytest.mark.parametrize(
    "path,stream", [("responses", False), ("responses", True), ("embeddings", False)]
)
def test_real_litellm_adapter_preserves_routing_and_serving_identity(
    path: str,
    stream: bool,
) -> None:
    """Use the pinned adapter against a loopback fake, never a real provider."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from preloop.services.openai_gateway import LiteLLMModelGatewayBackend

    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            """Keep the fake provider silent."""

        def do_POST(self) -> None:  # noqa: N802
            """Record a request and return synthetic OpenRouter metadata."""
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            response = {
                "id": "gen-wire-example",
                "provider": "Host A",
                "created": 1,
                "model": "example/model",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Done"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
            if path == "embeddings":
                response["object"] = "list"
                response["data"] = [
                    {"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}
                ]
                response.pop("choices")
            if body.get("stream"):
                response.update(
                    object="chat.completion.chunk",
                    choices=[
                        {
                            "index": 0,
                            "delta": {"content": "Done"},
                            "finish_reason": "stop",
                        }
                    ],
                )
                data = (
                    "data: " + json.dumps(response) + "\n\ndata: [DONE]\n\n"
                ).encode()
            else:
                data = json.dumps(response).encode()
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/event-stream" if body.get("stream") else "application/json",
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gateway = service()
    gateway.upstream_backend = LiteLLMModelGatewayBackend()
    configured = model(api_endpoint=f"http://127.0.0.1:{server.server_port}/api/v1")
    try:
        with (
            patch.object(gateway, "_resolve_requested_model", return_value=configured),
            patch.object(gateway, "_check_budget", return_value=None),
            patch.object(gateway, "_record_gateway_request"),
            patch("preloop.services.openai_gateway.get_secret_service") as secret,
            patch(
                "preloop.services.openai_gateway.checked_model_api_base",
                return_value=configured.api_endpoint,
            ),
        ):
            secret.return_value.resolve_ai_model_credentials.return_value = (
                SimpleNamespace(credential_type="api_key", value="synthetic-secret")
            )
            payload = {
                "model": "example/model",
                "input": "Synthetic request",
                "max_output_tokens": 123,
                "provider": {"only": ["host-b"], "allow_fallbacks": True},
            }
            if path == "embeddings":
                gateway.create_embedding(payload)
            elif stream:
                list(gateway.stream_response(payload))
            else:
                gateway.create_response(payload)
        assert seen[-1]["provider"] == configured.model_parameters["provider"]
        assert gateway._last_upstream_provider == "Host A"
        assert gateway._last_upstream_generation_id == "gen-wire-example"
        assert gateway._last_upstream_parameters["model"] == seen[-1]["model"]
        if path != "embeddings":
            assert (
                gateway._last_upstream_parameters["max_tokens"]
                == seen[-1]["max_tokens"]
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("provider", ["openai", "anthropic", "openai-compatible"])
def test_provider_routing_never_leaks_to_other_upstreams(provider: str) -> None:
    gateway = service()
    configured = model(
        provider_name=provider,
        model_identifier="example-model",
        api_endpoint="https://example.com/v1",
    )
    with patch("preloop.services.openai_gateway.get_secret_service") as secret:
        secret.return_value.resolve_ai_model_credentials.return_value = SimpleNamespace(
            credential_type="api_key", value="synthetic-secret"
        )
        kwargs = gateway._build_completion_kwargs(
            configured,
            messages=[],
            stream=False,
            provider="openai",
            payload={
                "provider": {"zdr": True},
                "extra_body": {"provider": {"only": ["host-a"]}},
            },
        )
    assert "provider" not in kwargs
    assert "provider" not in kwargs.get("extra_body", {})
    assert "provider" not in gateway._last_upstream_parameters


def test_continued_responses_request_reapplies_current_model_policy() -> None:
    gateway = service()
    configured = model()
    gateway.upstream_backend.completion.return_value = {
        "id": "gen-continuation",
        "choices": [{"message": {"content": "Done"}}],
    }
    with (
        patch.object(gateway, "_resolve_requested_model", return_value=configured),
        patch.object(gateway, "_check_budget", return_value=None),
        patch.object(gateway, "_record_gateway_request"),
        patch("preloop.services.openai_gateway.get_secret_service") as secret,
    ):
        secret.return_value.resolve_ai_model_credentials.return_value = SimpleNamespace(
            credential_type="api_key", value="synthetic-secret"
        )
        first = gateway.create_response({"model": "example/model", "input": "Hello"})
        configured.model_parameters = {"provider": {"only": ["host-c"], "zdr": True}}
        gateway.create_response(
            {
                "model": "example/model",
                "previous_response_id": first["id"],
                "input": "Continue",
                "provider": {"only": ["host-b"]},
            }
        )
    assert (
        gateway.upstream_backend.completion.call_args.kwargs["extra_body"]["provider"]
        == configured.model_parameters["provider"]
    )


def test_serving_provider_and_generation_are_persisted_and_exported(
    db_session: "Session", test_user: "models.User"
) -> None:
    from preloop.models.crud import crud_ai_model, crud_api_usage

    configured = crud_ai_model.create_with_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={
            "name": "Example model",
            "provider_name": "openrouter",
            "model_identifier": "example/model",
            "api_key": "synthetic-secret",
            "model_parameters": {"provider": {"only": ["host-a"]}},
        },
    )
    gateway = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="test", user=test_user)
    )
    gateway._response_to_dict({"id": "gen-persisted", "provider": "Host A"})
    gateway._upstream_is_openrouter = True
    gateway._record_effective_request_parameters(
        {"model": "example/model", "provider": {"only": ["host-a"]}}
    )
    gateway._record_gateway_request_inner(
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        ai_model=configured,
        requested_model="example/model",
        endpoint_kind="responses",
        response_payload={},
        upstream_response={"id": "resp-client"},
    )
    row = crud_api_usage.get(db_session, id=gateway.last_usage_id)
    assert row.upstream_request_id == "gen-persisted"
    assert row.meta_data["upstream_provider"] == "Host A"
    assert row.meta_data["upstream_parameters"]["provider"] == {"only": ["host-a"]}
    from datetime import UTC, datetime, timedelta

    rows = crud_api_usage.export_gateway_rows(
        db_session,
        account_id=test_user.account_id,
        start=datetime.now(UTC) - timedelta(hours=1),
        end=datetime.now(UTC) + timedelta(hours=1),
        limit=10,
    )
    assert rows[0]["upstream_provider"] == "Host A"
    assert rows[0]["upstream_request_id"] == "gen-persisted"
    assert set(rows[0]) == {
        "id",
        "timestamp",
        "model_alias",
        "provider_name",
        "upstream_provider",
        "upstream_request_id",
        "status_code",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "estimated_cost",
        "upstream_parameters",
    }


@pytest.mark.parametrize(
    "body,expected",
    [
        (
            {
                "model": "example",
                "text": {
                    "format": {
                        "type": "json_schema",
                        "strict": True,
                        "schema": {"private": "data"},
                    }
                },
                "reasoning": {"effort": "high", "summary": "private"},
                "max_output_tokens": 256,
            },
            {
                "output_format": {"type": "json_schema", "strict": True},
                "reasoning_effort": "high",
                "max_tokens": 256,
            },
        ),
        (
            {
                "model": "example",
                "output_config": {
                    "format": {"type": "json_schema", "schema": {"private": "data"}},
                    "effort": "high",
                },
                "thinking": {"type": "adaptive"},
                "max_tokens": 256,
            },
            {
                "output_format": {"type": "json_schema", "strict": False},
                "reasoning_effort": "high",
                "max_tokens": 256,
            },
        ),
        (
            {
                "model": "example",
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseSchema": {"private": "data"},
                    "maxOutputTokens": 256,
                    "thinkingConfig": {"thinkingBudget": 128},
                },
                "tools": [{"functionDeclarations": [{"name": "a"}, {"name": "b"}]}],
            },
            {
                "output_format": {"type": "json_schema", "strict": False},
                "max_tokens": 256,
                "tool_count": 2,
            },
        ),
        (
            {
                "model": "example",
                "outputConfig": {
                    "textFormat": {
                        "type": "json_schema",
                        "structure": {"jsonSchema": {"schema": "private"}},
                    }
                },
                "inferenceConfig": {"maxTokens": 256},
                "additionalModelRequestFields": {
                    "thinking": {"type": "enabled", "budget_tokens": 128}
                },
                "toolConfig": {"tools": [{"toolSpec": {"name": "private"}}]},
            },
            {
                "output_format": {"type": "json_schema", "strict": False},
                "max_tokens": 256,
                "thinking": {"type": "enabled", "budget_tokens": 128},
            },
        ),
    ],
)
def test_native_protocol_summaries_are_content_free(body: dict, expected: dict) -> None:
    from preloop.services.upstream_request_summary import summarize_upstream_request

    summary = summarize_upstream_request(body)
    for key, value in expected.items():
        assert summary[key] == value
    assert "private" not in str(summary)
