"""Read stored Delivery Report runs for the console.

The report is the run's ``result`` document (see
:mod:`preloop.services.delivery_report`). These helpers turn a run into a
collection row, and mark reopen requests that went stale after the
assessment, reading current issue and pull request state through the CRUD
layer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy.orm import Session

from preloop.models.crud import crud_issue
from preloop.models.crud.issue_cost import crud_issue_cost
from preloop.schemas.delivery_report import (
    DeliveryReportDetail,
    DeliveryReportRow,
    DeliveryReportWindow,
)
from preloop.services.delivery_report import compute_summary
from preloop.services.flow_pr_binding import normalize_pr_url

logger = logging.getLogger(__name__)


def _parse_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _summary(report: Mapping[str, Any]) -> Mapping[str, Any]:
    summary = report.get("summary")
    return summary if isinstance(summary, Mapping) else compute_summary(report)


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def report_projects(report: Mapping[str, Any]) -> List[str]:
    """The projects a report names, from its shipped and closed rows."""
    names = set()
    for change in report.get("shipped") or []:
        if isinstance(change, Mapping) and change.get("project"):
            names.add(str(change["project"]))
    for issue in report.get("closed_issues") or []:
        ref = issue.get("ref") if isinstance(issue, Mapping) else None
        if isinstance(ref, str) and "#" in ref and ref.split("#", 1)[0]:
            names.add(ref.split("#", 1)[0])
    return sorted(names)


def report_row(execution: Any) -> DeliveryReportRow:
    """The collection row for one Delivery Report run."""
    report = execution.result if isinstance(execution.result, Mapping) else {}
    summary = _summary(report)
    raw_window = report.get("window")
    window: Mapping[str, Any] = raw_window if isinstance(raw_window, Mapping) else {}
    raw_spend = report.get("spend")
    spend: Mapping[str, Any] = raw_spend if isinstance(raw_spend, Mapping) else {}
    cost = spend.get("cost_usd")
    flow = getattr(execution, "flow", None)
    return DeliveryReportRow(
        id=execution.id,
        flow_id=execution.flow_id,
        flow_name=getattr(flow, "name", None),
        status=str(execution.status),
        report_status=(
            str(report["status"]) if isinstance(report.get("status"), str) else None
        ),
        started_at=execution.start_time,
        ended_at=execution.end_time,
        window=DeliveryReportWindow.model_validate(
            {"from": window.get("from"), "to": window.get("to")}
        ),
        projects=report_projects(report),
        shipped=_int(summary.get("shipped")),
        closed=_int(summary.get("closed")),
        closed_unverified=_int(summary.get("closed_unverified")),
        proposals=_int(summary.get("follow_ups_proposed"))
        + _int(summary.get("reopen_requests")),
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
    )


def _stale_reason(
    db: Session, *, account_id: Any, report: Mapping[str, Any], ref: str
) -> Optional[str]:
    closed = next(
        (
            i
            for i in report.get("closed_issues") or []
            if isinstance(i, Mapping) and i.get("ref") == ref
        ),
        None,
    )
    if closed is None:
        return None
    assessed_at = _parse_time(closed.get("updated_at"))
    url = closed.get("url")
    if assessed_at is not None and isinstance(url, str) and url:
        issue = crud_issue.get_by_external_url(
            db, external_url=url, account_id=str(account_id)
        )
        current = _parse_time(getattr(issue, "updated_at", None))
        if current is not None and current > assessed_at:
            return "The issue was edited after this assessment."
    assessed_head = closed.get("closing_head_sha")
    if not assessed_head:
        return None
    shipped = {
        c.get("ref"): c for c in report.get("shipped") or [] if isinstance(c, Mapping)
    }
    for pr_ref in closed.get("closed_by") or []:
        change = shipped.get(pr_ref) or {}
        # The assessed head belongs to one closing PR; another PR linked to
        # the same issue has its own head and is not compared against it.
        if change.get("head_sha") != assessed_head:
            continue
        key = normalize_pr_url(change.get("url"))
        if not key:
            continue
        pull = crud_issue_cost.get_pull_request(db, account_id=account_id, pr_key=key)
        head = getattr(pull, "head_sha", None)
        if head and head != assessed_head:
            return f"New commits landed on {pr_ref} after the assessed revision."
    return None


def stale_reopen_requests(
    db: Session, *, account_id: Any, report: Mapping[str, Any]
) -> Dict[str, str]:
    """Reopen requests that no longer apply, by issue ref, with the reason.

    A request goes stale when the issue was edited after the assessment or
    new commits landed on its closing pull request. A lookup that fails
    leaves the request as it was rather than failing the page.
    """
    stale: Dict[str, str] = {}
    for request in report.get("reopen_requests") or []:
        ref = request.get("ref") if isinstance(request, Mapping) else None
        if not isinstance(ref, str) or not ref:
            continue
        try:
            reason = _stale_reason(db, account_id=account_id, report=report, ref=ref)
        except Exception:  # noqa: BLE001 - an unknown state is shown as current
            logger.warning("Could not check reopen request %s", ref, exc_info=True)
            continue
        if reason:
            stale[ref] = reason
    return stale


def report_detail(
    db: Session, *, account_id: Any, execution: Any
) -> DeliveryReportDetail:
    """The full report of one run, with stale reopen requests marked."""
    row = report_row(execution)
    report = dict(execution.result) if isinstance(execution.result, Mapping) else {}
    return DeliveryReportDetail(
        **row.model_dump(by_alias=True),
        report=report,
        stale_reopen_requests=stale_reopen_requests(
            db, account_id=account_id, report=report
        ),
    )
