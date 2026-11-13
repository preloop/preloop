"""Secret-free registration metadata and the persisted drift expectation tuple."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from preloop.schemas.discovery_evidence import (
    AGENT_KIND_PATTERN,
    HEX_SHA256_PATTERN,
    ControlAxis,
)

Digest = Annotated[str, Field(pattern=HEX_SHA256_PATTERN)]
MetadataText = Annotated[str, Field(max_length=2000)]
MetadataList = Annotated[list[MetadataText], Field(max_length=32)]


class BundleManifest(BaseModel):
    """Allowlisted manifest only; rendered policy and credentials are excluded."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    manifest_schema: Literal["preloop.managed-hook-bundle.v1"] = Field(alias="schema")
    app_id: Literal["claude-code"]
    platform: Literal["darwin", "linux", "windows"]
    policy_revision_hash: Digest
    artifact: Literal["managed-settings.json"]
    keys: list[Literal["hooks.PreToolUse"]] = Field(min_length=1, max_length=1)
    target_locations: MetadataList
    managed_source: MetadataText
    documented_semantics: MetadataText
    verification: Literal["unverified"]
    prerequisites: MetadataList
    omitted_capabilities: MetadataList
    references: MetadataList


class ControlExpectationInput(BaseModel):
    """The source/workstation/app/context/axis/revision identity used by drift."""

    model_config = ConfigDict(extra="forbid")

    source_ref: UUID
    workstation_fingerprint: Digest
    agent_kind: str = Field(pattern=AGENT_KIND_PATTERN)
    config_path_hash: Digest
    axis: ControlAxis
    policy_revision_hash: Digest
    required_scope: Literal["managed_config", "user_config", "any"] = "managed_config"


class BundleRevisionResponse(BaseModel):
    """An active registered revision and its active generated row count."""

    id: UUID
    manifest: BundleManifest
    registered_at: datetime
    generated_count: int


class BundleRegistryResponse(BaseModel):
    """Account-scoped registry, with an uncapped generated expectation count."""

    bundles: list[BundleRevisionResponse]
    generated_count: int
