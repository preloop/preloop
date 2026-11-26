"""Bounded API contracts for extension content uploaded for review."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from preloop.schemas.agent_discovery import HEX_SHA256_PATTERN
from preloop.schemas.discovered_extension import HASH_PATTERN, ExtensionKind
from preloop.services import extension_content as policy

FileStatus = Literal[
    "stored",
    "binary",
    "too_large",
    "symlink",
    "private_key",
    "over_budget",
    "not_selected",
    "unreadable",
]
HookStatus = Literal["stored", "too_large", "private_key", "over_budget"]
WithheldReason = Literal["excluded", "too_large"]
Verification = Literal["verified", "partial", "unverified", "none"]
RedactionType = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
RedactionCounts = dict[RedactionType, Annotated[int, Field(ge=1, le=100_000)]]
VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9.+_\-]{0,63}$"


def _bounded_redactions(value: dict[str, int]) -> dict[str, int]:
    if len(value) > 64:
        raise ValueError("too many redaction types")
    return value


class ContentPair(BaseModel):
    """One extension content, named by kind and inventory hash."""

    model_config = ConfigDict(extra="forbid")

    extension_kind: ExtensionKind
    content_hash: str = Field(pattern=HASH_PATTERN)


class ContentQuery(BaseModel):
    """Collector question: which of these hashes does the account lack."""

    model_config = ConfigDict(extra="forbid")

    items: list[ContentPair] = Field(max_length=policy.MAX_QUERY_HASHES)


class ContentLimits(BaseModel):
    """Server-side caps, so a collector can stay under them."""

    text_bytes: int = policy.MAX_TEXT_BYTES
    item_bytes: int = policy.MAX_ITEM_BYTES
    item_entries: int = policy.MAX_ITEM_ENTRIES
    upload_items: int = policy.MAX_UPLOAD_ITEMS
    upload_bytes: int = policy.MAX_UPLOAD_BYTES


class ContentQueryResponse(BaseModel):
    """Whether the account collects content, and which hashes it lacks."""

    collect: bool
    reason: Literal["account_opt_out"] | None = None
    missing: list[ContentPair]
    retention_days: int
    limits: ContentLimits = Field(default_factory=ContentLimits)


class ContentFile(BaseModel):
    """One entry of an extension's file list, with text when it was sent."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024)
    sha256: str | None = Field(default=None, pattern=HEX_SHA256_PATTERN)
    size: int | None = Field(default=None, ge=0, le=16 * 1024 * 1024)
    status: FileStatus
    text: str | None = Field(default=None, max_length=policy.MAX_TEXT_BYTES)
    redactions: RedactionCounts = Field(default_factory=dict)

    _bounded = field_validator("redactions")(_bounded_redactions)


class ContentHook(BaseModel):
    """One hook command with the matcher it runs under."""

    model_config = ConfigDict(extra="forbid")

    matcher: str | None = Field(default=None, max_length=256)
    command_hash: str = Field(pattern=HASH_PATTERN)
    status: HookStatus
    command: str | None = Field(default=None, max_length=policy.MAX_TEXT_BYTES)
    redactions: RedactionCounts = Field(default_factory=dict)

    _bounded = field_validator("redactions")(_bounded_redactions)


class ContentItem(ContentPair):
    """The content behind one inventory hash, redacted on the device."""

    files: list[ContentFile] = Field(
        default_factory=list, max_length=policy.MAX_ITEM_ENTRIES
    )
    hooks: list[ContentHook] = Field(
        default_factory=list, max_length=policy.MAX_ITEM_HOOKS
    )
    withheld: WithheldReason | None = None


class ContentUpload(BaseModel):
    """One batch of content from one device."""

    model_config = ConfigDict(extra="forbid")

    workstation_fingerprint: str = Field(pattern=HEX_SHA256_PATTERN)
    collector_version: str | None = Field(default=None, pattern=VERSION_PATTERN)
    redaction_rules: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.+_\-]{0,63}$")
    items: list[ContentItem] = Field(min_length=1, max_length=policy.MAX_UPLOAD_ITEMS)


class ContentRejection(ContentPair):
    """An item the server did not store, with a fixed reason code."""

    reason: str


class ContentUploadResponse(BaseModel):
    """Counts of what happened to a batch. No text is echoed."""

    stored: int
    kept: int
    rejected: list[ContentRejection]


class StoredFile(BaseModel):
    """A file as an approver reads it."""

    path: str
    sha256: str | None = None
    size: int | None = None
    status: FileStatus
    text: str | None = None
    redactions: dict[str, int] = Field(default_factory=dict)
    verified: bool = False


class StoredHook(BaseModel):
    """A hook command as an approver reads it."""

    matcher: str | None = None
    command_hash: str
    status: HookStatus
    command: str | None = None
    redactions: dict[str, int] = Field(default_factory=dict)
    verified: bool = False


class ExtensionContentRead(ContentPair):
    """Stored content of one hash, decrypted for a permitted reader."""

    files: list[StoredFile]
    hooks: list[StoredHook]
    withheld: WithheldReason | None = None
    verification: Verification
    redacted: bool
    redaction_count: int
    withheld_count: int
    stored_bytes: int
    redaction_rules: str | None = None
    collector_version: str | None = None
    first_stored_at: datetime
    last_reported_at: datetime
    retention_days: int


class ExtensionContentSettingsResponse(BaseModel):
    """The account switch and what is stored under it."""

    collect_enabled: bool
    notice_pending: bool
    stored_contents: int
    stored_bytes: int
    retention_days: int
    updated_by_user_id: str | None = None
    updated_at: str | None = None


class ExtensionContentSettingsUpdate(BaseModel):
    """Change the switch, acknowledge the one-time notice, or both."""

    model_config = ConfigDict(extra="forbid")

    collect_enabled: bool | None = None
    acknowledge_notice: bool = False
