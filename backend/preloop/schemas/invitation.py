"""User invitation schemas for request and response validation."""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_serializer


class InvitationCreate(BaseModel):
    """Schema for creating a user invitation."""

    email: EmailStr
    role_ids: Optional[List[UUID]] = Field(default_factory=list)
    team_ids: Optional[List[UUID]] = Field(default_factory=list)


class InvitationAccept(BaseModel):
    """Schema for accepting an invitation."""

    token: str
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=8, max_length=72)
    full_name: Optional[str] = Field(None, max_length=255)


class InvitationResponse(BaseModel):
    """Response schema for invitation data."""

    id: UUID
    account_id: UUID
    email: EmailStr
    token: str
    status: str
    role_ids: Optional[str]  # Comma-separated UUIDs
    team_ids: Optional[str]  # Comma-separated UUIDs
    invited_by: Optional[UUID]
    created_at: datetime
    accepted_at: Optional[datetime]
    expires_at: datetime

    @field_serializer("id", "account_id")
    def serialize_uuid(self, value: UUID) -> str:
        """Serialize UUID to string for JSON response."""
        return str(value)

    @field_serializer("invited_by")
    def serialize_invited_by(self, value: Optional[UUID]) -> Optional[str]:
        """Serialize UUID to string for JSON response."""
        return str(value) if value is not None else None

    model_config = ConfigDict(from_attributes=True)


class InvitationListResponse(BaseModel):
    """Response schema for paginated invitation list."""

    invitations: List[InvitationResponse]
    total: int
    skip: int
    limit: int


class InvitationPublicInfo(BaseModel):
    """Public information about an invitation (for accept page)."""

    email: Optional[EmailStr] = None
    organization_name: Optional[str]
    expires_at: datetime
    is_valid: bool
    error_message: Optional[str] = None
    role_names: Optional[List[str]] = Field(default_factory=list)
    team_names: Optional[List[str]] = Field(default_factory=list)
