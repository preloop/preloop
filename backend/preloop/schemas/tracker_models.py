"""Base classes for issue tracker integrations."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, PrivateAttr

logger = logging.getLogger(__name__)


class IssueStatus(BaseModel):
    """Status information for an issue."""

    id: str = Field(..., description="Status identifier in the tracker")
    name: str = Field(..., description="User-friendly status name")
    category: str = Field(
        ..., description="Status category (e.g., 'todo', 'in_progress', 'done')"
    )


class IssuePriority(BaseModel):
    """Priority information for an issue."""

    id: str = Field(..., description="Priority identifier in the tracker")
    name: str = Field(..., description="User-friendly priority name")
    level: int = Field(
        ..., description="Numeric priority level (higher = more important)"
    )


class IssueUser(BaseModel):
    """User information for issues."""

    id: str = Field(..., description="User identifier in the tracker")
    name: str = Field(..., description="User's display name")
    email: Optional[str] = Field(None, description="User's email address")
    avatar_url: Optional[str] = Field(None, description="URL to user's avatar")


class IssueComment(BaseModel):
    """Comment on an issue."""

    id: str = Field(..., description="Comment identifier")
    body: str = Field(..., description="Comment text content")
    created_at: datetime = Field(..., description="Comment creation timestamp")
    updated_at: Optional[datetime] = Field(None, description="Comment update timestamp")
    author: IssueUser = Field(..., description="Comment author")
    url: Optional[str] = Field(None, description="URL to the comment")


class IssueRelation(BaseModel):
    """Relationship between issues."""

    relation_type: str = Field(
        ..., description="Relationship type (e.g., 'blocks', 'relates_to')"
    )
    issue_id: str = Field(..., description="ID of the related issue")
    issue_key: str = Field(..., description="Key of the related issue")
    summary: Optional[str] = Field(None, description="Summary of the related issue")


class Issue(BaseModel):
    """Standardized issue representation across trackers."""

    # Source payload for scanner ingestion; never serialized in tool responses.
    _sync_data: Dict[str, Any] = PrivateAttr(default_factory=dict)

    # Core issue data
    id: str = Field(..., description="Issue identifier in the tracker")
    key: str = Field(..., description="Issue key (e.g., PROJECT-123)")
    title: str = Field(..., description="Issue title/summary")
    description: Optional[str] = Field(None, description="Issue description/body")
    status: IssueStatus = Field(..., description="Current issue status")
    priority: Optional[IssuePriority] = Field(None, description="Issue priority")

    # Timeline data
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
    resolved_at: Optional[datetime] = Field(None, description="Resolution timestamp")

    # People
    reporter: Optional[IssueUser] = Field(
        None, description="User who reported the issue"
    )
    assignee: Optional[IssueUser] = Field(
        None, description="User assigned to the issue"
    )

    # Classification
    labels: List[str] = Field(default_factory=list, description="Issue labels/tags")
    components: List[str] = Field(default_factory=list, description="Issue components")

    # Relations
    parent: Optional[IssueRelation] = Field(
        None, description="Parent issue if applicable"
    )
    relations: List[IssueRelation] = Field(
        default_factory=list, description="Related issues"
    )

    # Comments
    comments: List[IssueComment] = Field(
        default_factory=list, description="Issue comments"
    )

    # URLs
    url: str = Field(..., description="URL to the issue in the tracker's web UI")
    api_url: Optional[str] = Field(
        None, description="URL to the issue in the tracker's API"
    )

    # Tracker metadata
    tracker_type: str = Field(..., description="Tracker type (e.g., 'jira', 'github')")
    project_key: str = Field(..., description="Project key in the tracker")

    # Custom fields
    custom_fields: Dict[str, Any] = Field(
        default_factory=dict, description="Tracker-specific custom fields"
    )


class IssueFilter(BaseModel):
    """Filter parameters for issue searches."""

    query: Optional[str] = Field(None, description="Text search query")
    status: Optional[List[str]] = Field(None, description="Status filter")
    labels: Optional[List[str]] = Field(None, description="Labels filter")
    created_after: Optional[datetime] = Field(None, description="Created after filter")
    created_before: Optional[datetime] = Field(
        None, description="Created before filter"
    )
    updated_after: Optional[datetime] = Field(None, description="Updated after filter")
    updated_before: Optional[datetime] = Field(
        None, description="Updated before filter"
    )
    assigned_to: Optional[str] = Field(None, description="Assignee filter")
    reported_by: Optional[str] = Field(None, description="Reporter filter")
    sort_by: Optional[str] = Field(None, description="Field to sort by")
    sort_direction: Optional[str] = Field(None, description="Sort direction (asc/desc)")


class IssueCreate(BaseModel):
    """Data for creating a new issue."""

    title: str = Field(..., description="Issue title/summary")
    description: Optional[str] = Field(None, description="Issue description/body")
    status: Optional[str] = Field(None, description="Initial issue status")
    priority: Optional[str] = Field(None, description="Issue priority")
    assignee: Optional[str] = Field(None, description="Assignee username or ID")
    labels: Optional[List[str]] = Field(None, description="Issue labels/tags")
    components: Optional[List[str]] = Field(None, description="Issue components")
    parent: Optional[str] = Field(None, description="Parent issue ID")
    custom_fields: Optional[Dict[str, Any]] = Field(
        None, description="Tracker-specific custom fields"
    )


class IssueUpdate(BaseModel):
    """Data for updating an existing issue."""

    title: Optional[str] = Field(None, description="New issue title/summary")
    description: Optional[str] = Field(None, description="New issue description/body")
    status: Optional[str] = Field(None, description="New issue status")
    priority: Optional[str] = Field(None, description="New issue priority")
    assignee: Optional[str] = Field(None, description="New assignee username or ID")
    labels: Optional[List[str]] = Field(None, description="New issue labels/tags")
    components: Optional[List[str]] = Field(None, description="New issue components")
    custom_fields: Optional[Dict[str, Any]] = Field(
        None, description="New tracker-specific custom fields"
    )


class ProjectMetadata(BaseModel):
    """Metadata about a project in a tracker."""

    key: str = Field(..., description="Project key in the tracker")
    name: str = Field(..., description="Project name")
    description: Optional[str] = Field(None, description="Project description")

    # Available values for issue fields
    statuses: List[IssueStatus] = Field(
        default_factory=list, description="Available statuses"
    )
    priorities: List[IssuePriority] = Field(
        default_factory=list, description="Available priorities"
    )

    # URL to the project in the tracker's web UI
    url: str = Field(..., description="URL to the project in the tracker's web UI")


class TrackerConnection(BaseModel):
    """Connection status for a tracker."""

    connected: bool = Field(..., description="Whether the connection was successful")
    message: str = Field(..., description="Status message")
    rate_limit: Optional[Dict[str, Any]] = Field(
        None, description="Rate limit information if available"
    )
    server_info: Optional[Dict[str, Any]] = Field(
        None, description="Server information if available"
    )
