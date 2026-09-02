"""Tests for the scheduled model-catalog sync worker task.

The scheduler publishes ``sync_model_catalog`` on an interval when
``model_catalog_sync_scheduled_enabled`` is on (default OFF). These tests pin
the worker-side contract: the task re-checks the setting so a stale queued
task after a disable still no-ops, and an enabled run dispatches the same
sync logic as the manual endpoint, attributed to the system actor.
"""

from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from preloop.config import settings
from preloop.services import ai_model_catalog_sync
from preloop.sync import tasks


@pytest.fixture
def mock_db(mocker: MockerFixture) -> MagicMock:
    db = MagicMock()
    mocker.patch.object(tasks, "get_db_session", return_value=iter([db]))
    return db


@pytest.mark.asyncio
async def test_task_noops_when_setting_disabled(
    mocker: MockerFixture, mock_db: MagicMock
):
    mocker.patch.object(settings, "model_catalog_sync_scheduled_enabled", False)
    sync_all = mocker.patch.object(
        ai_model_catalog_sync, "sync_all_account_model_catalogs"
    )

    assert await tasks.sync_model_catalog() is None

    sync_all.assert_not_called()


@pytest.mark.asyncio
async def test_task_runs_sync_for_all_accounts_when_enabled(
    mocker: MockerFixture, mock_db: MagicMock
):
    mocker.patch.object(settings, "model_catalog_sync_scheduled_enabled", True)
    sync_all = mocker.patch.object(
        ai_model_catalog_sync,
        "sync_all_account_model_catalogs",
        return_value={"acc-1": 2},
    )

    result = await tasks.sync_model_catalog()

    sync_all.assert_awaited_once_with(mock_db)
    assert result == {"acc-1": 2}
    mock_db.close.assert_called_once()


@pytest.mark.asyncio
async def test_task_single_account_uses_system_actor(
    mocker: MockerFixture, mock_db: MagicMock
):
    mocker.patch.object(settings, "model_catalog_sync_scheduled_enabled", True)
    summary = ai_model_catalog_sync.CatalogSyncSummary(
        providers=[
            ai_model_catalog_sync.ProviderCatalogSyncResult(
                provider="anthropic", source="live", added=["anthropic/new-model"]
            )
        ]
    )
    sync_one = mocker.patch.object(
        ai_model_catalog_sync, "sync_account_model_catalog", return_value=summary
    )

    result = await tasks.sync_model_catalog(account_id="acc-42")

    actor = sync_one.call_args.kwargs["actor"]
    assert actor.actor_type == "system"
    assert actor.actor_id == "model-catalog-sync"
    assert actor.user_id is None
    assert actor.account_id == "acc-42"
    assert result == {"acc-42": 1}


def test_sync_model_catalog_is_dispatchable():
    """A task missing from the registry is silently never delivered to
    dedicated worker pools (WORKQUEUE subject filters must be enumerated)."""
    assert "sync_model_catalog" in tasks.DISPATCHABLE_TASKS
