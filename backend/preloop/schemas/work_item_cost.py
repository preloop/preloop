"""Schemas for cost per work item: issues grouped with their pull requests."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

WorkItemKind = Literal["issue", "pull_request"]

SPLIT_RULE = (
    "A flow run or agentic session that worked on several work items splits "
    "its spend evenly between them, so groups add up to the attributed total "
    "without counting any request twice."
)


class WorkItemSlice(BaseModel):
    """One contributor dimension (agent, flow, model or user) of a group."""

    key: str = Field(..., description="Stable id of the agent, flow, model or user.")
    label: str = Field(..., description="Display name.")
    estimated_cost: float = Field(
        0.0, description="Priced subtotal in USD after the even split."
    )
    total_tokens: int = Field(0, description="Tokens after the even split.")
    request_count: float = Field(
        0.0, description="Gateway requests after the even split (may be fractional)."
    )


class WorkItemRef(BaseModel):
    """One issue or pull request inside a group."""

    kind: WorkItemKind
    key: str = Field(..., description="Canonical key, ``owner/repo#N`` or ``PROJ-N``.")
    title: Optional[str] = None
    url: Optional[str] = None
    state: Optional[str] = Field(None, description="Pull request state when known.")
    estimated_cost: float = 0.0
    total_tokens: int = 0
    request_count: float = 0.0


class WorkItemGroup(BaseModel):
    """An issue with the pull requests that implement or reference it."""

    key: str = Field(..., description="Key of the lead item (the issue when any).")
    kind: WorkItemKind
    title: Optional[str] = None
    url: Optional[str] = None
    estimated_cost: float = Field(0.0, description="Priced subtotal in USD.")
    partial: bool = Field(
        False,
        description="Some contributing requests had no price or no token counts.",
    )
    total_tokens: int = 0
    request_count: float = 0.0
    execution_count: int = Field(0, description="Flow runs that contributed.")
    session_count: int = Field(0, description="Agentic sessions that contributed.")
    items: list[WorkItemRef] = Field(default_factory=list)
    agents: list[WorkItemSlice] = Field(default_factory=list)
    flows: list[WorkItemSlice] = Field(default_factory=list)
    models: list[WorkItemSlice] = Field(default_factory=list)
    users: list[WorkItemSlice] = Field(default_factory=list)


class WorkItemCostReport(BaseModel):
    """Spend per work item over one window, most expensive first."""

    period_start: datetime
    period_end: datetime
    groups: list[WorkItemGroup] = Field(default_factory=list)
    total_groups: int = Field(0, description="Groups before the limit.")
    estimated_cost: float = Field(0.0, description="Attributed spend of all groups.")
    total_tokens: int = 0
    truncated: bool = Field(
        False,
        description="A source hit its row cap, so older contributors may be missing.",
    )
    split_rule: str = SPLIT_RULE
