"""Read stored Delivery Report runs for the console.

The report is the run's ``result`` document (see
:mod:`preloop.services.delivery_report`). These helpers turn a run into a
collection row, and mark reopen requests that went stale after the
assessment, reading current issue and pull request state through the CRUD
layer.
"""

from __future__ import annotations

import logging
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_issue
from preloop.models.crud.assessment import crud_assessment
from preloop.models.crud.issue_cost import crud_issue_cost
from preloop.schemas.delivery_report import (
    DeliveryReportDetail,
    DeliveryReportAttention,
    DeliveryReportPendingProposal,
    DeliveryReportRow,
    DeliveryReportWindow,
    ExecutionPullRequest,
)
from preloop.services.delivery_report import compute_summary
from preloop.services.delivery_report_proposals import needs_confirmation
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


def report_work_projects(report: Mapping[str, Any]) -> List[str]:
    """Work scope used as the fallback target for a follow-up tracker write."""
    names = set()
    for change in report.get("shipped") or []:
        if isinstance(change, Mapping) and change.get("project"):
            names.add(str(change["project"]))
    for item in report.get("closed_issues") or []:
        ref = item.get("ref") if isinstance(item, Mapping) else None
        if isinstance(ref, str) and "#" in ref and ref.split("#", 1)[0]:
            names.add(ref.split("#", 1)[0])
    return sorted(names)


def report_projects(report: Mapping[str, Any]) -> List[str]:
    """All named projects for display and reruns, including open work."""
    names = set(report_work_projects(report))
    for section in (
        "closed_issues",
        "open_past_window",
        "reopen_requests",
        "follow_ups",
    ):
        for item in report.get(section) or []:
            ref = item.get("ref") if isinstance(item, Mapping) else None
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
    # Project abandoned claims as unconfirmed without changing the persisted
    # claim: the action service still enforces its no-duplicate-write guard.
    report["proposal_actions"] = {
        key: {**decision, "status": "unconfirmed"}
        if needs_confirmation(decision)
        else decision
        for key, decision in (report.get("proposal_actions") or {}).items()
    }
    return DeliveryReportDetail(
        **row.model_dump(by_alias=True),
        report=report,
        stale_reopen_requests=stale_reopen_requests(
            db, account_id=account_id, report=report
        ),
        rerun_projects=rerun_projects(db, account_id=account_id, report=report),
    )


def rerun_projects(
    db: Session, *, account_id: Any, report: Mapping[str, Any]
) -> List[str]:
    """Connected projects in this report that the current policy permits."""
    from preloop.services.delivery_report import report_project_errors

    projects = report_projects(report)
    errors = report_project_errors(db, account_id=account_id, projects=projects)
    return [project for project in projects if project not in errors]


def report_attention(
    db: Session, *, account_id: Any, execution: models.FlowExecution
) -> Optional[DeliveryReportAttention]:
    """List undecided and unconfirmed proposals; never alert on verdicts."""
    report = execution.result if isinstance(execution.result, dict) else {}
    decisions = report.get("proposal_actions") or {}
    proposals = []
    for kind, entries in (
        ("reopen", report.get("reopen_requests") or []),
        ("follow_up", report.get("follow_ups") or []),
    ):
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            ref = entry.get("ref")
            if kind == "reopen" and (not ref or entry.get("applied")):
                continue
            if kind == "follow_up" and entry.get("issue_url"):
                continue
            key = f"reopen:{ref}" if kind == "reopen" else f"follow_up:{index}"
            decision = decisions.get(key) or {}
            unconfirmed = needs_confirmation(decision)
            if decision.get("status") in {"applied", "dismissed"} or (
                decision.get("status") == "pending" and not unconfirmed
            ):
                continue
            proposals.append(
                DeliveryReportPendingProposal(
                    key=key,
                    kind=kind,
                    title=(
                        f"Reopen {ref}: {entry.get('title') or ref}"
                        if kind == "reopen"
                        else entry.get("title") or "Proposed follow-up"
                    ),
                    reason=entry.get("reason"),
                    unconfirmed=unconfirmed,
                )
            )
    if not proposals:
        return None
    pending_refs = {
        p.key.removeprefix("reopen:") for p in proposals if p.kind == "reopen"
    }
    if pending_refs:
        stale = stale_reopen_requests(
            db,
            account_id=account_id,
            report={
                **report,
                "reopen_requests": [
                    r
                    for r in report.get("reopen_requests") or []
                    if isinstance(r, dict) and r.get("ref") in pending_refs
                ],
            },
        )
        for proposal in proposals:
            if proposal.kind == "reopen":
                proposal.stale_reason = stale.get(proposal.key.removeprefix("reopen:"))
    return DeliveryReportAttention(
        id=execution.id,
        started_at=execution.start_time,
        projects=report_projects(report),
        proposals=proposals,
        fingerprint=hashlib.sha256(
            json.dumps([p.model_dump() for p in proposals], sort_keys=True).encode()
        ).hexdigest(),
    )


def execution_pull_request(
    db: Session, *, account_id: Any, execution: models.FlowExecution
) -> Optional[ExecutionPullRequest]:
    """Read the produced PR's state and assessment for its current revision.

    Unknown heads never borrow an assessment from an unrelated revision.
    The execution result is only a binding; forge facts come from the ledger.
    """
    result = execution.result if isinstance(execution.result, dict) else {}
    url = normalize_pr_url(result.get("pr_url"))
    if not url:
        return None
    pull = crud_issue_cost.get_pull_request(db, account_id=account_id, pr_key=url)
    if pull is None:
        return ExecutionPullRequest(url=url)
    assessment = (
        crud_assessment.newest(
            db,
            account_id=account_id,
            issue_cost_pull_request_id=pull.id,
            revision=pull.head_sha,
        )
        if pull.head_sha
        else None
    )
    return ExecutionPullRequest(
        url=url,
        state=pull.state,
        revision=pull.head_sha,
        purpose_verdict=assessment.purpose_verdict if assessment else None,
    )
