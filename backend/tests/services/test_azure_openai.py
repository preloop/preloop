"""Tests for Azure OpenAI request shaping and its gateway wiring."""

import json
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from preloop.models.models.ai_model import AIModel
from preloop.services.azure_openai import (
    azure_api_version,
    azure_request_kwargs,
    is_azure_model,
    normalize_azure_endpoint,
)
from preloop.services.litellm_routing import preloop_user_agent, to_litellm_model
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_pricing import estimate_ai_model_usage_cost_detailed
from preloop.services.openai_gateway import OpenAIGatewayService

RESOURCE = "https://example-resource.openai.azure.com"


def _azure_model(
    endpoint: Optional[str] = RESOURCE,
    *,
    identifier: str = "chat-deployment",
    runtime: Optional[Dict[str, Any]] = None,
) -> SimpleNamespace:
    meta: Dict[str, Any] = {}
    if runtime is not None:
        meta["provider_runtime"] = runtime
    return SimpleNamespace(
        provider_name="azure",
        model_identifier=identifier,
        api_endpoint=endpoint,
        meta_data=meta,
    )


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        (RESOURCE, (RESOURCE, None)),
        (f"{RESOURCE}/", (RESOURCE, None)),
        ("example-resource.openai.azure.com", (RESOURCE, None)),
        (
            f"{RESOURCE}/openai/deployments/chat-deployment/chat/completions"
            "?api-version=2024-10-21",
            (RESOURCE, "2024-10-21"),
        ),
        (f"{RESOURCE}/openai/v1/", (RESOURCE, "v1")),
        (f"{RESOURCE}/openai/v1?api-version=preview", (RESOURCE, "preview")),
        (f"{RESOURCE}/openai", (RESOURCE, None)),
        (
            "https://example.cognitiveservices.azure.com/openai/deployments/x",
            ("https://example.cognitiveservices.azure.com", None),
        ),
        # Proxy and API Management prefixes are kept; only the Azure suffix
        # LiteLLM appends itself is removed.
        (
            "https://gw.example.com/tenants/openai/v1",
            ("https://gw.example.com/tenants", "v1"),
        ),
        (
            "https://gw.example.com/openai/openai/deployments/x/chat/completions",
            ("https://gw.example.com/openai", None),
        ),
        # Not an Azure suffix: a lookalike segment and a bare proxy path.
        (
            "https://gw.example.com/openai-gw/tenant",
            ("https://gw.example.com/openai-gw/tenant", None),
        ),
        (
            "https://gw.example.com/openai/tenant",
            ("https://gw.example.com/openai/tenant", None),
        ),
        # A deployment named "openai" does not move the cut.
        (
            f"{RESOURCE}/openai/deployments/openai/chat/completions",
            (RESOURCE, None),
        ),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_normalize_azure_endpoint(endpoint, expected) -> None:
    """Pasted portal URLs reduce to the resource root plus any version."""
    assert normalize_azure_endpoint(endpoint) == expected


def test_configured_api_version_wins_over_endpoint_hint() -> None:
    model = _azure_model(
        f"{RESOURCE}/openai/deployments/d/chat/completions?api-version=2024-02-01",
        runtime={"api_version": " 2024-10-21 "},
    )
    assert azure_api_version(model) == "2024-10-21"


def test_api_version_falls_back_to_endpoint_hint_then_none() -> None:
    assert azure_api_version(_azure_model(f"{RESOURCE}/openai/v1")) == "v1"
    assert azure_api_version(_azure_model(runtime={"api_version": ""})) is None


def test_request_kwargs_only_for_azure() -> None:
    assert is_azure_model(_azure_model())
    assert (
        azure_request_kwargs(
            SimpleNamespace(provider_name="openai", api_endpoint=RESOURCE, meta_data={})
        )
        == {}
    )
    assert azure_request_kwargs(_azure_model(runtime={"api_version": "v1"})) == {
        "api_base": RESOURCE,
        "api_version": "v1",
    }


def test_azure_routes_deployment_through_litellm_azure_adapter() -> None:
    model = AIModel(provider_name="azure", model_identifier="chat-deployment")
    assert to_litellm_model(model) == "azure/chat-deployment"


def _service(upstream: MagicMock) -> OpenAIGatewayService:
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    return OpenAIGatewayService(MagicMock(), auth_context, upstream_backend=upstream)


def test_gateway_completion_sends_resource_root_and_api_version() -> None:
    """A pasted deployment URL must not reach LiteLLM as the api_base."""
    upstream = MagicMock()
    service = _service(upstream)
    model = _azure_model(
        f"{RESOURCE}/openai/deployments/chat-deployment/chat/completions"
        "?api-version=2024-10-21"
    )
    with patch("preloop.services.openai_gateway.get_secret_service") as secrets:
        secrets.return_value.resolve_ai_model_credentials.return_value = (
            SimpleNamespace(credential_type="api_key", value="azure-key")
        )
        service._call_litellm(
            model,
            messages=[{"role": "user", "content": "ping"}],
            payload={},
            provider="openai",
        )
    upstream.completion.assert_called_once_with(
        model="azure/chat-deployment",
        messages=[{"role": "user", "content": "ping"}],
        api_key="azure-key",
        api_base=RESOURCE,
        api_version="2024-10-21",
        timeout=600,
        drop_params=True,
        extra_headers={"User-Agent": preloop_user_agent()},
    )


def test_gateway_streaming_requests_usage_from_azure() -> None:
    """Streaming keeps include_usage so the usage row carries tokens."""
    service = _service(MagicMock())
    model = _azure_model(runtime={"api_version": "2024-10-21"})
    with patch("preloop.services.openai_gateway.get_secret_service") as secrets:
        secrets.return_value.resolve_ai_model_credentials.return_value = (
            SimpleNamespace(credential_type="api_key", value="azure-key")
        )
        kwargs = service._build_completion_kwargs(
            model,
            messages=[{"role": "user", "content": "ping"}],
            payload={},
            stream=True,
            provider="openai",
        )
    assert kwargs["stream_options"] == {"include_usage": True}
    assert kwargs["api_version"] == "2024-10-21"
    assert kwargs["api_base"] == RESOURCE


def test_gateway_embedding_uses_azure_kwargs() -> None:
    service = _service(MagicMock())
    model = _azure_model(runtime={"api_version": "2024-10-21"})
    with patch("preloop.services.openai_gateway.get_secret_service") as secrets:
        secrets.return_value.resolve_ai_model_credentials.return_value = (
            SimpleNamespace(credential_type="api_key", value="azure-key")
        )
        kwargs = service._build_embedding_kwargs(model, payload={"input": "hi"})
    assert kwargs["model"] == "azure/chat-deployment"
    assert kwargs["api_base"] == RESOURCE
    assert kwargs["api_version"] == "2024-10-21"


def test_gateway_unpacks_aws_blob_for_aws_provider_alias() -> None:
    """``aws`` routes to Bedrock, so its JSON credential must become aws_*."""
    upstream = MagicMock()
    service = _service(upstream)
    model = SimpleNamespace(
        provider_name="aws",
        model_identifier="amazon.nova-micro-v1:0",
        api_endpoint=None,
        meta_data={"provider_runtime": {"region": "eu-west-1"}},
    )
    with patch("preloop.services.openai_gateway.get_secret_service") as secrets:
        secrets.return_value.resolve_ai_model_credentials.return_value = (
            SimpleNamespace(
                credential_type="api_key",
                value=json.dumps(
                    {
                        "aws_access_key_id": "AKIA_TEST",
                        "aws_secret_access_key": "secret-test",
                    }
                ),
            )
        )
        service._call_litellm(
            model,
            messages=[{"role": "user", "content": "ping"}],
            payload={},
            provider="openai",
        )
    kwargs = upstream.completion.call_args.kwargs
    assert kwargs["model"] == "bedrock/converse/amazon.nova-micro-v1:0"
    assert kwargs["aws_access_key_id"] == "AKIA_TEST"
    assert kwargs["aws_region_name"] == "eu-west-1"
    assert "api_key" not in kwargs


def _cost(model: AIModel) -> Any:
    return estimate_ai_model_usage_cost_detailed(
        model, prompt_tokens=1000, completion_tokens=100, total_tokens=1100
    )


def test_azure_deployment_name_is_unpriced_without_base_model() -> None:
    model = AIModel(
        provider_name="azure",
        model_identifier="team-chat-prod",
        meta_data={"gateway": {"model_alias": "azure/team-chat-prod"}},
    )
    assert _cost(model).source == "unpriced"


def test_azure_base_model_prices_a_custom_deployment_name() -> None:
    # The base model is looked up in the Azure namespace first, so it gets
    # the catalog's Azure price for that model.
    priced = AIModel(provider_name="azure", model_identifier="azure/gpt-4o-mini")
    model = AIModel(
        provider_name="azure",
        model_identifier="team-chat-prod",
        meta_data={
            "gateway": {"model_alias": "azure/team-chat-prod"},
            "provider_runtime": {"base_model": "gpt-4o-mini"},
        },
    )
    estimate = _cost(model)
    assert estimate.source == "catalog"
    assert estimate.cost == _cost(priced).cost
    assert estimate.cost and estimate.cost > 0


def test_bedrock_base_model_prices_an_application_inference_profile() -> None:
    arn = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/example"
    unpriced = AIModel(provider_name="bedrock", model_identifier=arn)
    assert _cost(unpriced).source == "unpriced"
    model = AIModel(
        provider_name="bedrock",
        model_identifier=arn,
        meta_data={"provider_runtime": {"base_model": "amazon.nova-micro-v1:0"}},
    )
    estimate = _cost(model)
    assert estimate.source == "catalog"
    assert estimate.cost and estimate.cost > 0
