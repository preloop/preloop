"""Allowlisted observation metadata; collector claims are never verification."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

AGENT_KIND_PATTERN = r"^[a-z0-9][a-z0-9_\-]{0,63}$"
HEX_SHA256_PATTERN = r"^[0-9a-f]{64}$"
VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9.+_\-]{0,63}$"

State = Literal["true", "false", "unknown"]
ControlAxis = Literal[
    "model_route",
    "model_restriction",
    "native_tool_hook",
    "mcp_control",
    "vendor_policy",
    "content_inspection",
    "usage_visibility",
]


class SafeEvidenceModel(BaseModel):
    """Reject unrecognized metadata rather than storing it."""

    model_config = ConfigDict(extra="forbid")


class ObservedControl(SafeEvidenceModel):
    """Independent collector assertions, with no effective/verified field."""

    axis: ControlAxis
    supported: State = "unknown"
    requested: State = "unknown"
    applied: State = "unknown"
    method: Literal[
        "adapter_descriptor", "local_config", "managed_config", "unknown"
    ] = "unknown"


class ObservedApplication(SafeEvidenceModel):
    """Safe application facts bound to a candidate's hashed context."""

    agent_kind: str = Field(pattern=AGENT_KIND_PATTERN)
    config_path_hash: str = Field(pattern=HEX_SHA256_PATTERN)
    app_version: str | None = Field(default=None, pattern=VERSION_PATTERN)
    surface: Literal["cli", "desktop", "editor", "extension", "other", "unknown"] = (
        "unknown"
    )
    installed: State = "unknown"
    configured: State = "unknown"
    observed_active: State = "unknown"
    controls: list[ObservedControl] = Field(default_factory=list, max_length=7)

    @model_validator(mode="after")
    def unique_controls(self) -> ObservedApplication:
        """Each axis occurs at most once in a source observation."""
        if len({control.axis for control in self.controls}) != len(self.controls):
            raise ValueError("Control axes must be unique")
        return self


class DiscoveryEvidence(SafeEvidenceModel):
    """Versioned bounded observation, including empty and failed scans."""

    schema_version: Literal[1] = 1
    observation_id: UUID
    collector_version: str | None = Field(default=None, pattern=VERSION_PATTERN)
    os_family: Literal["darwin", "linux", "windows", "other"] | None = None
    detector_version: str | None = Field(default=None, pattern=VERSION_PATTERN)
    source_environment: Literal[
        "workstation", "managed_device", "container", "vm", "unknown"
    ] = "unknown"
    started_at: AwareDatetime
    ended_at: AwareDatetime
    scan_scope: list[
        Literal["installed_apps", "user_config", "managed_config", "running_apps"]
    ] = Field(min_length=1, max_length=4)
    completeness: Literal["complete", "partial", "failed"]
    errors: list[
        Literal[
            "permission_denied", "unavailable", "unsupported", "parse_failed", "timeout"
        ]
    ] = Field(default_factory=list, max_length=5)
    applications: list[ObservedApplication] = Field(
        default_factory=list, max_length=100
    )

    @model_validator(mode="after")
    def valid_window(self) -> DiscoveryEvidence:
        """Bound collection windows and disallow contradictory complete scans."""
        if (
            self.started_at > self.ended_at
            or self.ended_at - self.started_at > timedelta(days=1)
        ):
            raise ValueError("Collection window must be ordered and at most one day")
        if self.completeness == "complete" and self.errors:
            raise ValueError("Complete collection cannot include errors")
        keys = [(app.agent_kind, app.config_path_hash) for app in self.applications]
        if len(set(keys)) != len(keys):
            raise ValueError("Application contexts must be unique")
        return self
