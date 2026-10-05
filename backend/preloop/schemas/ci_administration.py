"""Human-only setup schemas; resource identity and secrets have separate lifetimes."""

from datetime import datetime
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from preloop.schemas.ci_principal import CiAction, CiGrant
from preloop.schemas.ci_subscription import CiSubscriptionCreate


class CiAdminCreate(BaseModel):
    """Provision one stable identity and disclose one initial credential."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    grant: CiGrant
    expires_at: datetime | None = None


class CiAdminPreview(BaseModel):
    """Validate a concrete resource grant without issuing a credential."""

    model_config = ConfigDict(extra="forbid")
    grant: CiGrant


class CiAdminUpdate(BaseModel):
    """Change grant actions or explicitly enable/disable a stable identity."""

    model_config = ConfigDict(extra="forbid")
    grant: CiGrant | None = None
    enabled: StrictBool | None = None

    @model_validator(mode="after")
    def require_change(self) -> "CiAdminUpdate":
        if not self.model_fields_set or any(
            getattr(self, name) is None for name in self.model_fields_set
        ):
            raise ValueError("Provide a non-null grant or enabled state")
        return self


class CiAdminIssue(BaseModel):
    """Issue a replacement key with an optional narrower action ceiling."""

    model_config = ConfigDict(extra="forbid")
    actions: tuple[CiAction, ...] | None = Field(default=None, min_length=1)
    expires_at: datetime | None = None

    @field_validator("actions")
    @classmethod
    def unique_actions(
        cls, actions: tuple[CiAction, ...] | None
    ) -> tuple[CiAction, ...] | None:
        if actions is not None and len(set(actions)) != len(actions):
            raise ValueError("Credential actions must be unique")
        return actions


class CiAdminRotate(BaseModel):
    """Replace one valid key while preserving stable principal ownership."""

    model_config = ConfigDict(extra="forbid")
    expires_at: datetime | None = None


class CiAdminSubscription(CiSubscriptionCreate):
    """A real principal key is an audit anchor, not administrator authentication."""

    key_id: UUID


class CiAdminKeyRead(BaseModel):
    """Expose recovery identifiers and lifetime metadata without token material."""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    actions: list[str]
    is_active: bool
    expires_at: datetime | None
    created_at: datetime | None
    last_used_at: datetime | None


class CiAdminIdentityRead(BaseModel):
    """Safe account-local identity and immutable repository metadata."""

    id: UUID
    name: str
    is_active: bool
    credential_version: int
    grant: CiGrant
    repository_identifier: str
    repository_slug: str
    tracker_type: str
    keys: list[CiAdminKeyRead]


class CiAdminIssued(BaseModel):
    """Return the newly created identity and initial credential exactly once."""

    identity: CiAdminIdentityRead
    key_id: UUID
    token: str
    secret_note: str = (
        "Shown once. Store securely; metadata reads never return this token."
    )


class CiAdminKeyIssued(BaseModel):
    """Return a replacement token once alongside safe ownership identifiers."""

    principal_id: UUID
    key_id: UUID
    token: str
    secret_note: str = (
        "Shown once. Store securely; metadata reads never return this token."
    )


class CiAdminCapabilities(BaseModel):
    """Report rollout readiness separately from current human authority."""

    available: bool
    can_view: bool
    can_manage: bool
    supported_actions: list[CiAction]
    binding: str = "One account-local project and its dedicated hosted flow"
