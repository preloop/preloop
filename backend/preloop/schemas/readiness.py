"""Sampled configured-policy readiness; never a forge mergeability promise."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

GateState = Literal["pass", "fail", "unknown"]
ReadinessState = Literal["ready", "not_ready", "unknown"]
ReadinessCoverage = Literal["complete", "partial", "unsupported"]


class ReadinessPolicyInput(BaseModel):
    """Every gate choice must be explicit when opting a project in."""

    model_config = ConfigDict(extra="forbid")
    required_build_keys: tuple[str, ...]
    minimum_approvals: int = Field(ge=0)
    changes_requests_block: bool
    unresolved_tasks_block: bool

    @field_validator("required_build_keys")
    @classmethod
    def validate_keys(cls, keys: tuple[str, ...]) -> tuple[str, ...]:
        """Reject duplicate/empty keys rather than infer configuration."""
        if any(not key or key != key.strip() for key in keys):
            raise ValueError("Build keys must be nonempty and unpadded")
        if len(set(keys)) != len(keys):
            raise ValueError("Build keys must be unique")
        return keys


class ReadinessPolicy(ReadinessPolicyInput):
    """Immutable opt-in policy. Missing keys are deliberately not defaults."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: UUID
    required_build_keys: tuple[str, ...]
    minimum_approvals: int = Field(ge=0)
    changes_requests_block: bool
    unresolved_tasks_block: bool
    scope: Literal["configured_policy"] = "configured_policy"


class GateEvidence(BaseModel):
    """One retrieval; gates are sampled, not an atomic forge snapshot."""

    name: str
    state: GateState
    source: str
    retrieved_at: AwareDatetime
    reason: str | None = None
    source_sha: str | None = None
    target_sha: str | None = None
    probe_version: str | None = None
    strategy: str | None = None


class ReadinessObservation(BaseModel):
    """Read-only assessment for exact identities and one captured policy."""

    observation_id: UUID
    account_id: UUID
    tracker_id: UUID
    repository: str
    pr_id: int
    source_sha: str | None
    target_sha: str | None
    policy_version: UUID | None
    scope: Literal["configured_policy", "provider_enforced"] = "configured_policy"
    started_at: AwareDatetime
    completed_at: AwareDatetime
    state: ReadinessState
    coverage: ReadinessCoverage
    forge_coverage: Literal["unknown"] = "unknown"
    gates: tuple[GateEvidence, ...] = ()
    reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_interval(self) -> ReadinessObservation:
        """Evidence must fit within the published observation interval."""
        if self.completed_at < self.started_at:
            raise ValueError("Invalid observation interval")
        if any(
            gate.retrieved_at < self.started_at or gate.retrieved_at > self.completed_at
            for gate in self.gates
        ):
            raise ValueError("Gate retrieval outside observation interval")
        if self.scope == "provider_enforced" and self.state == "ready":
            raise ValueError("Provider-enforced readiness is unsupported")
        if self.state == "ready" and (
            self.coverage != "complete"
            or self.policy_version is None
            or not self.source_sha
            or not self.target_sha
            or not self.gates
            or any(gate.state != "pass" for gate in self.gates)
        ):
            raise ValueError("Ready requires complete known passing evidence")
        return self


class TicketCreationEvidence(BaseModel):
    """Authoritative Jira field, distinct from local/parser creation times."""

    created_at: AwareDatetime | None
    tracker_id: UUID
    issue_key: str
    retrieved_at: AwareDatetime
    source_field: Literal["fields.created"] = "fields.created"
    reason: str | None = None


def parse_jira_created(value: object) -> datetime | None:
    """Parse an authoritative offset-bearing field; never invent its value."""
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        return None
    return result.astimezone(UTC)
