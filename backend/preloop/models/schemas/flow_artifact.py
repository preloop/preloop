"""Versioned, runner-independent references to isolated recovery artifacts."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ArtifactReference(BaseModel):
    """Opaque reference; authorization is checked on every retrieval."""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    artifact_id: UUID
    execution_id: UUID
    storage_kind: Literal["hosted", "runner_local"] = "hosted"
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ArtifactManifest(BaseModel):
    """Metadata committed atomically with a validated artifact."""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    kind: Literal["workspace", "native_session"]
    execution_id: UUID
    thread_id: str = Field(min_length=1, max_length=200)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    expanded_bytes: int = Field(ge=0)
    created_at: datetime
    expires_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
