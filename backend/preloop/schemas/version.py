from typing import Dict, Optional

from pydantic import BaseModel, Field


class ClientVersionInfo(BaseModel):
    """Update contract for a native app client (iOS/Android)."""

    min_version: str = Field(
        "", description="Oldest client version still allowed; older must update."
    )
    latest_version: str = Field(
        "", description="Newest released client version; older should update."
    )
    store_url: str = Field("", description="App Store / Play Store URL for updating.")


class VersionInfo(BaseModel):
    """Schema for returning version information."""

    server_version: str = Field(
        ..., description="Current version of the Preloop server."
    )
    min_client_version: str = Field(
        ...,
        description="Minimum required version for clients connecting to this Preloop server.",
    )
    max_client_version: str = Field(
        ...,
        description="Maximum recommended version for clients connecting to this Preloop server.",
    )
    # CLI update-check contract. The Preloop CLI reads these exact keys
    # (cli/internal/version/check.go); without them the CLI can never
    # detect that a newer release exists.
    latest_version: str = Field(
        "",
        description="Latest released version, used by the Preloop CLI update check.",
    )
    min_version: str = Field(
        "",
        description="Minimum supported client version, used by the Preloop CLI.",
    )
    download_url: str = Field(
        "",
        description="Where to fetch the latest CLI release.",
    )
    release_notes: str = Field(
        "",
        description="Optional short note shown by the CLI when an update is available.",
    )
    update_available: bool = Field(
        False,
        description=(
            "Whether a newer Preloop release than this server is available "
            "(from the instance's own daily version check; always False when "
            "telemetry is disabled)."
        ),
    )
    clients: Dict[str, ClientVersionInfo] = Field(
        default_factory=dict,
        description="Per-platform (ios/android) client update contracts.",
    )


class VersionStatus(BaseModel):
    """Admin-facing update status for this installation."""

    version: str = Field(..., description="Version this server is running.")
    update_available: bool = Field(False)
    latest_version: Optional[str] = Field(None)
    update_url: Optional[str] = Field(None)
    changelog_url: Optional[str] = Field(None)
    checked_at: Optional[str] = Field(
        None, description="ISO timestamp of the last successful version check."
    )
    telemetry_enabled: bool = Field(
        True, description="False when PRELOOP_DISABLE_TELEMETRY is set."
    )
