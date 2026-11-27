"""Preview and apply contract for stored review recovery."""

from pydantic import BaseModel, ConfigDict, Field


class RefinalizeRequest(BaseModel):
    """A dry run by default; application requires the exact preview digest."""

    model_config = ConfigDict(extra="forbid", strict=True)
    dry_run: bool = True
    expected_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
