"""Bounded extension metadata and account approval API contracts."""

import re
import unicodedata
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from preloop.schemas.extension_capabilities import ExtensionCapability
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

ExtensionKind = Literal["plugin", "skill", "hook", "command", "mcp_server"]
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


DetailText = Annotated[str, Field(max_length=300)]


class ExtensionHookCommand(BaseModel):
    """Executable classification without arguments or source paths."""

    model_config = ConfigDict(extra="forbid")
    executable: str = Field(pattern=r"^[A-Za-z0-9_.+\-]{1,128}$")
    preloop_managed: bool
    script: bool = False
    in_project: bool = False


class ExtensionHookMatcher(BaseModel):
    """One matcher and the commands bound to it."""

    model_config = ConfigDict(extra="forbid")
    matcher: DetailText = ""
    matcher_withheld: bool = False
    count: int = Field(ge=1, le=10000)
    commands: list[ExtensionHookCommand] = Field(max_length=10000)

    @model_validator(mode="after")
    def consistent_count(self) -> "ExtensionHookMatcher":
        """Require one classification per command and withhold private matchers."""
        if self.count != len(self.commands) or (self.matcher_withheld and self.matcher):
            raise ValueError("Inconsistent hook matcher")
        validate_detail_text(self.matcher)
        return self


class ExtensionBundleCounts(BaseModel):
    """Observed components within an installed plugin."""

    model_config = ConfigDict(extra="forbid")
    skills: int = Field(ge=0, le=10000)
    commands: int = Field(ge=0, le=10000)
    hooks: int = Field(ge=0, le=10000)
    mcp_servers: int = Field(ge=0, le=10000)
    agents: int = Field(ge=0, le=10000)


BundleName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:\-]{1,64}$")]
EventName = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_\-]{0,31}$")]
ToolName = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:*()| \-]{0,63}$")]
OPTIONAL_HOST_PATTERN = (
    r"^(?:localhost|[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)+)?$"
)
OptionalHost = Annotated[str, Field(max_length=253, pattern=OPTIONAL_HOST_PATTERN)]


class ExtensionBundleNames(BaseModel):
    """Names of the components a plugin bundles, bounded for display."""

    model_config = ConfigDict(extra="forbid")
    skills: list[BundleName] = Field(default_factory=list, max_length=50)
    commands: list[BundleName] = Field(default_factory=list, max_length=50)
    agents: list[BundleName] = Field(default_factory=list, max_length=50)
    mcp_servers: list[BundleName] = Field(default_factory=list, max_length=50)


def validate_detail_text(value: str) -> str:
    """Reject paths, URLs, controls and noncanonical authored whitespace."""
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        raise ValueError("Control characters are not allowed")
    if " ".join(value.split()) != value:
        raise ValueError("Text must have normalized whitespace")
    if re.search(
        r"(?i)([a-z][a-z0-9+.-]*://|www\.|[a-z]:[\\/]|(^|[\s(\"'])[/\\~]|[\w.-]+/[^\s]+)",
        value,
    ):
        raise ValueError("Paths and URLs are not allowed")
    return value


class ExtensionDetails(BaseModel):
    """Bounded descriptive metadata; bodies and command strings are forbidden."""

    model_config = ConfigDict(extra="forbid")
    name: DetailText = ""
    description: DetailText = ""
    description_withheld: bool = False
    invocation: DetailText = ""
    script_count: int | None = Field(default=None, ge=0, le=10000)
    plugin: DetailText = ""
    author: DetailText = ""
    bundles: ExtensionBundleCounts | None = None
    hook_matchers: list[ExtensionHookMatcher] = Field(
        default_factory=list, max_length=10000
    )
    # Schema 5 decision metadata: host names only, never URLs or paths.
    marketplace: DetailText = ""
    homepage_host: OptionalHost = ""
    repository_host: OptionalHost = ""
    license: str = Field(default="", pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9.+\-]{0,63})?$")
    modified_at: str = Field(default="", pattern=r"^(?:\d{4}-\d{2}-\d{2})?$")
    tools_declared: bool = False
    allowed_tools: list[ToolName] = Field(default_factory=list, max_length=50)
    hook_events: list[EventName] = Field(default_factory=list, max_length=50)
    bundle_names: ExtensionBundleNames | None = None
    transport: Literal["", "stdio", "http", "sse"] = ""
    command: str = Field(default="", pattern=r"^(?:[A-Za-z0-9_.+\-]{1,128})?$")
    url_host: OptionalHost = ""

    @field_validator("name", "description", "plugin", "author", "marketplace")
    @classmethod
    def safe_text(cls, value: str) -> str:
        """Apply the authored-text boundary independently of the collector."""
        return validate_detail_text(value)

    @field_validator("invocation")
    @classmethod
    def safe_invocation(cls, value: str) -> str:
        """Allow only the documented generated invocation grammar."""
        if value and not re.fullmatch(
            r"(?:(?:/[A-Za-z0-9_:.-]+|\$[A-Za-z0-9_:.-]+ \(or /skills\))(?:; loaded by the model when relevant)?|loaded by the model when relevant|Runs on [A-Za-z][A-Za-z0-9_-]{0,31})",
            value,
        ):
            raise ValueError("Invalid invocation")
        return value

    @model_validator(mode="after")
    def withheld_description(self) -> "ExtensionDetails":
        """A withheld description never accompanies authored text."""
        if self.description_withheld and self.description:
            raise ValueError("Withheld descriptions must be empty")
        return self


SCHEMA5_DETAIL_FIELDS = (
    "marketplace",
    "homepage_host",
    "repository_host",
    "license",
    "modified_at",
    "tools_declared",
    "allowed_tools",
    "hook_events",
    "bundle_names",
    "transport",
    "command",
    "url_host",
)


class ExtensionMetadata(BaseModel):
    """Identifiers, hashes and optional bounded descriptive metadata."""

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
    details: ExtensionDetails | None = None

    @model_serializer(mode="wrap")
    def serialize_optional_details(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Keep historical v3 replay bytes stable when details are absent."""
        data: dict[str, Any] = handler(self)
        if self.details is None:
            data.pop("details", None)
        return data

    def uses_schema5(self) -> bool:
        """Whether these details carry decision metadata added in schema 5."""
        details = self.details
        if details is None:
            return False
        if self.kind in {"mcp_server", "plugin"} and (
            self.kind == "mcp_server" or details.script_count is not None
        ):
            return True
        return any(getattr(details, field) for field in SCHEMA5_DETAIL_FIELDS) or any(
            command.script or command.in_project
            for matcher in details.hook_matchers
            for command in matcher.commands
        )

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
        if self.details is not None:
            details = self.details
            if (
                self.kind not in {"skill", "plugin"}
                and details.script_count is not None
            ):
                raise ValueError("Only skills and plugins have script counts")
            if self.kind != "plugin" and (
                details.bundles is not None
                or details.author
                or details.bundle_names is not None
                or details.homepage_host
                or details.repository_host
            ):
                raise ValueError("Only plugins have author and bundle contents")
            if self.kind != "mcp_server" and (
                details.transport or details.command or details.url_host
            ):
                raise ValueError("Only MCP servers have a transport")
            if self.kind not in {"skill", "command", "plugin"} and (
                details.tools_declared or details.allowed_tools
            ):
                raise ValueError("Only skills, commands and plugins declare tools")
            if self.kind not in {"skill", "plugin"} and details.hook_events:
                raise ValueError("Only skills and plugins register hook events")
            if details.hook_matchers:
                if (
                    self.kind != "hook"
                    or sum(item.count for item in details.hook_matchers) != self.count
                ):
                    raise ValueError("Hook matcher counts must match the event count")
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


class ExtensionIdentity(BaseModel):
    """Natural key for one account-wide extension decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    agent_kind: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]{0,63}$")
    extension_kind: ExtensionKind
    extension_id: str = Field(pattern=IDENTIFIER_PATTERN)

    @field_validator("extension_id")
    @classmethod
    def bounded_identifier(cls, value: str) -> str:
        """Enforce the same byte limit as individual decisions."""
        return ExtensionMetadata.bounded_identifier(value)


class BulkExtensionApproval(BaseModel):
    """A bounded state-only decision over explicit identities."""

    model_config = ConfigDict(extra="forbid")
    extensions: list[ExtensionIdentity] = Field(min_length=1, max_length=500)
    approval_state: Literal["approved", "unapproved"]


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

    details: ExtensionDetails | None = None
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
    capabilities: list[ExtensionCapability] = Field(default_factory=list)


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
    details: ExtensionDetails | None = None
    device_count: int
    versions: list[str]
    source_kinds: list[SourceKind]
    approval_state: ApprovalState
    effective_state: EffectiveState = Field(
        description="Worst per-device state among the devices counted in this row"
    )
    pin_count: int = 0
    content_hashes: list[ContentHashBucket] = Field(default_factory=list)
    capabilities: list[ExtensionCapability] = Field(default_factory=list)
    first_seen_at: datetime | None = None
    people_count: int = Field(
        default=0,
        description="Distinct people whose credentials reported this extension",
    )


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
