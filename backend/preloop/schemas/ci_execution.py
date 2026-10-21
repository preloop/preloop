"""The closed v1 machine trigger and persisted review correlation."""

from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote, urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def gitlab_review_url(tracker_url: str | None, slug: str, iid: int) -> str:
    """Derive an MR URL from the trusted integration host and bound repository."""
    base = (tracker_url or "https://gitlab.com").rstrip("/")
    if base.endswith("/api/v4"):
        base = base[:-7]
    parsed = urlsplit(base)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Unsupported GitLab review URL host")
    parts = slug.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Unsupported GitLab repository path")
    path = "/".join(quote(part, safe="") for part in parts)
    return f"{base}/{path}/-/merge_requests/{iid}"


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
    review_url: str | None = Field(default=None, max_length=2048)
    review_title: str | None = Field(default=None, max_length=1024)
    review_description: str | None = Field(default=None, max_length=16384)
    review_author: str | None = Field(default=None, max_length=255)
    review_source_branch: str | None = Field(
        default=None,
        strict=True,
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$",
    )

    @model_validator(mode="after")
    def bound_review_url(self) -> "CiReviewBinding":
        """An accepted URL cannot point outside its immutable GitLab binding."""
        if self.review_url is None and any(
            value is not None
            for value in (
                self.review_title,
                self.review_description,
                self.review_author,
                self.review_source_branch,
            )
        ):
            raise ValueError("Restricted CI review metadata requires a bound URL")
        if self.review_url is not None and (
            self.tracker_type != "gitlab"
            or self.review_url
            != gitlab_review_url(self.tracker_url, self.repository_slug, self.pr_number)
        ):
            raise ValueError("Restricted CI review URL is outside the bound repository")
        return self

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, version: object) -> object:
        """A coerced numeric value is not a stored protocol version."""
        if type(version) is not int:
            raise ValueError("CI review version must be an integer")
        return version

    @field_validator("base_branch", "review_source_branch")
    @classmethod
    def safe_review_branch(cls, branch: str | None) -> str | None:
        """Use a provider-confirmed conventional Git ref, never shell syntax."""
        if branch is None:
            return None
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
        attributes = {
            "id": binding.provider_pr_id,
            "iid": binding.pr_number,
            "title": binding.review_title or f"MR !{binding.pr_number}",
            "sha": binding.head_sha,
            "last_commit": {"id": binding.head_sha},
            "source_branch": binding.review_source_branch or binding.head_sha,
            "target_branch": binding.base_branch,
            "source_project_id": binding.repository_identifier,
            "target_project_id": binding.repository_identifier,
        }
        if binding.review_url is not None:
            attributes.update(
                {
                    "url": binding.review_url,
                    "description": binding.review_description or "",
                    "author": binding.review_author or "",
                }
            )
        payload = {
            "project": {
                "id": binding.repository_identifier,
                "path_with_namespace": binding.repository_slug,
                "default_branch": binding.base_branch,
            },
            "object_attributes": attributes,
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
