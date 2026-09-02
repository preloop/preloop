"""Tests for the account model-catalog sync service.

The sync exists so newly released provider models enter the account catalog
without an operator re-adding them in the console: it reuses the live
discovery service with credentials the account already stores. These tests
pin the load-bearing behavior: which models are added, that credentials and
gateway exposure are inherited from the seed model (never widened), that
principal-bound OAuth credentials are never used for discovery, and that
every write lands in the audit trail.
"""

import uuid
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from preloop.services import ai_model_catalog_sync
from preloop.services.ai_model_catalog_sync import sync_account_model_catalog
from preloop.services.ai_model_provider import ModelDiscoveryResult


def _fake_model(
    *,
    provider_name: str = "anthropic",
    model_identifier: str = "claude-fable-5-20260415",
    principal_bound: bool = False,
    gateway_enabled: bool = True,
    secret_id: str | None = None,
) -> MagicMock:
    model = MagicMock()
    model.id = uuid.uuid4()
    model.provider_name = provider_name
    model.model_identifier = model_identifier
    model.api_endpoint = ""
    model.credentials_secret_id = secret_id or str(uuid.uuid4())
    model.is_principal_bound_oauth = principal_bound
    model.meta_data = (
        {
            "gateway": {
                "enabled": True,
                "model_alias": f"{provider_name}/{model_identifier}",
            }
        }
        if gateway_enabled
        else {}
    )
    return model


def _fake_user() -> MagicMock:
    user = MagicMock()
    user.id = uuid.uuid4()
    user.account_id = uuid.uuid4()
    return user


@pytest.fixture
def mock_crud(mocker: MockerFixture) -> MagicMock:
    return mocker.patch.object(ai_model_catalog_sync, "crud_ai_model")


@pytest.fixture
def mock_audit_log(mocker: MockerFixture) -> MagicMock:
    return mocker.patch.object(ai_model_catalog_sync, "crud_audit_log")


@pytest.fixture
def mock_config_audit(mocker: MockerFixture) -> MagicMock:
    return mocker.patch.object(ai_model_catalog_sync, "log_config_change")


@pytest.mark.asyncio
async def test_sync_adds_newly_discovered_models_sharing_seed_credential(
    mock_crud: MagicMock,
    mock_audit_log: MagicMock,
    mock_config_audit: MagicMock,
    mocker: MockerFixture,
):
    """New identifiers become catalog rows on the seed's credential; existing
    identifiers are skipped; the batch commits once; the sync is audited."""
    seed = _fake_model(model_identifier="claude-fable-5-20260415")
    user = _fake_user()
    db = MagicMock()

    mock_crud.get_by_account.return_value = [seed]
    mock_crud.get_for_account.return_value = seed
    mock_crud.resolve_listing_secret.return_value = "sk-ant-live"
    mocker.patch.object(
        ai_model_catalog_sync,
        "get_available_models_for_provider",
        return_value=ModelDiscoveryResult(
            models=["claude-fable-5-1-20260901", "claude-fable-5-20260415"],
            source="live",
        ),
    )

    summary = await sync_account_model_catalog(db, user=user)

    assert len(summary.providers) == 1
    result = summary.providers[0]
    assert result.provider == "anthropic"
    assert result.source == "live"
    assert result.added == ["anthropic/claude-fable-5-1-20260901"]
    assert result.skipped_existing == 1

    mock_crud.create_with_account.assert_called_once()
    kwargs = mock_crud.create_with_account.call_args.kwargs
    assert kwargs["account_id"] == user.account_id
    assert kwargs["commit"] is False
    created = kwargs["obj_in"]
    assert created["model_identifier"] == "claude-fable-5-1-20260901"
    assert created["provider_name"] == seed.provider_name
    assert created["credentials_secret_id"] == seed.credentials_secret_id
    assert created["meta_data"]["gateway"] == {
        "enabled": True,
        "model_alias": "anthropic/claude-fable-5-1-20260901",
    }
    db.commit.assert_called_once()

    audit_kwargs = mock_audit_log.log_action.call_args.kwargs
    assert audit_kwargs["action"] == "ai_model_catalog_sync"
    assert audit_kwargs["details"]["added"] == ["anthropic/claude-fable-5-1-20260901"]
    mock_config_audit.assert_called_once()


@pytest.mark.asyncio
async def test_sync_dry_run_reports_without_writing(
    mock_crud: MagicMock,
    mock_audit_log: MagicMock,
    mock_config_audit: MagicMock,
    mocker: MockerFixture,
):
    seed = _fake_model()
    mock_crud.get_by_account.return_value = [seed]
    mock_crud.get_for_account.return_value = seed
    mock_crud.resolve_listing_secret.return_value = "sk-ant-live"
    mocker.patch.object(
        ai_model_catalog_sync,
        "get_available_models_for_provider",
        return_value=ModelDiscoveryResult(
            models=["claude-fable-5-1-20260901"], source="live"
        ),
    )
    db = MagicMock()

    summary = await sync_account_model_catalog(db, user=_fake_user(), dry_run=True)

    assert summary.dry_run is True
    assert summary.providers[0].added == ["anthropic/claude-fable-5-1-20260901"]
    mock_crud.create_with_account.assert_not_called()
    db.commit.assert_not_called()
    # A dry run is still audited (details carry dry_run=True).
    assert mock_audit_log.log_action.call_args.kwargs["details"]["dry_run"] is True


@pytest.mark.asyncio
async def test_sync_never_uses_principal_bound_oauth_credentials(
    mock_crud: MagicMock,
    mock_audit_log: MagicMock,
    mock_config_audit: MagicMock,
    mocker: MockerFixture,
):
    """A provider backed only by Claude Code / Codex OAuth is skipped: those
    credentials cannot authenticate discovery and their models are per-agent."""
    oauth_only = _fake_model(principal_bound=True)
    mock_crud.get_by_account.return_value = [oauth_only]
    discovery = mocker.patch.object(
        ai_model_catalog_sync, "get_available_models_for_provider"
    )

    summary = await sync_account_model_catalog(MagicMock(), user=_fake_user())

    result = summary.providers[0]
    assert result.error == "missing_key"
    assert "subscription-OAuth" in (result.note or "")
    assert result.added == []
    discovery.assert_not_called()
    mock_crud.get_for_account.assert_not_called()


@pytest.mark.asyncio
async def test_sync_skips_unbounded_and_interactive_providers(
    mock_crud: MagicMock,
    mock_audit_log: MagicMock,
    mock_config_audit: MagicMock,
    mocker: MockerFixture,
):
    mock_crud.get_by_account.return_value = [
        _fake_model(provider_name="openrouter", model_identifier="meta/llama-4"),
        _fake_model(provider_name="bedrock", model_identifier="anthropic.claude"),
    ]
    discovery = mocker.patch.object(
        ai_model_catalog_sync, "get_available_models_for_provider"
    )

    summary = await sync_account_model_catalog(MagicMock(), user=_fake_user())

    assert {r.provider for r in summary.providers} == {"openrouter", "bedrock"}
    assert all(r.error == "unsupported" for r in summary.providers)
    assert all(r.added == [] for r in summary.providers)
    discovery.assert_not_called()


@pytest.mark.asyncio
async def test_sync_provider_filter_without_models_reports_missing_key(
    mock_crud: MagicMock,
    mock_audit_log: MagicMock,
    mock_config_audit: MagicMock,
):
    mock_crud.get_by_account.return_value = [_fake_model(provider_name="openai")]

    summary = await sync_account_model_catalog(
        MagicMock(), user=_fake_user(), provider="Anthropic"
    )

    assert len(summary.providers) == 1
    assert summary.providers[0].provider == "anthropic"
    assert summary.providers[0].error == "missing_key"


@pytest.mark.asyncio
async def test_sync_adds_nothing_when_discovery_falls_back(
    mock_crud: MagicMock,
    mock_audit_log: MagicMock,
    mock_config_audit: MagicMock,
    mocker: MockerFixture,
):
    seed = _fake_model()
    mock_crud.get_by_account.return_value = [seed]
    mock_crud.get_for_account.return_value = seed
    mock_crud.resolve_listing_secret.return_value = "sk-ant-live"
    mocker.patch.object(
        ai_model_catalog_sync,
        "get_available_models_for_provider",
        return_value=ModelDiscoveryResult(
            models=[], source="fallback", error="network"
        ),
    )

    summary = await sync_account_model_catalog(MagicMock(), user=_fake_user())

    result = summary.providers[0]
    assert result.source == "fallback"
    assert result.error == "network"
    assert result.added == []
    mock_crud.create_with_account.assert_not_called()
    mock_audit_log.log_action.assert_not_called()


@pytest.mark.asyncio
async def test_sync_surfaces_provider_auth_rejection_safely(
    mock_crud: MagicMock,
    mock_audit_log: MagicMock,
    mock_config_audit: MagicMock,
    mocker: MockerFixture,
):
    seed = _fake_model()
    mock_crud.get_by_account.return_value = [seed]
    mock_crud.get_for_account.return_value = seed
    mock_crud.resolve_listing_secret.return_value = "sk-ant-revoked"
    mocker.patch.object(
        ai_model_catalog_sync,
        "get_available_models_for_provider",
        side_effect=ValueError("The provider rejected the API key"),
    )

    summary = await sync_account_model_catalog(MagicMock(), user=_fake_user())

    result = summary.providers[0]
    assert result.error == "auth"
    assert result.added == []
    mock_crud.create_with_account.assert_not_called()


@pytest.mark.asyncio
async def test_sync_inherits_gateway_disabled_seed_exposure(
    mock_crud: MagicMock,
    mock_audit_log: MagicMock,
    mock_config_audit: MagicMock,
    mocker: MockerFixture,
):
    """A seed model kept off the gateway must not put new models on it."""
    seed = _fake_model(gateway_enabled=False)
    mock_crud.get_by_account.return_value = [seed]
    mock_crud.get_for_account.return_value = seed
    mock_crud.resolve_listing_secret.return_value = "sk-ant-live"
    mocker.patch.object(
        ai_model_catalog_sync,
        "get_available_models_for_provider",
        return_value=ModelDiscoveryResult(
            models=["claude-fable-5-1-20260901"], source="live"
        ),
    )

    await sync_account_model_catalog(MagicMock(), user=_fake_user())

    created = mock_crud.create_with_account.call_args.kwargs["obj_in"]
    assert created["meta_data"]["gateway"]["enabled"] is False
