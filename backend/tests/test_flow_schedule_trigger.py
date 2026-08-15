"""Tests for native schedule (cron) flow triggers."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.models.crud import crud_account, crud_flow, crud_flow_execution
from preloop.models.models import Account, Flow
from preloop.models.models.event import Event
from preloop.models.schemas.flow import FlowCreate, ScheduleConfig
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.sync.services.flow_schedules import (
    FLOW_SCHEDULE_JOB_PREFIX,
    sync_flow_schedule_jobs,
)


@pytest.fixture
def test_account(db_session: Session) -> Account:
    """Create a test account (organization)."""
    account_data = {
        "organization_name": f"Test Org {uuid4().hex[:8]}",
        "is_active": True,
    }
    return crud_account.create(db_session, obj_in=account_data)


@pytest.fixture
def scheduled_flow(db_session: Session, test_account: Account) -> Flow:
    """Create a flow with a schedule (cron) trigger."""
    flow_in = FlowCreate(
        name=f"Scheduled Flow {uuid4().hex[:8]}",
        description="Periodic security scan",
        trigger_event_source="schedule",
        trigger_event_types=["schedule"],
        schedule_config=ScheduleConfig(cron="*/10 * * * *", timezone="UTC"),
        prompt_template="Run the periodic security scan",
        agent_type="openhands",
        agent_config={"max_iterations": 10},
    )
    return crud_flow.create(db=db_session, flow_in=flow_in, account_id=test_account.id)


class TestScheduleConfigValidation:
    """Cron expression and timezone validation."""

    def test_valid_config(self):
        config = ScheduleConfig(cron="0 6 * * 1-5", timezone="Europe/Athens")
        assert config.cron == "0 6 * * 1-5"
        assert config.timezone == "Europe/Athens"
        assert config.next_fire_time() is not None

    def test_timezone_defaults_to_utc(self):
        assert ScheduleConfig(cron="0 * * * *").timezone == "UTC"

    def test_invalid_cron_rejected(self):
        with pytest.raises(ValidationError, match="cron"):
            ScheduleConfig(cron="not a cron")

    def test_invalid_timezone_rejected(self):
        with pytest.raises(ValidationError, match="timezone"):
            ScheduleConfig(cron="0 * * * *", timezone="Mars/Olympus")

    def test_every_minute_rejected(self):
        with pytest.raises(ValidationError, match="minimum"):
            ScheduleConfig(cron="* * * * *")

    def test_two_minute_interval_rejected(self):
        with pytest.raises(ValidationError, match="minimum"):
            ScheduleConfig(cron="*/2 * * * *")

    def test_irregular_minute_list_rejected(self):
        # 12:00 -> 12:03 gap is below the 5 minute minimum
        with pytest.raises(ValidationError, match="minimum"):
            ScheduleConfig(cron="0,3 * * * *")

    def test_five_minute_interval_allowed(self):
        assert ScheduleConfig(cron="*/5 * * * *").cron == "*/5 * * * *"

    def test_daily_cron_allowed(self):
        assert ScheduleConfig(cron="30 2 * * *").cron == "30 2 * * *"


class TestScheduledTick:
    """Worker-side handling of schedule ticks."""

    @pytest.mark.asyncio
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    async def test_tick_triggers_execution(
        self, mock_nats, db_session: Session, scheduled_flow: Flow
    ):
        """A tick on an idle, enabled flow creates an execution."""
        mock_nats.return_value = AsyncMock()
        service = FlowTriggerService(db_session)

        with patch.object(
            service, "_start_flow_execution", new_callable=AsyncMock
        ) as mock_run:
            outcome = await service.run_scheduled_tick(scheduled_flow.id)

        assert outcome == "triggered"
        mock_run.assert_called_once()
        event_data = mock_run.call_args[1]["event_data"]
        assert event_data["source"] == "schedule"
        assert event_data["type"] == "schedule"
        assert event_data["payload"]["cron"] == "*/10 * * * *"
        assert event_data["payload"]["timezone"] == "UTC"
        assert event_data["payload"]["scheduled_at"]

    @pytest.mark.asyncio
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    async def test_tick_skipped_when_previous_running(
        self, mock_nats, db_session: Session, scheduled_flow: Flow
    ):
        """Overlap policy: skip the tick and record it in the audit trail."""
        mock_nats.return_value = AsyncMock()
        running = crud_flow_execution.create(
            db_session,
            obj_in=FlowExecutionCreate(flow_id=scheduled_flow.id, status="RUNNING"),
        )
        service = FlowTriggerService(db_session)

        with patch.object(
            service, "_start_flow_execution", new_callable=AsyncMock
        ) as mock_run:
            outcome = await service.run_scheduled_tick(scheduled_flow.id)

        assert outcome == "skipped_overlap"
        mock_run.assert_not_called()

        skip_events = (
            db_session.query(Event)
            .filter(Event.event_type == "flow_schedule_tick_skipped")
            .all()
        )
        assert len(skip_events) == 1
        event_data = skip_events[0].event_data
        assert event_data["flow_id"] == str(scheduled_flow.id)
        assert event_data["reason"] == "previous_execution_running"
        assert str(running.id) in event_data["running_execution_ids"]

    @pytest.mark.asyncio
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    async def test_tick_suppressed_when_disabled(
        self, mock_nats, db_session: Session, scheduled_flow: Flow
    ):
        """Paused (disabled) flows never fire."""
        mock_nats.return_value = AsyncMock()
        scheduled_flow.is_enabled = False
        db_session.flush()
        service = FlowTriggerService(db_session)

        with patch.object(
            service, "_start_flow_execution", new_callable=AsyncMock
        ) as mock_run:
            outcome = await service.run_scheduled_tick(scheduled_flow.id)

        assert outcome == "suppressed_disabled"
        mock_run.assert_not_called()
        # Suppression is not an overlap skip - no audit skip event
        skip_events = (
            db_session.query(Event)
            .filter(Event.event_type == "flow_schedule_tick_skipped")
            .count()
        )
        assert skip_events == 0

    @pytest.mark.asyncio
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    async def test_tick_ignored_for_missing_flow(self, mock_nats, db_session: Session):
        mock_nats.return_value = AsyncMock()
        service = FlowTriggerService(db_session)
        outcome = await service.run_scheduled_tick(uuid4())
        assert outcome == "not_scheduled"


class TestScheduleJobSync:
    """Scheduler-side reconciliation of cron jobs."""

    def _patch_db(self, db_session: Session):
        return patch(
            "preloop.sync.services.flow_schedules.get_db_session",
            return_value=iter([db_session]),
        )

    def test_sync_adds_job_for_scheduled_flow(
        self, db_session: Session, scheduled_flow: Flow
    ):
        scheduler = MagicMock()
        scheduler.get_jobs.return_value = []

        with self._patch_db(db_session), patch.object(db_session, "close"):
            sync_flow_schedule_jobs(scheduler)

        scheduler.add_job.assert_called_once()
        kwargs = scheduler.add_job.call_args[1]
        assert kwargs["id"] == f"{FLOW_SCHEDULE_JOB_PREFIX}{scheduled_flow.id}"
        assert kwargs["args"] == [str(scheduled_flow.id)]
        assert "*/10" in repr(kwargs["trigger"])

    def test_sync_removes_job_for_disabled_flow(
        self, db_session: Session, scheduled_flow: Flow
    ):
        scheduled_flow.is_enabled = False
        db_session.flush()

        existing_job = MagicMock()
        existing_job.id = f"{FLOW_SCHEDULE_JOB_PREFIX}{scheduled_flow.id}"
        scheduler = MagicMock()
        scheduler.get_jobs.return_value = [existing_job]

        with self._patch_db(db_session), patch.object(db_session, "close"):
            sync_flow_schedule_jobs(scheduler)

        scheduler.remove_job.assert_called_once_with(existing_job.id)
        scheduler.add_job.assert_not_called()

    def test_sync_ignores_preset_flows(
        self, db_session: Session, test_account: Account
    ):
        flow_in = FlowCreate(
            name=f"Preset Scheduled Flow {uuid4().hex[:8]}",
            trigger_event_source="schedule",
            trigger_event_types=["schedule"],
            schedule_config=ScheduleConfig(cron="0 * * * *"),
            prompt_template="Preset prompt",
            agent_type="openhands",
            agent_config={},
            is_preset=True,
        )
        crud_flow.create(db=db_session, flow_in=flow_in, account_id=test_account.id)
        scheduler = MagicMock()
        scheduler.get_jobs.return_value = []

        with self._patch_db(db_session), patch.object(db_session, "close"):
            sync_flow_schedule_jobs(scheduler)

        scheduler.add_job.assert_not_called()
