"""Bounded extension metadata and account approval API contracts."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ExtensionKind = Literal["plugin", "skill", "hook", "command"]
ApprovalState = Literal["approved", "unapproved", "unknown"]
EffectiveState = Literal[
    "approved", "unapproved", "unknown", "content_changed", "content_unverified"
]
ContentHashStatus = Literal["ok", "too_large", "unreadable"]
SourceKind = Literal[
    "official_marketplace",
    "third_party_marketplace",
    "git",
    "local",
    "builtin",
    "unknown",
]
HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"
IDENTIFIER_PATTERN = r"^[^\x00-\x1f\x7f/\\]{1,128}$"
VERSION_CONSTRAINT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9.+_\-]*(?:\.\*)?$"


class ExtensionMetadata(BaseModel):
    """Only identifiers, version, classification and final hashes leave a device."""

    model_config = ConfigDict(extra="forbid")

    kind: ExtensionKind
    id: str = Field(pattern=IDENTIFIER_PATTERN)
    version: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9.+_\-]{0,63}$"
    )
    source_kind: SourceKind
    source_ref_hash: str | None = Field(default=None, pattern=HASH_PATTERN)
    scope: Literal["user", "project"]
    project_ref_hash: str | None = Field(default=None, pattern=HASH_PATTERN)
    enabled: Literal["true", "false", "unknown"] = "unknown"
    content_hash: str | None = Field(default=None, pattern=HASH_PATTERN)
    content_hash_status: ContentHashStatus = "ok"
    event: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_\-]{0,31}$")
    count: int | None = Field(default=None, ge=1, le=10000)
    id_hashed: bool = False

    @field_validator("id")
    @classmethod
    def bounded_identifier(cls, value: str) -> str:
        """Bound UTF-8 bytes as well as characters."""
        if len(value.encode("utf-8")) > 128:
            raise ValueError("Extension identifier exceeds 128 bytes")
        return value

    @model_validator(mode="after")
    def consistent_scope(self) -> "ExtensionMetadata":
        """Do not alias projects or accept ambiguous hook aggregates."""
        if (self.scope == "project") != (self.project_ref_hash is not None):
            raise ValueError(
                "Project scope requires a project hash; user scope forbids it"
            )
        if self.content_hash_status != "ok" and self.content_hash is not None:
            raise ValueError("A bounded or unreadable extension has no content hash")
        if self.kind == "hook" and (self.event is None or self.count is None):
            raise ValueError("Hooks require event and count")
        if self.kind != "hook" and (self.event is not None or self.count is not None):
            raise ValueError("Only hooks have event and count")
        return self


class ExtensionPinCreate(BaseModel):
    """Pin an approval to one reviewed content hash."""

    model_config = ConfigDict(extra="forbid")

    content_hash: str = Field(pattern=HASH_PATTERN)
    version: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9.+_\-]{0,63}$"
    )
    note: str | None = Field(default=None, max_length=512)


class ExtensionPinSummary(BaseModel):
    """A pinned hash with who pinned it and when."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    content_hash: str
    version: str | None
    note: str | None
    pinned_by_user_id: UUID | None
    pinned_by: str | None = None
    pinned_at: datetime


class ExtensionApprovalFields(BaseModel):
    """Decision fields stored on the approval row."""

    model_config = ConfigDict(extra="forbid")

    approval_state: ApprovalState
    version_constraint: str | None = Field(
        default=None, max_length=64, pattern=VERSION_CONSTRAINT_PATTERN
    )
    note: str | None = Field(default=None, max_length=512)


class ExtensionApprovalUpdate(ExtensionApprovalFields):
    """Replace a decision; null version constraint means any version.

    ``pin`` approves this exact content: the approval and the pin are written
    in one transaction, and the pin requires ``approval_state="approved"``.
    """

    pin: ExtensionPinCreate | None = None

    @model_validator(mode="after")
    def pin_requires_approval(self) -> "ExtensionApprovalUpdate":
        """Only an approved decision can be pinned to content."""
        if self.pin is not None and self.approval_state != "approved":
            raise ValueError("A pin requires approval_state 'approved'")
        return self


class ExtensionApprovalSummary(ExtensionApprovalFields):
    """A decision, its audit attribution and its content pins."""

    model_config = ConfigDict(from_attributes=True, extra="ignore")

    id: UUID
    agent_kind: str
    extension_kind: ExtensionKind
    extension_id: str
    decided_by_user_id: UUID | None
    decided_at: datetime | None
    pins: list[ExtensionPinSummary] = Field(default_factory=list)


class DiscoveredExtensionSummary(BaseModel):
    """Device inventory joined to its account-wide decision."""

    fingerprint: str
    profile_ref: str
    agent_kind: str
    extension_kind: ExtensionKind
    extension_id: str
    id_hashed: bool
    version: str | None
    source_kind: SourceKind
    source_ref_hash: str | None
    scope: Literal["user", "project"]
    project_ref_hash: str | None
    enabled: Literal["true", "false", "unknown"]
    present: bool
    first_seen_at: datetime
    last_seen_at: datetime
    approval_state: ApprovalState
    effective_state: EffectiveState
    content_hash: str | None
    content_hash_status: ContentHashStatus


class ContentHashBucket(BaseModel):
    """Devices reporting one content hash, or the null bucket with its status."""

    content_hash: str | None
    status: ContentHashStatus = "ok"
    device_count: int
    pinned: bool


class ExtensionAggregate(BaseModel):
    """Distinct installed extensions across the account's devices."""

    agent_kind: str
    extension_kind: ExtensionKind
    extension_id: str
    device_count: int
    versions: list[str]
    source_kinds: list[SourceKind]
    approval_state: ApprovalState
    effective_state: EffectiveState = Field(
        description="Worst per-device state among the devices counted in this row"
    )
    pin_count: int = 0
    content_hashes: list[ContentHashBucket] = Field(default_factory=list)


class ExtensionCoverage(BaseModel):
    """Latest safe coverage metadata reported by one device."""

    fingerprint: str
    collector_version: str | None = None
    completeness: Literal["complete", "partial", "unknown"]
    reasons: list[str] = Field(default_factory=list)
    reported_at: datetime | None = None


class DiscoveredExtensionList(BaseModel):
    """Keyset-paginated device rows or account aggregates."""

    items: list[DiscoveredExtensionSummary] | list[ExtensionAggregate]
    coverage: list[ExtensionCoverage] = Field(default_factory=list)
    completeness: Literal["complete", "partial", "unknown"] = "unknown"
    next_cursor: str | None


class ExtensionApprovalList(BaseModel):
    """Keyset-paginated account approvals."""

    items: list[ExtensionApprovalSummary]
    next_cursor: str | None


class ExtensionPolicyEntry(BaseModel):
    """One offline decision. Notes and user ids never leave with the file."""

    model_config = ConfigDict(extra="forbid")

    agent_kind: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]{0,63}$")
    extension_kind: ExtensionKind
    extension_id: str = Field(pattern=IDENTIFIER_PATTERN)
    approval_state: ApprovalState
    version_constraint: str | None = Field(
        default=None, max_length=64, pattern=VERSION_CONSTRAINT_PATTERN
    )
    content_hashes: list[Annotated[str, Field(pattern=HASH_PATTERN)]] = Field(
        default_factory=list
    )


class ExtensionPolicyFile(BaseModel):
    """Account policy a device can evaluate without uploading inventory."""

    model_config = ConfigDict(populate_by_name=True)

    policy_schema: Literal["preloop.extension-policy.v1"] = Field(
        default="preloop.extension-policy.v1",
        alias="schema",
        serialization_alias="schema",
    )
    account_ref: str = Field(pattern=r"^[0-9a-f]{16}$")
    generated_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    entries: list[ExtensionPolicyEntry]
