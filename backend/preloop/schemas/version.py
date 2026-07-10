from pydantic import BaseModel, Field


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
