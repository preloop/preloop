"""User management schemas for request and response validation."""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_serializer,
    field_validator,
)


class UserBase(BaseModel):
    """Base user schema with common attributes."""

    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    full_name: Optional[str] = Field(None, max_length=255)


class AdminUserCreate(UserBase):
    """Schema for creating a new user."""

    model_config = {"title": "AdminUserCreate"}

    password: str = Field(..., min_length=8)
    user_source: str = Field(default="local")
    oauth_provider: Optional[str] = None
    oauth_id: Optional[str] = None
    external_id: Optional[str] = None
    is_active: bool = Field(default=True)

    @field_validator("username")
    @classmethod
    def username_alphanumeric(cls, v: str) -> str:
        """Validate that username contains only alphanumeric characters and underscores."""
        if not v.replace("_", "").isalnum():
            raise ValueError(
                "Username must contain only alphanumeric characters and underscores"
            )
        return v


class AdminUserUpdate(BaseModel):
    """Schema for updating a user."""

    model_config = {"title": "AdminUserUpdate"}

    email: Optional[EmailStr] = None
    full_name: Optional[str] = Field(None, max_length=255)
    is_active: Optional[bool] = None


class AdminUserRoleResponse(BaseModel):
    """Response schema for role data attached to admin user responses."""

    id: UUID
    name: str
    description: Optional[str] = None
    team_name: Optional[str] = None

    @field_serializer("id")
    def serialize_uuid(self, value: UUID) -> str:
        """Serialize UUID to string for JSON response."""
        return str(value)

    model_config = ConfigDict(from_attributes=True, title="AdminUserRoleResponse")


class UserPasswordUpdate(BaseModel):
    """Schema for updating user password."""

    current_password: str
    new_password: str = Field(..., min_length=8)


class AdminUserResponse(BaseModel):
    """Response schema for user data."""

    id: UUID
    account_id: UUID
    username: str
    email: EmailStr
    email_verified: bool
    full_name: Optional[str]
    is_active: bool
    user_source: str
    oauth_provider: Optional[str]
    last_login: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    roles: Optional[List[AdminUserRoleResponse]] = None
    inherited_roles: Optional[List[AdminUserRoleResponse]] = None

    @field_serializer("id", "account_id")
    def serialize_uuid(self, value: UUID) -> str:
        """Serialize UUID to string for JSON response."""
        return str(value)

    model_config = ConfigDict(from_attributes=True, title="AdminUserResponse")


class UserSummary(BaseModel):
    """Summary schema for user data (minimal info)."""

    id: UUID
    username: str
    email: EmailStr
    full_name: Optional[str]
    is_active: bool

    @field_serializer("id")
    def serialize_uuid(self, value: UUID) -> str:
        """Serialize UUID to string for JSON response."""
        return str(value)

    model_config = ConfigDict(from_attributes=True)


class UserListResponse(BaseModel):
    """Response schema for paginated user list."""

    users: List[AdminUserResponse]
    total: int
    skip: int
    limit: int
