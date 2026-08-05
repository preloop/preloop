"""Available-models endpoints must never take the provider key in the URL.

Incident (2026-08-04): these endpoints accepted ``api_key`` as a QUERY
PARAMETER, so uvicorn access-logged live provider keys in plaintext:

    "GET /api/v1/ai-models/providers/openrouter/available-models?...&api_key=sk-or-v1-..." 200

Two live OpenRouter keys leaked this way during one onboarding session. The
key now travels in the POST body or the X-Provider-Api-Key header, and the
query parameter is gone rather than merely deprecated.
"""

import pytest
import yaml
from pathlib import Path
from unittest.mock import AsyncMock

from preloop.api.endpoints import ai_models
from preloop.schemas.ai_model import AvailableModelsRequest
from preloop.services.ai_model_provider import ModelDiscoveryResult

ENDPOINT_PATH = "/ai-models/providers/{provider}/available-models"
SPEC_PATH = "/api/v1" + ENDPOINT_PATH


def _routes_for_path():
    return [r for r in ai_models.router.routes if r.path == ENDPOINT_PATH]


def test_endpoint_is_registered_for_get_and_post():
    methods = set()
    for route in _routes_for_path():
        methods |= route.methods
    assert {"GET", "POST"} <= methods


def test_no_route_accepts_api_key_as_query_parameter():
    """The regression that leaked the keys, pinned at the route level."""
    for route in _routes_for_path():
        for param in route.dependant.query_params:
            assert param.name != "api_key", (
                f"{route.methods} {route.path} still accepts api_key as a "
                "query parameter; it would be written to access logs"
            )


def test_api_key_is_a_body_field_on_the_post_route():
    assert "api_key" in AvailableModelsRequest.model_fields


def test_serialized_request_never_carries_the_key():
    """Anything that dumps this model (a log, a trace, an error report) is safe."""
    payload = AvailableModelsRequest(api_key="sk-or-v1-secret-value")
    dumped = payload.model_dump()
    assert dumped["api_key"] == "***"
    assert "sk-or-v1-secret-value" not in str(dumped)
    assert "sk-or-v1-secret-value" not in payload.model_dump_json()


def test_published_openapi_spec_has_no_api_key_query_parameter():
    """Guards the contract clients generate from, not just the running app."""
    spec_file = Path(__file__).resolve().parents[3] / "openapi.yaml"
    spec = yaml.safe_load(spec_file.read_text(encoding="utf-8"))
    operations = spec["paths"][SPEC_PATH]

    for method, operation in operations.items():
        for param in operation.get("parameters", []) or []:
            assert not (param["in"] == "query" and param["name"] == "api_key"), (
                f"openapi.yaml still documents api_key as a query parameter "
                f"on {method.upper()} {SPEC_PATH}"
            )


def test_both_routes_require_authentication():
    """The endpoint fetches a caller-supplied URL, so it cannot stay public."""
    spec_file = Path(__file__).resolve().parents[3] / "openapi.yaml"
    spec = yaml.safe_load(spec_file.read_text(encoding="utf-8"))
    operations = spec["paths"][SPEC_PATH]

    for method, operation in operations.items():
        assert operation.get("security"), (
            f"{method.upper()} {SPEC_PATH} is unauthenticated"
        )


@pytest.mark.asyncio
async def test_get_route_reads_the_key_from_the_header(mocker):
    fetch = mocker.patch.object(
        ai_models,
        "get_available_models_for_provider",
        new=AsyncMock(
            return_value=ModelDiscoveryResult(models=["a-model"], source="live")
        ),
    )

    result = await ai_models.get_provider_available_models(
        provider="openai-compatible",
        x_provider_api_key="sk-or-v1-secret-value",
        api_endpoint="https://openrouter.ai/api/v1",
        model_kind="llm",
    )

    # The deprecated GET form keeps the bare-list shape for old clients.
    assert result == ["a-model"]
    fetch.assert_awaited_once_with(
        "openai-compatible",
        "sk-or-v1-secret-value",
        "llm",
        "https://openrouter.ai/api/v1",
    )


@pytest.mark.asyncio
async def test_post_route_reads_the_key_from_the_body(mocker):
    fetch = mocker.patch.object(
        ai_models,
        "get_available_models_for_provider",
        new=AsyncMock(
            return_value=ModelDiscoveryResult(
                models=["openrouter/auto-beta"], source="live"
            )
        ),
    )

    result = await ai_models.list_provider_available_models(
        provider="openai-compatible",
        request_in=AvailableModelsRequest(
            api_key="sk-or-v1-secret-value",
            api_endpoint="https://openrouter.ai/api/v1",
        ),
    )

    assert result.models == ["openrouter/auto-beta"]
    assert result.source == "live"
    assert result.error is None
    fetch.assert_awaited_once_with(
        "openai-compatible",
        "sk-or-v1-secret-value",
        "llm",
        "https://openrouter.ai/api/v1",
    )


@pytest.mark.asyncio
async def test_post_route_works_without_a_body(mocker):
    fetch = mocker.patch.object(
        ai_models,
        "get_available_models_for_provider",
        new=AsyncMock(
            return_value=ModelDiscoveryResult(models=["gpt-5.4"], source="fallback")
        ),
    )

    result = await ai_models.list_provider_available_models(
        provider="openai", request_in=None
    )

    assert result.models == ["gpt-5.4"]
    assert result.source == "fallback"
    fetch.assert_awaited_once_with("openai", None, "llm", None)


@pytest.mark.asyncio
async def test_post_response_never_carries_key_or_endpoint_material(mocker):
    """Provenance errors are a fixed vocabulary, never raw exception text.

    Raw provider errors can embed the queried URL and, for query-string
    keys, the key itself (2026-08-04 incident). The response body must not
    contain either even when the live fetch failed.
    """
    mocker.patch.object(
        ai_models,
        "get_available_models_for_provider",
        new=AsyncMock(
            return_value=ModelDiscoveryResult(
                models=["fallback-model"], source="fallback", error="network"
            )
        ),
    )

    result = await ai_models.list_provider_available_models(
        provider="openai-compatible",
        request_in=AvailableModelsRequest(
            api_key="sk-or-v1-secret-value",
            api_endpoint="https://secret-gateway.internal.example/v1",
        ),
    )

    dumped = result.model_dump_json()
    assert "sk-or-v1-secret-value" not in dumped
    assert "secret-gateway.internal.example" not in dumped
    assert result.error == "network"
