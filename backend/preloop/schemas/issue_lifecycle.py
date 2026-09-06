"""Contracts shared by triage, readiness and independent completion audits."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Criterion(BaseModel):
    """Observable acceptance, independently addressable by evidence."""

    id: str = Field(min_length=1, max_length=80)
    outcome: str = Field(min_length=1, max_length=2000)
    verification: str = Field(min_length=1, max_length=2000)
    deployment_required: bool = False


class ReadinessContract(BaseModel):
    """A proposal can be incomplete; readiness explains each missing decision."""

    model_config = ConfigDict(extra="forbid")
    issue_revision: str
    problem: str = ""
    user_outcome: str = ""
    constraints: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    blocking_decisions: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    criteria: list[Criterion] = Field(default_factory=list)
    code_entry_points: list[str] = Field(default_factory=list)
    test_entry_points: list[str] = Field(default_factory=list)
    dependencies: list[int] = Field(default_factory=list)
    environment_profile: str = ""
    test_commands: list[str] = Field(default_factory=list)


class CriterionEvidence(BaseModel):
    """Evidence is classified explicitly; code inspection is not a test run."""

    criterion_id: str
    verdict: Literal["complete", "gap", "unknown"]
    code: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)
    reason: str


class FollowUp(BaseModel):
    """A bounded, confirmed defect or enhancement; never auto-ready."""

    criterion_id: str
    kind: Literal["defect", "enhancement"]
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=6000)
    evidence: list[str] = Field(min_length=1)


class AuditResult(BaseModel):
    """Structured output accepted only from the bound independent execution."""

    status: Literal["success"]
    checked_out_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    issue_revision: str
    criteria: list[CriterionEvidence]
    follow_ups: list[FollowUp] = Field(default_factory=list, max_length=20)
