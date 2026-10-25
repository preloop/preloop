"""Console views over stored Delivery Report runs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class DeliveryReportWindow(BaseModel):
    """The period a run covered, as the run recorded it."""

    start: Optional[str] = Field(None, alias="from")
    end: Optional[str] = Field(None, alias="to")

    model_config = {"populate_by_name": True}


class DeliveryReportRow(BaseModel):
    """One run on the collection page."""

    id: UUID
    flow_id: UUID
    flow_name: Optional[str] = None
    status: str
    report_status: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    window: DeliveryReportWindow
    projects: List[str] = Field(default_factory=list)
    shipped: int = 0
    closed: int = 0
    closed_unverified: int = 0
    proposals: int = 0
    cost_usd: Optional[float] = Field(
        None, description="Gateway spend; null when the gateway could not price it."
    )


class DeliveryReportList(BaseModel):
    """A page of runs and the total that matched the filter."""

    items: List[DeliveryReportRow]
    total: int


class DeliveryReportActions(BaseModel):
    """Which report actions this server can carry out.

    All three are served by ``POST /delivery-reports/{id}/actions``. A
    server that reports one as false (an older build) gets it rendered
    disabled in the console.
    """

    open_issue: bool = True
    reopen: bool = True
    dismiss: bool = True


class DeliveryReportDetail(DeliveryReportRow):
    """One run with its full report document."""

    report: Dict[str, Any]
    stale_reopen_requests: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Reopen requests that no longer apply, by issue ref, with the reason: "
            "the issue was edited, or new commits landed on its closing pull "
            "request, after the assessed revision."
        ),
    )
    actions: DeliveryReportActions = Field(default_factory=DeliveryReportActions)
