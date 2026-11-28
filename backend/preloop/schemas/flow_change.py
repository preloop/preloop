"""Closed inputs and review records for the flow definition tools."""

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator


class ClosedModel(BaseModel):
    """Reject unrecognized fields at every proposal input boundary."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class InspectFlows(ClosedModel):
    """Supply a flow ID for detail, or a cursor for the next list page."""

    project_id: UUID
    flow_id: UUID | None = None
    after: UUID | None = None

    @model_validator(mode="after")
    def one_operation(self) -> "InspectFlows":
        """Cursors apply only to the list operation."""
        if self.flow_id and self.after:
            raise ValueError("after is only valid when listing flows")
        return self


class FlowTarget(ClosedModel):
    """One flow explicitly scoped to one project."""

    project_id: UUID
    flow_id: UUID


class RunSettings(ClosedModel):
    """Numeric edits leave credentials, tool grants and schedule payloads intact."""

    timeout_seconds: Annotated[int, Field(strict=True, ge=60, le=86400)] | None = None
    max_budget: Annotated[float, Field(strict=True, gt=0, le=1_000_000)] | None = None
    max_iterations: Annotated[int, Field(strict=True, ge=1, le=1_000_000)] | None = None
    interval_every: Annotated[int, Field(strict=True, ge=1, le=527040)] | None = Field(
        default=None,
        description=(
            "Change the count of an existing interval schedule in its current unit."
        ),
    )

    @model_validator(mode="after")
    def require_values(self) -> "RunSettings":
        """Require at least one explicit finite ceiling or interval."""
        if not self.model_fields_set or any(
            getattr(self, field) is None for field in self.model_fields_set
        ):
            raise ValueError("Supply at least one non-null setting")
        return self


class Observation(ClosedModel):
    """Caller-reported measurements; no execution contents are copied."""

    metric: Literal["duration_seconds", "cost_usd", "iterations", "runs_per_day"]
    observed_mean: Annotated[float, Field(strict=True, ge=0)]
    sample_count: Annotated[int, Field(strict=True, ge=1, le=1_000_000)]


Reason = Literal["excessive_cost", "run_timeouts", "iteration_limit", "run_frequency"]
Outcome = Literal[
    "lower_run_cost", "fewer_timeouts", "more_completed_runs", "adjust_run_frequency"
]


class ProposeFlowChange(FlowTarget):
    """Review material for a change at one exact configuration revision."""

    base_revision: Annotated[int, Field(strict=True, ge=1)]
    changes: RunSettings
    reason: Reason
    evidence: Observation
    expected_outcome: Outcome
    evaluation_window_days: Annotated[int, Field(strict=True, ge=1, le=365)]


class ReadFlowProposal(FlowTarget):
    """Read the stored proposal by its canonical SHA-256 digest."""

    digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class SettingChange(ClosedModel):
    """The stored numeric value before and after one proposed edit."""

    field: Literal["timeout_seconds", "max_budget", "max_iterations", "interval_every"]
    before: int | float | None
    after: int | float


class ProposalDocument(FlowTarget):
    """Immutable review content, with server-resolved scope and authorship."""

    version: Literal[1] = 1
    account_id: UUID
    proposer_user_id: UUID
    proposer_execution_id: UUID
    base_revision: int
    diff: list[SettingChange]
    interval_unit: Literal["minutes", "hours", "days"] | None
    reason: Reason
    evidence: Observation
    expected_outcome: Outcome
    evaluation_window_days: int
    # This version has no operation capable of changing access.
    access_changes: tuple[()] = ()


class ProposalReceipt(ClosedModel):
    """The canonical document and its review identifier."""

    digest: str
    document: ProposalDocument


class ProposalReview(ProposalReceipt):
    """The stored review with a fresh target revision for comparison."""

    current_revision: int


def _tool(name: str, description: str, request: type[ClosedModel]) -> dict[str, Any]:
    """Generate the catalog and wire schemas from the same request model."""
    arguments = create_model(
        request.__name__ + "Arguments", __base__=ClosedModel, request=(request, ...)
    )
    return dict(
        name=name,
        description=description,
        source="builtin",
        requires_tracker=True,
        required_tracker_types=[],
        schema=arguments.model_json_schema(),
    )


FLOW_DEFINITION_TOOLS = (
    _tool(
        "inspect_flows",
        "List visible flows in the calling flow's single project, or inspect one "
        "current redacted definition.",
        InspectFlows,
    ),
    _tool(
        "propose_flow_change",
        "Record a reviewable change to run limits or interval frequency at an "
        "exact current base revision. Submission grants no authority to apply it.",
        ProposeFlowChange,
    ),
    _tool(
        "get_flow_change_proposal",
        "Read the original scoped change proposal by its digest, including its "
        "base revision and numeric diff.",
        ReadFlowProposal,
    ),
)
