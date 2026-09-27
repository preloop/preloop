"""Schemas for the per-tracker-issue cost and cycle-time rollup (#958)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


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
        ..., description="Sum of the contributing executions' estimated_cost."
    )
    total_tokens: int
    run_count: int
    failed_run_count: int
    first_event_at: Optional[datetime] = None
    pr_opened_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None
    merged_at: Optional[datetime] = None
    first_event_to_pr_opened_hours: Optional[float] = Field(
        None, description="Blank when the pull request was not opened yet."
    )
    pr_opened_to_approved_hours: Optional[float] = Field(
        None, description="Blank when the pull request was not approved yet."
    )
    approved_to_merged_hours: Optional[float] = Field(
        None, description="Blank when the pull request was not merged yet."
    )
    execution_ids: Optional[List[UUID]] = Field(
        None, description="Contributing execution ids (JSON export only)."
    )


class IssueCostSummary(BaseModel):
    """Sum of issue rows for one project or one flow."""

    id: Optional[UUID] = None
    name: str
    issue_count: int
    estimated_cost: float
    total_tokens: int
    run_count: int
    failed_run_count: int


class IssueCostUnassigned(BaseModel):
    """Executions that could not be tied to exactly one issue."""

    estimated_cost: float = 0.0
    total_tokens: int = 0
    run_count: int = 0
    failed_run_count: int = 0
    executions: List[IssueCostExecutionRow] = Field(default_factory=list)


class IssueCostReport(BaseModel):
    """Issue rows, per-project and per-flow sums and the unassigned bucket."""

    start: Optional[datetime] = None
    end: Optional[datetime] = None
    project_id: Optional[UUID] = None
    flow_id: Optional[UUID] = None
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
