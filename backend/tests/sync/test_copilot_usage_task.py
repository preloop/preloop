"""Tests for the ``ingest_copilot_usage`` worker task (issue #788)."""

from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from preloop.config import settings
from preloop.services import copilot_usage_import
from preloop.sync import tasks


@pytest.fixture
def mock_db(mocker: MockerFixture) -> MagicMock:
    db = MagicMock()
    mocker.patch.object(tasks, "get_db_session", return_value=iter([db]))
    return db


def test_task_is_dispatchable() -> None:
    assert "ingest_copilot_usage" in tasks.DISPATCHABLE_TASKS


@pytest.mark.asyncio
async def test_scheduled_run_imports_every_connection(
    mocker: MockerFixture, mock_db: MagicMock
) -> None:
    mocker.patch.object(settings, "copilot_usage_sync_enabled", True)
    ingest = mocker.patch.object(
        copilot_usage_import, "ingest_copilot_usage", return_value={"a": {}}
    )
    assert await tasks.ingest_copilot_usage() == {"a": {}}
    ingest.assert_called_once_with(mock_db, account_id=None)
    mock_db.close.assert_called_once()


@pytest.mark.asyncio
async def test_scheduled_run_noops_when_disabled(
    mocker: MockerFixture, mock_db: MagicMock
) -> None:
    mocker.patch.object(settings, "copilot_usage_sync_enabled", False)
    ingest = mocker.patch.object(copilot_usage_import, "ingest_copilot_usage")
    assert await tasks.ingest_copilot_usage() is None
    ingest.assert_not_called()


@pytest.mark.asyncio
async def test_manual_sync_runs_even_when_schedule_disabled(
    mocker: MockerFixture, mock_db: MagicMock
) -> None:
    mocker.patch.object(settings, "copilot_usage_sync_enabled", False)
    ingest = mocker.patch.object(
        copilot_usage_import, "ingest_copilot_usage", return_value={}
    )
    await tasks.ingest_copilot_usage(account_id="acc-1")
    ingest.assert_called_once_with(mock_db, account_id="acc-1")


@pytest.mark.asyncio
async def test_unexpected_error_is_logged_not_raised(
    mocker: MockerFixture, mock_db: MagicMock
) -> None:
    mocker.patch.object(settings, "copilot_usage_sync_enabled", True)
    mocker.patch.object(
        copilot_usage_import, "ingest_copilot_usage", side_effect=RuntimeError("x")
    )
    assert await tasks.ingest_copilot_usage() is None
    mock_db.close.assert_called_once()
