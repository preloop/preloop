"""Narrow single-use authority for selected application enrollment."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

AppKind = Literal["claude_code", "codex", "cursor"]
SaltedRef = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class EnrollmentGrantRequest(BaseModel):
    """Administrative grant with no implicit tools or inference access."""

    model_config = ConfigDict(extra="forbid")
    apps: list[AppKind] = Field(min_length=1, max_length=3)
    expires_minutes: int = Field(default=15, ge=1, le=60)
    credential_hours: int = Field(default=720, ge=1, le=2160)
    allowed_mcp_tools: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("apps", "allowed_mcp_tools")
    @classmethod
    def unique_values(cls, values: list[str]) -> list[str]:
        """Reject duplicate, empty and excessively long grant entries."""
        if len(set(values)) != len(values) or any(
            not value.strip() or len(value) > 255 for value in values
        ):
            raise ValueError("Grant entries must be unique nonempty bounded strings")
        return values


class EnrollmentGrantResponse(BaseModel):
    """One-time token-file envelope, delivered separately from managed settings."""

    schema_version: Literal[1] = 1
    token: str
    salt: str
    expires_at: datetime


class EnrollmentExchangeRequest(BaseModel):
    """Pseudonymous target identity; raw OS and MDM identifiers stay local."""

    model_config = ConfigDict(extra="forbid")
    workstation_fingerprint: SaltedRef
    user_ref: SaltedRef
    device_ref_hash: SaltedRef | None = None
    apps: list[AppKind] = Field(min_length=1, max_length=3)

    @field_validator("apps")
    @classmethod
    def unique_apps(cls, values: list[str]) -> list[str]:
        """Reject repeated selections."""
        if len(values) != len(set(values)):
            raise ValueError("Apps must be unique")
        return values


class EnrollmentAppCredential(BaseModel):
    """Device/user/app-scoped credential returned exactly once."""

    app: AppKind
    agent_id: UUID
    runtime_principal_id: SaltedRef
    enrollment_id: UUID
    token: str
    expires_at: datetime


class EnrollmentExchangeResponse(BaseModel):
    """Credential checkpoint for local config application."""

    schema_version: Literal[1] = 1
    apps: list[EnrollmentAppCredential]


class EnrollmentIdentity(BaseModel):
    """The agent bound to the authenticated durable credential."""

    agent_id: UUID


class EnrollmentApplyResponse(BaseModel):
    """Acknowledgment of local config apply, without runtime validation claims."""

    state: Literal["configured"] = "configured"
