"""Shared contract for filtered lists linked from the weekly report."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

ReportKind = Literal[
    "usage",
    "agents",
    "models",
    "cache",
    "optimizations",
    "spend_outlier",
    "valuable_work",
    "approvals",
    "first_seen",
    "reliability",
    "users",
    "policy_notices",
]


class WeeklyReportRow(BaseModel):
    """A counted item with its evidence and optional resource action."""

    label: str
    value: str = ""
    meta: str = ""
    detail: str = ""
    url: str | None = None
    links: list["WeeklyReportRow"] = Field(default_factory=list)


class WeeklyReportItems(BaseModel):
    """One complete collection, paginated after applying URL filters."""

    kind: ReportKind
    title: str
    account_id: UUID
    start_date: datetime
    end_date: datetime
    items: list[WeeklyReportRow]
    total: int
    notes: list[str] = Field(default_factory=list)
