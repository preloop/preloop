import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from pytest_mock import MockerFixture

from preloop.api.endpoints import ai_models
from preloop.schemas.ai_model import (
    AIModelCreate,
    AIModelRead,
    AIModelUpdate,
    AvailableModelsResponse,
)
from preloop.models.models.account import Account
from preloop.schemas.ai_model_pricing import AIModelPrice, AIModelPricingResponse
from preloop.services.ai_model_pricing import (
    PriceFetchUnavailableError,
    PriceFetchUnsupportedError,
)

from tests.conftest import maybe_await


@pytest.fixture
def mock_account(mocker: MockerFixture) -> Account:
    """Provides a mock Account object for testing."""
    account = MagicMock(spec=Account)
    account.id = uuid.uuid4()
    account.account_id = uuid.uuid4()
    account.email = "test@example.com"
    return account


@pytest.mark.asyncio
async def test_create_ai_model(mock_account: Account, mocker: MockerFixture):
    """Tests that an AI model is created correctly."""
    # Arrange
    ai_model_in = AIModelCreate(
        name="Test AI Model",
        description="A test AI model",
        provider_name="openai",
        model_identifier="gpt-5.4",
        api_key="test_key",
    )

    mock_crud_ai_model = mocker.patch(
        "preloop.api.endpoints.ai_models.crud_ai_model",
        new_callable=MagicMock,
    )
    mock_crud_ai_model.create_with_account.return_value = AIModelRead(
        id=uuid.uuid4(),
        name=ai_model_in.name,
        description=ai_model_in.description,
        provider_name=ai_model_in.provider_name,
        model_identifier=ai_model_in.model_identifier,
        api_endpoint=ai_model_in.api_endpoint,
        is_default=False,
        model_parameters=None,
        meta_data=None,
        account_id=str(mock_account.id),
        credentials_secret_id=uuid.uuid4(),
        credentials_backend_type="local_encrypted",
        has_api_key=True,
    )

    # Act
    result = await maybe_await(
        ai_models.create_ai_model(
            db=MagicMock(),
            ai_model_in=ai_model_in,
            current_user=mock_account,
        )
    )

    # Assert
    assert result.name == ai_model_in.name
    mock_crud_ai_model.create_with_account.assert_called_once_with(
        db=mocker.ANY,
        obj_in=ai_model_in.model_dump(),
        account_id=mock_account.account_id,
    )


@pytest.mark.asyncio
async def test_list_ai_models(mock_account: Account, mocker: MockerFixture):
    """Tests that AI models are listed correctly."""
    # Arrange
    mock_crud_ai_model = mocker.patch(
        "preloop.api.endpoints.ai_models.crud_ai_model",
        new_callable=MagicMock,
    )
    mock_crud_ai_model.get_by_account.return_value = []

    # Act
    result = await maybe_await(
        ai_models.list_ai_models(db=MagicMock(), current_user=mock_account)
    )

    # Assert
    assert isinstance(result, list)
    mock_crud_ai_model.get_by_account.assert_called_once_with(
        db=mocker.ANY, account_id=mock_account.account_id
    )


@pytest.mark.asyncio
async def test_get_ai_model(mock_account: Account, mocker: MockerFixture):
    """Tests that a single AI model is read correctly."""
    # Arrange
    model_id = uuid.uuid4()
    mock_crud_ai_model = mocker.patch(
        "preloop.api.endpoints.ai_models.crud_ai_model",
        new_callable=MagicMock,
    )
    mock_db_model = MagicMock()
    mock_db_model.account_id = mock_account.account_id
    mock_crud_ai_model.get.return_value = mock_db_model

    # Act
    result = await maybe_await(
        ai_models.get_ai_model(
            db=MagicMock(), model_id=model_id, current_user=mock_account
        )
    )

    # Assert
    assert result == mock_db_model
    mock_crud_ai_model.get.assert_called_once_with(db=mocker.ANY, id=model_id)


@pytest.mark.asyncio
async def test_update_ai_model(mock_account: Account, mocker: MockerFixture):
    """Tests that an AI model is updated correctly."""
    # Arrange
    model_id = uuid.uuid4()
    ai_model_update = AIModelUpdate(name="Updated Model Name")
    mock_crud_ai_model = mocker.patch(
        "preloop.api.endpoints.ai_models.crud_ai_model",
        new_callable=MagicMock,
    )
    mock_ai_model = MagicMock(account_id=mock_account.account_id)
    mock_crud_ai_model.get.return_value = mock_ai_model
    mock_crud_ai_model.update.return_value = AIModelRead(
        id=model_id,
        name=ai_model_update.name,
        description="A test AI model",
        provider_name="openai",
        model_identifier="gpt-5.4",
        api_endpoint=None,
        is_default=False,
        model_parameters=None,
        meta_data=None,
        account_id=str(mock_account.account_id),
        credentials_secret_id=uuid.uuid4(),
        credentials_backend_type="local_encrypted",
        has_api_key=True,
    )

    # Act
    result = await maybe_await(
        ai_models.update_ai_model(
            db=MagicMock(),
            model_id=model_id,
            ai_model_in=ai_model_update,
            current_user=mock_account,
        )
    )

    # Assert
    assert result.name == ai_model_update.name
    mock_crud_ai_model.get.assert_called_once_with(db=mocker.ANY, id=model_id)
    mock_crud_ai_model.update.assert_called_once_with(
        db=mocker.ANY,
        db_obj=mock_ai_model,
        obj_in=ai_model_update.model_dump(exclude_unset=True),
    )


@pytest.mark.asyncio
async def test_update_ai_model_repoint_foreign_secret_is_400(
    mock_account: Account, mocker: MockerFixture
):
    """Cross-account secret reuse is a 400, not a silent no-op."""
    model_id = uuid.uuid4()
    ai_model_update = AIModelUpdate(credentials_secret_id=uuid.uuid4())
    mock_crud_ai_model = mocker.patch(
        "preloop.api.endpoints.ai_models.crud_ai_model",
        new_callable=MagicMock,
    )
    mock_ai_model = MagicMock(account_id=mock_account.account_id)
    mock_crud_ai_model.get.return_value = mock_ai_model
    mock_crud_ai_model.update.side_effect = ValueError(
        "Referenced credential secret belongs to a different account"
    )

    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            ai_models.update_ai_model(
                db=MagicMock(),
                model_id=model_id,
                ai_model_in=ai_model_update,
                current_user=mock_account,
            )
        )

    assert exc_info.value.status_code == 400
    assert "different account" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_delete_ai_model(mock_account: Account, mocker: MockerFixture):
    """Tests that an AI model is deleted correctly."""
    # Arrange
    model_id = uuid.uuid4()
    mock_crud_ai_model = mocker.patch(
        "preloop.api.endpoints.ai_models.crud_ai_model",
        new_callable=MagicMock,
    )
    mock_ai_model = MagicMock(account_id=mock_account.account_id)
    mock_crud_ai_model.get.return_value = mock_ai_model

    # Act
    await maybe_await(
        ai_models.delete_ai_model(
            db=MagicMock(), model_id=model_id, current_user=mock_account
        )
    )

    # Assert
    mock_crud_ai_model.get.assert_called_once_with(db=mocker.ANY, id=model_id)
    mock_crud_ai_model.remove.assert_called_once_with(db=mocker.ANY, id=model_id)


def test_available_models_post_route_declares_provenance_response_model():
    """The POST discovery route publishes {models, source, error}."""
    route = next(
        r
        for r in ai_models.router.routes
        if r.path == "/ai-models/providers/{provider}/available-models"
        and "POST" in r.methods
    )
    assert route.response_model is AvailableModelsResponse


def test_available_models_response_defaults_and_shape():
    response = AvailableModelsResponse(models=["kimi-k3"], source="live")
    dumped = response.model_dump()
    assert dumped == {"models": ["kimi-k3"], "source": "live", "error": None}


def test_available_models_response_rejects_unknown_source():
    with pytest.raises(ValidationError):
        AvailableModelsResponse(models=[], source="hardcoded")


def test_ai_model_schema_rejects_mixed_inline_and_external_credentials():
    """Schema validation should reject mixing api_key with external secret fields."""
    with pytest.raises(ValidationError) as create_error:
        AIModelCreate(
            name="Mixed Credentials Model",
            provider_name="openai",
            model_identifier="gpt-5.4",
            api_key="inline-key",
            credentials_backend_type="vault_kv_v2",
            credentials_external_ref="providers/openai/team-a",
        )

    with pytest.raises(ValidationError) as update_error:
        AIModelUpdate(
            api_key="inline-key",
            credentials_backend_type="vault_kv_v2",
            credentials_external_ref="providers/openai/team-a",
        )

    assert "api_key cannot be combined with other credential fields" in str(
        create_error.value
    )
    assert "api_key cannot be combined with other credential fields" in str(
        update_error.value
    )


def test_ai_model_schema_accepts_reused_credentials_secret_id():
    """Reusing an existing secret is the supported multi-model-per-key path."""
    secret_id = uuid.uuid4()

    model = AIModelCreate(
        name="Claude Haiku",
        provider_name="anthropic",
        model_identifier="claude-haiku-4-5",
        credentials_secret_id=secret_id,
    )

    assert model.credentials_secret_id == secret_id
    assert model.api_key is None

    update = AIModelUpdate(credentials_secret_id=secret_id)
    assert update.credentials_secret_id == secret_id
    assert update.model_dump(exclude_unset=True)["credentials_secret_id"] == secret_id


@pytest.mark.parametrize(
    "extra",
    [
        {"api_key": "inline-key"},
        {"credential_type": "oauth_openai_codex", "credential_payload": {}},
        {
            "credentials_backend_type": "vault_kv_v2",
            "credentials_external_ref": "providers/anthropic/team-a",
        },
    ],
)
def test_ai_model_schema_rejects_secret_reuse_with_new_credentials(extra):
    """Reusing a secret and supplying new credential material is contradictory."""
    with pytest.raises(ValidationError) as error:
        AIModelCreate(
            name="Confused Model",
            provider_name="anthropic",
            model_identifier="claude-haiku-4-5",
            credentials_secret_id=uuid.uuid4(),
            **extra,
        )

    assert "credentials_secret_id cannot be combined with new credential material" in (
        str(error.value)
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"api_key": "inline-key"},
        {"credential_type": "oauth_openai_codex", "credential_payload": {}},
        {
            "credentials_backend_type": "vault_kv_v2",
            "credentials_external_ref": "providers/anthropic/team-a",
        },
    ],
)
def test_ai_model_update_schema_rejects_secret_repoint_with_new_credentials(extra):
    """Repointing a secret and supplying new credential material is contradictory."""
    with pytest.raises(ValidationError) as error:
        AIModelUpdate(
            credentials_secret_id=uuid.uuid4(),
            **extra,
        )

    assert "credentials_secret_id cannot be combined with new credential material" in (
        str(error.value)
    )


@pytest.mark.asyncio
async def test_fetch_provider_models_500_does_not_leak_exception_text(
    mocker: MockerFixture,
):
    """Regression: an unexpected exception in _fetch_provider_models must
    return a fixed 500 message, never the raw exception text, which can
    contain endpoint URLs or key material."""
    from fastapi import HTTPException

    secret_detail = "Connection to https://api.example.com?key=sk-SUPERSECRET failed"

    mocker.patch(
        "preloop.api.endpoints.ai_models.get_available_models_for_provider",
        side_effect=RuntimeError(secret_detail),
    )

    with pytest.raises(HTTPException) as exc_info:
        await ai_models._fetch_provider_models(
            provider="openai",
            api_key="test-key",
            model_kind="llm",
            api_endpoint=None,
        )

    http_exc = exc_info.value
    assert http_exc.status_code == 500
    assert secret_detail not in http_exc.detail
    assert "Check server logs for details" in http_exc.detail


@pytest.mark.asyncio
async def test_fetch_provider_models_validation_error_is_400():
    """An SSRF-blocked or otherwise invalid api_endpoint is a bad request,
    not an authentication failure: it must map to 400, not 401."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await ai_models._fetch_provider_models(
            provider="openai-compatible",
            api_key="sk-SUPERSECRET-KEY",
            model_kind="llm",
            api_endpoint="http://169.254.169.254/latest/meta-data",
        )

    http_exc = exc_info.value
    assert http_exc.status_code == 400
    assert "private address" in http_exc.detail
    assert "sk-SUPERSECRET-KEY" not in http_exc.detail


@pytest.mark.asyncio
async def test_fetch_provider_models_auth_error_is_401(mocker: MockerFixture):
    """A provider-rejected API key maps to 401 with the fixed message."""
    from fastapi import HTTPException

    from preloop.services.ai_model_provider import ProviderAuthError

    mocker.patch(
        "preloop.api.endpoints.ai_models.get_available_models_for_provider",
        side_effect=ProviderAuthError(
            "Invalid OpenAI API key. Please check your API key and try again."
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await ai_models._fetch_provider_models(
            provider="openai",
            api_key="sk-SUPERSECRET-KEY",
            model_kind="llm",
            api_endpoint=None,
        )

    http_exc = exc_info.value
    assert http_exc.status_code == 401
    assert "Invalid OpenAI API key" in http_exc.detail
    assert "sk-SUPERSECRET-KEY" not in http_exc.detail


@pytest.mark.asyncio
async def test_fetch_provider_models_bare_value_error_is_400(mocker: MockerFixture):
    """Regression: an unexpected internal ValueError used to be mislabeled as
    401 unauthorized. Validation (400) is the safer default."""
    from fastapi import HTTPException

    mocker.patch(
        "preloop.api.endpoints.ai_models.get_available_models_for_provider",
        side_effect=ValueError("some internal invariant broke"),
    )

    with pytest.raises(HTTPException) as exc_info:
        await ai_models._fetch_provider_models(
            provider="openai",
            api_key="sk-SUPERSECRET-KEY",
            model_kind="llm",
            api_endpoint=None,
        )

    http_exc = exc_info.value
    assert http_exc.status_code == 400
    assert "sk-SUPERSECRET-KEY" not in http_exc.detail


@pytest.mark.asyncio
async def test_fetch_provider_models_forwards_aws_credentials(
    mocker: MockerFixture,
):
    """Bedrock discovery forwards AWS credential fields to the service layer."""
    from fastapi import HTTPException

    from preloop.schemas.ai_model import AvailableModelsRequest

    mock = mocker.patch(
        "preloop.api.endpoints.ai_models.get_available_models_for_provider",
        side_effect=ValueError("stop here"),
    )
    request_in = AvailableModelsRequest(
        model_kind="llm",
        aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
        aws_secret_access_key="shhh",
        aws_session_token="tok",
        aws_region_name="eu-west-1",
    )

    with pytest.raises(HTTPException):
        await ai_models._fetch_provider_models(
            provider="bedrock",
            api_key=None,
            model_kind="llm",
            api_endpoint=None,
            aws_auth=ai_models._aws_auth_from_request(request_in),
        )

    assert mock.call_args.kwargs["aws_auth"] == {
        "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
        "aws_secret_access_key": "shhh",
        "aws_session_token": "tok",
        "aws_region_name": "eu-west-1",
    }


def test_available_models_request_masks_aws_secrets():
    """AWS credential material never survives serialization (logs, traces)."""
    from preloop.schemas.ai_model import AvailableModelsRequest

    request_in = AvailableModelsRequest(
        api_key="sk-live-key",
        aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
        aws_secret_access_key="shhh",
        aws_session_token="tok",
    )
    dumped = request_in.model_dump()
    assert dumped["api_key"] == "***"
    serialized = request_in.model_dump_json()
    assert "AKIAIOSFODNN7EXAMPLE" not in serialized
    assert "shhh" not in serialized
    assert '"***"' in serialized


@pytest.mark.asyncio
async def test_sync_ai_model_catalog_endpoint_maps_service_results(
    mock_account: Account, mocker: MockerFixture
):
    """The sync endpoint forwards the provider filter / dry_run flag to the
    service and maps its per-provider results into the response schema."""
    from preloop.schemas.ai_model import AIModelCatalogSyncRequest
    from preloop.services.ai_model_catalog_sync import (
        CatalogSyncSummary,
        ProviderCatalogSyncResult,
    )

    mock_sync = mocker.patch(
        "preloop.api.endpoints.ai_models.sync_account_model_catalog",
        return_value=CatalogSyncSummary(
            providers=[
                ProviderCatalogSyncResult(
                    provider="anthropic",
                    source="live",
                    discovered=3,
                    added=["anthropic/claude-fable-5-1-20260901"],
                    skipped_existing=2,
                )
            ],
            dry_run=True,
        ),
    )

    result = await maybe_await(
        ai_models.sync_ai_model_catalog(
            request=MagicMock(),
            request_in=AIModelCatalogSyncRequest(provider="anthropic", dry_run=True),
            db=MagicMock(),
            current_user=mock_account,
        )
    )

    assert result.dry_run is True
    assert result.providers[0].provider == "anthropic"
    assert result.providers[0].added == ["anthropic/claude-fable-5-1-20260901"]
    assert result.providers[0].skipped_existing == 2
    kwargs = mock_sync.call_args.kwargs
    assert kwargs["provider"] == "anthropic"
    assert kwargs["dry_run"] is True


@pytest.mark.asyncio
async def test_available_models_on_subscription_oauth_model_never_calls_provider(
    mock_account: Account, mocker: MockerFixture
):
    """Founder repro: fetching available models on a Claude Code
    subscription-OAuth model must not 401 with an API-key error. The server
    never initiates provider calls with a principal-bound OAuth token
    (Anthropic fingerprints Claude Code OAuth traffic and can invalidate the
    subscription); it answers from the account catalog with an honest
    fallback reason instead."""
    from preloop.schemas.ai_model import AvailableModelsRequest

    oauth_model = MagicMock()
    oauth_model.provider_name = "anthropic"
    oauth_model.api_endpoint = ""
    oauth_model.is_principal_bound_oauth = True

    catalog_sibling = MagicMock()
    catalog_sibling.provider_name = "anthropic"
    catalog_sibling.model_identifier = "claude-fable-5-20260415"
    other_provider = MagicMock()
    other_provider.provider_name = "openai"
    other_provider.model_identifier = "gpt-5.4"

    mock_crud = mocker.patch(
        "preloop.api.endpoints.ai_models.crud_ai_model", new_callable=MagicMock
    )
    mock_crud.get_for_account.return_value = oauth_model
    mock_crud.get_by_account.return_value = [catalog_sibling, other_provider]
    mock_discovery = mocker.patch(
        "preloop.api.endpoints.ai_models.get_available_models_for_provider"
    )

    result = await maybe_await(
        ai_models.list_provider_available_models(
            provider="anthropic",
            request_in=AvailableModelsRequest(ai_model_id=uuid.uuid4()),
            db=MagicMock(),
            current_user=mock_account,
        )
    )

    assert result.source == "fallback"
    assert result.error == "subscription_oauth"
    assert result.models == ["claude-fable-5-20260415"]
    # The hard constraint: no upstream call, and the OAuth secret is never
    # even decrypted for listing.
    mock_discovery.assert_not_called()
    mock_crud.resolve_listing_secret.assert_not_called()


@pytest.mark.asyncio
async def test_available_models_typed_key_wins_over_subscription_oauth_model(
    mock_account: Account, mocker: MockerFixture
):
    """A user-typed API key on an OAuth model's edit form still lists live:
    the constraint covers the stored OAuth token, not explicit keys."""
    from preloop.schemas.ai_model import AvailableModelsRequest
    from preloop.services.ai_model_provider import ModelDiscoveryResult

    oauth_model = MagicMock()
    oauth_model.provider_name = "anthropic"
    oauth_model.api_endpoint = ""
    oauth_model.is_principal_bound_oauth = True

    mock_crud = mocker.patch(
        "preloop.api.endpoints.ai_models.crud_ai_model", new_callable=MagicMock
    )
    mock_crud.get_for_account.return_value = oauth_model
    mock_discovery = mocker.patch(
        "preloop.api.endpoints.ai_models.get_available_models_for_provider",
        return_value=ModelDiscoveryResult(
            models=["claude-fable-5-1-20260901"], source="live"
        ),
    )

    result = await maybe_await(
        ai_models.list_provider_available_models(
            provider="anthropic",
            request_in=AvailableModelsRequest(
                ai_model_id=uuid.uuid4(), api_key="sk-ant-typed"
            ),
            db=MagicMock(),
            current_user=mock_account,
        )
    )

    assert result.source == "live"
    assert result.models == ["claude-fable-5-1-20260901"]
    assert mock_discovery.call_args.args[1] == "sk-ant-typed"
    mock_crud.resolve_listing_secret.assert_not_called()


@pytest.mark.asyncio
async def test_get_ai_model_pricing_returns_the_effective_price(
    mock_account: Account, mocker: MockerFixture
):
    """The pricing endpoint answers for the account's own model only."""
    model_id = uuid.uuid4()
    db_model = MagicMock()
    db_model.id = model_id
    db_model.account_id = mock_account.account_id
    mock_crud = mocker.patch(
        "preloop.api.endpoints.ai_models.crud_ai_model", new_callable=MagicMock
    )
    mock_crud.get.return_value = db_model
    resolved = AIModelPricingResponse(
        ai_model_id=str(model_id),
        source="catalog",
        price=AIModelPrice(input_per_1m=3.0, output_per_1m=15.0),
        catalog_key="openrouter/anthropic/claude-sonnet-4",
        fetch_supported=True,
        fetch_provider_label="OpenRouter",
    )
    mocker.patch(
        "preloop.api.endpoints.ai_models.get_effective_pricing", return_value=resolved
    )

    result = await maybe_await(
        ai_models.get_ai_model_pricing(
            model_id=model_id, db=MagicMock(), current_user=mock_account
        )
    )

    assert result.source == "catalog"
    assert result.price.input_per_1m == 3.0


@pytest.mark.asyncio
async def test_fetch_ai_model_pricing_maps_provider_failures(
    mock_account: Account, mocker: MockerFixture
):
    """A provider with no price list is a 400; one that cannot answer is a 502."""
    model_id = uuid.uuid4()
    db_model = MagicMock()
    db_model.id = model_id
    db_model.account_id = mock_account.account_id
    mock_crud = mocker.patch(
        "preloop.api.endpoints.ai_models.crud_ai_model", new_callable=MagicMock
    )
    mock_crud.get.return_value = db_model

    mocker.patch(
        "preloop.api.endpoints.ai_models.fetch_provider_pricing",
        side_effect=PriceFetchUnsupportedError("Anthropic does not publish prices"),
    )
    with pytest.raises(HTTPException) as unsupported:
        await maybe_await(
            ai_models.fetch_ai_model_pricing(
                model_id=model_id, db=MagicMock(), current_user=mock_account
            )
        )
    assert unsupported.value.status_code == 400
    assert "Anthropic" in unsupported.value.detail

    mocker.patch(
        "preloop.api.endpoints.ai_models.fetch_provider_pricing",
        side_effect=PriceFetchUnavailableError("price list unreachable"),
    )
    with pytest.raises(HTTPException) as unavailable:
        await maybe_await(
            ai_models.fetch_ai_model_pricing(
                model_id=model_id, db=MagicMock(), current_user=mock_account
            )
        )
    assert unavailable.value.status_code == 502


def test_ai_models_credential_health_and_secret_redaction(mock_account, mocker):
    """GET /api/v1/ai-models and /ai-models/{id} return the five new credential health fields.

    Also asserts that no secret material (access, refresh, encrypted_value,
    external_ref, api_key) appears in any response.
    """
    from datetime import datetime, timezone
    from fastapi.testclient import TestClient
    from preloop.api.app import create_app
    from preloop.models.db.session import get_db_session
    from preloop.api.auth import get_current_active_user
    from preloop.models.models.ai_model import AIModel
    from preloop.models.models.secret_reference import SecretReference

    now = datetime.now(timezone.utc)
    failed_iso = now.isoformat()

    secret_error = SecretReference(
        id=uuid.uuid4(),
        account_id=mock_account.account_id,
        name="Codex Secret Error",
        backend_type="local_encrypted",
        secret_kind="ai_model_credentials",
        status="error",
        encrypted_value="secret-token-payload",
        external_ref="secret/vault/path",
        last_verified_at=now,
        meta_data={
            "credential_type": "oauth_openai_codex",
            "last_refresh_error": "openai refresh failed (status=401, code=invalid_grant)",
            "last_refresh_code": "invalid_grant",
            "last_refresh_status_code": 401,
            "last_refresh_failed_at": failed_iso,
        },
    )

    model_error = AIModel(
        id=uuid.uuid4(),
        account_id=mock_account.account_id,
        name="Codex Model Error",
        provider_name="openai",
        model_identifier="gpt-5.5",
        is_default=False,
        created_at=now,
        updated_at=now,
    )
    model_error.credentials_secret = secret_error

    secret_active = SecretReference(
        id=uuid.uuid4(),
        account_id=mock_account.account_id,
        name="Claude Secret Active",
        backend_type="local_encrypted",
        secret_kind="ai_model_credentials",
        status="active",
        encrypted_value="active-token-payload",
        external_ref="secret/vault/active",
        last_verified_at=now,
        meta_data={
            "credential_type": "oauth_anthropic_claude_code",
        },
    )

    model_active = AIModel(
        id=uuid.uuid4(),
        account_id=mock_account.account_id,
        name="Claude Model Active",
        provider_name="anthropic",
        model_identifier="claude-sonnet-4",
        is_default=False,
        created_at=now,
        updated_at=now,
    )
    model_active.credentials_secret = secret_active

    mock_crud = mocker.patch("preloop.api.endpoints.ai_models.crud_ai_model")
    mock_crud.get_by_account.return_value = [model_error, model_active]
    mock_crud.get.side_effect = lambda db, id: (
        model_error
        if id == model_error.id
        else (model_active if id == model_active.id else None)
    )

    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: mocker.MagicMock()
    app.dependency_overrides[get_current_active_user] = lambda: mock_account

    client = TestClient(app)

    # 1. GET /api/v1/ai-models
    res_list = client.get("/api/v1/ai-models")
    assert res_list.status_code == 200
    list_json = res_list.json()
    assert len(list_json) == 2

    error_row = next(m for m in list_json if m["id"] == str(model_error.id))
    active_row = next(m for m in list_json if m["id"] == str(model_active.id))

    assert error_row["credentials_status"] == "error"
    assert (
        error_row["credentials_last_error"]
        == "openai refresh failed (status=401, code=invalid_grant)"
    )
    assert error_row["credentials_last_error_code"] == "invalid_grant"
    assert error_row["credentials_last_failed_at"] is not None
    assert error_row["credentials_last_verified_at"] is not None

    assert active_row["credentials_status"] == "active"
    assert active_row["credentials_last_error"] is None
    assert active_row["credentials_last_error_code"] is None
    assert active_row["credentials_last_failed_at"] is None
    assert active_row["credentials_last_verified_at"] is not None

    # 2. GET /api/v1/ai-models/{id} (error)
    res_detail_err = client.get(f"/api/v1/ai-models/{model_error.id}")
    assert res_detail_err.status_code == 200
    detail_err_json = res_detail_err.json()

    assert detail_err_json["credentials_status"] == "error"
    assert (
        detail_err_json["credentials_last_error"]
        == "openai refresh failed (status=401, code=invalid_grant)"
    )
    assert detail_err_json["credentials_last_error_code"] == "invalid_grant"
    assert detail_err_json["credentials_last_failed_at"] is not None
    assert detail_err_json["credentials_last_verified_at"] is not None

    # 3. GET /api/v1/ai-models/{id} (active)
    res_detail_act = client.get(f"/api/v1/ai-models/{model_active.id}")
    assert res_detail_act.status_code == 200
    detail_act_json = res_detail_act.json()

    assert detail_act_json["credentials_status"] == "active"
    assert detail_act_json["credentials_last_error"] is None
    assert detail_act_json["credentials_last_error_code"] is None
    assert detail_act_json["credentials_last_failed_at"] is None
    assert detail_act_json["credentials_last_verified_at"] is not None

    # 4. Assert no secret keys appear in any payload
    forbidden_keys = {"access", "refresh", "encrypted_value", "external_ref", "api_key"}
    for payload in list_json + [detail_err_json, detail_act_json]:
        present = forbidden_keys.intersection(payload.keys())
        assert not present, f"Forbidden keys appeared in response: {present}"


_CODEX_EXPORT_SHAPE = {
    "access": "codex-access",
    "refresh": "codex-refresh",
    "account_id": "chatgpt-account",
    "expires": 1893456000000,
}
_CODEX_AUTH_JSON_SHAPE = {
    "access_token": "codex-access",
    "refresh_token": "codex-refresh",
    "id_token": "codex-id",
    "account_id": "chatgpt-account",
}


def _credential_payload_client(mock_account, mocker: MockerFixture):
    """Build a TestClient with the model CRUD and secret service mocked.

    Returns the client, the mocked endpoint CRUD, the mocked secret service
    the real CRUD would write through, and an existing model for PUT.
    """
    from fastapi.testclient import TestClient

    from preloop.api.app import create_app
    from preloop.api.auth import get_current_active_user
    from preloop.models.db.session import get_db_session

    existing = AIModelRead(
        id=uuid.uuid4(),
        name="Codex",
        provider_name="openai-codex",
        model_identifier="gpt-5.5",
        account_id=str(mock_account.account_id),
        credentials_secret_id=uuid.uuid4(),
        credential_type="oauth_openai_codex",
        credentials_status="active",
        has_api_key=True,
    )
    mock_crud = mocker.patch("preloop.api.endpoints.ai_models.crud_ai_model")
    mock_crud.get.return_value = mocker.MagicMock(
        id=existing.id, account_id=mock_account.account_id
    )
    mock_crud.create_with_account.return_value = existing
    mock_crud.update.return_value = existing
    secret_service = mocker.MagicMock()
    mocker.patch(
        "preloop.models.crud.ai_model.get_secret_service",
        return_value=secret_service,
    )

    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: mocker.MagicMock()
    app.dependency_overrides[get_current_active_user] = lambda: mock_account
    return TestClient(app), mock_crud, secret_service, existing


def _create_body(payload: dict) -> dict:
    return {
        "name": "Codex",
        "provider_name": "openai-codex",
        "model_identifier": "gpt-5.5",
        "credential_type": "oauth_openai_codex",
        "credential_payload": payload,
    }


def _assert_payload_422(response, *fragments: str) -> None:
    assert response.status_code == 422, response.text
    messages = " ".join(item["msg"] for item in response.json()["detail"])
    assert "invalid credential_payload for oauth_openai_codex" in messages
    for fragment in fragments:
        assert fragment in messages


def test_post_ai_model_rejects_auth_json_key_names_without_storing(
    mock_account, mocker: MockerFixture
):
    """POST with Codex's own auth.json names is a 422 and writes nothing."""
    client, crud, secrets, _ = _credential_payload_client(mock_account, mocker)

    response = client.post(
        "/api/v1/ai-models", json=_create_body(_CODEX_AUTH_JSON_SHAPE)
    )

    _assert_payload_422(
        response,
        "missing keys: access, refresh, expires",
        "unexpected key 'access_token': use 'access'",
        "unexpected key 'refresh_token': use 'refresh'",
    )
    crud.create_with_account.assert_not_called()
    secrets.create_local_secret_reference.assert_not_called()


def test_put_ai_model_rejects_incomplete_payload_without_touching_secret(
    mock_account, mocker: MockerFixture
):
    """The reproduced case: PUT used to answer 200 and mark the model active."""
    client, crud, secrets, existing = _credential_payload_client(mock_account, mocker)

    response = client.put(
        f"/api/v1/ai-models/{existing.id}",
        json={
            "credential_type": "oauth_openai_codex",
            "credential_payload": _CODEX_AUTH_JSON_SHAPE,
        },
    )

    _assert_payload_422(response, "missing keys: access, refresh, expires")
    crud.update.assert_not_called()
    secrets.create_local_secret_reference.assert_not_called()


def test_put_ai_model_rejects_expires_in_seconds(mock_account, mocker: MockerFixture):
    client, crud, _, existing = _credential_payload_client(mock_account, mocker)

    response = client.put(
        f"/api/v1/ai-models/{existing.id}",
        json={
            "credential_type": "oauth_openai_codex",
            "credential_payload": dict(_CODEX_EXPORT_SHAPE, expires=1893456000),
        },
    )

    _assert_payload_422(response, "expires looks like epoch seconds")
    crud.update.assert_not_called()


def test_post_ai_model_accepts_export_shape(mock_account, mocker: MockerFixture):
    client, crud, _, _ = _credential_payload_client(mock_account, mocker)

    response = client.post(
        "/api/v1/ai-models", json=_create_body(dict(_CODEX_EXPORT_SHAPE))
    )

    assert response.status_code == 201, response.text
    _, kwargs = crud.create_with_account.call_args
    assert kwargs["obj_in"]["credential_payload"] == _CODEX_EXPORT_SHAPE


def test_put_ai_model_accepts_export_shape(mock_account, mocker: MockerFixture):
    client, crud, _, existing = _credential_payload_client(mock_account, mocker)

    response = client.put(
        f"/api/v1/ai-models/{existing.id}",
        json={
            "credential_type": "oauth_openai_codex",
            "credential_payload": dict(_CODEX_EXPORT_SHAPE),
        },
    )

    assert response.status_code == 200, response.text
    _, kwargs = crud.update.call_args
    assert kwargs["obj_in"]["credential_payload"] == _CODEX_EXPORT_SHAPE
