"""Request and response shapes for the session artifact deposit and list API.

The request ``content`` is an MCP ``ContentBlock`` (spec 2026-07-28) and the
response ``content_block`` is an MCP ``ResourceLink``. Preloop fields with no
MCP slot travel in ``_meta["preloop.dev/artifact"]``, as decided in the
artifacts memo, section B.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ArtifactDepositMetadata(BaseModel):
    """Fields of a deposit that are not the content itself.

    This is the ``metadata`` part of a ``multipart/form-data`` deposit.
    ``name`` defaults to the uploaded file name there.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Optional[str] = Field(
        None,
        description=(
            "Artifact kind. Inferred from the media type when omitted "
            "(image -> screenshot, text/vtt -> transcript, ...)."
        ),
    )
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    labels: Optional[dict[str, Any]] = Field(
        None,
        description=(
            "Up to 16 keys matching ^[a-z][a-z0-9_.-]{0,62}$; string values "
            "up to 128 characters; `tags` is a list of strings. Documented "
            "keys: site, tenant_ref, consent_basis, retention_class, tags."
        ),
    )
    activity_id: Optional[str] = Field(
        None,
        description="Existing timeline row in this session the artifact illustrates.",
    )
    parent_artifact_id: Optional[str] = Field(
        None, description="Artifact in this account this one derives from."
    )
    tool_name: Optional[str] = Field(None, min_length=1, max_length=255)


class ArtifactDepositIn(ArtifactDepositMetadata):
    """JSON deposit: the metadata fields plus one MCP ``ContentBlock``."""

    name: str = Field(..., min_length=1, max_length=255)
    content: dict[str, Any] = Field(
        ...,
        description=(
            "One MCP ContentBlock: text, image, audio, or resource (text or "
            "blob). A resource_link carries no bytes and is refused."
        ),
    )


class McpResourceLink(BaseModel):
    """MCP ``ResourceLink`` pointing at the artifact's byte route."""

    model_config = ConfigDict(populate_by_name=True)

    type: Literal["resource_link"] = "resource_link"
    uri: str
    name: str
    mime_type: str = Field(..., alias="mimeType")
    size: int
    meta: dict[str, Any] = Field(default_factory=dict, alias="_meta")


class RuntimeSessionArtifactOut(BaseModel):
    """Descriptor of one stored session artifact."""

    id: str
    runtime_session_id: str
    activity_id: Optional[str] = None
    kind: str
    name: Optional[str] = None
    content_type: str
    size_bytes: int
    sha256: str
    labels: dict[str, Any] = Field(default_factory=dict)
    producer: Optional[str] = None
    agent_id: Optional[str] = None
    tool_name: Optional[str] = None
    parent_artifact_id: Optional[str] = None
    text_status: Optional[str] = None
    availability: str
    legal_hold: bool
    created_at: datetime
    content_block: McpResourceLink


class RuntimeSessionArtifactListOut(BaseModel):
    """One page of a session's artifacts, newest first."""

    items: list[RuntimeSessionArtifactOut]
    next_cursor: Optional[str] = Field(
        None, description="Pass as `cursor` for the next page; null on the last page."
    )
