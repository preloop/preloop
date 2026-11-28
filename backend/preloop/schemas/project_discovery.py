"""Read-only project discovery API contract."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

Source = Literal["sessions", "tracker"]
Coverage = Literal["observed", "unknown"]


class ProjectDiscoveryCoverage(BaseModel):
    """Observed means eligible evidence was seen in the requested window."""

    sessions: Coverage = "unknown"
    tracker: Coverage = "unknown"


class ProjectDiscoveryEvidence(BaseModel):
    """Latest dated observation of a distinct work item, push or session."""

    reason: Literal[
        "session_activity",
        "repository_push",
        "issue_activity",
        "pull_request_activity",
        "pull_request_merged",
    ]
    at: datetime
    ref: str
    session_id: UUID | None = None
    sources: list[Source]


class ProjectDiscoveryCandidate(BaseModel):
    """A tracker project, observed repository or unresolved session workspace."""

    id: str
    label: str
    project_id: UUID | None = None
    repository_id: str | None = Field(
        default=None,
        description="Provider, host and immutable provider repository identifier, when known.",
    )
    repository: str | None = Field(
        default=None, description="Host-qualified repository when known."
    )
    possible_project_ids: list[UUID] = Field(
        default_factory=list, description="Up to 20 visible matches for confirmation."
    )
    possible_project_count: int = 0
    evidence_count: int = 0
    evidence: list[ProjectDiscoveryEvidence] = Field(
        default_factory=list, description="Up to eight latest distinct items."
    )
    coverage: ProjectDiscoveryCoverage = Field(default_factory=ProjectDiscoveryCoverage)


class ProjectDiscovery(BaseModel):
    """Ranked candidates for an inclusive UTC window."""

    start: datetime
    end: datetime
    coverage: ProjectDiscoveryCoverage
    candidates: list[ProjectDiscoveryCandidate]
    truncated: bool = Field(
        description="An input row cap or the candidate limit was reached."
    )
