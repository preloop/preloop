"""Human-only setup schemas; resource identity and secrets have separate lifetimes."""

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
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


# GitHub limits repository_dispatch ``event_type`` to 100 characters.
DISPATCH_EVENT_TYPE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$"
# Classic, fine-grained and GitHub App installation tokens share this alphabet.
DISPATCH_TOKEN_PATTERN = r"^[A-Za-z0-9_]{20,1024}$"


class CiAdminDispatchToken(BaseModel):
    """A GitHub token that may call repository_dispatch on the bound repository.

    GitHub requires ``contents: write`` on that repository for the call. Use a
    dedicated GitHub App installation or fine-grained token limited to the
    one repository. The value is stored encrypted and never returned.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    token: SecretStr

    @field_validator("token")
    @classmethod
    def token_shape(cls, value: SecretStr) -> SecretStr:
        """Reject values that cannot be a GitHub token, without echoing them."""
        if not re.fullmatch(DISPATCH_TOKEN_PATTERN, value.get_secret_value()):
            raise ValueError("Dispatch token has an unsupported format")
        return value


class CiAdminDispatchSubscription(CiAdminDispatchToken):
    """Deliver completion as a GitHub repository_dispatch, not an HTTP POST.

    The target is always the identity's bound GitHub repository; no URL is
    accepted.
    """

    key_id: UUID
    event_type: str = Field(
        default="preloop-review-completed", pattern=DISPATCH_EVENT_TYPE_PATTERN
    )
    description: str | None = Field(default=None, max_length=255)
    active: StrictBool = True


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
    retired: bool = False
    credential_version: int
    grant: CiGrant
    # Fixed when the identity is provisioned. ``shared`` means the flow admits
    # several projects; the identity is still bound to ``grant.project_id`` only.
    binding_mode: Literal["dedicated", "shared"] = "dedicated"
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
    binding: str = (
        "One account-local project per identity, on that project's dedicated "
        "hosted flow or on a shared read-only review flow that admits the project"
    )
    binding_modes: list[Literal["dedicated", "shared"]] = ["dedicated", "shared"]


class CiSharedFlowUpdate(BaseModel):
    """The complete project allowlist of one shared read-only review flow.

    The list replaces the current allowlist. Each project must be an active
    project of the caller's account whose owning tracker is a GitHub App
    installation; Preloop derives the repository and its tracker from the
    project, so no repository URL or tracker is accepted here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    project_ids: tuple[UUID, ...] = Field(min_length=1, max_length=100)

    @field_validator("project_ids")
    @classmethod
    def unique_projects(cls, project_ids: tuple[UUID, ...]) -> tuple[UUID, ...]:
        """Reject duplicates rather than normalize them."""
        if len(set(project_ids)) != len(project_ids):
            raise ValueError("Shared flow projects must be unique")
        return project_ids


class CiSharedFlowProject(BaseModel):
    """One admitted project and how many live identities are bound to it."""

    project_id: UUID
    name: str | None
    repository_slug: str | None
    identities: int


class CiSharedFlowRead(BaseModel):
    """Administrator view of a flow's restricted CI project allowlist."""

    flow_id: UUID
    name: str
    binding_mode: Literal["dedicated", "shared"]
    read_only: bool
    projects: list[CiSharedFlowProject]
