"""Request and response shapes for opt-in agent discovery reporting.

The request models forbid unknown fields and constrain every string to a
narrow alphabet. That is the privacy rule in executable form: a client that
tries to send a hostname, user name, clear path, MCP URL or argument has
nowhere to put it, and the request is rejected rather than stored.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

HEX_SHA256_PATTERN = r"^[0-9a-f]{64}$"
AGENT_KIND_PATTERN = r"^[a-z0-9][a-z0-9_\-]{0,63}$"
VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9.+_\-]{0,63}$"

#: Upper bound on candidates in one report. A workstation has a dozen known
#: agent tools at most; anything near this is not a discovery report.
MAX_CANDIDATES_PER_REPORT = 100

OsFamily = Literal["darwin", "linux", "windows", "other"]
CandidateStatus = Literal["new", "onboarded", "ignored"]


class DiscoverySaltResponse(BaseModel):
    """The per-account salt the CLI keys its hashes with."""

    salt: str
    algorithm: Literal["hmac-sha256"] = "hmac-sha256"
    retention_days: int


class DiscoveryReportCandidate(BaseModel):
    """One agent tool found on the workstation."""

    model_config = ConfigDict(extra="forbid")

    agent_kind: str = Field(pattern=AGENT_KIND_PATTERN)
    agent_version: Optional[str] = Field(default=None, pattern=VERSION_PATTERN)
    config_path_hash: str = Field(pattern=HEX_SHA256_PATTERN)
    mcp_server_count: int = Field(default=0, ge=0, le=10_000)
    enrolled: bool = False


class DiscoveryReportRequest(BaseModel):
    """Body of ``POST /api/v1/agents/discovery-reports``."""

    model_config = ConfigDict(extra="forbid")

    workstation_fingerprint: str = Field(pattern=HEX_SHA256_PATTERN)
    cli_version: Optional[str] = Field(default=None, pattern=VERSION_PATTERN)
    os: Optional[OsFamily] = None
    candidates: list[DiscoveryReportCandidate] = Field(
        default_factory=list, max_length=MAX_CANDIDATES_PER_REPORT
    )


class DiscoveryReportResponse(BaseModel):
    """What the server did with a report."""

    received: int
    created: int
    updated: int


class DiscoveredAgentCandidateSummary(BaseModel):
    """One row of the console's "Not yet governed" list."""

    id: UUID
    agent_kind: str
    agent_version: Optional[str] = None
    workstation_fingerprint: str
    config_path_hash: str
    mcp_server_count: int
    enrolled: bool
    os_family: Optional[str] = None
    status: CandidateStatus
    managed_agent_id: Optional[UUID] = None
    first_seen_at: datetime
    last_seen_at: datetime


class DiscoveredAgentCandidateList(BaseModel):
    """Capped console list plus the count of every matching candidate."""

    items: list[DiscoveredAgentCandidateSummary]
    total: int
    truncated: bool


class DiscoveredAgentCandidateUpdate(BaseModel):
    """Console action on a candidate: mark ignored, or un-ignore."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["new", "ignored"]
