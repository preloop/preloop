"""Authentication schemas for request and response validation."""

import re
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
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
    # Per-user token generation ("gen" claim). None when the token was minted
    # before the claim existed; enforcement treats that as generation 0.
    gen: Optional[int] = None
    # CLI login session id ("sid" claim, a cli_session row). Only CLI JWTs
    # minted by /oauth/token carry it; the row can be revoked on its own.
    sid: Optional[str] = None
    # Refresh token id ("jti" claim). A CLI refresh token rotates only while
    # its jti is the one recorded on the cli_session row.
    jti: Optional[str] = None
    # Every claim of the token, for extensions that issue their own claims.
    # Kept out of repr so request logs do not grow with custom claims.
    claims: Dict[str, Any] = Field(default_factory=dict, repr=False)


class LogoutResponse(BaseModel):
    """Result of ``POST /auth/logout``."""

    # Same-origin path the client navigates to after clearing its tokens.
    # None means the client's own default destination.
    redirect_url: Optional[str] = None


class CliSessionResponse(BaseModel):
    """One CLI login session (``preloop auth login``) of the signed-in user."""

    id: UUID
    created_at: datetime
    last_seen_at: Optional[datetime] = None
    user_agent: Optional[str] = None
    hostname: Optional[str] = None
    current: bool = Field(
        False, description="True for the session the request's own token belongs to"
    )


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
    password: str = Field(..., min_length=8, max_length=72)
    full_name: Optional[str] = None
    bootstrap_token: Optional[str] = Field(
        None,
        description=(
            "First-user setup token from the install (required while the "
            "instance has zero users and PRELOOP_BOOTSTRAP_TOKEN is set)."
        ),
    )
    plan_choice: Optional[str] = Field(
        None,
        max_length=64,
        description=(
            "The plan this person already picked on the pricing page before "
            "they got here. Its presence, not its value, is what matters: it "
            "records that the choice has been made, so the console never "
            "asks again. Paid plans do not arrive this way (they go through "
            "checkout first), so in practice this is the free plan's id."
        ),
    )

    @field_validator("username")
    @classmethod
    def username_alphanumeric(cls, v: str) -> str:
        if not v.isalnum():
            raise ValueError("Username must be alphanumeric")
        return v

    @field_validator("plan_choice")
    @classmethod
    def plan_choice_is_a_slug(cls, v: Optional[str]) -> Optional[str]:
        """Keep plan ids, drop anything else, never refuse the signup.

        This value arrives from a marketing link, so a mangled or hostile one
        has to cost the visitor nothing: a truncated query string must not be
        able to stop somebody creating an account. Anything that is not a
        plan id shape is therefore ignored rather than rejected, and ignoring
        it only means the person is asked to choose a plan once, in the
        console, which is the default behaviour anyway.
        """
        if v is None:
            return None
        candidate = v.strip().lower()
        if not candidate or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", candidate):
            return None
        return candidate


class AuthUserUpdate(BaseModel):
    """User update model."""

    model_config = {"title": "AuthUserUpdate"}

    full_name: Optional[str] = None


class RunnerPolicy(BaseModel):
    """Account policy for installing a personal runner on this user's machine."""

    model_config = {"title": "RunnerPolicy"}

    requirement: Literal["optional", "required", "forbidden"] = "optional"
    capabilities: List[str] = Field(
        default_factory=lambda: ["flows", "inventory", "sessions"],
        description=(
            "Capabilities a runner installed for this user may offer. "
            "Ids include flows, inventory, and sessions."
        ),
    )
    mandated_capabilities: List[str] = Field(
        default_factory=list,
        description=(
            "Capabilities the account requires on a member machine when "
            "requirement is required. Empty when the user may decline."
        ),
    )
    grace_until: Optional[datetime] = None
    can_decide: bool = Field(
        True,
        description=(
            "True when this user may decline a runner. False for a member "
            "who is not the owner of a multi-user account. The CLI still "
            "asks; a decline tells them to ask an admin."
        ),
    )
    message: Optional[str] = Field(
        None,
        description=(
            "Optional sentence the CLI prints for required or forbidden. "
            "Null uses the CLI's built-in sentence."
        ),
    )


def _default_runner_policy() -> RunnerPolicy:
    """Policy used when a caller builds a profile without one."""
    return RunnerPolicy(
        requirement="optional",
        capabilities=["flows", "inventory", "sessions"],
        mandated_capabilities=[],
        grace_until=None,
        can_decide=True,
        message=None,
    )


class AuthUserResponse(BaseModel):
    """Response model for user data."""

    model_config = {"title": "AuthUserResponse"}

    id: UUID = Field(
        ...,
        description=(
            "The caller's user id. Clients compare it against an approval "
            "workflow's approver_user_ids to tell whether a pending approval "
            "is waiting for this person."
        ),
    )
    account_id: UUID = Field(..., description="The account this user belongs to.")
    username: str
    email: EmailStr
    full_name: Optional[str] = None
    email_verified: bool
    is_superuser: bool = False
    permissions: Optional[List[str]] = None
    avatar_url: Optional[str] = None
    avatar_source: Optional[str] = None
    plan_choice_made: bool = Field(
        True,
        description=(
            "Whether this person has already chosen a plan. False is the "
            "console's cue to ask the billing plugin whether to show the "
            "first-login plan choice; true means it never asks. It defaults "
            "to true so a client talking to an older server, or any caller "
            "reading a profile built without a user row, is never dragged "
            "into an onboarding step it cannot reason about."
        ),
    )
    team_ids: List[UUID] = Field(
        ...,
        description=(
            "Ids of the teams the caller belongs to inside account_id. "
            "Clients intersect these with an approval workflow's "
            "approver_team_ids to tell whether a pending approval is waiting "
            "for this person. Empty when the user is in no team."
        ),
    )
    runner_policy: RunnerPolicy = Field(
        default_factory=_default_runner_policy,
        description=(
            "Whether this user should install a personal runner, and "
            "whether they may decline. requirement is optional, required, "
            "or forbidden. An older server omits the object; clients treat "
            "that as optional."
        ),
    )


class LoginRequest(BaseModel):
    """Model for login requests."""

    username: str
    # No max_length: a schema cap would 422 existing accounts whose password
    # is longer than 72 characters. verify_password uses bcrypt's 72-byte
    # prefix, which is how passlib hashed those secrets before bcrypt 5.
    password: str


class RefreshRequest(BaseModel):
    """Model for token refresh requests."""

    refresh_token: str


class EmailVerificationRequest(BaseModel):
    """Model for email verification requests."""

    token: str


class EmailVerificationResendRequest(BaseModel):
    """Model for asking for another verification email."""

    email: EmailStr


class PasswordResetRequest(BaseModel):
    """Model for password reset requests."""

    email: EmailStr


class PasswordResetConfirmRequest(BaseModel):
    """Model for password reset confirmation."""

    token: str
    new_password: str = Field(..., min_length=8, max_length=72)


class PasswordChangeRequest(BaseModel):
    """Password change request schema."""

    current_password: str
    new_password: str = Field(..., min_length=8, max_length=72)


class ApiKeyCreate(BaseModel):
    """Model for API key creation."""

    name: str = Field(..., min_length=1, max_length=100)
    expires_at: Optional[datetime] = None
    scopes: List[str] = Field(default_factory=list)
    trusted_upstream_secret: Optional[str] = Field(
        default=None,
        min_length=16,
        max_length=512,
        description=(
            "Only with the model_gateway:trusted_upstream scope. Shared secret "
            "the upstream gateway sends in x-preloop-upstream-secret; stored "
            "as a sha256 hash, never returned."
        ),
    )
    per_subject_budget: Optional["PerSubjectBudget"] = Field(
        default=None,
        description=(
            "Only with the model_gateway:trusted_upstream scope. Default "
            "budget for every developer the upstream gateway names."
        ),
    )


class PerSubjectBudget(BaseModel):
    """Default budget per gateway subject on a trusted upstream key."""

    period: Literal["hourly", "daily", "weekly", "monthly", "yearly", "all_time"] = (
        "monthly"
    )
    hard_limit_usd: Optional[float] = Field(default=None, ge=0)
    soft_limit_usd: Optional[float] = Field(default=None, ge=0)
    model_alias: Optional[str] = Field(default=None, max_length=255)


ApiKeyCreate.model_rebuild()


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
    #: Non-fatal problems with the request, e.g. a tool restriction that
    #: resolved to zero tools. The token is still valid.
    warnings: List[str] = Field(default_factory=list)


class ApiUsageStatistics(BaseModel):
    """Model for API usage statistics."""

    total_requests: int
    requests_by_date: Dict[str, int]
    issues_created: int
    issues_updated: int
    issues_closed: int
    requests_by_endpoint: Dict[str, int]
