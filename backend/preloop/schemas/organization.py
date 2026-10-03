"""Organization schemas for request and response validation."""

from datetime import datetime
from typing import Dict, Optional
from uuid import UUID

from pydantic import BaseModel, Field, ConfigDict, field_serializer


class OrganizationBase(BaseModel):
    """Base model for organization data."""

    name: str = Field(..., description="Organization name")
    identifier: str = Field(..., description="Unique identifier for the organization")
    description: Optional[str] = Field(None, description="Organization description")
    settings: Optional[Dict] = Field(None, description="Organization-wide settings")


class OrganizationCreate(OrganizationBase):
    """Model for creating a new organization."""

    pass


class OrganizationUpdate(BaseModel):
    """Model for updating an organization."""

    name: Optional[str] = Field(None, description="New organization name")
    description: Optional[str] = Field(None, description="New organization description")
    settings: Optional[Dict] = Field(None, description="Updated organization settings")


class OrganizationResponse(OrganizationBase):
    """Response model for organization data."""

    id: UUID = Field(..., description="Organization ID")
    tracker_id: UUID = Field(..., description="Tracker ID")
    is_active: bool = Field(True, description="Whether the organization is active")
    created_at: Optional[datetime] = Field(None, description="Creation timestamp")
    updated_at: Optional[datetime] = Field(None, description="Last update timestamp")
    meta_data: Dict = Field(default_factory=dict, description="Additional metadata")

    @field_serializer("id", "tracker_id")
    def serialize_uuid(self, value: UUID) -> str:
        """Serialize UUID to string for JSON response."""
        return str(value)

    model_config = ConfigDict(
        from_attributes=True,  # Modern way of saying orm_mode = True
        # Allow arbitrary types for field validation
        arbitrary_types_allowed=True,
    )
