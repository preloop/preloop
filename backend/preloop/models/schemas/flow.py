import json
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Dict, List, Literal, Optional, Tuple, Union
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
from preloop.services.backport_branches import (
    branch_component,
    validate_branch_name,
)
from preloop.services.report_publication import (
    MAX_COMMIT_MESSAGE_LENGTH,
    MAX_PATH_LENGTH,
)
from preloop.services.stream_stall import (
    STREAM_IDLE_TIMEOUT_CONFIG_KEY,
    validate_stream_idle_timeout,
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


_REPOSITORY_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class RepositoryBinding(BaseModel):
    """A code-host repository that an issue-only tracker's flows work on.

    A Jira project has no git repository of its own. A binding names the
    repository on a code-host tracker the account already has (GitHub,
    GitLab, or any tracker whose client sets ``hosts_repositories``). The
    clone and push credential comes from that tracker, never from the
    issue tracker's token.
    """

    tracker_id: UUID = Field(
        description="Code-host tracker that hosts the repository and supplies "
        "the clone and push credential"
    )
    repository: str = Field(
        min_length=3,
        max_length=255,
        description="Repository path on the code host: owner/name, "
        "group/subgroup/name, or workspace/repo",
    )
    base_branch: Optional[str] = Field(
        default=None,
        max_length=255,
        description="Branch to check out and open the pull request against. "
        "When unset, the flow's source_branch is used",
    )
    default: bool = Field(
        default=False,
        description="Use this entry when the binding lists several repositories",
    )

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        """Require a slash-separated path of plain segments."""
        cleaned = value.strip().strip("/")
        if cleaned.endswith(".git"):
            cleaned = cleaned[: -len(".git")]
        segments = cleaned.split("/")
        if len(segments) < 2 or any(
            not segment
            or segment in {".", ".."}
            or not _REPOSITORY_SEGMENT_RE.match(segment)
            for segment in segments
        ):
            raise ValueError(
                "repository must be a path such as owner/name or workspace/repo"
            )
        return cleaned

    @field_validator("base_branch")
    @classmethod
    def validate_base_branch(cls, value: Optional[str]) -> Optional[str]:
        """Reject branch names git would refuse or a shell could misread."""
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if (
            cleaned.startswith(("-", "/"))
            or cleaned.endswith(("/", ".lock"))
            or ".." in cleaned
            or any(ch.isspace() or ch in "~^:?*[\\" for ch in cleaned)
        ):
            raise ValueError(f"base_branch is not a valid branch name: {value!r}")
        return cleaned

    @field_serializer("tracker_id")
    def serialize_tracker_id(self, value: UUID) -> str:
        """Serialize the tracker id to a string."""
        return str(value)


def validate_repository_bindings(
    bindings: List[RepositoryBinding],
) -> List[RepositoryBinding]:
    """Reject binding lists that cannot pick one repository.

    Args:
        bindings: Parsed binding entries.

    Returns:
        The same list.

    Raises:
        ValueError: More than one entry is marked default, or the same
            repository is listed twice.
    """
    if sum(1 for binding in bindings if binding.default) > 1:
        raise ValueError("repository_bindings may mark at most one entry as default")
    seen: set[tuple[str, str]] = set()
    for binding in bindings:
        key = (str(binding.tracker_id), binding.repository.lower())
        if key in seen:
            raise ValueError(
                f"repository_bindings lists {binding.repository} more than once"
            )
        seen.add(key)
    return bindings


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


MAX_BACKPORT_TARGETS = 10
MAX_BACKPORT_REVIEWERS = 15


class Backport(BaseModel):
    """Cherry-pick a merged pull request onto later release branches.

    Issue #961. When a pull request merges into ``source_branch``, the control
    plane cherry-picks its merge commit onto a new branch cut from each entry
    of ``target_branches``, in order, and opens one pull request per target.
    No agent runs and nothing is ever merged. A conflict is reported with the
    conflicting files and left for a person.

    This block is separate from ``GitCloneConfig.source_branch``, which names
    the branch an agent checkout starts from, a different meaning.

    ``extra='forbid'``: a misspelled key here is a backport opened against the
    wrong branch, or not at all.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description=(
            "Whether merged pull requests are backported by the control plane "
            "instead of running an agent"
        ),
    )
    source_branch: str = Field(
        description=(
            "Release branch whose merged pull requests are backported. A merge "
            "into any other branch does not start the flow"
        ),
    )
    target_branches: List[str] = Field(
        min_length=1,
        max_length=MAX_BACKPORT_TARGETS,
        description=(
            "Branches the change is cherry-picked onto, in order, for example "
            "the next release branch and then the default branch"
        ),
    )
    reviewers: List[str] = Field(
        default_factory=list,
        max_length=MAX_BACKPORT_REVIEWERS,
        description=(
            "Usernames asked to review every backport pull request. A failed "
            "review request is recorded and the pull request stays open"
        ),
    )
    comment_on_original: bool = Field(
        default=True,
        description=(
            "Post one summary comment with the status of every target on the "
            "original pull request"
        ),
    )

    @field_validator("source_branch")
    @classmethod
    def validate_source_branch(cls, value: str) -> str:
        """The source branch is a plain branch name."""
        return validate_branch_name(value)

    @field_validator("target_branches")
    @classmethod
    def validate_target_branches(cls, value: List[str]) -> List[str]:
        """Targets are plain, unique, and map to distinct backport branches."""
        cleaned: List[str] = []
        components: Dict[str, str] = {}
        for raw in value:
            name = validate_branch_name(raw)
            if name in cleaned:
                raise ValueError(f"backport.target_branches lists '{name}' twice")
            component = branch_component(name)
            if component in components:
                raise ValueError(
                    f"backport.target_branches '{components[component]}' and "
                    f"'{name}' would use the same backport branch name"
                )
            components[component] = name
            cleaned.append(name)
        return cleaned

    @field_validator("reviewers")
    @classmethod
    def validate_reviewers(cls, value: List[str]) -> List[str]:
        """Reviewer usernames are short, non-empty and deduplicated."""
        cleaned: List[str] = []
        for reviewer in value:
            text = (reviewer or "").strip().lstrip("@")
            if not text:
                raise ValueError("backport.reviewers may not contain empty names")
            if len(text) > 100 or any(ch.isspace() for ch in text):
                raise ValueError(f"backport.reviewers entry '{text[:40]}' is invalid")
            if text not in cleaned:
                cleaned.append(text)
        return cleaned

    @model_validator(mode="after")
    def validate_source_not_a_target(self) -> "Backport":
        """Backporting a branch onto itself would reopen the original change."""
        if self.source_branch in self.target_branches:
            raise ValueError(
                "backport.target_branches may not include backport.source_branch"
            )
        return self


class GitCloneConfig(BaseModel):
    """Configuration for git clone operations before agent execution."""

    enabled: bool = Field(default=False, description="Whether git clone is enabled")
    repositories: List[GitCloneRepository] = Field(
        default_factory=list, description="List of repositories to clone"
    )
    repository_bindings: List[RepositoryBinding] = Field(
        default_factory=list,
        description=(
            "Code-host repositories for flows triggered by an issue-only "
            "tracker (Jira). Used only when repositories is empty and the "
            "trigger is Jira; overrides the Jira project's default binding"
        ),
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

    backport: Optional[Backport] = Field(
        default=None,
        description=(
            "Backport merged pull requests from a release branch onto later "
            "branches. When enabled, the control plane runs the backport and "
            "no agent runs"
        ),
    )

    @field_validator("repository_bindings")
    @classmethod
    def validate_bindings(
        cls, value: List[RepositoryBinding]
    ) -> List[RepositoryBinding]:
        """At most one default, no duplicate repositories."""
        return validate_repository_bindings(value)

    @model_validator(mode="after")
    def validate_backport(self) -> "GitCloneConfig":
        """A backport run publishes its own pull requests and nothing else.

        The agent publication paths would open a second, unrelated pull
        request from an agent checkout that never runs, so they are refused
        next to an enabled backport block.
        """
        block = self.backport
        if block is None or not block.enabled:
            return self
        if self.create_pull_request:
            raise ValueError(
                "backport cannot be combined with create_pull_request: the "
                "backport opens one pull request per target branch itself"
            )
        if self.report_publication is not None and self.report_publication.enabled:
            raise ValueError("backport cannot be combined with report_publication")
        if self.follow_up_filing is not None and self.follow_up_filing.enabled:
            raise ValueError("backport cannot be combined with follow_up_filing")
        return self

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
# How far back ScheduleBase.fire_window searches for the previous fire time.
# The last scan always runs at exactly this bound; eight years covers a
# "29 February only" cron, whose two previous fires can be 2921 days apart.
_FIRE_WINDOW_MAX_LOOKBACK = timedelta(days=366 * 8)

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
        for reserved in (
            "workspace_files",
            "schedule",
            "scheduled_at",
            "previous_scheduled_at",
            "last_successful_scheduled_at",
            "window",
        ):
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

    def fire_window(self, at: datetime) -> Tuple[datetime, datetime]:
        """Return ``(current_fire, previous_fire)`` for a tick observed at ``at``.

        Both come from the schedule definition, not from execution history:
        ``current_fire`` is the latest fire time at or before ``at`` and
        ``previous_fire`` the one before it. A skipped or failed run still
        moves the window, so consecutive windows tile time without gaps.
        When the definition yields fewer than two fires (it never fired
        before ``at``), both fall back to ``at`` and ``at`` minus the
        shortest legal interval.
        """
        trigger = self.build_trigger()
        lookback = MIN_SCHEDULE_INTERVAL * 2
        while True:
            fires: List[datetime] = []
            prev: Optional[datetime] = None
            cursor = at - lookback
            while True:
                nxt = trigger.get_next_fire_time(prev, cursor)
                if nxt is None or nxt > at:
                    break
                fires.append(nxt)
                fires = fires[-2:]
                prev = nxt
                cursor = nxt + timedelta(microseconds=1)
            if len(fires) == 2:
                return (
                    fires[1].astimezone(timezone.utc),
                    fires[0].astimezone(timezone.utc),
                )
            if lookback >= _FIRE_WINDOW_MAX_LOOKBACK:
                break
            # Widen geometrically, but always finish with one scan at the
            # cap itself, so the bound is the real bound.
            lookback = min(lookback * 4, _FIRE_WINDOW_MAX_LOOKBACK)
        return at, at - MIN_SCHEDULE_INTERVAL


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

    def fire_window(self, at: datetime) -> Tuple[datetime, datetime]:
        """An interval fires relative to when its job was registered, which
        the definition does not record, so the previous fire is one period
        before this tick."""
        return at, at - timedelta(**{self.unit: self.every})

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


def flow_schedule_state(
    trigger_event_source: Optional[str],
    schedule_config: Optional[Union[ScheduleBase, Dict[str, Any]]],
    is_enabled: bool,
) -> Optional[Dict[str, Any]]:
    """Project the common schedule presentation for full and summary flows.

    Args:
        trigger_event_source: The stored flow trigger source.
        schedule_config: A validated config or its stored JSON representation.
        is_enabled: Whether this flow's schedule is active.

    Returns:
        Schedule metadata, including the next run only for active schedules;
        None for non-schedule triggers or absent configuration.
    """
    if trigger_event_source != "schedule" or not schedule_config:
        return None
    config = parse_schedule_config(schedule_config)
    next_run = config.next_fire_time() if is_enabled else None
    state = {
        "active": is_enabled,
        "type": config.type,
        "description": config.describe(),
        "timezone": config.timezone,
        "next_run_at": next_run.isoformat() if next_run else None,
    }
    if isinstance(config, CronSchedule):
        state["cron"] = config.expr
    return state


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
    """Failure-side keys: a consecutive-failure alert plus two ignored keys.

    ``comment_on_trigger_issue`` and ``attention_item`` were removed; they are
    still parsed and dropped so a flow stored before the removal, or a client
    that still sends them, parses instead of failing with a 422.
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
    alert_after_consecutive_failures: Optional[int] = Field(
        default=None,
        ge=1,
        le=100,
        description=(
            "Notify the account's owners after this many consecutive failed "
            "executions of the flow. Unset means the default of 3. A successful "
            "execution resets the streak; the alert fires once per streak."
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

    employee_secret: Optional[str] = Field(
        default=None,
        min_length=32,
        repr=False,
        json_schema_extra={"writeOnly": True},
        description="HMAC secret for signed employee-event ingress.",
    )

    webhook_secret: Optional[str] = Field(
        default=None,
        description=(
            "Secure token for authenticating webhook requests (auto-generated "
            "for webhook triggers; unset on flows triggered by tracker events)"
        ),
    )
    supersede_on_update: bool = Field(
        default=False,
        description=(
            "When a pull or merge request gets a new head, stop this flow's "
            "executions still working on an older head of the same request "
            "before starting the new one. Off by default; the Pull Request "
            "Reviewer preset turns it on."
        ),
    )
    dedupe_path: Optional[str] = Field(
        default=None,
        description=(
            "Dotted JSON path into the webhook body used to build a "
            "deduplication key (e.g. 'data.issue.id'). When unset, defaults "
            "to 'attachments.0.title_link' then 'data.issue.id'."
        ),
    )


class WebhookConfigResponse(BaseModel):
    """Webhook settings on a flow response. The ingress secret is omitted."""

    webhook_secret: Optional[str] = Field(
        default=None,
        description=(
            "Secure token for authenticating webhook requests (auto-generated "
            "for webhook triggers; unset on flows triggered by tracker events)"
        ),
    )
    supersede_on_update: bool = Field(
        default=False,
        description=(
            "When a pull or merge request gets a new head, stop this flow's "
            "executions still working on an older head of the same request "
            "before starting the new one. Off by default; the Pull Request "
            "Reviewer preset turns it on."
        ),
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


#: Top-level flow fields that are stored as ``agent_config.limits`` keys.
#: The console's flow form edits a per-run spend limit and an iteration
#: limit; the gateway enforces the ``limits`` keys on every request of a run.
FLOW_LIMIT_FIELDS: Dict[str, str] = {
    "max_budget": "max_usd",
    "max_iterations": "max_turns",
}


def apply_flow_limit_fields(
    agent_config: Optional[Dict[str, Any]], values: Dict[str, Any]
) -> Dict[str, Any]:
    """Return a copy of ``agent_config`` with flow limit fields folded in.

    Args:
        agent_config: The agent configuration to merge onto (not mutated).
        values: Limit fields the caller set, by top-level name
            (``max_budget``, ``max_iterations``). A ``None`` value clears that
            limit; a field absent from ``values`` leaves it as stored.

    Returns:
        The merged agent configuration. ``limits`` is dropped entirely when
        no limit is left, so "no limits" has one representation.
    """
    merged: Dict[str, Any] = dict(agent_config or {})
    limits: Dict[str, Any] = dict(merged.get("limits") or {})
    for field_name, limit_key in FLOW_LIMIT_FIELDS.items():
        if field_name not in values:
            continue
        value = values[field_name]
        if value is None:
            limits.pop(limit_key, None)
        else:
            limits[limit_key] = value
    if limits:
        merged["limits"] = FlowExecutionLimits.model_validate(limits).model_dump(
            exclude_none=True
        )
    else:
        merged.pop("limits", None)
    return merged


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
    review_instructions: Optional[str] = Field(
        default=None,
        max_length=32768,
        description=(
            "Blocking review rules for the Pull Request Reviewer. Same "
            "content as .preloop/review-policy.md, for a repository that "
            "cannot commit that file. Injected as "
            "{{flow.review_instructions}}. Empty means the repository file "
            "is the only source."
        ),
    )
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
    # Per-run limits as the console's flow form edits them. They are stored
    # as agent_config.limits (max_usd / max_turns), which the model gateway
    # enforces, so they are excluded from model_dump() and never reach the
    # ORM as columns. FlowResponse reads them back from agent_config.
    max_budget: Optional[float] = Field(
        default=None,
        gt=0,
        le=1_000_000,
        exclude=True,
        description=(
            "Spend limit for one execution, in USD (estimated cost). Stored "
            "as agent_config.limits.max_usd: the gateway refuses further "
            "model requests once the run has reached it and the execution "
            "fails as budget exceeded. Null clears the limit; unset leaves it "
            "unchanged."
        ),
    )
    max_iterations: Optional[int] = Field(
        default=None,
        ge=1,
        le=1_000_000,
        exclude=True,
        description=(
            "Maximum model requests (agent iterations) for one execution. "
            "Stored as agent_config.limits.max_turns and enforced by the "
            "gateway; OpenHands also uses it for its own iteration cap. Null "
            "clears the limit; unset leaves it unchanged."
        ),
    )

    @field_validator("review_instructions")
    @classmethod
    def normalize_review_instructions(cls, value: Optional[str]) -> Optional[str]:
        """Store blank instructions as NULL and reject an oversized paste."""
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("review_instructions must be a string")
        text = value.strip()
        if not text:
            return None
        if len(text) > 32768:
            raise ValueError("review_instructions must be at most 32768 characters")
        return text

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
        idle = v.get(STREAM_IDLE_TIMEOUT_CONFIG_KEY)
        if idle is not None:
            validate_stream_idle_timeout(idle)
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

    @model_validator(mode="after")
    def validate_harness_selector(self):
        """Validate the harness routing keys of agent_config (contract B).

        ``harness`` must map to ``agent_type`` (copilot_cli -> copilot,
        cursor_cli -> cursor); ``harness_fallback`` is queue,
        fallback_server (with ``fallback_model_identifier``) or fail;
        ``harness_queue_timeout_seconds`` is 60..86400. An update without
        ``agent_type`` is checked by the endpoint against the stored flow.
        """
        if isinstance(self, FlowResponse) or not isinstance(self.agent_config, dict):
            return self
        if self.agent_type is None:
            return self
        from preloop.services.host_exec import harness_selector_config_error

        error = harness_selector_config_error(self.agent_type, self.agent_config)
        if error:
            raise ValueError(error)
        return self

    @model_validator(mode="after")
    def fold_limit_fields_into_agent_config(self):
        """Store max_budget / max_iterations as agent_config.limits keys.

        Only for a payload that carries an agent_config: an update without
        one is merged onto the stored configuration by the endpoint, since
        replacing agent_config here would drop everything else in it. The
        explicit fields win over limits sent in the same agent_config.
        """
        if isinstance(self, FlowResponse):
            return self
        values = {
            name: getattr(self, name)
            for name in FLOW_LIMIT_FIELDS
            if name in self.model_fields_set
        }
        if values and self.agent_config is not None:
            self.agent_config = apply_flow_limit_fields(self.agent_config, values)
        return self


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
    # Read back from agent_config.limits (the stored, enforced values), so a
    # client edits and displays the same numbers the gateway checks.
    max_budget: Optional[float] = None
    max_iterations: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def read_limit_fields_from_agent_config(self):
        """Fill max_budget / max_iterations from agent_config.limits."""
        limits = (
            (self.agent_config or {}).get("limits")
            if isinstance(self.agent_config, dict)
            else None
        )
        limits = limits if isinstance(limits, dict) else {}
        self.max_budget = limits.get("max_usd")
        self.max_iterations = limits.get("max_turns")
        return self

    @field_serializer("webhook_config", return_type=Optional[WebhookConfigResponse])
    def redact_employee_secret(
        self, value: Optional[WebhookConfig]
    ) -> Optional[WebhookConfigResponse]:
        """Keep ingress credentials in storage but out of all Flow responses.

        A dedicated response model keeps ``webhook_secret``,
        ``supersede_on_update`` and ``dedupe_path`` in the published schema.
        A dict return type would widen the field to an untyped object.
        """
        if value is None:
            return None
        return WebhookConfigResponse.model_validate(
            value.model_dump(exclude={"employee_secret"})
        )

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
        state = flow_schedule_state(
            self.trigger_event_source, self.schedule_config, bool(self.is_enabled)
        )
        if state is not None:
            self.schedule_state = state
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


class HarnessModelOption(BaseModel):
    """One model a harness can run on the caller's runners."""

    id: str
    source: str
    runners_online: int = 0


class HarnessRunnerOption(BaseModel):
    """One runner reporting the harness (for the ``runner_id`` pin)."""

    id: UUID
    name: str
    online: bool
    eligible: bool
    reason: Optional[str] = None


class HarnessOption(BaseModel):
    """One routable harness across the runners the caller may use."""

    harness: str
    display_name: str
    agent_type: str
    billing: str
    models: List[HarnessModelOption] = Field(default_factory=list)
    runners_online: int = 0
    runners_total: int = 0
    runners: List[HarnessRunnerOption] = Field(default_factory=list)


class HarnessOptionsResponse(BaseModel):
    """``GET /api/v1/flows/harness-options`` (contract B editor endpoint)."""

    harnesses: List[HarnessOption] = Field(default_factory=list)
