"""Project schemas for request and response validation."""

from typing import Dict, Optional
from uuid import UUID

from pydantic import BaseModel, Field, ConfigDict, field_serializer


class ProjectBase(BaseModel):
    """Base model for project data."""

    name: str = Field(..., description="Project name")
    identifier: str = Field(..., description="Project identifier")
    description: Optional[str] = Field(None, description="Project description")
    settings: Optional[Dict] = Field(None, description="Project-specific settings")
    tracker_configurations: Optional[Dict] = Field(
        None, description="Issue tracker configurations"
    )


class ProjectCreate(ProjectBase):
    """Model for creating a new project."""

    organization_id: str = Field(..., description="Organization ID")


class ProjectUpdate(BaseModel):
    """Model for updating a project."""

    name: Optional[str] = Field(None, description="New project name")
    description: Optional[str] = Field(None, description="New project description")
    settings: Optional[Dict] = Field(None, description="Updated project settings")
    tracker_configurations: Optional[Dict] = Field(
        None, description="Updated issue tracker configurations"
    )


class ProjectResponse(ProjectBase):
    """Response model for project data."""

    id: UUID = Field(..., description="Project ID")
    organization_id: UUID = Field(..., description="Organization ID")
    created_at: str = Field(..., description="Creation timestamp")
    updated_at: str = Field(..., description="Last update timestamp")
    group: Optional[str] = Field(
        None,
        description=(
            "Grouping label inside the organization, for example the "
            "Bitbucket project a repository belongs to."
        ),
    )

    @field_serializer("id", "organization_id")
    def serialize_uuid(self, value: UUID) -> str:
        """Serialize UUID to string for JSON response."""
        return str(value)

    model_config = ConfigDict(from_attributes=True)


class TestConnectionRequest(BaseModel):
    """Request model for testing a project connection."""

    organization: str = Field(..., description="Organization identifier (name or ID)")
    project: str = Field(..., description="Project identifier (name or ID)")


class TestConnectionResponse(BaseModel):
    """Response model for testing a project connection."""

    success: bool = Field(..., description="Whether the connection test was successful")
    message: str = Field(..., description="Connection test result message")
    details: Optional[Dict] = Field(
        None, description="Additional details about the connection"
    )
