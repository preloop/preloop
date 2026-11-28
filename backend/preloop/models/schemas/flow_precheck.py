"""Schema of a flow's deterministic pre-check (#2541).

A pre-check is a short list of read-only tracker checks the server evaluates
before it dispatches an execution. When nothing matches, the tick is
recorded as a ``SKIPPED`` execution and no agent starts; when something
matches, the run starts and receives the matched items.

These models validate the shape only. Whether the named repositories belong
to the flow, and whether their tracker has a reader, needs the database and
is checked by ``preloop.services.flow_precheck.validate_precheck_scope`` when
the flow is saved and again when the pre-check is evaluated.
"""

from __future__ import annotations

from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Most checks one pre-check may hold.
MAX_PRECHECK_CHECKS = 8
#: Default and largest number of items one check returns to the run.
DEFAULT_PRECHECK_MAX_ITEMS = 25
MAX_PRECHECK_MAX_ITEMS = 50
#: Most repositories one check may name.
MAX_PRECHECK_REPOSITORIES = 10
#: Most entries in one name list (labels, authors, branches, workflows).
MAX_PRECHECK_NAMES = 20
#: Upper bound of every hour-valued setting: one year.
MAX_PRECHECK_HOURS = 24 * 365

PrecheckName = Annotated[str, Field(min_length=1, max_length=200)]
NameList = Annotated[List[PrecheckName], Field(max_length=MAX_PRECHECK_NAMES)]
Hours = Annotated[float, Field(gt=0, le=MAX_PRECHECK_HOURS)]

#: Reasons an open pull request can need attention.
PullRequestNeed = Literal[
    "failing_checks",
    "merge_conflict",
    "unresolved_review_threads",
    "changes_requested",
]


class _Strict(BaseModel):
    """Refuse unknown keys, so a typo is a 422 rather than a silent no-op."""

    model_config = ConfigDict(extra="forbid")


class PrecheckLabelFilter(_Strict):
    """Label conditions; every non-empty list must hold."""

    any: NameList = Field(
        default_factory=list, description="At least one of these labels."
    )
    all: NameList = Field(default_factory=list, description="Every one of these.")
    none: NameList = Field(default_factory=list, description="None of these.")


class PrecheckAuthorFilter(_Strict):
    """Author logins to keep or drop."""

    include: NameList = Field(
        default_factory=list,
        description="Only these authors; empty means every author.",
    )
    exclude: NameList = Field(default_factory=list, description="Never these.")


class PullRequestExclusions(_Strict):
    """Pull requests the check leaves out even when they need attention."""

    approved_and_mergeable: bool = Field(
        default=True,
        description="Leave out a pull request that is approved and mergeable.",
    )
    active_execution: bool = Field(
        default=True,
        description=(
            "Leave out a pull request a non-terminal Preloop execution is "
            "bound to (it triggered the run, or the run publishes to it)."
        ),
    )
    claim_labels: NameList = Field(
        default_factory=list,
        description="Labels that mean someone owns the pull request right now.",
    )
    claim_max_age_hours: Optional[Hours] = Field(
        default=None,
        description=(
            "A claim label applied longer ago than this is stale and ignored, "
            "as is one whose application is not among the pull request's "
            "last 100 label events. Unset: a claim label never goes stale."
        ),
    )


class _CheckBase(_Strict):
    """Fields every check carries."""

    id: str = Field(
        pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$",
        description=(
            "Name of the check in the decision document, for example "
            "'stale_prs'. Lower case letters, digits, '-' and '_'."
        ),
    )
    repositories: List[PrecheckName] = Field(
        default_factory=list,
        max_length=MAX_PRECHECK_REPOSITORIES,
        description=(
            "owner/name of the repositories to read. Empty means every "
            "repository of the flow's projects. Each one must belong to the "
            "flow's projects."
        ),
    )
    min_count: int = Field(
        default=1,
        ge=1,
        le=MAX_PRECHECK_MAX_ITEMS,
        description="The check matches when at least this many items are found.",
    )
    max_items: int = Field(
        default=DEFAULT_PRECHECK_MAX_ITEMS,
        ge=1,
        le=MAX_PRECHECK_MAX_ITEMS,
        description="Most items passed to the run; the rest set truncated.",
    )


class OpenPullRequestsCheck(_CheckBase):
    """Open pull requests that need attention and nobody owns."""

    type: Literal["open_pull_requests"]
    authors: PrecheckAuthorFilter = Field(default_factory=PrecheckAuthorFilter)
    labels: PrecheckLabelFilter = Field(default_factory=PrecheckLabelFilter)
    include_drafts: bool = False
    needs_any: List[PullRequestNeed] = Field(
        default_factory=list,
        description=(
            "Keep a pull request only when it has at least one of these "
            "reasons. Empty keeps every open pull request the filters allow."
        ),
    )
    idle_hours: Optional[Hours] = Field(
        default=None,
        description="Keep a pull request only when it was not updated for N hours.",
    )
    exclude: PullRequestExclusions = Field(default_factory=PullRequestExclusions)


class DefaultBranchCiCheck(_CheckBase):
    """The combined check state of a branch head."""

    type: Literal["default_branch_ci"]
    branch: str = Field(
        default="",
        max_length=200,
        description="Branch to read; empty means the repository default branch.",
    )
    state: Literal["failing", "passing"] = "failing"
    required_only: bool = Field(
        default=True,
        description=(
            "Count only the checks branch protection requires. Falls back to "
            "every check when the branch has no required checks or the "
            "credential cannot read branch protection."
        ),
    )


class PrecheckSinceHours(_Strict):
    """A fixed look-back window."""

    hours: Hours


class FailedCiRunsCheck(_CheckBase):
    """Workflow runs that failed since a point in time."""

    type: Literal["failed_ci_runs"]
    since: Union[Literal["last_run", "previous_tick"], PrecheckSinceHours] = Field(
        default="last_run",
        description=(
            "last_run: since the newest execution of this flow that was not "
            "skipped. previous_tick: since the schedule's previous fire time. "
            "{hours: N}: the last N hours."
        ),
    )
    branches: NameList = Field(
        default_factory=list, description="Only these branches; empty means all."
    )
    workflows: NameList = Field(
        default_factory=list,
        description="Only these workflow names; empty means all.",
    )


class IssuesCheck(_CheckBase):
    """Open issues (not pull requests) matching labels and a milestone."""

    type: Literal["issues"]
    labels: PrecheckLabelFilter = Field(default_factory=PrecheckLabelFilter)
    milestone: Optional[str] = Field(default=None, min_length=1, max_length=200)
    unassigned: bool = False
    order_by_labels: NameList = Field(
        default_factory=list,
        description=(
            "Order matched issues by the first of these labels they carry "
            "(for example P0, P1, P2), then oldest first."
        ),
    )


PrecheckCheck = Annotated[
    Union[OpenPullRequestsCheck, DefaultBranchCiCheck, FailedCiRunsCheck, IssuesCheck],
    Field(discriminator="type"),
]

#: Trigger kinds a pre-check can gate. Event triggers arrive in a later
#: release; until then a pre-check that names them is refused.
PrecheckTriggerKind = Literal["schedule", "manual"]


def _default_applies_to() -> List[PrecheckTriggerKind]:
    return ["schedule"]


class FlowPrecheck(_Strict):
    """A flow's pre-check: run only when the tracker says there is work."""

    version: Literal[1] = 1
    applies_to: List[PrecheckTriggerKind] = Field(
        default_factory=_default_applies_to,
        min_length=1,
        description=(
            "Trigger kinds that evaluate the pre-check. schedule: every tick. "
            "manual: console, API and CLI runs, unless the caller passes "
            "skip_precheck. Retries, continuations, delegated children and "
            "restricted CI runs never evaluate it."
        ),
    )
    run_if: Literal["any", "all"] = Field(
        default="any",
        description="Run when any check matches, or only when all of them do.",
    )
    on_error: Literal["fail", "run"] = Field(
        default="fail",
        description=(
            "fail: a tracker error records a FAILED execution "
            "(precheck_failed) and the agent does not start. run: the run "
            "starts and the context names the error."
        ),
    )
    max_skip_hours: Optional[Hours] = Field(
        default=None,
        description=(
            "Run anyway (forced) once the newest execution that was not "
            "skipped is older than this."
        ),
    )
    checks: List[PrecheckCheck] = Field(min_length=1, max_length=MAX_PRECHECK_CHECKS)

    @field_validator("applies_to")
    @classmethod
    def _unique_kinds(cls, value: List[str]) -> List[str]:
        """Drop repeated trigger kinds, keeping the first order."""
        return list(dict.fromkeys(value))

    @field_validator("checks")
    @classmethod
    def _unique_ids(cls, value: List[_CheckBase]) -> List[_CheckBase]:
        """Refuse two checks with the same id; the decision is keyed by it."""
        seen: set[str] = set()
        for check in value:
            if check.id in seen:
                raise ValueError(f"duplicate pre-check id '{check.id}'")
            seen.add(check.id)
        return value
