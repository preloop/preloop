import json
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Dict, List, Literal, Optional, Union
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import (
    computed_field,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    TypeAdapter,
    field_serializer,
    field_validator,
    model_validator,
)

from preloop.models.schemas.verification import (
    EffectivePublicationPolicy,
    ResolvedVerificationPolicy,
    VerificationPolicy,
)
from preloop.services.report_publication import (
    MAX_COMMIT_MESSAGE_LENGTH,
    MAX_PATH_LENGTH,
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


class ReportPublication(BaseModel):
    """Land a generated document in the repository as a pull request.

    For flows whose agent holds no write tools: the document the run produced
    is copied into a throwaway worktree of the checkout after the agent has
    exited, committed on a stable branch keyed on the document, and offered
    through the same pull request path every other change uses. Issue #648.

    ``extra='forbid'``: a misspelled key here is a document that silently
    lands somewhere else, or not at all.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description="Whether the run publishes its report as a pull request",
    )
    source_path: Optional[str] = Field(
        default=None,
        max_length=MAX_PATH_LENGTH,
        description=(
            "Workspace relative path of the generated document, for example "
            "evidence/portfolio-report.md"
        ),
    )
    destination_path: Optional[str] = Field(
        default=None,
        max_length=MAX_PATH_LENGTH,
        description=(
            "Repository relative path the document lands at, for example PORTFOLIO.md"
        ),
    )
    branch: Optional[str] = Field(
        default=None,
        max_length=MAX_PATH_LENGTH,
        description=(
            "Branch the document is maintained on. Defaults to "
            "preloop/report/<document slug>, which is stable across runs so a "
            "re-run updates the open pull request instead of opening a second"
        ),
    )
    commit_message: Optional[str] = Field(
        default=None,
        max_length=MAX_COMMIT_MESSAGE_LENGTH,
        description="Commit subject. Defaults to 'Update <destination_path>'",
    )

    @model_validator(mode="after")
    def validate_paths(self) -> "ReportPublication":
        """An enabled block has to name both ends of the copy, safely."""
        if not self.enabled:
            return self
        from preloop.services.report_publication import (
            ReportPublicationError,
            report_branch_name,
            validated_relative_path,
        )

        if validated_relative_path(self.source_path) is None:
            raise ValueError(
                "report_publication.source_path must be a workspace relative path"
            )
        destination = validated_relative_path(self.destination_path)
        if destination is None:
            raise ValueError(
                "report_publication.destination_path must be a repository relative path"
            )
        try:
            report_branch_name(destination, self.branch)
        except ReportPublicationError as error:
            raise ValueError(str(error)) from error
        return self


class FollowUpFiling(BaseModel):
    """File the follow ups a human approved as issues in the tracker.

    Same argument as :class:`ReportPublication`, applied to the other output
    of a review: the agent holds no write tool, and the approved rows are
    turned into issues by the control plane after the agent has exited, using
    the account's configured tracker credential. Issue #687.

    It lives beside ``report_publication`` because both describe what the
    platform does with a run's outputs against the repository the flow is
    configured for, and because ``repositories[].project_id`` is where the
    tracker project is read from when this block does not name one.

    ``extra='forbid'``: a misspelled key here is an issue filed somewhere
    else, or not at all.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description=(
            "Whether approved follow ups in the run's result are filed as "
            "tracker issues after the agent has exited"
        ),
    )
    project_id: Optional[UUID] = Field(
        default=None,
        description=(
            "Tracker project the issues are filed into. Defaults to the "
            "project of the repository this flow clones, then the project "
            "that triggered the run"
        ),
    )
    labels: List[str] = Field(
        default_factory=lambda: ["preloop", "portfolio-review", "follow-up"],
        max_length=10,
        description="Labels applied to every filed issue",
    )
    max_issues: int = Field(
        default=25,
        ge=1,
        le=100,
        description=(
            "Ceiling on issues filed by one run, so a misconfigured portfolio "
            "cannot empty itself into a tracker"
        ),
    )

    @field_serializer("project_id")
    def serialize_project_id(self, value: Optional[UUID]) -> Optional[str]:
        """Serialize the project id to a string."""
        return str(value) if value is not None else None

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, value: List[str]) -> List[str]:
        """Labels are short, non-empty and deduplicated."""
        cleaned: List[str] = []
        for label in value:
            text = (label or "").strip()
            if not text:
                raise ValueError("follow_up_filing.labels may not contain empty labels")
            if len(text) > 50:
                raise ValueError(
                    "follow_up_filing.labels entries may not exceed 50 characters"
                )
            if text not in cleaned:
                cleaned.append(text)
        return cleaned


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
    publication_approval: Optional[Union[bool, Literal["required", "require"]]] = Field(
        default=None,
        description=(
            "When true, required, or require, isolated publication mints a "
            "writer lease only after a human platform approval covers each "
            "frozen candidate repository, branch, base, and head SHA. "
            "Default flows omit this and keep existing publication behaviour."
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

    report_publication: Optional[ReportPublication] = Field(
        default=None,
        description=(
            "Publish the document this run generated as a pull request, "
            "after the agent has exited and without giving it write tools"
        ),
    )

    follow_up_filing: Optional[FollowUpFiling] = Field(
        default=None,
        description=(
            "File the follow ups a human approved in this run as tracker "
            "issues, after the agent has exited and without giving it write tools"
        ),
    )

    @model_validator(mode="after")
    def validate_report_publication(self) -> "GitCloneConfig":
        """Publishing a report is a pull request, never a direct commit.

        The whole point of the path is that the document leaves through the
        pull request surface, so an enabled block with pull requests turned
        off is a configuration that cannot do what it says. Isolated
        publication_mode is a different publisher (trusted control plane);
        combining it with this block would skip the marker and publish
        nothing.
        """
        block = self.report_publication
        if block is None or not block.enabled:
            return self
        if not self.create_pull_request:
            raise ValueError(
                "report_publication requires create_pull_request: the report "
                "lands as a pull request, never as a direct commit"
            )
        if self.publication_mode == "isolated":
            raise ValueError(
                "report_publication cannot use publication_mode isolated: "
                "the report is published by the post-execution block, not "
                "the isolated publisher"
            )
        return self

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


# How many flows one flow may be allowed to call. A ceiling at all is the
# point: an allowlist is meant to be reviewable by a human operator.
CALLABLE_FLOWS_MAX_ENTRIES = 50


class CallableFlowEntry(BaseModel):
    """One delegation allowlist entry: a flow this flow may run, with ceilings.

    ``extra='forbid'`` is deliberate. A misspelled key in an allowlist is a
    ceiling that silently does not apply, so an unknown key is rejected on
    write rather than stored and ignored.
    """

    model_config = ConfigDict(extra="forbid")

    flow: str = Field(
        min_length=1,
        max_length=255,
        description=(
            "Slug or name of the flow that may be called, resolved inside the "
            "calling account. A reference that names no flow in the account is "
            "rejected on write."
        ),
    )
    max_children: Optional[int] = Field(
        default=None,
        description=(
            "Maximum number of children one execution of the calling flow may "
            "start through this entry. Unset means no per entry count ceiling."
        ),
    )
    max_usd_per_child: Optional[float] = Field(
        default=None,
        description=(
            "Maximum spend in USD for each child started through this entry. "
            "Unset means no per child cost ceiling."
        ),
    )
    allow_self: bool = Field(
        default=False,
        description=(
            "Explicit opt in to a self reference. A flow calling itself is a "
            "recursion risk, so it has to be asked for by name."
        ),
    )

    @field_validator("flow")
    @classmethod
    def strip_reference(cls, value: str) -> str:
        """Trim the reference and refuse a blank one."""
        reference = value.strip()
        if not reference:
            raise ValueError("callable_flows entry needs a flow slug or name")
        return reference

    @model_validator(mode="after")
    def validate_ceilings(self) -> "CallableFlowEntry":
        """Ceilings must be positive: zero or negative is not a budget.

        Checked here rather than with ``gt=0`` field constraints so the message
        names the entry it came from; an allowlist is read by a human.
        """
        if self.max_children is not None and self.max_children <= 0:
            raise ValueError(
                f"callable_flows entry '{self.flow}': max_children must be a "
                f"positive integer, got {self.max_children}"
            )
        if self.max_usd_per_child is not None and self.max_usd_per_child <= 0:
            raise ValueError(
                f"callable_flows entry '{self.flow}': max_usd_per_child must be "
                f"a positive number of USD, got {self.max_usd_per_child}"
            )
        return self


_callable_flows_adapter: TypeAdapter = TypeAdapter(List[CallableFlowEntry])


def validate_callable_flows_shape(
    value: Optional[List[Any]],
) -> Optional[List[CallableFlowEntry]]:
    """Validate an incoming ``callable_flows`` list: entries and duplicates.

    Account scoped resolution of each reference is a separate, database backed
    step (``preloop.services.flow_delegation``); this is the shape half.

    Raises:
        ValueError: If two entries name the same flow.
    """
    if value is None:
        return None
    entries = [
        item if isinstance(item, CallableFlowEntry) else CallableFlowEntry(**item)
        for item in value
    ]
    if len(entries) > CALLABLE_FLOWS_MAX_ENTRIES:
        raise ValueError(
            f"callable_flows supports at most {CALLABLE_FLOWS_MAX_ENTRIES} "
            f"entries, got {len(entries)}"
        )
    seen: set[str] = set()
    for entry in entries:
        key = entry.flow.casefold()
        if key in seen:
            raise ValueError(
                f"callable_flows has a duplicate entry for '{entry.flow}'; "
                "each flow may appear once, with one set of ceilings"
            )
        seen.add(key)
    return entries


def parse_callable_flows(value: Any) -> List[CallableFlowEntry]:
    """Read a stored ``callable_flows`` value into a list of entries.

    This is the reader helper every consumer should go through, because it is
    what makes NULL and ``[]`` the same thing: both mean "this flow may call
    nothing at all". Enforcement is not built yet, so nothing calls this for a
    decision; it exists so the first caller does not have to re-derive that
    equivalence.

    Raises:
        pydantic.ValidationError: If the stored value is not a valid allowlist.
    """
    if value is None:
        return []
    return _callable_flows_adapter.validate_python(value)


def callable_flows_for(flow: Any) -> List[CallableFlowEntry]:
    """Read the allowlist off a flow row or response, NULL safe."""
    return parse_callable_flows(getattr(flow, "callable_flows", None))


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

# Bounds on ScheduleBase.payload, the static trigger payload a schedule
# carries. A schedule states options (which baseline to diff against, a
# depth knob), never data: anything larger belongs to a caller who can read
# the trigger response.
MAX_SCHEDULE_PAYLOAD_KEYS = 20
MAX_SCHEDULE_PAYLOAD_BYTES = 4096


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
    payload: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Static trigger payload merged into every scheduled run, for "
            "options a schedule has no other way to state (e.g. "
            "previous_result_execution_id). Bounded and inline only."
        ),
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

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, v: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Bound the static payload and keep file seeding out of it.

        A schedule config is read on every tick and rendered in the console,
        so it carries options, not data: the cap is small on purpose, and
        ``workspace_files`` is refused because inline file seeding belongs
        to a caller who can see the response, not to a stored config.
        """
        if v is None:
            return None
        if MAX_SCHEDULE_PAYLOAD_KEYS < len(v):
            raise ValueError(
                f"schedule payload declares {len(v)} keys; max is "
                f"{MAX_SCHEDULE_PAYLOAD_KEYS}"
            )
        for reserved in ("workspace_files", "schedule", "scheduled_at"):
            if reserved in v:
                raise ValueError(f"schedule payload may not declare '{reserved}'")
        try:
            encoded = len(json.dumps(v, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"schedule payload must be JSON: {exc}") from exc
        if encoded > MAX_SCHEDULE_PAYLOAD_BYTES:
            raise ValueError(
                f"schedule payload is {encoded} bytes, which exceeds the "
                f"{MAX_SCHEDULE_PAYLOAD_BYTES} byte cap"
            )
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
    """Failure-side keys, all ignored.

    Both options were removed. The block itself is kept so a flow stored
    before the removal, or a client that still sends the keys, parses instead
    of failing with a 422.
    """

    comment_on_trigger_issue: bool = Field(
        default=False,
        description=(
            "Ignored since 2026-09. Used to post a failure comment with a "
            "redacted log tail on the triggering issue; a failed execution is "
            "an attention item on Overview instead. Kept so stored JSON and "
            "older clients still parse."
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


class ModelByLabelRule(BaseModel):
    """One complexity label to one model and reasoning effort (#851).

    The short form of a routing rule, for the common case an operator wants
    from the console: "issues labelled complexity:high run on the big model,
    thinking hard". It desugars into the same ordered rules engine as
    ``model_routing``, so the two can never disagree about a label.

    Deliberately model and effort only. Switching harness per label is what
    ``model_routing`` is for, and a field the console cannot edit is a field
    the next console save would quietly drop.
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(
        min_length=1,
        max_length=64,
        description="Label that selects this rule, matched against the issue's current labels.",
    )
    ai_model_id: Optional[UUID] = Field(
        default=None,
        description="Model to run on. Omit to keep the flow's selected model.",
    )
    reasoning_effort: Optional[Literal["low", "medium", "high"]] = Field(
        default=None,
        description="Reasoning effort for this run. Omit to leave the model's own default.",
    )

    @field_validator("label")
    @classmethod
    def strip_label(cls, value: str) -> str:
        """A label with surrounding spaces never matches; reject it early."""
        label = value.strip()
        if not label:
            raise ValueError("label must be non-empty")
        return label

    @model_validator(mode="after")
    def require_an_override(self) -> "ModelByLabelRule":
        """A rule that changes nothing is a rule somebody mis-saved."""
        if not self.ai_model_id and not self.reasoning_effort:
            raise ValueError(
                "each model_by_label rule must set ai_model_id and/or reasoning_effort"
            )
        return self

    @field_serializer("ai_model_id")
    def serialize_model_id(self, value: Optional[UUID]) -> Optional[str]:
        """Store model ids as strings inside agent_config JSON."""
        return str(value) if value is not None else None


class ModelByLabelConfig(RootModel[List[ModelByLabelRule]]):
    """``agent_config.model_by_label``: an ordered list, first match wins."""

    root: List[ModelByLabelRule] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def unique_labels(self) -> "ModelByLabelConfig":
        """A label twice means one of the two never applies."""
        seen: set[str] = set()
        for rule in self.root:
            if rule.label in seen:
                raise ValueError(f"duplicate model_by_label label '{rule.label}'")
            seen.add(rule.label)
        return self


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


class FlowExecutionLimits(BaseModel):
    """Optional per-execution ceilings inside ``agent_config.limits`` (#840).

    A flow bounds one run by wall clock (``timeout_seconds``); these bound
    what that run may spend. All three are optional and independent; unset
    means that ceiling does not apply. Values must be positive, and the
    gateway refuses a request only once the run has *reached* a ceiling, so
    the request that crosses it is allowed to complete.
    """

    model_config = ConfigDict(extra="forbid")

    max_total_tokens: Optional[int] = Field(
        default=None,
        ge=1,
        le=2_000_000_000,
        description=(
            "Hard ceiling on input+output tokens attributed to one execution. "
            "The gateway sums the run's usage before each model request and "
            "refuses once the total has reached it."
        ),
    )
    max_usd: Optional[float] = Field(
        default=None,
        gt=0,
        le=1_000_000,
        description=(
            "Hard ceiling in USD on the estimated cost attributed to one "
            "execution. Unpriced runs are not compared (an unknown cost is "
            "not an exceeded one)."
        ),
    )
    max_turns: Optional[int] = Field(
        default=None,
        ge=1,
        le=1_000_000,
        description=(
            "Hard ceiling on model requests (turns) attributed to one "
            "execution. Counted at the gateway as one turn per request."
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
    callable_flows: Optional[List[CallableFlowEntry]] = Field(
        default=None,
        description=(
            "Delegation allowlist: the flows an execution of this flow is "
            "permitted to run, each with optional per child ceilings. Unset or "
            "an empty list means no delegation, which is the default for every "
            "flow. Nothing enforces the list yet."
        ),
    )
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
            "`--runner` / `_runner` override takes precedence. If no runner "
            "in a chosen private pool has a free slot the job queues for 15 "
            "minutes then FAILS."
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
    approval_window_seconds: Optional[int] = Field(
        default=None,
        ge=60,
        le=2592000,
        description=(
            "How long a human has to answer an approval or question raised by "
            "an execution of this flow, in seconds (60s to 30 days). Leave "
            "unset to use the approval workflow's timeout, then the "
            "deployment default (300s). While the request is outstanding the "
            "execution is parked (WAITING_FOR_HUMAN): it holds no container "
            "and no runner, and the decision resumes it, so a window measured "
            "in days costs calendar time only."
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
        """Validate optional agent_config keys before persistence."""
        if not isinstance(v, dict):
            return v
        routing = v.get("model_routing")
        if routing is not None:
            ModelRoutingConfig.model_validate(routing)
        limits = v.get("limits")
        if limits is not None:
            FlowExecutionLimits.model_validate(limits)
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

    @field_validator("callable_flows")
    @classmethod
    def validate_callable_flows(cls, v):
        """Reject duplicate allowlist entries and an oversized list."""
        return validate_callable_flows_shape(v)


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
    # Catalog identity for built-in presets. Null for account flows and for
    # cloned presets, whose name is user-editable and is not identity.
    slug: Optional[str] = None
    # Catalog marker copied from the preset YAML. Not a flow column: account
    # copies inherit it from the global preset they were cloned from.
    supports_persistent: bool = False
    # Template tracking - expose in response for UI to show update notifications
    source_preset_id: Optional[UUID] = None
    prompt_customized: bool = False
    tools_customized: bool = False
    preset_update_available: bool = False
    execution_stats: Optional[Dict[str, Any]] = None
    # Computed schedule state for schedule-triggered flows (read-only)
    schedule_state: Optional[Dict[str, Any]] = None
    ai_model_name: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

    @computed_field
    @property
    def effective_publication_policy(self) -> EffectivePublicationPolicy:
        """Read-only description derived from this saved flow's configuration."""
        from preloop.services.verification import describe_effective_publication_policy

        return describe_effective_publication_policy(
            self.git_clone_config.model_dump() if self.git_clone_config else None
        )

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
    issue_key: Optional[str] = None
    project_id: Optional[str] = None
    number: Optional[int] = None
    execution_id: Optional[str] = None
    execution_status: Optional[str] = None
    execution_url: Optional[str] = None
    error: Optional[str] = None
    # True when this request reused an existing execution. Triage includes
    # completed runs of the same revision; other presets reuse active runs.
    coalesced: bool = False


class RunPresetResponse(BaseModel):
    """Result of resolving (and optionally starting) a preset run."""

    execution_id: Optional[str] = None
    flow_id: str
    flow_name: str
    flow_created: bool
    execution_url: Optional[str] = None
    results: Optional[List[RunPresetItemResult]] = None
