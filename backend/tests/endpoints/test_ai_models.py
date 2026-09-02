import uuid
from unittest.mock import MagicMock

import pytest
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
