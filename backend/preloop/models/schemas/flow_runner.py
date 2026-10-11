"""Pydantic schemas for self-hosted flow runners."""

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from preloop.models.models.flow_runner import (
    DEFAULT_RUNNER_CONCURRENCY,
    MAX_RUNNER_CONCURRENCY,
)


class HostExecProfileAdvertisement(BaseModel):
    """Name and capability flags a runner advertises. No executable path."""

    name: str = Field(max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    capabilities: List[str] = Field(default_factory=list, max_length=16)
    models: List[str] = Field(default_factory=list, max_length=64)


#: Harness ids a runner may report (contract A, closed in wave 1; extend by PR).
HarnessId = Literal[
    "copilot_cli",
    "cursor_cli",
    "claude_code",
    "codex_cli",
    "opencode",
    "gemini_cli",
    "claude_desktop",
    "vscode_copilot",
]
HARNESS_IDS = frozenset(get_args(HarnessId))
HarnessLoginState = Literal["signed_in", "signed_out", "unknown", "not_applicable"]
HarnessLoginSource = Literal["env", "stored", "cli_status", "none", "unknown"]
HarnessGovernance = Literal["governed", "partial", "ungoverned", "unknown"]
HarnessSupportLevel = Literal["flows_and_sessions", "flows_only", "presence_only"]
HarnessSessionMode = Literal["resume", "stream", "replay", "none"]
HarnessBilling = Literal["seat", "metered", "unknown"]
HarnessModelSource = Literal["probed", "configured", "static", "observed"]

MAX_HARNESS_INVENTORY_ENTRIES = 32
MAX_HARNESS_MODELS = 64
MAX_HARNESS_MODEL_ID_LENGTH = 128


class HarnessModel(BaseModel):
    """One model a harness can run, and where the runner learned about it."""

    id: str = Field(min_length=1, max_length=MAX_HARNESS_MODEL_ID_LENGTH)
    source: HarnessModelSource


class HarnessInventoryEntry(BaseModel):
    """One locally installed harness as reported by a runner.

    Never carries executable paths, argv, env values, tokens or usernames.
    """

    harness: HarnessId
    display_name: str = Field(max_length=128)
    version: Optional[str] = Field(None, max_length=64)
    login_state: HarnessLoginState = "unknown"
    login_source: HarnessLoginSource = "unknown"
    #: Host only (``github.com``, ``<x>.ghe.com``); absent for non-GitHub harnesses.
    account_host: Optional[str] = Field(None, max_length=255)
    governance: HarnessGovernance = "unknown"
    support_level: HarnessSupportLevel = "presence_only"
    enabled: bool = True
    #: Set on the host only (``preloop runner sessions enable``), never by the server.
    sessions_enabled: bool = False
    session_mode: HarnessSessionMode = "none"
    billing: HarnessBilling = "unknown"
    models: List[HarnessModel] = Field(
        default_factory=list, max_length=MAX_HARNESS_MODELS
    )
    generated_profile: Optional[str] = Field(
        None, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
    )
    capabilities: List[str] = Field(default_factory=list, max_length=16)


class HarnessInventory(BaseModel):
    """Harness inventory a runner publishes with register and heartbeat."""

    model_config = ConfigDict(populate_by_name=True)

    #: Wire name is ``schema``; renamed here because it shadows BaseModel.schema.
    schema_version: int = Field(1, ge=1, alias="schema")
    generated_at: datetime
    hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    entries: List[HarnessInventoryEntry] = Field(
        default_factory=list, max_length=MAX_HARNESS_INVENTORY_ENTRIES
    )

    @field_validator("entries", mode="before")
    @classmethod
    def drop_unknown_harnesses(cls, value: Any) -> Any:
        """Ignore entries for harness ids this server does not know yet.

        A newer runner may report a harness added after this release; that
        must not make the whole register or heartbeat fail.
        """
        if not isinstance(value, list):
            return value
        return [
            entry
            for entry in value
            if not isinstance(entry, dict) or entry.get("harness") in HARNESS_IDS
        ]

    def to_wire(self) -> Dict[str, Any]:
        """Serialize with wire field names, dropping absent optional fields."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


def harness_inventory_hash(entries: List[HarnessInventoryEntry]) -> str:
    """Return ``sha256:<hex>`` of the canonical JSON of ``entries``.

    Canonical JSON: absent (null) fields omitted, keys sorted, no whitespace,
    UTF-8 without ASCII escaping. The Go runner computes the same value
    (``harnessInventoryHash`` in ``cli/internal/cmd/runner_inventory.go``).
    """
    payload = [
        entry.model_dump(mode="json", by_alias=True, exclude_none=True)
        for entry in entries
    ]
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RunnerRegisterRequest(BaseModel):
    """Register or resume a runner for the logged-in account."""

    name: Optional[str] = Field(None, max_length=200)
    hostname: Optional[str] = Field(None, max_length=255)
    os: Optional[str] = Field(None, max_length=30)
    arch: Optional[str] = Field(None, max_length=30)
    labels: List[str] = Field(default_factory=list)
    #: True for `preloop runner fg --ephemeral`: the row exists for one
    #: process and is deleted, not kept offline, once its heartbeat lapses.
    ephemeral: bool = False
    runner_id: Optional[UUID] = None
    instance_id: Optional[UUID] = None
    host_exec_profiles: Optional[List[HostExecProfileAdvertisement]] = Field(
        default_factory=list, max_length=64
    )

    @field_validator("host_exec_profiles", mode="before")
    @classmethod
    def accept_null_host_exec_profiles(cls, value: Any) -> Any:
        """Map JSON null to an empty list so older clients can still register."""
        if value is None:
            return []
        return value

    #: Locally installed harnesses (contract A). Absent or null for runners
    #: that predate it ("inventory unknown").
    harness_inventory: Optional[HarnessInventory] = None

    #: How many jobs this process is willing to run at once. It may lower the
    #: stored ceiling for as long as it is connected; it never raises it.
    concurrency: Optional[int] = Field(None, ge=1, le=MAX_RUNNER_CONCURRENCY)


class RunnerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    account_id: UUID
    registered_by_user_id: Optional[UUID] = None
    instance_id: Optional[UUID] = None
    name: str
    hostname: Optional[str] = None
    os: Optional[str] = None
    arch: Optional[str] = None
    labels: List[str] = Field(default_factory=list)
    ephemeral: bool = False
    status: str
    last_heartbeat: Optional[datetime] = None
    current_execution_id: Optional[UUID] = None
    #: The owner's ceiling on concurrent jobs for this runner.
    concurrency: int = DEFAULT_RUNNER_CONCURRENCY
    #: What the connected process says it can run at once, if it said.
    reported_concurrency: Optional[int] = None
    #: Ceiling and report combined: what a dispatcher may actually fill.
    capacity: int = DEFAULT_RUNNER_CONCURRENCY
    #: Executions this runner holds right now.
    running_count: int = 0
    running_execution_ids: List[UUID] = Field(default_factory=list)
    registered_by_email: Optional[str] = None
    capabilities: Dict[str, Any] = Field(default_factory=dict)
    #: Last published harness inventory; null means "inventory unknown".
    harness_inventory: Optional[HarnessInventory] = None
    harness_inventory_updated_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class RunnerRegisterResponse(RunnerResponse):
    token: str


class RunnerDeleteResponse(BaseModel):
    """Outcome of deleting a runner; its token is rejected from now on."""

    id: UUID
    deleted: bool = True
    #: Executions the runner held that a forced delete stopped.
    halted_execution_ids: List[UUID] = Field(default_factory=list)


class RunnerConcurrencyUpdate(BaseModel):
    """Edit one runner's slot ceiling from the console."""

    concurrency: int = Field(ge=1, le=MAX_RUNNER_CONCURRENCY)


class RunnerFleetSummary(BaseModel):
    runner_count: int
    online_runner_count: int
    last_runner_heartbeat: Optional[str] = None
