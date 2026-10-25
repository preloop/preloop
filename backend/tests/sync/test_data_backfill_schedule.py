"""Post-deploy data backfills run from the sync role, not the migration hook."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from preloop.models.backfills import BackfillResult
from preloop.sync import tasks


def test_task_is_dispatchable() -> None:
    assert "run_data_backfills" in tasks.DISPATCHABLE_TASKS


@pytest.mark.asyncio
async def test_task_runs_pending_backfills_and_closes_the_session(
    mocker: MockerFixture,
) -> None:
    db = MagicMock()
    mocker.patch.object(tasks, "get_db_session", return_value=iter([db]))
    runner = mocker.patch(
        "preloop.models.backfills.run_pending_backfills",
        return_value=[BackfillResult("agents", batches=4, ready=True)],
    )

    result = await tasks.run_data_backfills()

    runner.assert_called_once_with(db)
    assert result == [{"name": "agents", "batches": 4, "ready": True, "errors": []}]
    db.close.assert_called_once()


@pytest.mark.asyncio
async def test_scheduler_registers_an_early_bounded_job(
    mocker: MockerFixture,
) -> None:
    from preloop.sync.cli import scheduler_commands

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
    jobs = {c.kwargs.get("id"): c for c in scheduler.add_job.call_args_list}

    job = jobs["data_backfill_job"]
    assert scheduler_commands.DATA_BACKFILL_INTERVAL_MINUTES == 15
    assert job.kwargs["max_instances"] == 1
    assert job.kwargs["trigger"].interval == timedelta(
        minutes=scheduler_commands.DATA_BACKFILL_INTERVAL_MINUTES
    )
    first = job.kwargs["next_run_time"] - datetime.now(timezone.utc)
    assert timedelta(0) < first <= timedelta(minutes=1)


@pytest.mark.asyncio
async def test_failed_pass_raises_and_closes_session(mocker: MockerFixture) -> None:
    db = MagicMock()
    mocker.patch.object(tasks, "get_db_session", return_value=iter([db]))
    mocker.patch(
        "preloop.models.backfills.run_pending_backfills",
        return_value=[BackfillResult("agents", errors=["batch failed"])],
    )
    with pytest.raises(RuntimeError, match="Data backfills failed: agents"):
        await tasks.run_data_backfills()
    db.close.assert_called_once()
