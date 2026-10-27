"""Bound hosted reasoning output before reserving budget and calling upstream."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService


def _service() -> OpenAIGatewayService:
    return OpenAIGatewayService(
        MagicMock(),
        ModelGatewayAuthContext(
            token="token", user=SimpleNamespace(id="user", account_id="account")
        ),
    )


def _model(**overrides: object) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "id": "model",
            "provider_name": "qwen",
            "model_identifier": "deepseek-v4.1-flash",
            "model_parameters": None,
            "api_endpoint": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "meta_data": {"hosted": True},
            "account_id": None,
            **overrides,
        }
    )


@pytest.mark.parametrize("stream", [False, True])
def test_hosted_reasoning_default_reaches_budget_and_upstream(stream: bool) -> None:
    service = _service()
    payload = {"model": "qwen/deepseek-v4.1-flash-hosted", "input": "Review"}
    model = _model()
    response = {"choices": [{"message": {"content": "Done"}}]}
    chunks = iter(
        [{"choices": [{"delta": {"content": "Done"}, "finish_reason": "stop"}]}]
    )
    with (
        patch.object(service, "_resolve_requested_model", return_value=model),
        patch.object(service, "_check_budget", return_value=None) as budget,
        patch.object(
            service, "_call_litellm", return_value=chunks if stream else response
        ) as upstream,
        patch.object(service, "_record_gateway_request"),
    ):
        if stream:
            list(service.stream_response(payload))
        else:
            service.create_response(payload)
    assert budget.call_args.args[1]["max_output_tokens"] == 8192
    assert upstream.call_args.kwargs["payload"]["max_output_tokens"] == 8192
    assert "max_output_tokens" not in payload


@pytest.mark.parametrize(
    "field", ["max_tokens", "max_completion_tokens", "max_output_tokens"]
)
def test_explicit_output_limits_survive_chat_translation(field: str) -> None:
    service = _service()
    with patch("preloop.services.openai_gateway.get_secret_service") as secret:
        secret.return_value.resolve_ai_model_credentials.return_value = SimpleNamespace(
            credential_type="api_key", value="synthetic-secret"
        )
        kwargs = service._build_completion_kwargs(
            _model(),
            messages=[{"role": "user", "content": "Review"}],
            payload={field: 256},
            stream=True,
            provider="openai",
        )
    assert kwargs["max_tokens"] == 256


@pytest.mark.parametrize(
    "field", ["max_tokens", "max_completion_tokens", "max_output_tokens"]
)
def test_default_never_expands_explicit_limit(field: str) -> None:
    payload = {field: 256, "input": "Review"}
    assert _service()._responses_output_budget(_model(), payload) is payload


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider_name": "deepseek", "api_endpoint": "https://api.deepseek.com/v1"},
        {"model_identifier": "qwen3.5-plus"},
        {"provider_name": "openai-compatible"},
        {"provider_name": "custom"},
        {"meta_data": {}, "account_id": "byok-account"},
    ],
)
def test_default_does_not_change_other_routes(overrides: dict[str, object]) -> None:
    payload = {"input": "Review"}
    assert _service()._responses_output_budget(_model(**overrides), payload) is payload


def test_default_respects_operator_model_ceiling_and_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.config import settings

    monkeypatch.setattr(settings, "gateway_hosted_reasoning_max_output_tokens", 4096)
    assert (
        _service()._responses_output_budget(_model(), {})["max_output_tokens"] == 4096
    )
    assert (
        _service()._responses_output_budget(
            _model(model_parameters={"max_output_tokens": 512}), {}
        )["max_output_tokens"]
        == 512
    )


def test_reasoning_only_length_reports_exhausted_budget_without_completion() -> None:
    import json

    service = _service()
    chunks = iter(
        [
            {"choices": [{"delta": {"reasoning_content": "Synthetic reasoning"}}]},
            {"choices": [{"delta": {}, "finish_reason": "length"}]},
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 31619,
                    "completion_tokens": 1024,
                    "total_tokens": 32643,
                    "completion_tokens_details": {"reasoning_tokens": 1024},
                },
            },
        ]
    )
    with (
        patch.object(service, "_resolve_requested_model", return_value=_model()),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_call_litellm", return_value=chunks),
        patch.object(service, "_record_gateway_request") as record,
    ):
        events = [
            json.loads(e.split("data: ", 1)[1])
            for e in service.stream_response(
                {
                    "model": "qwen/deepseek-v4.1-flash-hosted",
                    "input": "Review",
                    "max_output_tokens": 1024,
                }
            )
            if "[DONE]" not in e
        ]
    error = next(e for e in events if e.get("type") == "error")
    assert error["code"] == "output_budget_exhausted"
    assert "exhausted the output token budget" in error["message"]
    assert not any(e.get("type") == "response.completed" for e in events)
    assert record.call_args.kwargs["status_code"] == 502


def test_busy_tariff_allowance_fits_small_flow_budget() -> None:
    from datetime import datetime, timezone
    from preloop.services.model_pricing import estimate_ai_model_usage_cost

    # Conservative: all input uncached at the documented busy-hour tariff.
    cost = estimate_ai_model_usage_cost(
        _model(),
        prompt_tokens=31619,
        completion_tokens=8192,
        total_tokens=39811,
        observed_at=datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc),
    )
    assert cost is not None
    assert cost == pytest.approx(0.019316)
    assert cost < 0.20 < 0.25


def test_native_responses_remains_unchanged() -> None:
    payload = {"input": "Review"}
    with patch(
        "preloop.services.openai_gateway.should_use_responses_passthrough",
        return_value=True,
    ):
        assert _service()._responses_output_budget(_model(), payload) is payload


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reason", ["account_budget_exceeded", "flow_budget_exceeded"])
def test_default_allowance_cannot_bypass_budget_denial(
    stream: bool, reason: str
) -> None:
    from dataclasses import fields
    from preloop.services.model_gateway_budget import BudgetCheckResult
    from preloop.services.model_gateway_errors import ModelGatewayAPIError

    service = _service()
    values = {field.name: None for field in fields(BudgetCheckResult)}
    values.update(
        account_current_spend_usd=0.19,
        flow_current_spend_usd=0.19,
        account_limit_usd=0.20,
        flow_limit_usd=0.20,
        estimated_request_cost_usd=0.019316,
        account_estimated_total_usd=0.209316,
        flow_estimated_total_usd=0.209316,
        hard_limit_exceeded=True,
        soft_limit_exceeded=False,
        enforcement_reason=reason,
        pricing_available=True,
    )
    with (
        patch.object(service, "_resolve_requested_model", return_value=_model()),
        patch.object(
            service, "_check_budget", return_value=BudgetCheckResult(**values)
        ) as budget,
        patch.object(service, "_call_litellm") as upstream,
        patch.object(service, "_record_gateway_request"),
    ):
        with pytest.raises(ModelGatewayAPIError) as denied:
            if stream:
                list(service.stream_response({"input": "Review"}))
            else:
                service.create_response({"input": "Review"})
        assert denied.value.status_code == 429
    assert budget.call_args.args[1]["max_output_tokens"] == 8192
    upstream.assert_not_called()


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"max_tokens": 8192, "max_output_tokens": 256}, 256),
        (
            {
                "max_tokens": 8192,
                "max_output_tokens": 512,
                "max_completion_tokens": 256,
            },
            256,
        ),
    ],
)
def test_alias_precedence_matches_budget_estimate(
    payload: dict[str, int], expected: int
) -> None:
    """Multiple aliases must not reserve a small cap then send a larger one."""
    with patch("preloop.services.openai_gateway.get_secret_service") as secret:
        secret.return_value.resolve_ai_model_credentials.return_value = SimpleNamespace(
            credential_type="api_key", value="synthetic-secret"
        )
        kwargs = _service()._build_completion_kwargs(
            _model(), messages=[], payload=payload, stream=True, provider="openai"
        )
    assert kwargs["max_tokens"] == expected


@pytest.mark.parametrize("client_cap,expected_cap", [(None, 8192), (256, 256)])
def test_exhaustion_retains_upstream_evidence_and_effective_controls(
    client_cap: int | None, expected_cap: int
) -> None:
    import json

    service = _service()
    model = _model(
        provider_name="openrouter",
        model_identifier="deepseek/example-reasoning-model",
        api_endpoint="https://openrouter.ai/api/v1",
        model_parameters={"max_output_tokens": 8192},
        meta_data={},
        account_id="account",
    )
    reported_usage = {
        "prompt_tokens": 100,
        "completion_tokens": expected_cap,
        "total_tokens": 100 + expected_cap,
        "completion_tokens_details": {"reasoning_tokens": expected_cap},
    }

    def upstream(*args: object, **kwargs: object) -> object:
        service._build_completion_kwargs(
            model,
            messages=[],
            payload=kwargs["payload"],
            stream=True,
            provider="openai",
        )
        return iter(
            [
                {
                    "id": "gen-example",
                    "choices": [{"delta": {}, "finish_reason": "length"}],
                },
                {"id": "gen-example", "choices": [], "usage": reported_usage},
            ]
        )

    payload = {
        "model": "example-model",
        "input": "Review",
        "reasoning": {"effort": "low", "summary": "auto"},
    }
    if client_cap is not None:
        payload["max_output_tokens"] = client_cap
    with (
        patch.object(service, "_resolve_requested_model", return_value=model),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_call_litellm", side_effect=upstream),
        patch.object(service, "_record_gateway_request") as record,
        patch("preloop.services.openai_gateway.get_secret_service") as secret,
    ):
        secret.return_value.resolve_ai_model_credentials.return_value = SimpleNamespace(
            credential_type="api_key", value="synthetic-secret"
        )
        events = [
            json.loads(event.split("data: ", 1)[1])
            for event in service.stream_response(payload)
            if "[DONE]" not in event
        ]
    error = next(event for event in events if event.get("type") == "error")
    recorded = record.call_args.kwargs
    assert recorded["upstream_response"]["usage"] == reported_usage
    assert recorded["upstream_response"]["id"] == "gen-example"
    assert service._extract_finish_reason(recorded["upstream_response"]) == "length"
    assert f"max_output_tokens={expected_cap}" in error["message"]
    assert "reasoning_effort=low" in error["message"]
    assert service._gateway_request_id in error["message"]
    assert "synthetic-secret" not in error["message"]
    assert not any(event.get("type") == "response.completed" for event in events)


def test_success_preserves_real_upstream_id_and_finish_reason() -> None:
    service = _service()
    with (
        patch.object(service, "_resolve_requested_model", return_value=_model()),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(
            service,
            "_call_litellm",
            return_value=iter(
                [
                    {
                        "id": "gen-example",
                        "choices": [
                            {"delta": {"content": "Done"}, "finish_reason": "stop"}
                        ],
                    },
                    {
                        "id": "gen-example",
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 100,
                            "completion_tokens": 10,
                            "total_tokens": 110,
                        },
                    },
                ]
            ),
        ),
        patch.object(service, "_record_gateway_request") as record,
    ):
        list(service.stream_response({"model": "example-model", "input": "Review"}))
        service.flush_deferred_stream_record()
    upstream = record.call_args.kwargs["upstream_response"]
    assert upstream["id"] == "gen-example"
    assert service._extract_finish_reason(upstream) == "stop"
    assert record.call_args.kwargs["response_payload"]["id"].startswith("resp_")


def test_upstream_controls_do_not_leak_into_a_subsequent_request() -> None:
    service = _service()
    with patch("preloop.services.openai_gateway.get_secret_service") as secret:
        secret.return_value.resolve_ai_model_credentials.return_value = SimpleNamespace(
            credential_type="api_key", value="synthetic-secret"
        )
        service._build_completion_kwargs(
            _model(provider_name="openrouter"),
            messages=[],
            payload={"max_tokens": 1024, "reasoning": {"effort": "low"}},
            stream=True,
            provider="openai",
        )
        assert service._last_upstream_parameters == {
            "max_tokens": 1024,
            "reasoning_effort": "low",
        }
        service._build_completion_kwargs(
            _model(), messages=[], payload={}, stream=False, provider="openai"
        )
        assert service._last_upstream_parameters == {}
        service._last_upstream_parameters = {"max_tokens": 1024}
        service._begin_request_accounting()
        assert service._last_upstream_parameters == {}


@pytest.mark.parametrize(
    "completion,reasoning,expected",
    [
        (10, 12, ["reasoning_tokens_exceed_output_tokens"]),
        (10, 10, []),
        (10, 0, []),
        (10, None, []),
        (None, 12, []),
    ],
)
def test_usage_inconsistency_is_flagged_without_inventing_tokens(
    completion: int | None, reasoning: int | None, expected: list[str]
) -> None:
    from copy import deepcopy

    usage = {
        "prompt_tokens": 100,
        "completion_tokens": completion,
        "completion_tokens_details": {"reasoning_tokens": reasoning},
    }
    original = deepcopy(usage)
    normalized = OpenAIGatewayService._normalize_usage(
        usage, prompt_key="prompt_tokens", completion_key="completion_tokens"
    )
    client = OpenAIGatewayService._responses_api_usage(usage, normalized)
    assert OpenAIGatewayService._usage_warnings(usage) == expected
    assert usage == original
    assert client["output_tokens"] == (completion or 0)
    assert client["output_tokens_details"]["reasoning_tokens"] == (reasoning or 0)


@pytest.mark.parametrize("reported_reasoning", [8, 12])
def test_real_litellm_stream_preserves_reported_counters(
    reported_reasoning: int,
) -> None:
    """The adapter accumulator, not only a usage helper, preserves evidence."""
    import json

    import httpx
    import litellm
    from litellm.llms.custom_httpx.http_handler import HTTPHandler

    reported = {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "total_tokens": 110,
        "completion_tokens_details": {"reasoning_tokens": reported_reasoning},
    }
    frames = [
        {
            "id": "gen-example",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "example/model",
            "choices": [
                {
                    "index": 0,
                    "delta": {"reasoning": "Synthetic reasoning"},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "gen-example",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "example/model",
            "choices": [
                {"index": 0, "delta": {"content": "Done"}, "finish_reason": "stop"}
            ],
        },
        {
            "id": "gen-example",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "example/model",
            "choices": [],
            "usage": reported,
        },
    ]
    body = (
        "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
        + "data: [DONE]\n\n"
    )

    def response(*, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            content=body.encode(),
            headers={"content-type": "text/event-stream"},
        )

    with (
        patch.object(HTTPHandler, "post", side_effect=response),
        patch.object(
            httpx.Client, "send", side_effect=AssertionError("Unexpected HTTP request")
        ),
        patch.object(litellm, "disable_streaming_logging", True),
        patch(
            "litellm.litellm_core_utils.streaming_chunk_builder_utils.token_counter",
            return_value=41,
        ),
    ):
        stream = litellm.completion(
            model="openrouter/example/model",
            messages=[{"role": "user", "content": "Example"}],
            api_key="sk-test",
            stream=True,
            stream_options={"include_usage": True},
        )
        chunks = list(stream)
    terminal = next(
        chunk.usage.model_dump()
        for chunk in reversed(chunks)
        if getattr(chunk, "usage", None)
    )
    retained = next(
        chunk.usage.model_dump()
        for chunk in reversed(stream.chunks)
        if getattr(chunk, "usage", None)
    )
    for usage in (retained, terminal):
        assert usage["completion_tokens"] == 10
        assert (
            usage["completion_tokens_details"]["reasoning_tokens"] == reported_reasoning
        )
    service = _service()
    normalized = service._normalize_usage(
        terminal, prompt_key="prompt_tokens", completion_key="completion_tokens"
    )
    client = service._responses_api_usage(terminal, normalized)
    assert client["output_tokens"] == 10
    assert client["output_tokens_details"]["reasoning_tokens"] == reported_reasoning
    assert service._usage_warnings(terminal) == (
        ["reasoning_tokens_exceed_output_tokens"] if reported_reasoning > 10 else []
    )


@pytest.mark.parametrize("effort", ["low", "max"])
@pytest.mark.parametrize("location", ["reasoning_effort", "reasoning", "extra_body"])
def test_model_studio_effective_effort_is_recorded(effort: str, location: str) -> None:
    service = _service()
    payload = (
        {
            "reasoning_effort": effort,
        }
        if location == "reasoning_effort"
        else {
            location: {
                "effort" if location == "reasoning" else "reasoning_effort": effort
            }
        }
    )
    with patch("preloop.services.openai_gateway.get_secret_service") as secret:
        secret.return_value.resolve_ai_model_credentials.return_value = SimpleNamespace(
            credential_type="api_key", value="synthetic-secret"
        )
        kwargs = service._build_completion_kwargs(
            _model(), messages=[], payload=payload, stream=True, provider="openai"
        )
    assert kwargs["extra_body"]["reasoning_effort"] == effort
    assert service._last_upstream_parameters == {"reasoning_effort": effort}
