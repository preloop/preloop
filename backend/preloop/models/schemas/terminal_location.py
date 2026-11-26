"""Metadata-only terminal observations; never terminal content."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class TerminalPlace(BaseModel):
    """Bounded display metadata supplied by a terminal multiplexer."""

    multiplexer: str = Field(default="herdr", min_length=1, max_length=32)
    workspace_id: str = Field(max_length=256)
    workspace_label: str = Field(default="", max_length=256)
    tab_id: str = Field(max_length=256)
    tab_label: str = Field(default="", max_length=256)
    pane_id: str = Field(max_length=256)
    pane_label: str = Field(default="", max_length=256)
    terminal_title: str = Field(default="", max_length=256)
    state: Literal["working", "idle", "blocked", "done", "closed", "unknown"]

    @field_validator(
        "workspace_label", "tab_label", "pane_label", "terminal_title", mode="before"
    )
    @classmethod
    def display_text(cls, value: str) -> str:
        """Bound person-written labels and remove control characters."""
        if not isinstance(value, str):
            return value
        return "".join(c for c in value if c.isprintable())[:256]


class TerminalLocation(TerminalPlace):
    """Session-retained location linked to an authenticated device report."""

    device_id: str = Field(max_length=256)
    observed_at: datetime
    pane_closed_at: datetime | None = None


class HerdrAgent(TerminalPlace):
    """Observed agent; unknown harnesses and native identities stay unmanaged."""

    multiplexer: Literal["herdr"] = "herdr"
    harness_kind: str = Field(max_length=256)
    harness_session_id: str = Field(default="", max_length=512)
    cwd: str | None = Field(default=None, max_length=4096)
    runtime_session_id: str | None = None
    managed: bool = False


class HerdrInventory(BaseModel):
    """Complete observation snapshot; malformed lists must never imply closure."""

    protocol: int
    observe_enabled: bool
    prompt_enabled: bool = False
    observed_at: datetime
    status: Literal["herdr_protocol_unsupported"] | None = None
    agents: list[HerdrAgent] = Field(max_length=512)

    @field_validator("agents")
    @classmethod
    def unique_panes(cls, agents: list[HerdrAgent]) -> list[HerdrAgent]:
        """A complete snapshot cannot assign two agents to the same pane."""
        if len({agent.pane_id for agent in agents}) != len(agents):
            raise ValueError("duplicate pane identity")
        return agents
