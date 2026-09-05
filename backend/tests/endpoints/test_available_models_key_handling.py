"""Available-models endpoints must never take the provider key in the URL.

Incident (2026-08-04): these endpoints accepted ``api_key`` as a QUERY
PARAMETER, so uvicorn access-logged live provider keys in plaintext:

    "GET /api/v1/ai-models/providers/openrouter/available-models?...&api_key=sk-or-v1-..." 200

Two live OpenRouter keys leaked this way during one onboarding session. The
key now travels in the POST body or the X-Provider-Api-Key header, and the
query parameter is gone rather than merely deprecated.
"""

import pytest
import uuid
import yaml
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException

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
        aws_auth=None,
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
        aws_auth=None,
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
    fetch.assert_awaited_once_with("openai", None, "llm", None, aws_auth=None)


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


def _stored_api_key_model(*, provider_name: str = "zai") -> MagicMock:
    """Stored model mock for the API-key listing path.

    MagicMock is truthy, so ``is_principal_bound_oauth`` must be False
    or the OAuth short-circuit skips decrypt and never hits live listing.
    """
    stored = MagicMock()
    stored.provider_name = provider_name
    stored.api_endpoint = None
    stored.meta_data = {}
    stored.is_principal_bound_oauth = False
    return stored


def test_ai_model_id_is_a_body_field_on_the_post_route():
    assert "ai_model_id" in AvailableModelsRequest.model_fields


@pytest.mark.asyncio
async def test_post_edit_refresh_decrypts_stored_key(mocker):
    """Edit-mode refresh sends the model id; the server decrypts the key."""
    model_id = uuid.uuid4()
    user = MagicMock()
    user.account_id = uuid.uuid4()
    stored = _stored_api_key_model()
    mocker.patch.object(ai_models.crud_ai_model, "get_for_account", return_value=stored)
    mocker.patch.object(
        ai_models.crud_ai_model,
        "resolve_listing_secret",
        return_value="stored-zai-key",
    )
    fetch = mocker.patch.object(
        ai_models,
        "get_available_models_for_provider",
        new=AsyncMock(
            return_value=ModelDiscoveryResult(
                models=["glm-5.3", "glm-5.3-flash"], source="live"
            )
        ),
    )

    result = await ai_models.list_provider_available_models(
        provider="zai",
        request_in=AvailableModelsRequest(ai_model_id=model_id),
        db=MagicMock(),
        current_user=user,
    )

    assert result.models == ["glm-5.3", "glm-5.3-flash"]
    assert result.source == "live"
    assert "stored-zai-key" not in result.model_dump_json()
    fetch.assert_awaited_once_with("zai", "stored-zai-key", "llm", None, aws_auth=None)


@pytest.mark.asyncio
async def test_post_typed_key_wins_over_stored_key(mocker):
    model_id = uuid.uuid4()
    user = MagicMock()
    user.account_id = uuid.uuid4()
    stored = _stored_api_key_model()
    mocker.patch.object(ai_models.crud_ai_model, "get_for_account", return_value=stored)
    mocker.patch.object(
        ai_models.crud_ai_model,
        "resolve_listing_secret",
        return_value="stored-zai-key",
    )
    fetch = mocker.patch.object(
        ai_models,
        "get_available_models_for_provider",
        new=AsyncMock(
            return_value=ModelDiscoveryResult(models=["glm-5.3-flash"], source="live")
        ),
    )

    result = await ai_models.list_provider_available_models(
        provider="zai",
        request_in=AvailableModelsRequest(
            ai_model_id=model_id, api_key="typed-zai-key"
        ),
        db=MagicMock(),
        current_user=user,
    )

    assert result.models == ["glm-5.3-flash"]
    fetch.assert_awaited_once_with("zai", "typed-zai-key", "llm", None, aws_auth=None)
    assert "stored-zai-key" not in result.model_dump_json()
    assert "typed-zai-key" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_post_unknown_stored_model_is_404(mocker):
    user = MagicMock()
    user.account_id = uuid.uuid4()
    mocker.patch.object(ai_models.crud_ai_model, "get_for_account", return_value=None)

    with pytest.raises(HTTPException) as exc_info:
        await ai_models.list_provider_available_models(
            provider="zai",
            request_in=AvailableModelsRequest(ai_model_id=uuid.uuid4()),
            db=MagicMock(),
            current_user=user,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_post_provider_mismatch_never_decrypts_the_stored_key(mocker):
    """A stored key is only usable for the provider it was stored for.

    The edit form leaves the provider dropdown enabled, so a caller can pair
    a Z.ai model id with ``provider=custom`` and an attacker-controlled
    ``api_endpoint``. ``validate_discovery_endpoint`` blocks private hosts but
    not public ones, so without this check the decrypted Z.ai key would be
    forwarded to that URL.
    """
    user = MagicMock()
    user.account_id = uuid.uuid4()
    stored = _stored_api_key_model()
    mocker.patch.object(ai_models.crud_ai_model, "get_for_account", return_value=stored)
    resolve = mocker.patch.object(
        ai_models.crud_ai_model,
        "resolve_listing_secret",
        return_value="stored-zai-key",
    )
    fetch = mocker.patch.object(
        ai_models,
        "get_available_models_for_provider",
        new=AsyncMock(
            return_value=ModelDiscoveryResult(models=["a-model"], source="live")
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await ai_models.list_provider_available_models(
            provider="custom",
            request_in=AvailableModelsRequest(
                ai_model_id=uuid.uuid4(),
                api_endpoint="https://attacker.example/v1",
            ),
            db=MagicMock(),
            current_user=user,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == (
        "Stored model provider does not match the requested provider"
    )
    resolve.assert_not_called()
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_provider_match_is_case_insensitive(mocker):
    """Provider casing from the path must not break a legitimate refresh."""
    user = MagicMock()
    user.account_id = uuid.uuid4()
    stored = _stored_api_key_model(provider_name="ZAI")
    mocker.patch.object(ai_models.crud_ai_model, "get_for_account", return_value=stored)
    mocker.patch.object(
        ai_models.crud_ai_model,
        "resolve_listing_secret",
        return_value="stored-zai-key",
    )
    fetch = mocker.patch.object(
        ai_models,
        "get_available_models_for_provider",
        new=AsyncMock(
            return_value=ModelDiscoveryResult(models=["glm-5.3"], source="live")
        ),
    )

    result = await ai_models.list_provider_available_models(
        provider="zai",
        request_in=AvailableModelsRequest(ai_model_id=uuid.uuid4()),
        db=MagicMock(),
        current_user=user,
    )

    assert result.models == ["glm-5.3"]
    fetch.assert_awaited_once_with("zai", "stored-zai-key", "llm", None, aws_auth=None)


@pytest.mark.asyncio
async def test_post_unexpected_secret_error_is_not_swallowed(mocker):
    """Only expected decryption failures degrade to an unauthenticated list.

    Expected failures all normalize to ValueError. A programming bug (say an
    AttributeError after a schema change) must surface instead of quietly
    turning into a "missing_key" fallback.
    """
    user = MagicMock()
    user.account_id = uuid.uuid4()
    stored = _stored_api_key_model()
    mocker.patch.object(ai_models.crud_ai_model, "get_for_account", return_value=stored)
    mocker.patch.object(
        ai_models.crud_ai_model,
        "resolve_listing_secret",
        side_effect=AttributeError("credentials_secret vanished"),
    )

    with pytest.raises(AttributeError):
        await ai_models.list_provider_available_models(
            provider="zai",
            request_in=AvailableModelsRequest(ai_model_id=uuid.uuid4()),
            db=MagicMock(),
            current_user=user,
        )


@pytest.mark.asyncio
async def test_post_undecryptable_secret_still_falls_back(mocker):
    """An unreadable stored secret still degrades gracefully."""
    user = MagicMock()
    user.account_id = uuid.uuid4()
    stored = _stored_api_key_model()
    mocker.patch.object(ai_models.crud_ai_model, "get_for_account", return_value=stored)
    mocker.patch.object(
        ai_models.crud_ai_model,
        "resolve_listing_secret",
        side_effect=ValueError("Unable to decrypt value"),
    )
    fetch = mocker.patch.object(
        ai_models,
        "get_available_models_for_provider",
        new=AsyncMock(
            return_value=ModelDiscoveryResult(
                models=[], source="fallback", error="missing_key"
            )
        ),
    )

    result = await ai_models.list_provider_available_models(
        provider="zai",
        request_in=AvailableModelsRequest(ai_model_id=uuid.uuid4()),
        db=MagicMock(),
        current_user=user,
    )

    assert result.error == "missing_key"
    fetch.assert_awaited_once_with("zai", None, "llm", None, aws_auth=None)
