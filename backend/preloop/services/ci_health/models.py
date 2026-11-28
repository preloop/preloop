"""Provider-neutral CI records the adapters produce and the aggregator reads."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

#: Normalized job and run conclusions.
SUCCESS = "success"
FAILURE = "failure"
CANCELLED = "cancelled"
SKIPPED = "skipped"
RUNNING = "running"
OTHER = "other"


@dataclass
class CiJob:
    """One job attempt.

    ``created_at`` is when the job was queued; ``started_at`` when a runner
    picked it up; ``completed_at`` when it ended. Queue wait and duration are
    derived from them.
    """

    id: str
    name: str
    conclusion: str
    created_at: Optional[datetime]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    attempt: int = 1
    url: str = ""
    failed_step: Optional[str] = None
    runner_label: Optional[str] = None

    @property
    def queue_seconds(self) -> Optional[float]:
        """Seconds between queued and started."""
        if self.created_at and self.started_at:
            return max(0.0, (self.started_at - self.created_at).total_seconds())
        return None

    @property
    def duration_seconds(self) -> Optional[float]:
        """Seconds between started and completed."""
        if self.started_at and self.completed_at:
            return max(0.0, (self.completed_at - self.started_at).total_seconds())
        return None


@dataclass
class CiRun:
    """One workflow run (GitHub) or pipeline (GitLab), latest attempt first."""

    id: str
    workflow: str
    branch: str
    event: str
    head_sha: str
    conclusion: str
    created_at: datetime
    updated_at: datetime
    attempt: int = 1
    url: str = ""
    jobs: list[CiJob] = field(default_factory=list)
    jobs_complete: bool = False

    @property
    def completed(self) -> bool:
        """Whether the run has ended (its record no longer changes)."""
        return self.conclusion != RUNNING


@dataclass
class RepositoryScan:
    """What one adapter call returned for one repository."""

    repository: str
    provider: str
    default_branch: Optional[str]
    runs: list[CiRun]
    partial: bool = False
    partial_reason: Optional[str] = None
    requests: int = 0
    rate_limit_remaining: Optional[int] = None
