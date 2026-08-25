import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Dict, List, Literal, Optional, Union
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_serializer,
    field_validator,
    model_validator,
)


class GitCloneRepository(BaseModel):
    """Configuration for a single repository to clone."""

    tracker_id: UUID = Field(description="ID of the tracker (GitHub/GitLab) to use")
    project_id: Optional[UUID] = Field(
        default=None,
        description="Project ID to clone. If None, uses repository_url or trigger event",
    )
    repository_url: Optional[str] = Field(
        default=None,
        description="Repository URL to clone. If None, resolved from project or trigger",
    )
    clone_path: str = Field(
        default="workspace",
        description="Relative path where repository should be cloned",
    )
    branch: Optional[str] = Field(
        default=None, description="Branch to clone. If None, uses default branch"
    )

    @field_serializer("tracker_id", "project_id")
    def serialize_uuids(self, value: Optional[UUID]) -> Optional[str]:
        """Serialize UUID fields to strings."""
        return str(value) if value is not None else None


class GitCloneConfig(BaseModel):
    """Configuration for git clone operations before agent execution."""

    enabled: bool = Field(default=False, description="Whether git clone is enabled")
    repositories: List[GitCloneRepository] = Field(
        default_factory=list, description="List of repositories to clone"
    )
    git_user_name: Optional[str] = Field(
        default="Preloop", description="Name to use for git commits"
    )
    git_user_email: Optional[str] = Field(
        default="git@preloop.ai", description="Email to use for git commits"
    )
    source_branch: Optional[str] = Field(
        default="main", description="Branch to checkout for base code"
    )
    target_branch: Optional[str] = Field(
        default=None,
        description="Branch to create for commits (auto-generated if empty)",
    )
    create_pull_request: Optional[bool] = Field(
        default=False, description="Whether to create a Pull Request / Merge Request"
    )
    pull_request_title: Optional[str] = Field(
        default=None, description="Title for the Pull/Merge Request"
    )
    pull_request_description: Optional[str] = Field(
        default=None, description="Description for the Pull/Merge Request"
    )


class CustomCommands(BaseModel):
    """Configuration for custom commands (admin-only)."""

    enabled: bool = Field(
        default=False, description="Whether custom commands are enabled"
    )
    commands: List[str] = Field(
        default_factory=list,
        description="List of shell commands to execute before agent starts",
    )


# Minimum interval between two scheduled runs of the same flow.
MIN_SCHEDULE_INTERVAL = timedelta(minutes=5)
# Maximum interval between two scheduled runs of the same flow. Bounds
# ``IntervalSchedule.every`` so absurd values fail pydantic validation
# (HTTP 422) instead of overflowing ``timedelta``/datetime arithmetic
# (OverflowError -> HTTP 500) here or later inside APScheduler.
MAX_SCHEDULE_INTERVAL = timedelta(days=366)
# How many consecutive fire times we simulate when checking the interval.
# The simulation is anchored at the schedule's own next fire time (not a
# wall-clock horizon), so seasonal crons (e.g. "*/2 * * 1 *", January only)
# are caught no matter when validation runs. Cron minute/hour patterns
# repeat every matched hour/day, so any sub-minimum gap shows up within the
# first few matched days - well inside 200 ticks.
_SCHEDULE_CHECK_MAX_TICKS = 200


# Canonical weekday order for weekly schedules (APScheduler abbreviations).
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_WEEKDAY_LABELS = {
    "mon": "Mon",
    "tue": "Tue",
    "wed": "Wed",
    "thu": "Thu",
    "fri": "Fri",
    "sat": "Sat",
    "sun": "Sun",
}
_TIME_OF_DAY_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ScheduleBase(BaseModel):
    """Shared fields/behaviour for all schedule trigger forms."""

    timezone: str = Field(
        default="UTC",
        description="IANA timezone name the schedule is evaluated in",
    )

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        """Ensure the timezone is a valid IANA name."""
        try:
            ZoneInfo(v)
        except Exception:
            raise ValueError(f"Unknown IANA timezone: '{v}'")
        return v

    def build_trigger(self):
        """Build the APScheduler trigger for this schedule form."""
        raise NotImplementedError

    def describe(self) -> str:
        """Human-readable one-line description of the schedule."""
        raise NotImplementedError

    def next_fire_times(self, count: int = 3) -> List[datetime]:
        """Compute the next ``count`` fire times from now."""
        try:
            trigger = self.build_trigger()
        except ValueError:
            return []
        times: List[datetime] = []
        now = datetime.now(timezone.utc)
        prev: Optional[datetime] = None
        for _ in range(count):
            nxt = trigger.get_next_fire_time(
                prev, now if prev is None else prev + timedelta(microseconds=1)
            )
            if nxt is None:
                break
            times.append(nxt)
            prev = nxt
        return times

    def next_fire_time(self) -> Optional[datetime]:
        """Compute the next fire time from now, or None if it never fires."""
        times = self.next_fire_times(count=1)
        return times[0] if times else None


class CronSchedule(ScheduleBase):
    """Advanced schedule form: a raw 5-field crontab expression."""

    type: Literal["cron"] = "cron"
    expr: str = Field(
        description="5-field crontab expression (minute hour day month day_of_week)"
    )

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_cron_key(cls, data: Any) -> Any:
        """Accept the legacy field name ``cron`` as an alias for ``expr``."""
        if isinstance(data, dict) and "expr" not in data and "cron" in data:
            data = dict(data)
            data["expr"] = data.pop("cron")
        return data

    @model_validator(mode="after")
    def validate_cron(self) -> "CronSchedule":
        """Parse the cron expression and enforce the minimum interval."""
        trigger = self.build_trigger()
        # Simulate successive fire times and reject schedules that would
        # ever fire more often than MIN_SCHEDULE_INTERVAL apart (e.g.
        # "* * * * *" or irregular minute lists like "0,3 * * * *").
        # Anchored at the first fire time with no wall-clock horizon so
        # crons whose fire times are far in the future (month/day-restricted
        # schedules) are simulated too, not silently accepted.
        now = datetime.now(timezone.utc)
        prev = trigger.get_next_fire_time(None, now)
        for _ in range(_SCHEDULE_CHECK_MAX_TICKS):
            if prev is None:
                break
            nxt = trigger.get_next_fire_time(prev, prev + timedelta(microseconds=1))
            if nxt is None:
                break
            if nxt - prev < MIN_SCHEDULE_INTERVAL:
                minutes = int(MIN_SCHEDULE_INTERVAL.total_seconds() // 60)
                raise ValueError(
                    f"Schedule '{self.expr}' fires more often than the minimum "
                    f"interval of {minutes} minutes "
                    f"(e.g. {prev.isoformat()} -> {nxt.isoformat()})"
                )
            prev = nxt
        return self

    def build_trigger(self):
        """Build an APScheduler CronTrigger from this config.

        Raises:
            ValueError: If the cron expression is invalid.
        """
        from apscheduler.triggers.cron import CronTrigger

        try:
            return CronTrigger.from_crontab(self.expr, timezone=self.timezone)
        except ValueError as e:
            raise ValueError(f"Invalid cron expression '{self.expr}': {e}")

    def describe(self) -> str:
        return f"Cron '{self.expr}' ({self.timezone})"


class IntervalSchedule(ScheduleBase):
    """Friendly schedule form: run every N minutes/hours/days."""

    type: Literal["interval"] = "interval"
    every: int = Field(ge=1, description="Run every N units")
    unit: Literal["minutes", "hours", "days"] = Field(
        description="Unit of the interval"
    )

    @model_validator(mode="after")
    def validate_min_interval(self) -> "IntervalSchedule":
        """Enforce the minimum and maximum interval between runs."""
        try:
            delta = timedelta(**{self.unit: self.every})
        except OverflowError:
            delta = None
        if delta is None or delta > MAX_SCHEDULE_INTERVAL:
            max_days = MAX_SCHEDULE_INTERVAL.days
            raise ValueError(
                f"Interval of {self.every} {self.unit} exceeds the maximum "
                f"interval of {max_days} days"
            )
        if delta < MIN_SCHEDULE_INTERVAL:
            minutes = int(MIN_SCHEDULE_INTERVAL.total_seconds() // 60)
            raise ValueError(
                f"Interval of {self.every} {self.unit} is below the minimum "
                f"interval of {minutes} minutes"
            )
        return self

    def build_trigger(self):
        """Build an APScheduler IntervalTrigger from this config."""
        from apscheduler.triggers.interval import IntervalTrigger

        return IntervalTrigger(**{self.unit: self.every}, timezone=self.timezone)

    def describe(self) -> str:
        unit = self.unit[:-1] if self.every == 1 else self.unit
        return f"Every {self.every} {unit}"


class DailySchedule(ScheduleBase):
    """Friendly schedule form: run once a day at a fixed local time."""

    type: Literal["daily"] = "daily"
    at: str = Field(description="Time of day in 24h 'HH:MM' format")

    @field_validator("at")
    @classmethod
    def validate_at(cls, v: str) -> str:
        """Ensure the time of day is a valid 24h HH:MM string."""
        if not _TIME_OF_DAY_RE.match(v):
            raise ValueError(f"Invalid time of day '{v}' - expected 24h 'HH:MM'")
        return v

    def build_trigger(self):
        """Build an APScheduler CronTrigger firing daily at the given time."""
        from apscheduler.triggers.cron import CronTrigger

        hour, minute = self.at.split(":")
        return CronTrigger(hour=int(hour), minute=int(minute), timezone=self.timezone)

    def describe(self) -> str:
        return f"Daily at {self.at} ({self.timezone})"


class WeeklySchedule(ScheduleBase):
    """Friendly schedule form: run on selected weekdays at a fixed time."""

    type: Literal["weekly"] = "weekly"
    days: List[Weekday] = Field(
        min_length=1, description="Weekdays the schedule fires on (mon..sun)"
    )
    at: str = Field(description="Time of day in 24h 'HH:MM' format")

    @field_validator("at")
    @classmethod
    def validate_at(cls, v: str) -> str:
        """Ensure the time of day is a valid 24h HH:MM string."""
        if not _TIME_OF_DAY_RE.match(v):
            raise ValueError(f"Invalid time of day '{v}' - expected 24h 'HH:MM'")
        return v

    @field_validator("days")
    @classmethod
    def normalize_days(cls, v: List[str]) -> List[str]:
        """Deduplicate and order days canonically (mon..sun)."""
        return sorted(set(v), key=WEEKDAYS.index)

    def build_trigger(self):
        """Build an APScheduler CronTrigger firing weekly on the given days."""
        from apscheduler.triggers.cron import CronTrigger

        hour, minute = self.at.split(":")
        return CronTrigger(
            day_of_week=",".join(self.days),
            hour=int(hour),
            minute=int(minute),
            timezone=self.timezone,
        )

    def describe(self) -> str:
        days = ", ".join(_WEEKDAY_LABELS[d] for d in self.days)
        return f"Weekly on {days} at {self.at} ({self.timezone})"


# Discriminated union over all supported schedule forms. The friendly
# forms (interval/daily/weekly) map onto native APScheduler triggers;
# cron remains the power option.
ScheduleConfig = Annotated[
    Union[CronSchedule, IntervalSchedule, DailySchedule, WeeklySchedule],
    Field(discriminator="type"),
]

_schedule_config_adapter: TypeAdapter = TypeAdapter(ScheduleConfig)


def _normalize_legacy_schedule_config(value: Any) -> Any:
    """Map the legacy ``{"cron": ..., "timezone": ...}`` shape to the union.

    Early schedule-trigger rows/payloads had no ``type`` discriminator;
    they are always cron schedules.
    """
    if isinstance(value, dict) and "type" not in value and "cron" in value:
        value = {
            "type": "cron",
            "expr": value["cron"],
            "timezone": value.get("timezone", "UTC"),
        }
    return value


def parse_schedule_config(
    value: Any,
) -> Union[CronSchedule, IntervalSchedule, DailySchedule, WeeklySchedule]:
    """Validate a stored/incoming schedule_config value into the union type.

    Raises:
        pydantic.ValidationError: If the value is not a valid schedule config.
    """
    if isinstance(value, ScheduleBase):
        return value
    return _schedule_config_adapter.validate_python(
        _normalize_legacy_schedule_config(value)
    )


class SchedulePreviewRequest(BaseModel):
    """Request body for previewing a schedule trigger configuration."""

    schedule_config: ScheduleConfig

    @field_validator("schedule_config", mode="before")
    @classmethod
    def normalize_schedule_config(cls, v):
        """Accept the legacy untyped ``{"cron": ...}`` schedule shape."""
        return _normalize_legacy_schedule_config(v)


class SchedulePreviewResponse(BaseModel):
    """Computed preview of a schedule trigger configuration."""

    type: str
    description: str
    timezone: str
    next_run_times: List[datetime] = Field(
        description="The next run times (UTC) the schedule would fire at"
    )


class WebhookConfig(BaseModel):
    """Configuration for webhook triggers."""

    webhook_secret: str = Field(
        description="Secure token for authenticating webhook requests (auto-generated)"
    )
    dedupe_path: Optional[str] = Field(
        default=None,
        description=(
            "Dotted JSON path into the webhook body used to build a "
            "deduplication key (e.g. 'data.issue.id'). When unset, defaults "
            "to 'attachments.0.title_link' then 'data.issue.id'."
        ),
    )


class FlowBase(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    trigger_event_source: Optional[str] = None
    # Event types that trigger this flow (e.g., ['pull_request_created', 'pull_request_updated'])
    trigger_event_types: Optional[List[str]] = None
    trigger_organization_id: Optional[UUID] = None
    # Project IDs that can trigger this flow (empty/None = all projects in org)
    trigger_project_ids: Optional[List[UUID]] = None
    trigger_config: Optional[Dict[str, Any]] = None
    webhook_config: Optional[WebhookConfig] = None
    schedule_config: Optional[ScheduleConfig] = None
    prompt_template: Optional[str] = None
    ai_model_id: Optional[UUID] = None
    agent_type: Optional[str] = "openhands"
    agent_config: Optional[Dict[str, Any]] = None
    allowed_mcp_servers: Optional[List[str]] = None
    allowed_mcp_tools: Optional[List[Dict[str, Any]]] = None
    git_clone_config: Optional[GitCloneConfig] = None
    custom_commands: Optional[CustomCommands] = None
    is_preset: Optional[bool] = False
    is_enabled: Optional[bool] = True
    account_id: Optional[UUID] = None
    # Template tracking fields
    source_preset_id: Optional[UUID] = None
    source_prompt_hash: Optional[str] = None
    source_tools_hash: Optional[str] = None
    prompt_customized: Optional[bool] = False
    tools_customized: Optional[bool] = False
    preset_update_available: Optional[bool] = False
    runner_pool: Optional[str] = Field(
        default=None,
        description=(
            "Self-hosted runner pool (id, name, or label). When set, executions "
            "lease to a matching `preloop runner` instead of hosted compute. "
            "If no matching runner is online the job queues for 15 minutes then "
            "FAILS. A trigger-time `--runner` / `_runner` override takes "
            "precedence."
        ),
    )

    @field_validator("trigger_project_ids", mode="before")
    @classmethod
    def normalize_empty_project_ids(cls, v):
        """Normalize empty list to None so the DB stores NULL (wildcard)."""
        if isinstance(v, list) and len(v) == 0:
            return None
        return v

    @field_validator("schedule_config", mode="before")
    @classmethod
    def normalize_schedule_config(cls, v):
        """Accept the legacy untyped ``{"cron": ...}`` schedule shape."""
        return _normalize_legacy_schedule_config(v)


class FlowCreate(FlowBase):
    name: str
    # For webhook triggers, these can be None
    # trigger_event_source and trigger_event_type are set to 'webhook' on creation
    prompt_template: str
    agent_type: str = "openhands"
    agent_config: Dict[str, Any]
    allowed_mcp_servers: List[str] = []
    allowed_mcp_tools: List[Dict[str, Any]] = []


class FlowUpdate(FlowBase):
    pass


class FlowResponse(FlowBase):
    id: UUID
    account_id: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime
    # Template tracking - expose in response for UI to show update notifications
    source_preset_id: Optional[UUID] = None
    prompt_customized: bool = False
    tools_customized: bool = False
    preset_update_available: bool = False
    execution_stats: Optional[Dict[str, Any]] = None
    # Computed schedule state for schedule-triggered flows (read-only)
    schedule_state: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def compute_schedule_state(self) -> "FlowResponse":
        """Expose schedule state (next run etc.) for schedule triggers."""
        if self.trigger_event_source == "schedule" and self.schedule_config:
            config = self.schedule_config
            active = bool(self.is_enabled)
            next_run = config.next_fire_time() if active else None
            self.schedule_state = {
                "active": active,
                "type": config.type,
                "description": config.describe(),
                "timezone": config.timezone,
                "next_run_at": next_run.isoformat() if next_run else None,
            }
            if isinstance(config, CronSchedule):
                self.schedule_state["cron"] = config.expr
        return self

    @field_serializer(
        "id",
        "account_id",
        "ai_model_id",
        "trigger_organization_id",
        "source_preset_id",
    )
    def serialize_uuids(self, value: Optional[UUID]) -> Optional[str]:
        """Serialize UUID fields to strings."""
        return str(value) if value is not None else None

    @field_serializer("trigger_project_ids")
    def serialize_uuid_list(self, value: Optional[List[UUID]]) -> Optional[List[str]]:
        """Serialize UUID list fields to string list."""
        return [str(v) for v in value] if value is not None else None
