"""Governance review jobs are cron-only and absent when the setting is off."""

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from preloop.config import settings
from preloop.sync import tasks
from preloop.sync.cli.scheduler_commands import (
    governance_session_digest_sweep_trigger,
    governance_weekly_review_trigger,
)


def test_setting_defaults_to_enabled() -> None:
    assert type(settings).model_fields["governance_review_enabled"].default is True


def test_sweep_trigger_is_the_next_hour_not_startup() -> None:
    trigger = governance_session_digest_sweep_trigger()
    thursday = datetime(2026, 10, 15, 15, 30, tzinfo=timezone.utc)

    nxt = trigger.get_next_fire_time(None, thursday)

    assert nxt == datetime(2026, 10, 15, 16, 0, tzinfo=timezone.utc)


def test_weekly_trigger_is_monday_0700_utc() -> None:
    trigger = governance_weekly_review_trigger()
    thursday = datetime(2026, 10, 15, 15, 30, tzinfo=timezone.utc)

    nxt = trigger.get_next_fire_time(None, thursday)

    assert nxt == datetime(2026, 10, 19, 7, 0, tzinfo=timezone.utc)


def test_weekly_trigger_skips_a_monday_that_already_passed() -> None:
    trigger = governance_weekly_review_trigger()
    after_fire = datetime(2026, 10, 19, 7, 10, tzinfo=timezone.utc)

    nxt = trigger.get_next_fire_time(None, after_fire)

    assert nxt == datetime(2026, 10, 26, 7, 0, tzinfo=timezone.utc)


async def _registered_jobs(mocker: MockerFixture, enabled: bool) -> dict[str, Any]:
    from preloop.sync.cli import scheduler_commands

    mocker.patch.object(settings, "governance_review_enabled", enabled)
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
    jobs = {}
    for call in scheduler.add_job.call_args_list:
        jobs[call.kwargs.get("id")] = call
    return jobs


@pytest.mark.asyncio
@pytest.mark.parametrize("analysis_enabled", [False, True])
async def test_both_jobs_are_registered_with_their_triggers(
    mocker: MockerFixture,
    analysis_enabled: bool,
) -> None:
    mocker.patch.object(
        settings, "governance_review_model_analysis_enabled", analysis_enabled
    )
    jobs = await _registered_jobs(mocker, enabled=True)
    sweep = jobs["governance_session_digest_sweep_job"]
    weekly = jobs["governance_weekly_review_job"]
    thursday = datetime(2026, 10, 15, 15, 30, tzinfo=timezone.utc)

    assert sweep.kwargs["trigger"].get_next_fire_time(None, thursday) == datetime(
        2026, 10, 15, 16, 0, tzinfo=timezone.utc
    )
    assert weekly.kwargs["trigger"].get_next_fire_time(None, thursday) == datetime(
        2026, 10, 19, 7, 0, tzinfo=timezone.utc
    )
    assert sweep.kwargs.get("next_run_time") is None
    assert weekly.kwargs.get("next_run_time") is None

    publish = mocker.patch.object(
        __import__(
            "preloop.sync.cli.scheduler_commands", fromlist=["event_bus_service"]
        ).event_bus_service,
        "publish_task",
        new=mocker.AsyncMock(),
    )
    await sweep.args[0]()
    await weekly.args[0]()
    assert publish.await_args_list[0].args == ("governance_session_digest_sweep",)
    assert publish.await_args_list[1].args == ("governance_weekly_review",)


@pytest.mark.asyncio
async def test_both_jobs_are_absent_when_the_setting_is_false(
    mocker: MockerFixture,
) -> None:
    jobs = await _registered_jobs(mocker, enabled=False)
    assert "governance_session_digest_sweep_job" not in jobs
    assert "governance_weekly_review_job" not in jobs


def test_tasks_are_dispatchable() -> None:
    assert "governance_session_digest_sweep" in tasks.DISPATCHABLE_TASKS
    assert "governance_weekly_review" in tasks.DISPATCHABLE_TASKS


def test_tasks_noop_when_no_handler_is_registered(mocker: MockerFixture) -> None:
    manager = mocker.Mock()
    manager.get_service.return_value = None
    mocker.patch("preloop.plugins.base.get_plugin_manager", return_value=manager)

    assert tasks.governance_session_digest_sweep() is None
    assert tasks.governance_weekly_review() is None
    assert manager.get_service.call_args_list[0].args == (
        "governance_session_digest_sweep",
    )
    assert manager.get_service.call_args_list[1].args == ("governance_weekly_review",)


def test_task_calls_a_registered_handler(mocker: MockerFixture) -> None:
    db = mocker.Mock()
    handler = mocker.Mock(return_value={"segments": 1})
    manager = mocker.Mock()
    manager.get_service.return_value = handler
    mocker.patch("preloop.plugins.base.get_plugin_manager", return_value=manager)
    mocker.patch.object(tasks, "get_db_session", return_value=iter([db]))

    assert tasks.governance_weekly_review() == {"segments": 1}
    handler.assert_called_once_with(db)
    db.close.assert_called_once()


def test_model_analysis_defaults_off_and_env_override(monkeypatch):
    cls = type(settings)
    assert cls.model_fields["governance_review_model_analysis_enabled"].default is False
    monkeypatch.setenv("GOVERNANCE_REVIEW_MODEL_ANALYSIS_ENABLED", "true")
    assert (
        cls(
            _env_file=None,
            database=settings.database,
            security=settings.security,
            server=settings.server,
        ).governance_review_model_analysis_enabled
        is True
    )
