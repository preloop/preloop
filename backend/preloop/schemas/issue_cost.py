"""Schemas for the per-tracker-issue cost and cycle-time rollup (#958)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

#: Whether every run behind a cost figure had an execution cost (#1057).
#: Coverage says how much of the bucket is priced; it never says the figure is
#: what an invoice charged.
CostCoverage = Literal["complete", "partial", "unknown"]

COVERAGE_COMPLETE: CostCoverage = "complete"
COVERAGE_PARTIAL: CostCoverage = "partial"
COVERAGE_UNKNOWN: CostCoverage = "unknown"

#: Shared description of the two coverage fields, for every row shape.
COVERAGE_DESCRIPTION = (
    "Whether every contributing run had a cost estimate: complete, partial "
    "or unknown. A bucket with no runs is unknown, and a known zero counts "
    "as known. This describes execution-cost availability, never invoice "
    "accuracy."
)
#: The two counts are described separately so the generated spec says which
#: field counts priced runs and which counts unpriced ones; a consumer should
#: not have to infer it from the field name.
KNOWN_COST_RUN_COUNT_DESCRIPTION = (
    "Contributing runs that carry a cost estimate, and so are summed into "
    "estimated_cost. An explicit zero counts as known."
)
UNKNOWN_COST_RUN_COUNT_DESCRIPTION = (
    "Contributing runs with no cost estimate, for example subscription-backed "
    "runs. They contribute nothing to estimated_cost, which is therefore the "
    "subtotal of the known runs rather than total spend."
)
ATTRIBUTED_DESCRIPTION = (
    "The estimated_cost subtotal when cost_coverage is complete; null "
    "otherwise, so a partial or unknown bucket is never read as a total."
)


class IssueCostExecutionRow(BaseModel):
    """One execution that contributed to an issue (or to the unassigned bucket)."""

    execution_id: UUID
    flow_id: UUID
    flow_name: str
    status: str
    link: str = Field(
        ...,
        description=(
            "How the execution was attributed: lifecycle, resume, delegated, "
            "retry, trigger_issue, pull_request, closing_reference, ambiguous "
            "or unassigned."
        ),
    )
    pr_url: Optional[str] = None
    estimated_cost: Optional[float] = None
    total_tokens: int = 0
    start_time: datetime
    end_time: Optional[datetime] = None


class IssueCostRow(BaseModel):
    """One tracker issue with its summed cost and cycle-time milestones."""

    id: UUID
    tracker_id: UUID
    tracker_name: str
    tracker_type: str
    issue_key: str
    issue_id: Optional[UUID] = None
    title: Optional[str] = None
    issue_url: Optional[str] = None
    pr_url: Optional[str] = None
    project_id: Optional[UUID] = None
    project_name: Optional[str] = None
    estimated_cost: float = Field(
        ...,
        description=(
            "Sum of the contributing executions' estimated_cost. This is the "
            "known subtotal, not total spend: runs without a cost "
            "contribute nothing to it. Read cost_coverage next to it."
        ),
    )
    cost_coverage: CostCoverage = Field(
        ...,
        description=COVERAGE_DESCRIPTION,
    )
    known_cost_run_count: int = Field(
        ...,
        description=KNOWN_COST_RUN_COUNT_DESCRIPTION,
    )
    unknown_cost_run_count: int = Field(
        ...,
        description=UNKNOWN_COST_RUN_COUNT_DESCRIPTION,
    )
    attributed_cost_usd: Optional[float] = Field(
        None,
        description=ATTRIBUTED_DESCRIPTION,
    )
    total_tokens: int
    run_count: int
    failed_run_count: int
    first_event_at: Optional[datetime] = Field(
        None,
        description=(
            "The earliest attributed execution start of this issue, "
            "over all flows. It is not the ticket creation time: work queued "
            "before the first run is not measured. The period filter selects "
            "issues by this value."
        ),
    )
    pr_opened_at: Optional[datetime] = Field(
        None,
        description=(
            "When the linked pull request was opened. The forge's own "
            "created time wins; otherwise the time Preloop bound it to a run, "
            "otherwise the publishing run's end. pr_opened_at_source says "
            "which."
        ),
    )
    approved_at: Optional[datetime] = Field(
        None,
        description=(
            "Earliest recorded approval event on the linked pull request, at "
            "the time the forge reported for it. It is not verified "
            "mergeability: required checks, approval counts and branch "
            "restrictions are not evaluated."
        ),
    )
    merged_at: Optional[datetime] = Field(
        None,
        description=(
            "Earliest recorded merge event on the linked pull request, at the "
            "time the forge reported for it."
        ),
    )
    first_event_to_pr_opened_hours: Optional[float] = Field(
        None,
        description=(
            "Elapsed hours from first_event_at to pr_opened_at. Blank when "
            "the pull request was not opened yet or the times are out of "
            "order."
        ),
    )
    pr_opened_to_approved_hours: Optional[float] = Field(
        None,
        description=(
            "Elapsed hours from pr_opened_at to approved_at. Blank when the "
            "pull request was not approved yet or the times are out of order. "
            "Ends at a recorded approval, so it is not a ticket-to-mergeable "
            "duration."
        ),
    )
    approved_to_merged_hours: Optional[float] = Field(
        None,
        description=(
            "Elapsed hours from approved_at to merged_at. Blank when the pull "
            "request was not merged yet or the times are out of order."
        ),
    )
    pr_opened_at_source: Optional[str] = Field(
        None,
        description=(
            "Where pr_opened_at came from: forge (the pull request's own "
            "created_at), bind (when Preloop bound it to a run) or run_end "
            "(the publishing run's end)."
        ),
    )
    estimate_hours: Optional[float] = Field(
        None,
        description=(
            "Human estimate in hours as the tracker states it. Blank when "
            "the tracker has none; never derived."
        ),
    )
    estimate_hours_source: Optional[str] = Field(
        None,
        description=(
            "Where estimate_hours was read, for example "
            "jira:timeoriginalestimate, gitlab:time_estimate or "
            "label:estimate:."
        ),
    )
    estimate_points: Optional[float] = Field(
        None,
        description=(
            "Human estimate in points as the tracker states it. Blank when "
            "the tracker has none; never derived."
        ),
    )
    estimate_points_source: Optional[str] = Field(
        None,
        description=(
            "Where estimate_points was read, for example "
            "jira:customfield_10016, gitlab:weight or label:points:."
        ),
    )
    execution_ids: Optional[List[UUID]] = Field(
        None, description="Contributing execution ids (JSON export only)."
    )


class IssueCostSummary(BaseModel):
    """Sum of issue rows for one project or one flow."""

    id: Optional[UUID] = None
    name: str
    issue_count: int
    estimated_cost: float = Field(
        ...,
        description=(
            "Sum of the contributing executions' estimated_cost: the known "
            "subtotal of the bucket, not total spend."
        ),
    )
    cost_coverage: CostCoverage = Field(..., description=COVERAGE_DESCRIPTION)
    known_cost_run_count: int = Field(..., description=KNOWN_COST_RUN_COUNT_DESCRIPTION)
    unknown_cost_run_count: int = Field(
        ..., description=UNKNOWN_COST_RUN_COUNT_DESCRIPTION
    )
    attributed_cost_usd: Optional[float] = Field(
        None, description=ATTRIBUTED_DESCRIPTION
    )
    total_tokens: int
    run_count: int
    failed_run_count: int


class IssueCostUnassigned(BaseModel):
    """Executions that could not be tied to exactly one issue."""

    estimated_cost: float = Field(
        0.0,
        description=(
            "Sum of the unassigned executions' estimated_cost: the known "
            "subtotal, not total spend."
        ),
    )
    cost_coverage: CostCoverage = Field(
        COVERAGE_UNKNOWN, description=COVERAGE_DESCRIPTION
    )
    known_cost_run_count: int = Field(0, description=KNOWN_COST_RUN_COUNT_DESCRIPTION)
    unknown_cost_run_count: int = Field(
        0, description=UNKNOWN_COST_RUN_COUNT_DESCRIPTION
    )
    attributed_cost_usd: Optional[float] = Field(
        None, description=ATTRIBUTED_DESCRIPTION
    )
    total_tokens: int = 0
    run_count: int = 0
    failed_run_count: int = 0
    executions: List[IssueCostExecutionRow] = Field(default_factory=list)


class IssueCostReport(BaseModel):
    """Issue rows, per-project and per-flow sums and the unassigned bucket."""

    start: Optional[datetime] = Field(
        None,
        description=(
            "Inclusive lower bound on first_event_at. An issue whose first "
            "execution started earlier is excluded even when later events "
            "fall in the period."
        ),
    )
    end: Optional[datetime] = Field(
        None, description="Exclusive upper bound on first_event_at."
    )
    project_id: Optional[UUID] = None
    flow_id: Optional[UUID] = Field(
        None,
        description=(
            "When set, cost, token and run figures count only this flow's "
            "executions. Selected issues still report lifetime totals of that "
            "flow, and their milestones and intervals stay those of the whole "
            "issue."
        ),
    )
    issues: List[IssueCostRow]
    by_project: List[IssueCostSummary]
    by_flow: List[IssueCostSummary]
    unassigned: IssueCostUnassigned
    truncated: bool = False


class IssueCostRebuildRequest(BaseModel):
    """Window of finished executions to record."""

    start_date: datetime
    end_date: datetime

    @field_validator("start_date", "end_date")
    @classmethod
    def _utc_when_naive(cls, value: datetime) -> datetime:
        # A naive and an aware bound cannot be compared; read naive as UTC.
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @model_validator(mode="after")
    def _ordered(self) -> "IssueCostRebuildRequest":
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        return self


class IssueCostRebuildResponse(BaseModel):
    """How many executions a rebuild recorded."""

    recorded: int
    failed: int = Field(
        0, description="Executions skipped because recording them failed."
    )
    limit_reached: bool
