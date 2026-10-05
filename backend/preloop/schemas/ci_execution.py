"""The closed v1 machine trigger and persisted review correlation."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CiReviewRequest(BaseModel):
    """Caller-controlled review inputs; no runtime or configuration authority."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, revalidate_instances="always"
    )

    pr_number: int = Field(strict=True, gt=0)
    head_sha: str = Field(strict=True, pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")


class CiReviewBinding(CiReviewRequest):
    """Immutable server-owned identity, repository and provider PR snapshot."""

    version: Literal[1]
    account_id: UUID
    principal_id: UUID
    key_id: UUID
    project_id: UUID
    flow_id: UUID
    tracker_id: UUID
    repository_identifier: str = Field(min_length=1)
    repository_slug: str = Field(min_length=1)
    tracker_type: Literal["github", "gitlab"]
    tracker_url: str | None
    provider_pr_id: str = Field(min_length=1)
    base_branch: str = Field(
        strict=True,
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$",
    )

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, version: object) -> object:
        """A coerced numeric value is not a stored protocol version."""
        if type(version) is not int:
            raise ValueError("CI review version must be an integer")
        return version

    @field_validator("base_branch")
    @classmethod
    def safe_base_branch(cls, branch: str) -> str:
        """Use a provider-confirmed conventional Git ref, never shell syntax."""
        if (
            ".." in branch
            or "//" in branch
            or "/." in branch
            or branch.endswith((".", "/"))
            or any(part.endswith(".lock") for part in branch.split("/"))
        ):
            raise ValueError("Unsupported review target branch")
        return branch


def ci_review_event(binding: CiReviewBinding) -> dict[str, Any]:
    """Build provenance only from server-accepted immutable review fields."""
    repository = {
        "id": binding.repository_identifier,
        "full_name": binding.repository_slug,
        "default_branch": binding.base_branch,
    }
    if binding.tracker_type == "github":
        payload = {
            "repository": repository,
            "pull_request": {
                "id": binding.provider_pr_id,
                "number": binding.pr_number,
                "title": f"PR #{binding.pr_number}",
                "head": {
                    "sha": binding.head_sha,
                    "ref": binding.head_sha,
                    "repo": repository,
                },
                "base": {"repo": repository, "ref": binding.base_branch},
            },
        }
        event_type = "pull_request_updated"
    else:
        payload = {
            "project": {
                "id": binding.repository_identifier,
                "path_with_namespace": binding.repository_slug,
                "default_branch": binding.base_branch,
            },
            "object_attributes": {
                "id": binding.provider_pr_id,
                "iid": binding.pr_number,
                "title": f"MR !{binding.pr_number}",
                "sha": binding.head_sha,
                "last_commit": {"id": binding.head_sha},
                "source_branch": binding.head_sha,
                "target_branch": binding.base_branch,
                "source_project_id": binding.repository_identifier,
                "target_project_id": binding.repository_identifier,
            },
        }
        event_type = "merge_request_updated"
    return {
        "type": event_type,
        "source": binding.tracker_type,
        "project_id": str(binding.project_id),
        "payload": payload,
    }


class CiStopRequest(BaseModel):
    """Stop only, without arbitrary controller command payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    command: Literal["stop"]
    payload: None = None


class CiExecutionResponse(BaseModel):
    """Owned execution correlation and readiness without runtime internals."""

    model_config = ConfigDict(extra="forbid")
    id: UUID
    flow_id: UUID
    project_id: UUID
    repository_identifier: str
    pr_number: int
    provider_pr_id: str
    head_sha: str
    status: str
    start_time: datetime | None
    end_time: datetime | None
    failure_category: str | None
