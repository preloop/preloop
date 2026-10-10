"""Tests for the ``ingest_anthropic_usage`` worker task and its schedule (#1413)."""

from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from preloop.config import settings
from preloop.services import anthropic_usage_import
from preloop.sync import tasks


@pytest.fixture
def mock_db(mocker: MockerFixture) -> MagicMock:
    db = MagicMock()
    mocker.patch.object(tasks, "get_db_session", return_value=iter([db]))
    return db


def test_task_is_dispatchable() -> None:
    assert "ingest_anthropic_usage" in tasks.DISPATCHABLE_TASKS


def test_setting_defaults_to_enabled() -> None:
    assert type(settings).model_fields["anthropic_usage_sync_enabled"].default is True


@pytest.mark.asyncio
async def test_scheduled_run_imports_every_connection(
    mocker: MockerFixture, mock_db: MagicMock
) -> None:
    mocker.patch.object(settings, "anthropic_usage_sync_enabled", True)
    ingest = mocker.patch.object(
        anthropic_usage_import, "ingest_anthropic_usage", return_value={"a": {}}
    )
    assert await tasks.ingest_anthropic_usage() == {"a": {}}
    ingest.assert_called_once_with(mock_db, account_id=None)
    mock_db.close.assert_called_once()


@pytest.mark.asyncio
async def test_scheduled_run_noops_when_disabled(
    mocker: MockerFixture, mock_db: MagicMock
) -> None:
    mocker.patch.object(settings, "anthropic_usage_sync_enabled", False)
    ingest = mocker.patch.object(anthropic_usage_import, "ingest_anthropic_usage")
    assert await tasks.ingest_anthropic_usage() is None
    ingest.assert_not_called()


@pytest.mark.asyncio
async def test_manual_sync_runs_even_when_schedule_disabled(
    mocker: MockerFixture, mock_db: MagicMock
) -> None:
    mocker.patch.object(settings, "anthropic_usage_sync_enabled", False)
    ingest = mocker.patch.object(
        anthropic_usage_import, "ingest_anthropic_usage", return_value={}
    )
    await tasks.ingest_anthropic_usage(account_id="acc-1")
    ingest.assert_called_once_with(mock_db, account_id="acc-1")


@pytest.mark.asyncio
async def test_unexpected_error_is_logged_not_raised(
    mocker: MockerFixture, mock_db: MagicMock
) -> None:
    mocker.patch.object(settings, "anthropic_usage_sync_enabled", True)
    mocker.patch.object(
        anthropic_usage_import,
        "ingest_anthropic_usage",
        side_effect=RuntimeError("x"),
    )
    assert await tasks.ingest_anthropic_usage() is None
    mock_db.close.assert_called_once()


async def _registered_job_ids(mocker: MockerFixture, enabled: bool) -> list:
    import asyncio

    from preloop.sync.cli import scheduler_commands

    mocker.patch.object(settings, "anthropic_usage_sync_enabled", enabled)
    mocker.patch.object(
        scheduler_commands.event_bus_service, "connect", mocker.AsyncMock()
    )
    scheduler = MagicMock()
    run = asyncio.create_task(
        scheduler_commands.run_scheduler_async(scheduler, 60, MagicMock(), 1)
    )
    await asyncio.sleep(0.05)
    run.cancel()
    try:
        await run
    except asyncio.CancelledError:
        pass
    return [call.kwargs.get("id") for call in scheduler.add_job.call_args_list]


@pytest.mark.asyncio
async def test_daily_job_is_registered_next_to_copilot(mocker: MockerFixture) -> None:
    ids = await _registered_job_ids(mocker, enabled=True)
    assert "anthropic_usage_import_job" in ids
    assert ids.index("anthropic_usage_import_job") == (
        ids.index("copilot_usage_import_job") + 1
    )


@pytest.mark.asyncio
async def test_daily_job_is_not_registered_when_disabled(
    mocker: MockerFixture,
) -> None:
    ids = await _registered_job_ids(mocker, enabled=False)
    assert "anthropic_usage_import_job" not in ids
