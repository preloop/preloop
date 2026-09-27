"""Project schemas for request and response validation."""

from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_serializer,
    field_validator,
)

from preloop.models.schemas.flow import (
    RepositoryBinding,
    validate_repository_bindings,
)

REPOSITORY_BINDINGS_SETTING = "repository_bindings"

_BINDINGS_ADAPTER = TypeAdapter(List[RepositoryBinding])


def normalize_project_settings(
    settings: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Validate the repository binding stored in project settings.

    ``settings["repository_bindings"]`` is the project-level default that
    every flow triggered by this (issue-only) project inherits. It uses the
    same entry shape as ``git_clone_config.repository_bindings``.

    Args:
        settings: Settings dict from a create or update request.

    Returns:
        The settings with the binding list normalized, or None.

    Raises:
        ValueError: The binding list is malformed.
    """
    if not settings or REPOSITORY_BINDINGS_SETTING not in settings:
        return settings
    raw = settings[REPOSITORY_BINDINGS_SETTING]
    if raw is None:
        return settings
    bindings = validate_repository_bindings(_BINDINGS_ADAPTER.validate_python(raw))
    return {
        **settings,
        REPOSITORY_BINDINGS_SETTING: [
            binding.model_dump(mode="json") for binding in bindings
        ],
    }


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

    @field_validator("settings")
    @classmethod
    def validate_settings(cls, value: Optional[Dict]) -> Optional[Dict]:
        """Validate the repository binding default when present."""
        return normalize_project_settings(value)


class ProjectUpdate(BaseModel):
    """Model for updating a project."""

    name: Optional[str] = Field(None, description="New project name")
    description: Optional[str] = Field(None, description="New project description")
    settings: Optional[Dict] = Field(None, description="Updated project settings")
    tracker_configurations: Optional[Dict] = Field(
        None, description="Updated issue tracker configurations"
    )

    @field_validator("settings")
    @classmethod
    def validate_settings(cls, value: Optional[Dict]) -> Optional[Dict]:
        """Validate the repository binding default when present."""
        return normalize_project_settings(value)


class ProjectResponse(ProjectBase):
    """Response model for project data."""

    id: UUID = Field(..., description="Project ID")
    organization_id: UUID = Field(..., description="Organization ID")
    created_at: str = Field(..., description="Creation timestamp")
    updated_at: str = Field(..., description="Last update timestamp")

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
