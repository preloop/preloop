"""API contracts for supported-release vulnerability maintenance."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SupportedReleaseCreate(BaseModel):
    """Opt-in inventory row. Creating a row is the only way to enroll a release."""

    model_config = ConfigDict(extra="forbid")
    product_key: str = Field(min_length=1, max_length=128)
    release_key: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=255)
    project_id: UUID
    pinned_build_ref: str = Field(min_length=1, max_length=512)
    sbom_input_ref: str = Field(min_length=1, max_length=512)
    audit_flow_id: UUID
    implementation_flow_id: UUID
    recheck_flow_id: UUID | None = None
    approval_workflow_id: UUID
    approval_owner_user_id: UUID | None = None
    escalation_user_ids: list[UUID] = Field(default_factory=list)
    escalation_after_seconds: int = Field(default=604800, ge=60, le=2592000)
    max_retries: int = Field(default=3, ge=0, le=20)
    allowed_model_ids: list[UUID] | None = None
    allowed_input_kinds: list[str] | None = None
    enabled: bool = True

    @field_validator("product_key", "release_key")
    @classmethod
    def _identity_token(cls, value: str) -> str:
        token = value.strip()
        if not token or any(ch.isspace() for ch in token):
            raise ValueError("identity_token_must_be_compact")
        return token


class SupportedReleaseUpdate(BaseModel):
    """Partial inventory update. Identity keys are immutable."""

    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    pinned_build_ref: str | None = Field(default=None, min_length=1, max_length=512)
    sbom_input_ref: str | None = Field(default=None, min_length=1, max_length=512)
    audit_flow_id: UUID | None = None
    implementation_flow_id: UUID | None = None
    recheck_flow_id: UUID | None = None
    approval_workflow_id: UUID | None = None
    approval_owner_user_id: UUID | None = None
    escalation_user_ids: list[UUID] | None = None
    escalation_after_seconds: int | None = Field(default=None, ge=60, le=2592000)
    max_retries: int | None = Field(default=None, ge=0, le=20)
    allowed_model_ids: list[UUID] | None = None
    allowed_input_kinds: list[str] | None = None
    enabled: bool | None = None


class ScanFinding(BaseModel):
    """One advisory/component pair from a trusted scan ingest, not agent prose."""

    model_config = ConfigDict(extra="forbid")
    advisory_id: str = Field(min_length=1, max_length=128)
    component_id: str = Field(min_length=1, max_length=512)
    aliases: list[str] = Field(default_factory=list)
    severity: str = Field(default="unknown", max_length=32)
    present: bool = True


class WorkspaceSeedInput(BaseModel):
    """Inline workspace seed carried on the existing trigger contract."""

    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=512)
    content_base64: str = Field(min_length=1)


class ScanIngestRequest(BaseModel):
    """Trusted controller ingest. Missing product/release is unsupported."""

    model_config = ConfigDict(extra="forbid")
    product_key: str = Field(min_length=1, max_length=128)
    release_key: str = Field(min_length=1, max_length=128)
    input_kind: str = Field(default="cyclonedx-json", min_length=1, max_length=64)
    source: str = Field(default="local", min_length=1, max_length=64)
    execution_id: UUID | None = None
    issue_id: UUID | None = None
    sbom_content_base64: str | None = None
    workspace_files: list[WorkspaceSeedInput] = Field(default_factory=list)
    evidence_ref: dict[str, Any] = Field(default_factory=dict)
    findings: list[ScanFinding] = Field(default_factory=list)
    available: bool = True


class BaselineAcceptRequest(BaseModel):
    """Record an initial or replacement baseline from a bound audit execution."""

    model_config = ConfigDict(extra="forbid")
    audit_execution_id: UUID
    evidence_ref: dict[str, Any] = Field(default_factory=dict)


class RebuiltInputsRequest(BaseModel):
    """Authenticated rebuilt SBOM for the published commit. Not an automatic rebuild."""

    model_config = ConfigDict(extra="forbid")
    published_sha: str = Field(min_length=40, max_length=40)
    sbom_content_base64: str = Field(min_length=1)
    pinned_build_ref: str | None = Field(default=None, min_length=1, max_length=512)

    @field_validator("published_sha")
    @classmethod
    def _hex40(cls, value: str) -> str:
        token = value.strip().lower()
        if len(token) != 40 or any(ch not in "0123456789abcdef" for ch in token):
            raise ValueError("published_sha_must_be_hex40")
        return token


class ApprovalDecisionRequest(BaseModel):
    """Human decision. Agent-asserted approval in result JSON is ignored."""

    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2000)


class ResumeRequest(BaseModel):
    """Explicit human retry. Historical decisions stay append-only."""

    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2000)


ItemState = Literal[
    "open",
    "remediation_pending",
    "remediating",
    "tests_failed",
    "tests_passed",
    "approval_pending",
    "awaiting_build",
    "held",
    "escalated",
    "reaudit_pending",
    "reauditing",
    "reaudit_incomplete",
    "resolved",
]
