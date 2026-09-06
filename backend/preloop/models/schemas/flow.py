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

from preloop.models.schemas.verification import (
    ResolvedVerificationPolicy,
    VerificationPolicy,
)
from preloop.utils.schedule_text import (
    WEEKDAYS,
    describe_cron,
    describe_daily,
    describe_interval,
    describe_weekly,
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
    setup_commands: List[str] = Field(
        default_factory=list,
        description=(
            "Shell commands run inside the container after clone/restore and "
            "before the agent starts (dependency install, service bring-up). "
            "Output is captured to /workspace/evidence/setup.log; a failure "
            "fails the execution with failure_category 'setup_failed'."
        ),
    )
    create_pull_request: Optional[bool] = Field(
        default=False, description="Whether to create a Pull Request / Merge Request"
    )
    publication_mode: Literal["legacy", "isolated"] = Field(
        default="legacy",
        description=(
            "Legacy publishes inside the agent container. Isolated publishes from "
            "the trusted control plane after verification, using scoped App credentials."
        ),
    )
    pull_request_template: Optional[str] = Field(
        default=None,
        max_length=512,
        description="Repository-relative PR template; otherwise conventional default then lexical first",
    )
    pull_request_title: Optional[str] = Field(
        default=None, description="Title for the Pull/Merge Request"
    )
    pull_request_description: Optional[str] = Field(
        default=None, description="Description for the Pull/Merge Request"
    )
    # Publication gate (issue #428). When set to mode "gate", the
    # post-execution push and pull-request creation run only after the
    # runner-controlled verifier allowed them for the exact commit and tree
    # being published. Absent (the default for every flow saved before the
    # gate existed) means explicitly ungated: use effective_verification_policy()
    # to show what a flow actually runs under instead of leaving it implicit.
    verification: Optional[VerificationPolicy] = Field(
        default=None,
        description=(
            "Publication gate policy: required checks a commit must pass "
            "before the flow pushes it or opens a pull request"
        ),
    )

    def effective_verification_policy(self) -> ResolvedVerificationPolicy:
        """Effective policy for this config, computed by the contract.

        Keeps "existing flows are ungated" explicit and visible: the console
        and the API can render the resolved mode and its reason instead of
        leaving the behaviour implicit in an absent key.
        """
        from preloop.services.verification import resolve_verification_policy

        return resolve_verification_policy(self.model_dump())


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


# Canonical weekday order for weekly schedules (APScheduler abbreviations),
# re-exported from the renderer so the order and the labels come from one place.
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
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
        return describe_cron(self.expr, self.timezone)


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
        return describe_interval(self.every, self.unit)


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
        return describe_daily(self.at, self.timezone)


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
        return describe_weekly(self.days, self.at, self.timezone)


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


class FlowFailureNotifications(BaseModel):
    """What to do when an execution ends FAILED or TIMEOUT."""

    comment_on_trigger_issue: bool = Field(
        default=False,
        description=(
            "Post one comment on the triggering issue with status, "
            "execution link, failure category, and the last 20 log lines."
        ),
    )
    attention_item: bool = Field(
        default=False,
        description=(
            "Ignored. Failed executions always appear as console attention "
            "items of kind ``flow`` on Overview and /console/attention. "
            "Kept so stored JSON that set this flag still parses."
        ),
    )


class FlowSuccessNotifications(BaseModel):
    """What to do when an execution succeeds."""

    comment_on_trigger_issue: bool = Field(
        default=False,
        description=(
            "Post a short 'PR opened: <url>' comment on the triggering issue "
            "when the run recorded a pull request URL."
        ),
    )


class FlowNotifications(BaseModel):
    """Per-flow terminal notifications. NULL on the row means none."""

    on_failure: FlowFailureNotifications = Field(
        default_factory=FlowFailureNotifications,
        description="Actions to take when the execution fails or times out.",
    )
    on_success: FlowSuccessNotifications = Field(
        default_factory=FlowSuccessNotifications,
        description="Actions to take when the execution succeeds.",
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


class ModelRoutingLabelMatch(BaseModel):
    """Match current issue labels. ``any`` and ``all`` are combined with AND.

    Assessment predicates are reserved for a later slice and are rejected
    here (``extra='forbid'``) so untrusted payload fields cannot sneak in.
    """

    model_config = ConfigDict(extra="forbid")

    any: Optional[List[str]] = Field(
        default=None,
        max_length=16,
        description="Match if at least one of these current labels is present.",
    )
    all: Optional[List[str]] = Field(
        default=None,
        max_length=16,
        description="Match if every one of these current labels is present.",
    )

    @field_validator("any", "all")
    @classmethod
    def normalize_label_list(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        """Reject empty strings and cap each label at 64 characters."""
        if value is None:
            return None
        cleaned: List[str] = []
        for item in value:
            label = item.strip() if isinstance(item, str) else ""
            if not label:
                raise ValueError("label names must be non-empty")
            if len(label) > 64:
                raise ValueError("label names must be at most 64 characters")
            cleaned.append(label)
        return cleaned


class ModelRoutingRule(BaseModel):
    """One ordered rule: current labels -> account-owned model and harness."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="Stable rule id recorded on the execution when this rule matches.",
    )
    labels: ModelRoutingLabelMatch
    ai_model_id: UUID
    agent_type: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def require_label_predicates(self) -> "ModelRoutingRule":
        """A rule must state at least one any/all label to match."""
        any_labels = self.labels.any or []
        all_labels = self.labels.all or []
        if not any_labels and not all_labels:
            raise ValueError("each routing rule must set labels.any and/or labels.all")
        return self

    @field_serializer("ai_model_id")
    def serialize_model_id(self, value: UUID) -> str:
        """Store model ids as strings inside agent_config JSON."""
        return str(value)


class ModelRoutingConfig(BaseModel):
    """Optional per-flow ordered model/harness routing (agent_config.model_routing)."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    rules: List[ModelRoutingRule] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def unique_rule_ids(self) -> "ModelRoutingConfig":
        """Reject duplicate rule ids so provenance stays unambiguous."""
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                raise ValueError(f"duplicate routing rule id '{rule.id}'")
            seen.add(rule.id)
        return self


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
            "Runner pool for executions of this flow. Accepts a runner id, "
            "name, or label; the literal 'auto' for any online private "
            "runner; or the literal 'server' for the hosted executor. "
            "When unset, the account default_runner_pool applies, then any "
            "online private runner, then the hosted executor. A trigger-time "
            "`--runner` / `_runner` override takes precedence. If a chosen "
            "private pool has no idle runner the job queues for 15 minutes "
            "then FAILS."
        ),
    )
    timeout_seconds: Optional[int] = Field(
        default=None,
        ge=60,
        le=86400,
        description=(
            "Wall-clock budget for one execution of this flow, in seconds. "
            "Leave unset to use the deployment default (3600). A run that "
            "exceeds the budget is stopped and fails with the timeout "
            "category, and the failure message names the budget that expired."
        ),
    )
    notifications: Optional[FlowNotifications] = Field(
        default=None,
        description=(
            "When to comment on the triggering issue and raise a console "
            "attention item after a terminal execution. Leave unset for no "
            "notifications."
        ),
    )

    @field_validator("agent_config")
    @classmethod
    def validate_model_routing_config(cls, v):
        """Validate optional agent_config.model_routing shape before persistence."""
        if not isinstance(v, dict):
            return v
        routing = v.get("model_routing")
        if routing is None:
            return v
        ModelRoutingConfig.model_validate(routing)
        return v

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


TRIAGE_BATCH_MAX = 25


class RunPresetTarget(BaseModel):
    """Issue or pull-request target for an ad hoc preset run."""

    kind: Literal["issue", "pull_request"]
    issue_id: Optional[UUID] = None
    project_id: Optional[UUID] = None
    number: Optional[int] = None

    @model_validator(mode="after")
    def validate_kind_fields(self) -> "RunPresetTarget":
        """Require the identifiers that belong to each target kind."""
        if self.kind == "issue" and self.issue_id is None:
            raise ValueError("target.issue_id is required when kind is issue")
        if self.kind == "pull_request" and (
            self.project_id is None or self.number is None
        ):
            raise ValueError(
                "target.project_id and target.number are required when "
                "kind is pull_request"
            )
        return self

    @field_serializer("issue_id", "project_id")
    def serialize_target_uuids(self, value: Optional[UUID]) -> Optional[str]:
        """Serialize UUID fields to strings."""
        return str(value) if value is not None else None


class RunPresetRequest(BaseModel):
    """Body for POST /flows/run-preset."""

    preset_slug: str
    target: Optional[RunPresetTarget] = None
    targets: Optional[List[RunPresetTarget]] = None
    confirm_create: bool = False

    @model_validator(mode="after")
    def validate_target_or_targets(self) -> "RunPresetRequest":
        """Require exactly one of ``target`` or a non-empty ``targets`` list."""
        has_target = self.target is not None
        has_targets = self.targets is not None
        if has_target == has_targets:
            raise ValueError("Provide exactly one of target or targets")
        if self.targets is not None:
            if not self.targets:
                raise ValueError("targets must be a non-empty list")
            if len(self.targets) > TRIAGE_BATCH_MAX:
                raise ValueError(f"targets supports at most {TRIAGE_BATCH_MAX} entries")
        return self


class RunPresetItemResult(BaseModel):
    """Target outcome for a preset run, including dispatch failure receipts."""

    issue_id: Optional[str] = None
    project_id: Optional[str] = None
    number: Optional[int] = None
    execution_id: Optional[str] = None
    execution_status: Optional[str] = None
    execution_url: Optional[str] = None
    error: Optional[str] = None


class RunPresetResponse(BaseModel):
    """Result of resolving (and optionally starting) a preset run."""

    execution_id: Optional[str] = None
    flow_id: str
    flow_name: str
    flow_created: bool
    execution_url: Optional[str] = None
    results: Optional[List[RunPresetItemResult]] = None
