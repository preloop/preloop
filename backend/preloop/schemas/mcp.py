"""
Pydantic schemas for the MCP API endpoints.
"""

from pydantic import BaseModel, field_validator
from typing import Dict, Literal, Optional, List
from uuid import UUID

from preloop.schemas.issue import IssueResponse
from preloop.schemas.issue_compliance import IssueComplianceResultResponse
from preloop.schemas.issue_triage import ComplexityScheme, TriageIssue


class GetIssueRequest(BaseModel):
    """Request body for the get_issue tool.

    Unused by the MCP tool path: FastMCP calls ``get_issue`` with
    individual parameters, and runtime validation uses
    ``GET_ISSUE_SCHEMA`` via ``TRIAGE_INCLUDES``. Keep ``include`` in
    lockstep with that advertised enum; tests assert the match.
    """

    issue: str
    include: Optional[List[Literal["label_catalog", "revision"]]] = None


class GetIssueResponse(IssueResponse):
    """Response for the get_issue tool, including compliance data.

    The triage blocks below stay ``None`` unless ``get_issue`` was called with
    the matching ``include`` entry. They are read live from the tracker, not
    from the synchronized snapshot the rest of this model carries.
    """

    compliance_results: Optional[List[IssueComplianceResultResponse]] = None
    # include="label_catalog"
    label_catalog: Optional[List[Dict[str, str]]] = None
    complexity_scheme: Optional[ComplexityScheme] = None
    risk_scheme: Optional[ComplexityScheme] = None
    readiness_scheme: Optional[ComplexityScheme] = None
    # include="revision"
    expected_revision: Optional[str] = None
    provider_issue: Optional[TriageIssue] = None
    # Set whenever any include entry was requested.
    triage_limitations: Optional[List[str]] = None
    concurrency: Optional[str] = None


class CreateIssueRequest(BaseModel):
    """Request body for the create_issue tool."""

    project: str
    title: str
    description: str
    labels: Optional[List[str]] = None
    assignee: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    similarity_search: bool = True


class CreateIssueResponse(BaseModel):
    """Response for the create_issue tool."""

    issue_id: str
    status: str  # e.g., "created", "existing_duplicate_found"
    message: str
    url: Optional[str] = None

    @field_validator("issue_id", mode="before")
    @classmethod
    def validate_issue_id(cls, value: UUID | str) -> str:
        """Convert UUID to string for validation."""
        return str(value) if isinstance(value, UUID) else value


class UpdateIssueRequest(BaseModel):
    """Request body for the update_issue tool."""

    issue: str
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    assignee: Optional[str] = None
    labels: Optional[List[str]] = None


class UpdateIssueResponse(BaseModel):
    """Response for the update_issue tool."""

    issue_id: str
    status: str  # e.g., "updated", "failed"
    message: str
    url: Optional[str] = None

    @field_validator("issue_id", mode="before")
    @classmethod
    def validate_issue_id(cls, value: UUID | str) -> str:
        """Convert UUID to string for validation."""
        return str(value) if isinstance(value, UUID) else value


class SearchRequest(BaseModel):
    """Request body for the search tool."""

    query: str
    project: Optional[str] = None
    limit: Optional[int] = 10


# The response for the search tool can reuse the existing SearchResponse
# from the main API, but we'll define it here for clarity if needed.
# For now, we can rely on the endpoint to return the correct structure.


class EstimateComplianceRequest(BaseModel):
    """Request body for the estimate_compliance tool."""

    issues: List[str]


class ProcessingMetadata(BaseModel):
    """Metadata about processing results."""

    total_requested: int
    successfully_processed: int
    failed_count: int
    failed_issues: List[str] = []
    errors: List[str] = []


class EstimateComplianceResponse(BaseModel):
    """Response for the estimate_compliance tool."""

    results: List[IssueComplianceResultResponse]
    metadata: Optional[ProcessingMetadata] = None


class SuggestedUpdate(BaseModel):
    """A suggested tool call to update an issue."""

    tool_name: str = "update_issue"
    issue_identifier: str
    arguments: UpdateIssueRequest

    @field_validator("issue_identifier", mode="before")
    @classmethod
    def validate_issue_identifier(cls, value: UUID | str) -> str:
        """Convert UUID to string for validation."""
        return str(value) if isinstance(value, UUID) else value


class ImproveComplianceRequest(BaseModel):
    """Request body for the improve_compliance tool."""

    issues: List[str]


class ImproveComplianceResponse(BaseModel):
    """Response for the improve_compliance tool."""

    suggested_updates: List[SuggestedUpdate]
    metadata: ProcessingMetadata


class AddCommentRequest(BaseModel):
    """Request body for the add_comment tool."""

    target: str  # Issue, PR, or MR identifier (URL, key, or ID)
    comment: str
    # Optional parameters for inline/review comments
    line: Optional[int] = None  # Line number for inline comments
    path: Optional[str] = None  # File path for inline comments
    side: Optional[str] = None  # "LEFT" or "RIGHT" for diff comments
    in_reply_to: Optional[str] = None  # Comment ID to reply to (creates thread)


class AddCommentResponse(BaseModel):
    """Response for the add_comment tool."""

    comment_id: str
    status: str  # e.g., "created"
    message: str
    url: Optional[str] = None

    @field_validator("comment_id", mode="before")
    @classmethod
    def validate_comment_id(cls, value: UUID | str) -> str:
        """Convert UUID to string for validation."""
        return str(value) if isinstance(value, UUID) else value


class GetPullRequestRequest(BaseModel):
    """Request body for the get_pull_request tool."""

    pull_request: str  # PR identifier (URL, slug, or number)
    include_comments: bool = True  # Include all comments/discussions
    include_diff: bool = True  # Include file changes


class PullRequestResponse(BaseModel):
    """Response for the get_pull_request tool."""

    id: str
    number: int
    title: str
    description: Optional[str] = None
    state: str  # e.g., "open", "closed", "merged"
    author: Optional[str] = None
    assignees: List[str] = []
    reviewers: List[str] = []
    labels: List[str] = []
    url: str
    source_branch: Optional[str] = None
    target_branch: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    merged_at: Optional[str] = None
    is_draft: bool = False
    comments: List[dict] = []
    changes: Optional[dict] = None  # Diff/changes information


class CreatePullRequestRequest(BaseModel):
    """Request body for the create_pull_request tool."""

    project: str  # Project identifier (slug like "owner/repo" or project path)
    title: str
    source_branch: str  # Branch containing the changes (head)
    target_branch: str  # Branch to merge into (base)
    description: Optional[str] = None
    draft: bool = False
    assignees: Optional[List[str]] = None
    reviewers: Optional[List[str]] = None
    labels: Optional[List[str]] = None
    milestone: Optional[str] = None  # Milestone number or title
    # Extra options (primarily for GitLab)
    extra_options: Optional[dict] = (
        None  # {"squash": bool, "remove_source_branch": bool, etc.}
    )


class CreatePullRequestResponse(BaseModel):
    """Response for the create_pull_request tool."""

    pull_request_id: str
    number: int  # PR/MR number
    status: str  # e.g., "created"
    message: str
    url: str
    source_branch: str
    target_branch: str
    is_draft: bool = False

    @field_validator("pull_request_id", mode="before")
    @classmethod
    def validate_pull_request_id(cls, value) -> str:
        """Convert to string for validation."""
        return str(value) if value is not None else ""


class ReviewComment(BaseModel):
    """Inline review comment for pull request reviews."""

    path: str  # File path
    line: int  # Line number
    body: str  # Comment body
    side: Optional[str] = None  # "LEFT" or "RIGHT"


class UpdatePullRequestRequest(BaseModel):
    """Request body for the update_pull_request tool."""

    pull_request: str  # PR identifier (URL, slug, or number)
    # Metadata updates
    title: Optional[str] = None
    description: Optional[str] = None
    state: Optional[str] = None  # "open", "closed"
    assignees: Optional[List[str]] = None
    reviewers: Optional[List[str]] = None
    labels: Optional[List[str]] = None
    draft: Optional[bool] = None
    # Review submission
    review_action: Optional[str] = None  # "approve", "request_changes", "comment"
    review_body: Optional[str] = None  # Review summary comment
    review_comments: Optional[List[ReviewComment]] = None  # Inline review comments


class UpdatePullRequestResponse(BaseModel):
    """Response for the update_pull_request tool."""

    pull_request_id: str
    status: str  # e.g., "updated"
    message: str
    url: Optional[str] = None

    @field_validator("pull_request_id", mode="before")
    @classmethod
    def validate_pull_request_id(cls, value: UUID | str) -> str:
        """Convert UUID to string for validation."""
        return str(value) if isinstance(value, UUID) else value


class UpdateCommentRequest(BaseModel):
    """Request body for the update_comment tool."""

    target: str  # PR/MR identifier (URL, slug, or number)
    comment_id: str  # Comment/note ID to update
    body: Optional[str] = None  # New comment body
    resolved: Optional[bool] = None  # Whether to resolve/unresolve the thread
    thread_id: Optional[str] = None  # Thread/discussion ID for resolution
    comment_type: Optional[Literal["review_comment", "issue_comment"]] = None
    # Type of comment: review_comment for inline code comments,
    # issue_comment for PR conversation comments.
    # If not provided, tries review_comment first then issue_comment.


class UpdateCommentResponse(BaseModel):
    """Response for the update_comment tool."""

    comment_id: str
    status: str  # e.g., "updated"
    message: str
    url: Optional[str] = None

    @field_validator("comment_id", mode="before")
    @classmethod
    def validate_comment_id(cls, value: UUID | str) -> str:
        """Convert UUID to string for validation."""
        return str(value) if isinstance(value, UUID) else value


# Schemas for other tools will be added here.
