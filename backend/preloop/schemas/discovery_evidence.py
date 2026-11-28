"""Allowlisted observation metadata; collector claims are never verification."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from preloop.schemas.discovered_extension import ExtensionMetadata

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
    configured: State = "unknown"
    configuration_evidence: Literal["local_config", "managed_config", "unavailable"] = (
        "unavailable"
    )
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
    config_scope: Literal["user_config", "managed_config"] = "user_config"
    profile_ref: str | None = Field(default=None, pattern=HEX_SHA256_PATTERN)
    completeness: Literal["complete", "partial", "failed"] | None = None
    extensions: list[ExtensionMetadata] | None = Field(default=None, max_length=200)
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
        if self.config_scope == "managed_config" and self.profile_ref is not None:
            raise ValueError("System managed configuration has no user profile")
        if len({control.axis for control in self.controls}) != len(self.controls):
            raise ValueError("Control axes must be unique")
        return self


class ObservedProfile(SafeEvidenceModel):
    """Keyed local profile scope with fixed failure codes and no clear paths."""

    profile_ref: str = Field(pattern=HEX_SHA256_PATTERN)
    completeness: Literal["complete", "partial", "failed"]
    error_code: (
        Literal["profile_unavailable", "profile_unreadable", "probe_incomplete"] | None
    ) = None

    @model_validator(mode="after")
    def consistent_state(self) -> ObservedProfile:
        """A failed profile cannot masquerade as an empty successful scan."""
        # Older collectors omitted a reason when runtime probes were inconclusive.
        if (self.completeness == "complete" and self.error_code is not None) or (
            self.completeness == "failed" and self.error_code is None
        ):
            raise ValueError("Profile completeness and error must agree")
        return self


class DiscoveryEvidence(SafeEvidenceModel):
    """Versioned bounded observation, including empty and failed scans."""

    schema_version: Literal[1, 2, 3, 4] = 1
    extension_completeness: Literal["complete", "partial", "failed"] | None = None
    extension_errors: list[
        Literal[
            "extension_file_limit",
            "extension_hook_unbound",
            "extension_limit",
            "extension_malformed",
            "extension_project_unscanned",
            "extension_symlink",
            "extension_unreadable",
            "extension_unsafe_path",
            "extension_unsupported_scope",
            "extension_unsupported_version",
        ]
    ] = Field(default_factory=list, max_length=10)
    profiles: list[ObservedProfile] = Field(default_factory=list, max_length=64)
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
    scope_completeness: dict[
        Literal["user_config", "managed_config"],
        Literal["complete", "partial", "failed"],
    ] = Field(default_factory=dict, max_length=2)
    errors: list[
        Literal[
            "permission_denied", "unavailable", "unsupported", "parse_failed", "timeout"
        ]
    ] = Field(default_factory=list, max_length=5)
    applications: list[ObservedApplication] = Field(
        default_factory=list, max_length=100
    )

    @model_serializer(mode="wrap")
    def serialize_versioned(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Preserve v1 canonical bytes for immutable observation replay hashes."""
        data: dict[str, Any] = handler(self)
        if not self.extension_errors:
            data.pop("extension_errors", None)
        if not self.scope_completeness:
            data.pop("scope_completeness", None)
        if self.schema_version == 1:
            data.pop("profiles", None)
            for application in data.get("applications", []):
                application.pop("profile_ref", None)
        # Additive facts must not alter immutable replay hashes for old reports.
        for application in data.get("applications", []):
            if application.get("config_scope") == "user_config":
                application.pop("config_scope", None)
            for control in application.get("controls", []):
                if control.get("configuration_evidence") == "unavailable":
                    control.pop("configuration_evidence", None)
                    if control.get("configured") == "unknown":
                        control.pop("configured", None)
        if self.schema_version < 3:
            data.pop("extension_completeness", None)
            for application in data.get("applications", []):
                application.pop("extensions", None)
                application.pop("completeness", None)
        return data

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
        if any(scope not in self.scan_scope for scope in self.scope_completeness):
            raise ValueError("Scope completeness requires a declared scan scope")
        if self.completeness == "complete" and any(
            value != "complete" for value in self.scope_completeness.values()
        ):
            raise ValueError("Complete collection cannot include partial scopes")
        refs = [profile.profile_ref for profile in self.profiles]
        if len(set(refs)) != len(refs):
            raise ValueError("Profile references must be unique")
        if self.schema_version == 1 and (
            self.profiles or any(app.profile_ref for app in self.applications)
        ):
            raise ValueError("Profile metadata requires schema version 2")
        if self.schema_version == 2 or (self.schema_version >= 3 and self.profiles):
            if any(
                app.profile_ref not in refs
                and not (
                    app.profile_ref is None and app.config_scope == "managed_config"
                )
                for app in self.applications
            ):
                raise ValueError("Application must reference a collected profile")
            if self.completeness == "complete" and any(
                profile.completeness != "complete" for profile in self.profiles
            ):
                raise ValueError("Complete collection cannot include partial profiles")
        if self.extension_errors and (
            self.schema_version < 3 or self.extension_completeness != "partial"
        ):
            raise ValueError("Extension errors require partial v3 extension coverage")
        if self.schema_version < 3 and self.extension_completeness is not None:
            raise ValueError("Extension completeness requires schema version 3")
        if self.extension_completeness == "complete" and (
            self.scope_completeness.get("user_config", self.completeness) == "failed"
            or any(profile.completeness == "failed" for profile in self.profiles)
            or any(
                app.completeness != "complete"
                for app in self.applications
                if app.config_scope != "managed_config"
            )
        ):
            raise ValueError(
                "Complete extensions cannot include failed or partial extension scope"
            )
        for app in self.applications:
            if self.schema_version < 4 and any(
                ext.details is not None for ext in (app.extensions or [])
            ):
                raise ValueError("Extension details require schema version 4")
            if self.schema_version < 3 and (
                app.extensions is not None or app.completeness is not None
            ):
                raise ValueError("Extensions require schema version 3")
            if app.config_scope == "managed_config":
                if "managed_config" not in self.scan_scope:
                    raise ValueError("Managed configuration requires its scan scope")
                if app.extensions is not None or app.completeness is not None:
                    raise ValueError(
                        "Managed configuration does not collect extensions"
                    )
                continue
            if self.schema_version >= 3:
                if app.extensions is None or app.completeness is None:
                    raise ValueError(
                        "Version 3 requires extensions and app completeness"
                    )
                extension_keys = [
                    (ext.kind, ext.id, ext.scope, ext.project_ref_hash)
                    for ext in app.extensions
                ]
                if len(set(extension_keys)) != len(extension_keys):
                    raise ValueError(
                        "Extension keys must be unique within an application"
                    )
                if self.completeness == "complete" and app.completeness != "complete":
                    raise ValueError(
                        "Complete collection cannot include partial applications"
                    )
                if app.profile_ref is not None and app.profile_ref not in refs:
                    raise ValueError("Application must reference a collected profile")
        keys = [(app.agent_kind, app.config_path_hash) for app in self.applications]
        if len(set(keys)) != len(keys):
            raise ValueError("Application contexts must be unique")
        return self
