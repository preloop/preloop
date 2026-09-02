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
    assert audit_kwargs["user_id"] == user.id
    details = audit_kwargs["details"]
    assert details["actor_type"] == "user"
    assert details["trigger"] == "manual"
    assert details["total_added"] == 1
    assert details["providers"] == [
        {"provider": "anthropic", "added": ["anthropic/claude-fable-5-1-20260901"]}
    ]
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


@pytest.mark.asyncio
async def test_sync_create_value_error_records_provider_error_and_keeps_siblings(
    mock_crud: MagicMock,
    mock_audit_log: MagicMock,
    mock_config_audit: MagicMock,
    mocker: MockerFixture,
):
    """A create ValueError (e.g. deleted secret) is a per-provider error, not a
    500: the sibling identifier is still created and the batch commits."""
    seed = _fake_model()
    mock_crud.get_by_account.return_value = [seed]
    mock_crud.get_for_account.return_value = seed
    mock_crud.resolve_listing_secret.return_value = "sk-ant-live"
    mocker.patch.object(
        ai_model_catalog_sync,
        "get_available_models_for_provider",
        return_value=ModelDiscoveryResult(
            models=["claude-fable-5-1-20260901", "claude-opus-4-20250514"],
            source="live",
        ),
    )
    mock_crud.create_with_account.side_effect = [
        ValueError("Referenced credential secret does not exist"),
        MagicMock(),
    ]
    db = MagicMock()

    summary = await sync_account_model_catalog(db, user=_fake_user())

    result = summary.providers[0]
    assert result.error == "create"
    assert "Referenced credential secret does not exist" in (result.note or "")
    assert result.added == ["anthropic/claude-opus-4-20250514"]
    assert mock_crud.create_with_account.call_count == 2
    db.commit.assert_called_once()
    nested = db.begin_nested.return_value
    assert nested.rollback.call_count == 1
    assert nested.commit.call_count == 1


@pytest.mark.asyncio
async def test_system_actor_sync_audits_with_null_user(
    mock_crud: MagicMock,
    mock_audit_log: MagicMock,
    mock_config_audit: MagicMock,
    mocker: MockerFixture,
):
    """A scheduled run is attributed to the model-catalog-sync system actor:
    the audit event carries user_id=None plus actor_type/actor details, and
    the user-centric EE config-change plugin is never invoked."""
    seed = _fake_model()
    account_id = uuid.uuid4()
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

    summary = await sync_account_model_catalog(
        MagicMock(),
        actor=ai_model_catalog_sync.CatalogSyncActor.system(account_id),
    )

    assert summary.providers[0].added == ["anthropic/claude-fable-5-1-20260901"]
    audit_kwargs = mock_audit_log.log_action.call_args.kwargs
    assert audit_kwargs["account_id"] == account_id
    assert audit_kwargs["user_id"] is None
    details = audit_kwargs["details"]
    assert details["actor_type"] == "system"
    assert details["actor"] == "model-catalog-sync"
    assert details["trigger"] == "scheduled"
    assert details["total_added"] == 1
    mock_config_audit.assert_not_called()


@pytest.mark.asyncio
async def test_scheduled_sync_is_idempotent_second_run_adds_nothing(
    mock_crud: MagicMock,
    mock_audit_log: MagicMock,
    mock_config_audit: MagicMock,
    mocker: MockerFixture,
):
    """Second run against a catalog that already gained the model adds
    nothing, creates nothing, and emits no audit event (logs only)."""
    account_id = uuid.uuid4()
    seed = _fake_model()
    discovered = ModelDiscoveryResult(
        models=["claude-fable-5-1-20260901", "claude-fable-5-20260415"],
        source="live",
    )
    mocker.patch.object(
        ai_model_catalog_sync,
        "get_available_models_for_provider",
        return_value=discovered,
    )
    mock_crud.get_for_account.return_value = seed
    mock_crud.resolve_listing_secret.return_value = "sk-ant-live"

    # First run: the catalog only knows the seed model.
    mock_crud.get_by_account.return_value = [seed]
    first = await sync_account_model_catalog(
        MagicMock(), actor=ai_model_catalog_sync.CatalogSyncActor.system(account_id)
    )
    assert first.providers[0].added == ["anthropic/claude-fable-5-1-20260901"]
    assert mock_crud.create_with_account.call_count == 1
    assert mock_audit_log.log_action.call_count == 1

    # Second run: the catalog now contains what the first run added.
    mock_crud.get_by_account.return_value = [
        seed,
        _fake_model(model_identifier="claude-fable-5-1-20260901"),
    ]
    second = await sync_account_model_catalog(
        MagicMock(), actor=ai_model_catalog_sync.CatalogSyncActor.system(account_id)
    )
    assert second.providers[0].added == []
    assert second.providers[0].skipped_existing == 2
    assert mock_crud.create_with_account.call_count == 1  # unchanged
    assert mock_audit_log.log_action.call_count == 1  # no new audit event


@pytest.mark.asyncio
async def test_sync_all_accounts_runs_each_account_as_system_actor(
    mock_crud: MagicMock,
    mock_audit_log: MagicMock,
    mock_config_audit: MagicMock,
    mocker: MockerFixture,
):
    account_a, account_b = MagicMock(), MagicMock()
    account_a.id = uuid.uuid4()
    account_b.id = uuid.uuid4()
    mock_crud_account = mocker.patch.object(ai_model_catalog_sync, "crud_account")
    mock_crud_account.get_multi.side_effect = [[account_a, account_b]]

    seen_actors = []

    async def fake_sync(db, *, actor):
        seen_actors.append(actor)
        result = ai_model_catalog_sync.ProviderCatalogSyncResult(
            provider="anthropic",
            source="live",
            added=["anthropic/new-model"] if actor.account_id == account_a.id else [],
        )
        return ai_model_catalog_sync.CatalogSyncSummary(providers=[result])

    mocker.patch.object(
        ai_model_catalog_sync, "sync_account_model_catalog", side_effect=fake_sync
    )

    added = await ai_model_catalog_sync.sync_all_account_model_catalogs(MagicMock())

    assert [actor.account_id for actor in seen_actors] == [
        account_a.id,
        account_b.id,
    ]
    assert all(actor.actor_type == "system" for actor in seen_actors)
    assert all(actor.actor_id == "model-catalog-sync" for actor in seen_actors)
    assert added == {str(account_a.id): 1}
