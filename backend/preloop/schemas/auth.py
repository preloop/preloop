"""Authentication schemas for request and response validation."""

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator


class Token(BaseModel):
    """Token response model."""

    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int


class TokenData(BaseModel):
    """Token data model."""

    sub: Optional[str] = None
    scopes: List[str] = []
    exp: Optional[datetime] = None
    refresh: Optional[bool] = False
    # When the login session originally started ("sat" claim). Carried through
    # refresh-token rotations so the sliding session window can be capped.
    session_started_at: Optional[datetime] = None


class User(BaseModel):
    """User model."""

    username: str
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    disabled: Optional[bool] = None
    email_verified: Optional[bool] = None


class UserInDB(User):
    """User in database model."""

    hashed_password: str


class AuthUserCreate(BaseModel):
    """User creation model."""

    model_config = {"title": "AuthUserCreate"}

    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(..., min_length=8)
    full_name: Optional[str] = None
    bootstrap_token: Optional[str] = Field(
        None,
        description=(
            "First-user setup token from the install (required while the "
            "instance has zero users and PRELOOP_BOOTSTRAP_TOKEN is set)."
        ),
    )

    @field_validator("username")
    @classmethod
    def username_alphanumeric(cls, v: str) -> str:
        if not v.isalnum():
            raise ValueError("Username must be alphanumeric")
        return v


class AuthUserUpdate(BaseModel):
    """User update model."""

    model_config = {"title": "AuthUserUpdate"}

    full_name: Optional[str] = None


class AuthUserResponse(BaseModel):
    """Response model for user data."""

    model_config = {"title": "AuthUserResponse"}

    username: str
    email: EmailStr
    full_name: Optional[str] = None
    email_verified: bool
    is_superuser: bool = False
    permissions: Optional[List[str]] = None


class LoginRequest(BaseModel):
    """Model for login requests."""

    username: str
    password: str


class RefreshRequest(BaseModel):
    """Model for token refresh requests."""

    refresh_token: str


class EmailVerificationRequest(BaseModel):
    """Model for email verification requests."""

    token: str


class PasswordResetRequest(BaseModel):
    """Model for password reset requests."""

    email: EmailStr


class PasswordResetConfirmRequest(BaseModel):
    """Model for password reset confirmation."""

    token: str
    new_password: str = Field(..., min_length=8)


class PasswordChangeRequest(BaseModel):
    """Password change request schema."""

    current_password: str
    new_password: str = Field(..., min_length=8)


class ApiKeyCreate(BaseModel):
    """Model for API key creation."""

    name: str = Field(..., min_length=1, max_length=100)
    expires_at: Optional[datetime] = None
    scopes: List[str] = Field(default_factory=list)


class ApiKeyResponse(BaseModel):
    """Response model for API key data."""

    id: UUID
    name: str
    key: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    scopes: List[Any] = []  # Can be strings or dicts (e.g., {"device_token": "..."})
    user_id: UUID
    last_used_at: Optional[datetime] = None


class ApiKeySummary(BaseModel):
    """Summary model for API key data (without the key itself)."""

    id: UUID
    name: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    scopes: List[Any] = []  # Can be strings or dicts (e.g., {"device_token": "..."})
    last_used_at: Optional[datetime] = None
    managed_agent_id: Optional[UUID] = None
    runtime_principal_type: Optional[str] = None
    runtime_principal_id: Optional[str] = None
    runtime_principal_name: Optional[str] = None
    last_activity_at: Optional[datetime] = None
    activity_status: Optional[str] = None
    recent_model_calls: int = 0
    recent_tool_calls: int = 0


class PrincipalIdentity(BaseModel):
    """Optional CLI-supplied identity metadata for a runtime principal."""

    hostname: Optional[str] = Field(None, max_length=255)
    config_path: Optional[str] = Field(None, max_length=1024)
    source_type: Optional[str] = Field(None, max_length=64)
    derivation: Optional[str] = Field(None, max_length=16)


class RuntimeSessionTokenCreate(BaseModel):
    """Request model for minting a runtime-scoped session token."""

    session_source_type: str = Field(..., min_length=1, max_length=64)
    session_source_id: str = Field(..., min_length=1, max_length=255)
    session_reference: Optional[str] = Field(None, max_length=255)
    runtime_principal_id: Optional[str] = Field(None, max_length=255)
    runtime_principal_name: Optional[str] = Field(None, max_length=255)
    #: Durable product kind (``cursor``). Distinct from ``session_source_type``,
    #: which is the transport and is part of the v2 principal fingerprint.
    #: Older CLIs omit this; the server then keeps the stored kind. See #123.
    agent_kind: Optional[str] = Field(None, max_length=64)
    expires_in_minutes: int = Field(default=120, ge=1, le=1440)
    scopes: List[str] = Field(default_factory=lambda: ["mcp:read", "mcp:write"])
    allowed_mcp_tools: List[Any] = Field(default_factory=list)
    allowed_mcp_servers: List[str] = Field(default_factory=list)
    principal_identity: Optional[PrincipalIdentity] = None


class RuntimeSessionTokenResponse(BaseModel):
    """Response model for a minted runtime-scoped session token."""

    runtime_session_id: UUID
    token: str
    expires_at: datetime
    session_source_type: str
    session_source_id: str
    session_reference: Optional[str] = None


class ApiUsageStatistics(BaseModel):
    """Model for API usage statistics."""

    total_requests: int
    requests_by_date: Dict[str, int]
    issues_created: int
    issues_updated: int
    issues_closed: int
    requests_by_endpoint: Dict[str, int]
