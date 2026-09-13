"""Contracts for provider-backed issue assessment and scoped application."""

from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def provider_revision(title: str, body: str, labels: list[str], state: str) -> str:
    """Fingerprint a complete provider issue snapshot, excluding timestamps."""
    return sha256(
        json.dumps(
            [title, body, sorted(set(labels)), state], ensure_ascii=False
        ).encode()
    ).hexdigest()


class TriageIssue(BaseModel):
    title: str
    body: str
    url: str
    state: str
    labels: list[str]
    updated_at: str | None = None

    @property
    def revision(self) -> str:
        return provider_revision(self.title, self.body, self.labels, self.state)


class ComplexityScheme(BaseModel):
    name: str
    labels: list[str]
    create_missing: bool = False


class IssueTriageContext(BaseModel):
    issue: TriageIssue
    expected_revision: str
    catalogue: list[dict[str, str]]
    complexity_scheme: ComplexityScheme | None
    limitations: list[str] = Field(default_factory=list)
    concurrency: str = "optimistic preflight and verification; provider writes are not compare-and-swap"


class IssueTriageApply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    complexity_label: str | None = Field(default=None, min_length=1, max_length=255)
    assessment: str = Field(min_length=1, max_length=16000)
    title: str | None = Field(default=None, min_length=1, max_length=256)


class IssueTriageResult(BaseModel):
    status: Literal["updated", "unchanged", "conflict", "partial", "failed"]
    reason: str | None = None
    issue: TriageIssue | None = None
    operations: list[dict[str, str]] = Field(default_factory=list)
    cache_updated: bool = False
    next_action: str | None = None
