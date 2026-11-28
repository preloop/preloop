"""Public projections of recorded execution configuration and outcome evidence."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class ConfigurationSelection(BaseModel):
    """A distinct definition used during one stage of this execution."""

    stage: Literal["launch", "runner_lease", "runner_delivery"]
    revision: int


class ExecutionDefinitionPage(BaseModel):
    """A page of the redacted, historical definition selected by an execution."""

    revision: int
    definition_json: str
    next_offset: int | None


class ExecutionWork(BaseModel):
    """Identifiers from the execution's recorded project and event."""

    project_id: str | None
    event_id: str | None
    repository: str | None
    reference: str | None
    pull_request: str | None
    head_sha: str | None


class ExecutionRuntimeOutcome(BaseModel):
    """Controller status and the independently observed agent facts."""

    execution_status: str
    agent_status: str | None
    agent_type: str | None
    ai_model_id: str | None


class ExecutionOutputOutcome(BaseModel):
    """Result capture, schema validation and a retained evidence record."""

    capture: Literal["unknown", "absent", "pending", "captured"]
    validation: Literal["unknown", "valid", "failed"]
    evidence_artifact_id: UUID | None


class ReviewDestination(BaseModel):
    """An accepted review on the execution's intended pull request."""

    kind: Literal["pull_request_review"]
    source: Literal["platform_review_publication", "review_publication_receipt"]
    provider: str
    repository: str
    pull_request: str
    head_sha: str | None
    provider_review_id: str | None
    record_id: UUID


class PullRequestDestination(BaseModel):
    """One confirmed pull request and the number of published targets."""

    kind: Literal["pull_request"]
    source: Literal["controller_publication", "private_publication_receipt"]
    provider: str
    repository: str
    pull_request: str
    destination_count: int


class ExecutionDelivery(BaseModel):
    """Publication state and the destination evidence supporting it."""

    state: Literal["delivered", "unconfirmed"]
    destination: ReviewDestination | PullRequestDestination | None


class ExecutionOutcome(BaseModel):
    """Bounded maintainer read for one execution, including historical unknowns."""

    execution_id: UUID
    flow_id: UUID
    creation_revision: int | None
    retry_of_execution_id: UUID | None
    configurations: list[ConfigurationSelection]
    next_offset: int | None
    work: ExecutionWork
    runtime: ExecutionRuntimeOutcome
    output: ExecutionOutputOutcome
    delivery: ExecutionDelivery
