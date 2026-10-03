"""Tests for native schedule flow triggers (cron and friendly forms)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.models.crud import crud_account, crud_flow, crud_flow_execution
from preloop.models.models import Account, Flow
from preloop.models.models.event import Event
from preloop.models.schemas.flow import (
    MAX_SCHEDULE_PAYLOAD_BYTES,
    CronSchedule,
    DailySchedule,
    FlowCreate,
    IntervalSchedule,
    WeeklySchedule,
    parse_schedule_config,
)
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.sync.services.flow_schedules import (
    FLOW_SCHEDULE_JOB_PREFIX,
    sync_flow_schedule_jobs,
)


def _key(config) -> str:
    """Canonical schedule key as used by the reconcile loop's job args."""
    return json.dumps(config.model_dump(), sort_keys=True)


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
        schedule_config=CronSchedule(expr="*/10 * * * *", timezone="UTC"),
        prompt_template="Run the periodic security scan",
        agent_type="openhands",
        agent_config={"max_iterations": 10},
    )
    return crud_flow.create(db=db_session, flow_in=flow_in, account_id=test_account.id)


class TestCronScheduleValidation:
    """Cron expression and timezone validation (advanced form)."""

    def test_valid_config(self):
        config = CronSchedule(expr="0 6 * * 1-5", timezone="Europe/Athens")
        assert config.expr == "0 6 * * 1-5"
        assert config.timezone == "Europe/Athens"
        assert config.next_fire_time() is not None
        assert "0 6 * * 1-5" in config.describe()

    def test_timezone_defaults_to_utc(self):
        assert CronSchedule(expr="0 * * * *").timezone == "UTC"

    def test_legacy_cron_key_accepted(self):
        """The pre-union field name 'cron' still works as an alias."""
        config = CronSchedule(cron="0 6 * * *", timezone="UTC")
        assert config.expr == "0 6 * * *"

    def test_invalid_cron_rejected(self):
        with pytest.raises(ValidationError, match="cron"):
            CronSchedule(expr="not a cron")

    def test_invalid_timezone_rejected(self):
        with pytest.raises(ValidationError, match="timezone"):
            CronSchedule(expr="0 * * * *", timezone="Mars/Olympus")

    def test_every_minute_rejected(self):
        with pytest.raises(ValidationError, match="minimum"):
            CronSchedule(expr="* * * * *")

    def test_two_minute_interval_rejected(self):
        with pytest.raises(ValidationError, match="minimum"):
            CronSchedule(expr="*/2 * * * *")

    def test_irregular_minute_list_rejected(self):
        # 12:00 -> 12:03 gap is below the 5 minute minimum
        with pytest.raises(ValidationError, match="minimum"):
            CronSchedule(expr="0,3 * * * *")

    def test_five_minute_interval_allowed(self):
        assert CronSchedule(expr="*/5 * * * *").expr == "*/5 * * * *"

    def test_daily_cron_allowed(self):
        assert CronSchedule(expr="30 2 * * *").expr == "30 2 * * *"

    def test_seasonal_fast_cron_rejected(self):
        # Every 2 minutes but only in January: the fire times are far in
        # the future for most of the year, so a wall-clock-horizon check
        # would accept it. The simulation is anchored at the schedule's own
        # first fire time, so this must be rejected year-round.
        with pytest.raises(ValidationError, match="minimum"):
            CronSchedule(expr="*/2 * * 1 *")

    def test_seasonal_minute_list_rejected(self):
        # 3-minute gap, restricted to one day per year
        with pytest.raises(ValidationError, match="minimum"):
            CronSchedule(expr="0,3 * 1 1 *")

    def test_yearly_cron_allowed(self):
        assert CronSchedule(expr="0 0 1 1 *").expr == "0 0 1 1 *"


class TestSchedulePayloadValidation:
    """A schedule may carry a small static payload for its runs."""

    def test_absent_by_default(self):
        assert CronSchedule(expr="0 6 * * *").payload is None

    def test_carries_options(self):
        config = CronSchedule(
            expr="0 6 * * *",
            payload={"previous_result_execution_id": "last", "depth": "standard"},
        )
        assert config.payload["previous_result_execution_id"] == "last"

    def test_survives_the_stored_round_trip(self):
        stored = CronSchedule(
            expr="0 6 * * *", payload={"previous_result_execution_id": "last"}
        ).model_dump()
        assert parse_schedule_config(stored).payload == {
            "previous_result_execution_id": "last"
        }

    def test_file_seeding_is_refused(self):
        """Inline seeds belong to a caller who can read the response."""
        with pytest.raises(ValidationError, match="workspace_files"):
            CronSchedule(
                expr="0 6 * * *",
                payload={"workspace_files": [{"path": "a", "content_base64": "eA=="}]},
            )

    def test_schedule_fields_are_not_overridable(self):
        with pytest.raises(ValidationError, match="scheduled_at"):
            CronSchedule(expr="0 6 * * *", payload={"scheduled_at": "whenever"})

    def test_oversized_payload_rejected(self):
        with pytest.raises(ValidationError, match="byte cap"):
            CronSchedule(expr="0 6 * * *", payload={"note": "x" * 5000})

    def test_payload_cap_counts_utf8_bytes_not_characters(self):
        """CJK that fits as characters can still exceed the documented byte cap."""
        payload = {"note": "你" * 1400}
        encoded = json.dumps(payload, ensure_ascii=False)
        assert len(encoded) <= MAX_SCHEDULE_PAYLOAD_BYTES
        assert len(encoded.encode("utf-8")) > MAX_SCHEDULE_PAYLOAD_BYTES
        with pytest.raises(ValidationError, match="byte cap"):
            CronSchedule(expr="0 6 * * *", payload=payload)

    def test_utf8_payload_under_the_byte_cap_is_accepted(self):
        payload = {"note": "你" * 1000}
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        assert len(encoded) <= MAX_SCHEDULE_PAYLOAD_BYTES
        config = CronSchedule(expr="0 6 * * *", payload=payload)
        assert config.payload["note"] == "你" * 1000

    def test_too_many_keys_rejected(self):
        with pytest.raises(ValidationError, match="max is"):
            CronSchedule(
                expr="0 6 * * *",
                payload={f"key{index}": index for index in range(21)},
            )


class TestIntervalScheduleValidation:
    """Friendly interval form: every N minutes/hours/days."""

    def test_valid_minutes(self):
        config = IntervalSchedule(every=30, unit="minutes")
        assert config.timezone == "UTC"
        assert config.next_fire_time() is not None
        assert config.describe() == "Every 30 minutes"

    def test_singular_unit_description(self):
        assert IntervalSchedule(every=1, unit="hours").describe() == "Every 1 hour"

    def test_hours_and_days_allowed(self):
        assert IntervalSchedule(every=2, unit="hours").every == 2
        assert IntervalSchedule(every=1, unit="days").unit == "days"

    def test_below_min_interval_rejected(self):
        with pytest.raises(ValidationError, match="minimum"):
            IntervalSchedule(every=2, unit="minutes")

    def test_exactly_min_interval_allowed(self):
        assert IntervalSchedule(every=5, unit="minutes").every == 5

    def test_zero_and_negative_rejected(self):
        with pytest.raises(ValidationError):
            IntervalSchedule(every=0, unit="hours")
        with pytest.raises(ValidationError):
            IntervalSchedule(every=-1, unit="days")

    def test_above_max_interval_rejected(self):
        with pytest.raises(ValidationError, match="maximum"):
            IntervalSchedule(every=367, unit="days")

    def test_exactly_max_interval_allowed(self):
        assert IntervalSchedule(every=366, unit="days").every == 366

    def test_absurd_interval_rejected_without_overflow(self):
        # timedelta(days=10**12) raises OverflowError, which pydantic does
        # not convert to a 422; the validator must catch it and raise a
        # ValueError instead of leaking an HTTP 500.
        with pytest.raises(ValidationError, match="maximum"):
            IntervalSchedule(every=1_000_000_000_000, unit="days")
        with pytest.raises(ValidationError, match="maximum"):
            IntervalSchedule(every=1_000_000_000_000, unit="minutes")

    def test_invalid_unit_rejected(self):
        with pytest.raises(ValidationError, match="unit"):
            IntervalSchedule(every=10, unit="fortnights")

    def test_invalid_timezone_rejected(self):
        with pytest.raises(ValidationError, match="timezone"):
            IntervalSchedule(every=1, unit="hours", timezone="Nope/Nope")

    def test_next_fire_times_are_interval_apart(self):
        times = IntervalSchedule(every=1, unit="hours").next_fire_times(3)
        assert len(times) == 3
        assert (times[1] - times[0]).total_seconds() == 3600
        assert (times[2] - times[1]).total_seconds() == 3600


class TestDailyScheduleValidation:
    """Friendly daily form: run once a day at HH:MM."""

    def test_valid(self):
        config = DailySchedule(at="06:30", timezone="Europe/Athens")
        assert config.next_fire_time() is not None
        assert config.describe() == "Daily at 06:30 (Europe/Athens)"

    def test_midnight_and_end_of_day(self):
        assert DailySchedule(at="00:00").at == "00:00"
        assert DailySchedule(at="23:59").at == "23:59"

    @pytest.mark.parametrize("bad", ["24:00", "9:00", "09:60", "0900", "morning", ""])
    def test_invalid_time_rejected(self, bad):
        with pytest.raises(ValidationError, match="at"):
            DailySchedule(at=bad)

    def test_invalid_timezone_rejected(self):
        with pytest.raises(ValidationError, match="timezone"):
            DailySchedule(at="06:30", timezone="Nope/Nope")


class TestWeeklyScheduleValidation:
    """Friendly weekly form: run on selected weekdays at HH:MM."""

    def test_valid(self):
        config = WeeklySchedule(days=["mon", "fri"], at="09:00")
        assert config.next_fire_time() is not None
        assert config.describe() == "Weekly on Mon, Fri at 09:00 (UTC)"

    def test_days_deduplicated_and_ordered(self):
        config = WeeklySchedule(days=["fri", "mon", "fri", "wed"], at="09:00")
        assert config.days == ["mon", "wed", "fri"]

    def test_empty_days_rejected(self):
        with pytest.raises(ValidationError, match="days"):
            WeeklySchedule(days=[], at="09:00")

    def test_unknown_day_rejected(self):
        with pytest.raises(ValidationError, match="days"):
            WeeklySchedule(days=["funday"], at="09:00")

    def test_invalid_time_rejected(self):
        with pytest.raises(ValidationError, match="at"):
            WeeklySchedule(days=["mon"], at="25:00")


class TestParseScheduleConfig:
    """Stored-config parsing, including the legacy untyped shape."""

    def test_typed_forms_roundtrip(self):
        for config in (
            CronSchedule(expr="0 2 * * *"),
            IntervalSchedule(every=6, unit="hours"),
            DailySchedule(at="06:30"),
            WeeklySchedule(days=["mon"], at="09:00"),
        ):
            parsed = parse_schedule_config(config.model_dump())
            assert parsed == config

    def test_legacy_cron_dict_normalized(self):
        parsed = parse_schedule_config(
            {"cron": "0 2 * * *", "timezone": "Europe/Athens"}
        )
        assert isinstance(parsed, CronSchedule)
        assert parsed.expr == "0 2 * * *"
        assert parsed.timezone == "Europe/Athens"

    def test_unknown_type_rejected(self):
        with pytest.raises(ValidationError):
            parse_schedule_config({"type": "lunar", "at": "06:30"})

    def test_flow_create_accepts_legacy_dict(self):
        """FlowBase normalizes the legacy shape on API input too."""
        flow = FlowCreate(
            name="n",
            prompt_template="p",
            agent_type="openhands",
            agent_config={},
            trigger_event_source="schedule",
            schedule_config={"cron": "0 2 * * *"},
        )
        assert isinstance(flow.schedule_config, CronSchedule)
        assert flow.schedule_config.expr == "0 2 * * *"

    def test_flow_create_accepts_friendly_forms(self):
        flow = FlowCreate(
            name="n",
            prompt_template="p",
            agent_type="openhands",
            agent_config={},
            trigger_event_source="schedule",
            schedule_config={"type": "weekly", "days": ["mon"], "at": "09:00"},
        )
        assert isinstance(flow.schedule_config, WeeklySchedule)

    def test_flow_create_enforces_min_interval_on_friendly_form(self):
        with pytest.raises(ValidationError, match="minimum"):
            FlowCreate(
                name="n",
                prompt_template="p",
                agent_type="openhands",
                agent_config={},
                trigger_event_source="schedule",
                schedule_config={"type": "interval", "every": 1, "unit": "minutes"},
            )


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
        assert event_data["payload"]["schedule"] == {
            "type": "cron",
            "expr": "*/10 * * * *",
            "timezone": "UTC",
        }
        assert event_data["payload"]["timezone"] == "UTC"
        assert event_data["payload"]["scheduled_at"]

    @pytest.mark.asyncio
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    async def test_tick_carries_the_static_schedule_payload(
        self, mock_nats, db_session: Session, scheduled_flow: Flow
    ):
        """A review subscription states its baseline once, on the schedule."""
        scheduled_flow.schedule_config = CronSchedule(
            expr="*/10 * * * *",
            timezone="UTC",
            payload={"previous_result_execution_id": "last"},
        ).model_dump()
        db_session.flush()
        mock_nats.return_value = AsyncMock()
        service = FlowTriggerService(db_session)

        with patch.object(
            service, "_start_flow_execution", new_callable=AsyncMock
        ) as mock_run:
            outcome = await service.run_scheduled_tick(scheduled_flow.id)

        assert outcome == "triggered"
        payload = mock_run.call_args[1]["event_data"]["payload"]
        assert payload["previous_result_execution_id"] == "last"
        assert payload["schedule"]["expr"] == "*/10 * * * *"
        assert "payload" not in payload["schedule"]

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
        assert kwargs["args"] == [
            str(scheduled_flow.id),
            _key(CronSchedule(expr="*/10 * * * *", timezone="UTC")),
        ]
        assert "*/10" in repr(kwargs["trigger"])

    def test_sync_adds_interval_job_for_friendly_form(
        self, db_session: Session, test_account: Account
    ):
        """Interval schedules map onto a native APScheduler IntervalTrigger."""
        from apscheduler.triggers.interval import IntervalTrigger

        config = IntervalSchedule(every=30, unit="minutes")
        flow_in = FlowCreate(
            name=f"Interval Flow {uuid4().hex[:8]}",
            trigger_event_source="schedule",
            trigger_event_types=["schedule"],
            schedule_config=config,
            prompt_template="p",
            agent_type="openhands",
            agent_config={},
        )
        flow = crud_flow.create(
            db=db_session, flow_in=flow_in, account_id=test_account.id
        )
        scheduler = MagicMock()
        scheduler.get_jobs.return_value = []

        with self._patch_db(db_session), patch.object(db_session, "close"):
            sync_flow_schedule_jobs(scheduler)

        scheduler.add_job.assert_called_once()
        kwargs = scheduler.add_job.call_args[1]
        assert kwargs["id"] == f"{FLOW_SCHEDULE_JOB_PREFIX}{flow.id}"
        assert kwargs["args"] == [str(flow.id), _key(config)]
        assert isinstance(kwargs["trigger"], IntervalTrigger)
        assert kwargs["trigger"].interval.total_seconds() == 1800

    def test_sync_handles_legacy_untyped_config(
        self, db_session: Session, scheduled_flow: Flow
    ):
        """Legacy {'cron': ...} rows reconcile as cron schedules."""
        scheduled_flow.schedule_config = {"cron": "0 6 * * *", "timezone": "UTC"}
        db_session.flush()
        scheduler = MagicMock()
        scheduler.get_jobs.return_value = []

        with self._patch_db(db_session), patch.object(db_session, "close"):
            sync_flow_schedule_jobs(scheduler)

        scheduler.add_job.assert_called_once()
        kwargs = scheduler.add_job.call_args[1]
        assert kwargs["args"] == [
            str(scheduled_flow.id),
            _key(CronSchedule(expr="0 6 * * *", timezone="UTC")),
        ]

    def test_sync_is_idempotent_for_unchanged_config(
        self, db_session: Session, scheduled_flow: Flow
    ):
        """A job whose args match the stored config is left untouched."""
        existing_job = MagicMock()
        existing_job.id = f"{FLOW_SCHEDULE_JOB_PREFIX}{scheduled_flow.id}"
        existing_job.args = [
            str(scheduled_flow.id),
            _key(CronSchedule(expr="*/10 * * * *", timezone="UTC")),
        ]
        scheduler = MagicMock()
        scheduler.get_jobs.return_value = [existing_job]

        with self._patch_db(db_session), patch.object(db_session, "close"):
            sync_flow_schedule_jobs(scheduler)

        scheduler.add_job.assert_not_called()
        scheduler.remove_job.assert_not_called()

    def test_sync_replaces_job_when_config_changes(
        self, db_session: Session, scheduled_flow: Flow
    ):
        """A job whose args no longer match the stored config is replaced."""
        existing_job = MagicMock()
        existing_job.id = f"{FLOW_SCHEDULE_JOB_PREFIX}{scheduled_flow.id}"
        existing_job.args = [
            str(scheduled_flow.id),
            _key(CronSchedule(expr="0 6 * * *", timezone="Europe/Athens")),
        ]
        scheduler = MagicMock()
        scheduler.get_jobs.return_value = [existing_job]

        with self._patch_db(db_session), patch.object(db_session, "close"):
            sync_flow_schedule_jobs(scheduler)

        scheduler.add_job.assert_called_once()
        kwargs = scheduler.add_job.call_args[1]
        assert kwargs["replace_existing"] is True
        assert kwargs["args"] == [
            str(scheduled_flow.id),
            _key(CronSchedule(expr="*/10 * * * *", timezone="UTC")),
        ]

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
            schedule_config=CronSchedule(expr="0 * * * *"),
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


def _utc(*args) -> "datetime":
    from datetime import datetime, timezone

    return datetime(*args, tzinfo=timezone.utc)


class TestScheduleFireWindow:
    """The previous fire time comes from the schedule definition (#1105)."""

    def test_cron_hourly(self):
        current, previous = CronSchedule(expr="0 * * * *").fire_window(
            _utc(2026, 10, 4, 12, 0, 3)
        )
        assert current == _utc(2026, 10, 4, 12, 0)
        assert previous == _utc(2026, 10, 4, 11, 0)

    def test_cron_sparse_needs_a_long_lookback(self):
        # First of January only: the walk back has to widen past a year.
        current, previous = CronSchedule(expr="0 6 1 1 *").fire_window(
            _utc(2027, 1, 1, 6, 0, 1)
        )
        assert current == _utc(2027, 1, 1, 6, 0)
        assert previous == _utc(2026, 1, 1, 6, 0)

    def test_cron_leap_day_only_reaches_the_eight_year_bound(self):
        # 29 February only: from 2028-02-28 the previous two fires are
        # 2024-02-29 and 2020-02-29, 2921 days back.
        current, previous = CronSchedule(expr="0 6 29 2 *").fire_window(
            _utc(2028, 2, 28, 6, 0, 1)
        )
        assert current == _utc(2024, 2, 29, 6, 0)
        assert previous == _utc(2020, 2, 29, 6, 0)

    def test_interval_is_one_period_back(self):
        at = _utc(2026, 10, 4, 12, 7, 30)
        current, previous = IntervalSchedule(every=15, unit="minutes").fire_window(at)
        assert current == at
        assert previous == _utc(2026, 10, 4, 11, 52, 30)

    def test_daily(self):
        current, previous = DailySchedule(at="09:30", timezone="UTC").fire_window(
            _utc(2026, 10, 4, 9, 30, 2)
        )
        assert current == _utc(2026, 10, 4, 9, 30)
        assert previous == _utc(2026, 10, 3, 9, 30)

    def test_weekly(self):
        # 2026-10-05 is a Monday; the previous fire is Friday.
        current, previous = WeeklySchedule(days=["mon", "fri"], at="08:00").fire_window(
            _utc(2026, 10, 5, 8, 0, 1)
        )
        assert current == _utc(2026, 10, 5, 8, 0)
        assert previous == _utc(2026, 10, 2, 8, 0)

    def test_dst_spring_forward_in_a_non_utc_timezone(self):
        # Europe/Berlin moves to CEST on 2026-03-29: noon local is 11:00Z on
        # the 28th and 10:00Z on the 29th, so the window is 23 hours long.
        current, previous = DailySchedule(
            at="12:00", timezone="Europe/Berlin"
        ).fire_window(_utc(2026, 3, 29, 10, 0, 5))
        assert current == _utc(2026, 3, 29, 10, 0)
        assert previous == _utc(2026, 3, 28, 11, 0)

    def test_dst_fall_back_in_a_non_utc_timezone(self):
        current, previous = CronSchedule(
            expr="0 12 * * *", timezone="America/New_York"
        ).fire_window(_utc(2026, 11, 1, 17, 0, 5))
        assert current == _utc(2026, 11, 1, 17, 0)
        assert previous == _utc(2026, 10, 31, 16, 0)

    @pytest.mark.parametrize(
        "key", ["previous_scheduled_at", "last_successful_scheduled_at", "window"]
    )
    def test_window_fields_are_not_overridable(self, key):
        with pytest.raises(ValidationError, match=key):
            CronSchedule(expr="0 6 * * *", payload={key: "whenever"})


class TestScheduledTickWindow:
    """A scheduled run can read the time window since its previous fire."""

    @staticmethod
    async def _tick(service, flow_id, now):
        with (
            patch(
                "preloop.services.flow_trigger_service._schedule_now",
                return_value=now,
            ),
            patch.object(
                service, "_start_flow_execution", new_callable=AsyncMock
            ) as mock_run,
        ):
            outcome = await service.run_scheduled_tick(flow_id)
        payload = (
            mock_run.call_args[1]["event_data"]["payload"] if mock_run.called else None
        )
        return outcome, payload

    @pytest.mark.asyncio
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    async def test_first_run_uses_one_period_back(
        self, mock_nats, db_session: Session, scheduled_flow: Flow
    ):
        mock_nats.return_value = AsyncMock()
        service = FlowTriggerService(db_session)
        outcome, payload = await self._tick(
            service, scheduled_flow.id, _utc(2026, 10, 4, 12, 10, 4)
        )
        assert outcome == "triggered"
        assert payload["scheduled_at"] == "2026-10-04T12:10:04+00:00"
        assert payload["previous_scheduled_at"] == "2026-10-04T12:00:00+00:00"
        assert payload["window"] == {
            "from": "2026-10-04T12:00:00+00:00",
            "to": "2026-10-04T12:10:04+00:00",
        }
        assert payload["last_successful_scheduled_at"] is None

    @pytest.mark.asyncio
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    async def test_skipped_overlap_moves_the_window_not_the_last_success(
        self, mock_nats, db_session: Session, scheduled_flow: Flow
    ):
        """After a skipped tick, previous_scheduled_at is the skipped fire;
        last_successful_scheduled_at still points at the last good run so an
        evaluator can choose to catch up."""
        mock_nats.return_value = AsyncMock()
        crud_flow_execution.create(
            db_session,
            obj_in=FlowExecutionCreate(
                flow_id=scheduled_flow.id,
                status="SUCCEEDED",
                trigger_event_details={
                    "source": "schedule",
                    "payload": {"scheduled_at": "2026-10-04T12:00:02+00:00"},
                },
            ),
        )
        crud_flow_execution.create(
            db_session,
            obj_in=FlowExecutionCreate(
                flow_id=scheduled_flow.id,
                status="FAILED",
                trigger_event_details={
                    "source": "schedule",
                    "payload": {"scheduled_at": "2026-10-04T12:05:00+00:00"},
                },
            ),
        )
        running = crud_flow_execution.create(
            db_session,
            obj_in=FlowExecutionCreate(flow_id=scheduled_flow.id, status="RUNNING"),
        )
        service = FlowTriggerService(db_session)

        outcome, _ = await self._tick(
            service, scheduled_flow.id, _utc(2026, 10, 4, 12, 10, 1)
        )
        assert outcome == "skipped_overlap"
        skip = (
            db_session.query(Event)
            .filter(Event.event_type == "flow_schedule_tick_skipped")
            .one()
        )
        assert skip.event_data["previous_scheduled_at"] == "2026-10-04T12:00:00+00:00"

        running.status = "SUCCEEDED"
        db_session.flush()
        outcome, payload = await self._tick(
            service, scheduled_flow.id, _utc(2026, 10, 4, 12, 20, 1)
        )
        assert outcome == "triggered"
        assert payload["previous_scheduled_at"] == "2026-10-04T12:10:00+00:00"
        assert payload["window"]["from"] == "2026-10-04T12:10:00+00:00"
        assert payload["last_successful_scheduled_at"] == "2026-10-04T12:00:02+00:00"

    @pytest.mark.asyncio
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    async def test_last_success_is_scoped_to_the_flow(
        self, mock_nats, db_session: Session, scheduled_flow: Flow, test_account
    ):
        other = crud_flow.create(
            db=db_session,
            flow_in=FlowCreate(
                name=f"Other Flow {uuid4().hex[:8]}",
                trigger_event_source="schedule",
                trigger_event_types=["schedule"],
                schedule_config=CronSchedule(expr="*/10 * * * *"),
                prompt_template="p",
                agent_type="openhands",
                agent_config={},
            ),
            account_id=test_account.id,
        )
        crud_flow_execution.create(
            db_session,
            obj_in=FlowExecutionCreate(
                flow_id=other.id,
                status="SUCCEEDED",
                trigger_event_details={
                    "payload": {"scheduled_at": "2026-10-04T12:00:02+00:00"}
                },
            ),
        )
        mock_nats.return_value = AsyncMock()
        service = FlowTriggerService(db_session)
        _, payload = await self._tick(
            service, scheduled_flow.id, _utc(2026, 10, 4, 12, 10, 4)
        )
        assert payload["last_successful_scheduled_at"] is None


class TestWindowTemplateVariables:
    """The window fields render as prompt template variables."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path, expected",
        [
            ("payload.window.from", "2026-10-04T12:00:00+00:00"),
            ("payload.window.to", "2026-10-04T12:10:04+00:00"),
            ("payload.previous_scheduled_at", "2026-10-04T12:00:00+00:00"),
            ("payload.last_successful_scheduled_at", "2026-10-04T11:50:01+00:00"),
        ],
    )
    async def test_resolves(self, path, expected):
        from preloop.services.prompt_resolvers.base import ResolverContext
        from preloop.services.prompt_resolvers.trigger_event import (
            TriggerEventResolver,
        )

        context = ResolverContext(
            db=MagicMock(),
            trigger_event_data={
                "source": "schedule",
                "type": "schedule",
                "payload": {
                    "scheduled_at": "2026-10-04T12:10:04+00:00",
                    "previous_scheduled_at": "2026-10-04T12:00:00+00:00",
                    "window": {
                        "from": "2026-10-04T12:00:00+00:00",
                        "to": "2026-10-04T12:10:04+00:00",
                    },
                    "last_successful_scheduled_at": "2026-10-04T11:50:01+00:00",
                },
            },
            flow_id="flow-1",
            execution_id="exec-1",
        )
        assert await TriggerEventResolver().resolve(path, context) == expected


@pytest.mark.asyncio
async def test_null_last_success_resolves_to_none():
    """No successful run yet: the resolver answers None, so the orchestrator
    leaves the placeholder as written (documented in flow-triggers.md)."""
    from preloop.services.prompt_resolvers.base import ResolverContext
    from preloop.services.prompt_resolvers.trigger_event import TriggerEventResolver

    context = ResolverContext(
        db=MagicMock(),
        trigger_event_data={
            "source": "schedule",
            "payload": {"last_successful_scheduled_at": None},
        },
        flow_id="flow-1",
        execution_id="exec-1",
    )
    assert (
        await TriggerEventResolver().resolve(
            "payload.last_successful_scheduled_at", context
        )
        is None
    )
