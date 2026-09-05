"""Issue schemas for request and response validation."""

from typing import Any, Dict, List, Optional
from uuid import UUID
from pydantic import (
    BaseModel,
    Field,
    model_validator,
    field_validator,
    field_serializer,
    ValidationInfo,
    ConfigDict,
)
from datetime import datetime


# Validator function
def format_datetime_optional_to_iso_string(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, str):
        # Attempt to parse and reformat, or return as is if already ISO
        try:
            # Check if it's already a valid ISO 8601 string (simplistic check)
            datetime.fromisoformat(v.replace("Z", "+00:00"))
            return v
        except ValueError:
            # If parsing fails, it might be a different format or invalid
            # For now, we'll return it as is, assuming it might be handled elsewhere
            # or it's an error to be caught by further validation if strictness is required.
            # A more robust solution might raise an error here or attempt other parsing.
            return v  # Or raise ValueError("Invalid datetime format")
    return str(v)  # Fallback, convert to string


class IssueBase(BaseModel):
    """Base model for issue data."""

    title: str = Field(..., description="Issue title")
    description: Optional[str] = Field(None, description="Issue description")
    priority: Optional[str] = Field(
        None, description="Issue priority (Low, Medium, High)"
    )
    status: Optional[str] = Field(None, description="Issue status")
    assignee: Optional[str] = Field(None, description="Issue assignee")
    labels: Optional[List[str]] = Field(None, description="Issue labels")
    meta_data: Optional[Dict[str, Any]] = Field(
        None, description="Additional issue metadata"
    )


class IssueCreate(IssueBase):
    """Model for creating a new issue."""

    # Organization fields (optional if project is provided)
    organization_id: Optional[str] = Field(None, description="Organization ID (UUID)")
    organization_name: Optional[str] = Field(None, description="Organization name")
    organization: Optional[str] = Field(
        None, description="Organization identifier (name or ID)"
    )  # Keep for flexibility

    # Project fields (at least one is required)
    project_id: Optional[str] = Field(None, description="Project ID (UUID)")
    project_name: Optional[str] = Field(None, description="Project name")
    project: Optional[str] = Field(
        None, description="Project identifier (name or ID)"
    )  # Keep for flexibility

    @model_validator(mode="before")
    @classmethod
    def check_project_or_org_provided(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        """Validate that project information is provided, organization is optional."""
        has_project = (
            values.get("project_id")
            or values.get("project_name")
            or values.get("project")
        )

        if not has_project:
            raise ValueError(
                "At least one project parameter (project_id, project_name, or project) must be provided"
            )

        # Organization is optional if project is provided.
        # The endpoint logic will handle resolving the missing piece or raising errors if ambiguous.

        return values


class IssueUpdate(BaseModel):
    """Model for updating an issue."""

    organization: Optional[str] = Field(None, description="Organization name")
    project: Optional[str] = Field(None, description="Project name")
    title: Optional[str] = Field(None, description="New issue title")
    description: Optional[str] = Field(None, description="New issue description")
    status: Optional[str] = Field(None, description="New issue status")
    priority: Optional[str] = Field(None, description="New issue priority")
    assignee: Optional[str] = Field(None, description="New issue assignee")
    labels: Optional[List[str]] = Field(None, description="New issue labels")
    meta_data: Optional[Dict[str, Any]] = Field(
        None, description="Additional issue metadata"
    )


class IssueResponse(IssueBase):
    """Response model for issue data."""

    id: UUID = Field(..., description="Internal Preloop database ID (UUID)")
    external_id: str = Field(..., description="Issue ID in the original tracker")
    key: str = Field(..., description="Human-readable issue key (e.g., PROJ-123)")
    organization: str = Field(..., description="Organization name")
    project: str = Field(..., description="Project name")
    project_id: UUID = Field(..., description="Project ID")
    project_identifier: Optional[str] = Field(
        None, description="Project identifier/slug for API calls"
    )
    url: str = Field(..., description="URL to the issue in the original tracker")
    created_at: str = Field(..., description="Creation timestamp")
    updated_at: str = Field(..., description="Last update timestamp")
    comments: Optional[List[Dict[str, Any]]] = Field(
        default=[], description="List of comments on the issue"
    )
    score: Optional[float] = Field(
        None, description="Similarity score for search results (if applicable)"
    )

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def format_datetime_fields_to_str(
        cls, v: Any, info: ValidationInfo
    ) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, str):
            return v
        raise TypeError(
            f"Field '{info.field_name}' must be a datetime object or a string, got {type(v).__name__}"
        )

    @field_serializer("id", "project_id")
    def serialize_uuid(self, value: Optional[UUID]) -> Optional[str]:
        """Serialize UUID to string."""
        return str(value) if value else None

    model_config = ConfigDict(from_attributes=True)


class IssueSearchResults(BaseModel):
    """Response model for issue search results."""

    items: List[IssueResponse] = Field(..., description="Search result items")
    total: int = Field(..., description="Total number of matching issues")
    query: str = Field(..., description="Search query used")


class IssueListResponse(BaseModel):
    """Paginated list of issues for a project or tracker."""

    items: List[IssueResponse] = Field(..., description="Issue list items")
    total: int = Field(..., description="Total number of matching issues")
    skip: int = Field(..., description="Number of issues skipped")
    limit: int = Field(..., description="Page size")
