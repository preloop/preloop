"""Tracker actions taken from a Delivery Report (#2021).

The report proposes; a person or the account's policy decides. Two paths
write to the tracker:

* A click on Open issue or Reopen in the report page. The click is the
  approval, so it is carried out whatever the account's ``tracker_writes``
  rules say, and the audit row records ``allowed_by: "click"``.
* The report run itself, through the MCP tools, only where the account
  enabled it: ``create_follow_up: "allow"`` for new issues and
  ``reopen: "allow"`` for reopening. :func:`run_write_decision` is the
  rule the tool endpoints apply.

Closing is never done from a report, by either path. A reopen request is
pinned to the revision it was assessed at and cannot be applied once it went
stale (new commits on the closing pull request, or an edit to the issue
after the assessment). Every write, refusal and dismissal lands in the audit
trail, and the decision is stored on the run's result under
``proposal_actions`` so the page shows it and a second click is refused.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple

from sqlalchemy.orm import Session

from preloop.api.common import get_tracker_client
from preloop.models.crud import (
    crud_audit_log,
    crud_delivery_report_policy,
    crud_flow_execution,
    crud_issue,
    crud_project,
)
from preloop.schemas.tracker_models import IssueCreate, IssueUpdate
from preloop.services.delivery_report import (
    DeliveryReportPolicy,
    resolve_policy,
)
from preloop.services.delivery_report_views import (
    report_projects,
    stale_reopen_requests,
)

logger = logging.getLogger(__name__)

#: The audit action of every report write, refusal and dismissal.
AUDIT_ACTION = "delivery_report_tracker_write"

#: Tracker statuses that close an issue; a report never sets one.
CLOSING_STATUSES = frozenset({"closed", "close", "done", "resolved", "completed"})
#: Tracker statuses that reopen an issue.
REOPEN_STATUSES = frozenset({"open", "opened", "reopen", "reopened"})

ActionKind = Literal["open_issue", "reopen", "dismiss", "close"]


class ReportActionError(Exception):
    """A report action that was refused or failed.

    Attributes:
        code: Machine-readable reason (``close_denied``, ``stale``,
            ``already_decided``, ``not_found``, ``no_project``,
            ``tracker_error``).
        status_code: The HTTP status the endpoint answers with.
    """

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def report_link(execution_id: Any) -> str:
    """The console URL of one report run."""
    base = os.getenv("PRELOOP_URL", "https://preloop.ai").rstrip("/")
    return f"{base}/console/delivery-reports/{execution_id}"


def follow_up_key(index: int) -> str:
    """The ``proposal_actions`` key of the follow-up at ``index``."""
    return f"follow_up:{index}"


def reopen_key(ref: str) -> str:
    """The ``proposal_actions`` key of the reopen request for ``ref``."""
    return f"reopen:{ref}"


def account_policy(db: Session, *, account_id: Any) -> DeliveryReportPolicy:
    """The account layer of the report policy (tracker writes live there)."""
    return resolve_policy(
        account_overrides=crud_delivery_report_policy.get_overrides(
            db, account_id=account_id
        )
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strs(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if isinstance(v, str) and v.strip()]


def _audit(
    db: Session,
    *,
    account_id: Any,
    user_id: Any,
    status: str,
    resource_id: Optional[str],
    details: Dict[str, Any],
) -> None:
    crud_audit_log.log_action(
        db,
        account_id=account_id,
        user_id=user_id,
        action=AUDIT_ACTION,
        resource_type="issue",
        resource_id=resource_id,
        status=status,
        details=details,
    )


def follow_up_body(
    follow_up: Mapping[str, Any], *, execution_id: Any, label: str
) -> str:
    """The issue body of a follow-up: reason, evidence and the report link."""
    lines: List[str] = []
    reason = str(follow_up.get("reason") or "").strip()
    if reason:
        lines += [reason, ""]
    evidence = _strs(follow_up.get("evidence"))
    if evidence:
        lines += ["Evidence:", *[f"- {item}" for item in evidence], ""]
    ref = str(follow_up.get("ref") or "").strip()
    if ref:
        lines += [f"Follows up {ref}.", ""]
    lines.append(
        f"Proposed by the Delivery Report ({report_link(execution_id)}) "
        f"and labelled `{label}`."
    )
    return "\n".join(lines)


def reopen_comment(
    request: Mapping[str, Any], closed: Mapping[str, Any], *, execution_id: Any
) -> str:
    """The comment posted with a reopen: revision, unmet criteria, evidence."""
    criteria = (closed.get("purpose") or {}).get("criteria") or []
    unmet = [
        str(c.get("text"))
        for c in criteria
        if isinstance(c, Mapping) and c.get("text") and c.get("met") is not True
    ]
    lines = [
        "Reopened from the Delivery Report, assessed at revision "
        f"`{request.get('revision')}`.",
        "",
    ]
    if unmet:
        lines += ["Unmet criteria:", *[f"- {text}" for text in unmet], ""]
    reason = str(request.get("reason") or "").strip()
    if reason:
        lines += [f"Reason: {reason}", ""]
    evidence = _strs(request.get("evidence"))
    if evidence:
        lines += ["Evidence:", *[f"- {item}" for item in evidence], ""]
    lines.append(f"Report: {report_link(execution_id)}")
    return "\n".join(lines)


@dataclass
class ActionContext:
    """The run and the person acting on it."""

    db: Session
    account_id: Any
    user: Any
    execution: Any

    @property
    def report(self) -> Mapping[str, Any]:
        result = self.execution.result
        return result if isinstance(result, Mapping) else {}


def _claim(ctx: ActionContext, key: str, record: Dict[str, Any]) -> None:
    existing = crud_flow_execution.set_delivery_report_action(
        ctx.db,
        execution_id=ctx.execution.id,
        key=key,
        record={**record, "status": "pending"},
        claim=True,
    )
    if existing is not None:
        raise ReportActionError(
            "already_decided",
            f"This proposal was already {existing.get('status') or 'decided'}.",
            409,
        )


def _settle(ctx: ActionContext, key: str, record: Optional[Dict[str, Any]]) -> None:
    crud_flow_execution.set_delivery_report_action(
        ctx.db, execution_id=ctx.execution.id, key=key, record=record
    )
    ctx.db.refresh(ctx.execution)


def _project_for_follow_up(ctx: ActionContext, follow_up: Mapping[str, Any]) -> Any:
    names: List[str] = []
    if follow_up.get("project"):
        names.append(str(follow_up["project"]))
    ref = str(follow_up.get("ref") or "")
    if "#" in ref and ref.split("#", 1)[0]:
        names.append(ref.split("#", 1)[0])
    projects = report_projects(ctx.report)
    if len(projects) == 1:
        names.append(projects[0])
    for name in names:
        project = crud_project.get_by_slug_or_identifier(
            ctx.db, slug_or_identifier=name, account_id=str(ctx.account_id)
        )
        if project is not None:
            return project
    raise ReportActionError(
        "no_project",
        "The follow-up does not name a connected project to open the issue in.",
        422,
    )


async def open_follow_up(ctx: ActionContext, index: int) -> Dict[str, Any]:
    """Open the proposed follow-up at ``index`` as a labelled tracker issue.

    Args:
        ctx: The run and the person clicking.
        index: The follow-up's position in the report's ``follow_ups``.

    Returns:
        The stored decision, with the new issue's URL.

    Raises:
        ReportActionError: Unknown follow-up, already decided, no project, or
            the tracker refused.
    """
    follow_ups = ctx.report.get("follow_ups") or []
    if not (0 <= index < len(follow_ups)) or not isinstance(follow_ups[index], Mapping):
        raise ReportActionError("not_found", "No such follow-up.", 404)
    follow_up = follow_ups[index]
    if follow_up.get("issue_url"):
        raise ReportActionError(
            "already_decided", "The run already opened this follow-up.", 409
        )
    title = str(follow_up.get("title") or "").strip()
    if not title:
        raise ReportActionError("not_found", "The follow-up has no title.", 422)
    key = follow_up_key(index)
    project = _project_for_follow_up(ctx, follow_up)
    label = account_policy(
        ctx.db, account_id=ctx.account_id
    ).tracker_writes.follow_up_label
    base = {
        "kind": "open_issue",
        "user_id": str(ctx.user.id),
        "at": _now(),
        "allowed_by": "click",
    }
    _claim(ctx, key, base)
    details = {
        "execution_id": str(ctx.execution.id),
        "kind": "open_issue",
        "proposal": key,
        "allowed_by": "click",
        "project": project.identifier,
    }
    try:
        client = await get_tracker_client(
            project.organization_id, project.id, ctx.db, ctx.user
        )
        issue = await client.create_issue(
            project_key=project.identifier,
            issue_data=IssueCreate.model_validate(
                {
                    "title": title,
                    "description": follow_up_body(
                        follow_up, execution_id=ctx.execution.id, label=label
                    ),
                    "labels": [label],
                }
            ),
        )
    except Exception as exc:  # noqa: BLE001 - any tracker failure releases the claim
        logger.warning("Opening follow-up %s failed", key, exc_info=True)
        _settle(ctx, key, None)
        _audit(
            ctx.db,
            account_id=ctx.account_id,
            user_id=ctx.user.id,
            status="failure",
            resource_id=None,
            details={**details, "error": str(exc)[:500]},
        )
        raise ReportActionError(
            "tracker_error", f"The tracker refused the new issue: {exc}", 502
        ) from exc
    url = getattr(issue, "url", None)
    record = {**base, "status": "applied", "issue_url": url, "label": label}
    _settle(ctx, key, record)
    _audit(
        ctx.db,
        account_id=ctx.account_id,
        user_id=ctx.user.id,
        status="success",
        resource_id=url,
        details={**details, "issue_url": url, "label": label},
    )
    return record


def _reopen_request(
    ctx: ActionContext, ref: str
) -> Tuple[Mapping[str, Any], Mapping[str, Any]]:
    request = next(
        (
            r
            for r in ctx.report.get("reopen_requests") or []
            if isinstance(r, Mapping) and r.get("ref") == ref
        ),
        None,
    )
    if request is None:
        raise ReportActionError("not_found", "No reopen request for this issue.", 404)
    closed = next(
        (
            i
            for i in ctx.report.get("closed_issues") or []
            if isinstance(i, Mapping) and i.get("ref") == ref
        ),
        {},
    )
    return request, closed


async def reopen_issue(ctx: ActionContext, ref: str) -> Dict[str, Any]:
    """Reopen the issue a reopen request names and post the unmet criteria.

    Refused when the request went stale since the assessed revision: the
    issue must be assessed again first.

    Args:
        ctx: The run and the person clicking.
        ref: The issue ref the reopen request names.

    Returns:
        The stored decision.

    Raises:
        ReportActionError: Unknown request, stale, already decided, issue not
            synced, or the tracker refused.
    """
    request, closed = _reopen_request(ctx, ref)
    details: Dict[str, Any] = {
        "execution_id": str(ctx.execution.id),
        "kind": "reopen",
        "proposal": reopen_key(ref),
        "allowed_by": "click",
        "revision": request.get("revision"),
    }
    stale = stale_reopen_requests(ctx.db, account_id=ctx.account_id, report=ctx.report)
    if ref in stale:
        _audit(
            ctx.db,
            account_id=ctx.account_id,
            user_id=ctx.user.id,
            status="denied",
            resource_id=ref,
            details={**details, "reason": stale[ref]},
        )
        raise ReportActionError(
            "stale",
            f"{stale[ref]} Assess the issue again before reopening it.",
            409,
        )
    url = request.get("url") or closed.get("url")
    issue = (
        crud_issue.get_by_external_url(
            ctx.db, external_url=str(url), account_id=str(ctx.account_id)
        )
        if url
        else None
    )
    if issue is None:
        raise ReportActionError(
            "not_found", "The issue is not synced from its tracker.", 404
        )
    key = reopen_key(ref)
    base = {
        "kind": "reopen",
        "user_id": str(ctx.user.id),
        "at": _now(),
        "allowed_by": "click",
        "revision": request.get("revision"),
    }
    _claim(ctx, key, base)
    target = issue.key or issue.external_id
    try:
        client = await get_tracker_client(
            issue.project.organization_id, issue.project_id, ctx.db, ctx.user
        )
        await client.update_issue(
            target, IssueUpdate.model_validate({"status": "open"})
        )
        await client.add_comment(
            target, reopen_comment(request, closed, execution_id=ctx.execution.id)
        )
    except Exception as exc:  # noqa: BLE001 - any tracker failure releases the claim
        logger.warning("Reopening %s failed", ref, exc_info=True)
        _settle(ctx, key, None)
        _audit(
            ctx.db,
            account_id=ctx.account_id,
            user_id=ctx.user.id,
            status="failure",
            resource_id=ref,
            details={**details, "error": str(exc)[:500]},
        )
        raise ReportActionError(
            "tracker_error", f"The tracker refused the reopen: {exc}", 502
        ) from exc
    record = {**base, "status": "applied", "issue_url": url}
    _settle(ctx, key, record)
    _audit(
        ctx.db,
        account_id=ctx.account_id,
        user_id=ctx.user.id,
        status="success",
        resource_id=ref,
        details={**details, "issue_url": url},
    )
    return record


def dismiss(ctx: ActionContext, key: str, reason: str) -> Dict[str, Any]:
    """Dismiss a proposal with a reason; nothing is written to the tracker.

    Raises:
        ReportActionError: Unknown proposal or already decided.
    """
    kind, _, target = key.partition(":")
    if kind == "follow_up":
        follow_ups = ctx.report.get("follow_ups") or []
        if not (target.isdigit() and int(target) < len(follow_ups)):
            raise ReportActionError("not_found", "No such follow-up.", 404)
    elif kind == "reopen":
        _reopen_request(ctx, target)
    else:
        raise ReportActionError("not_found", "No such proposal.", 404)
    record = {
        "kind": "dismiss",
        "status": "dismissed",
        "user_id": str(ctx.user.id),
        "at": _now(),
        "reason": reason,
    }
    _claim(ctx, key, record)
    _settle(ctx, key, record)
    _audit(
        ctx.db,
        account_id=ctx.account_id,
        user_id=ctx.user.id,
        status="success",
        resource_id=target,
        details={
            "execution_id": str(ctx.execution.id),
            "kind": "dismiss",
            "proposal": key,
            "reason": reason,
        },
    )
    return record


def refuse_close(ctx: ActionContext, ref: Optional[str]) -> ReportActionError:
    """Log a close attempt from the report and return the refusal to raise."""
    _audit(
        ctx.db,
        account_id=ctx.account_id,
        user_id=ctx.user.id,
        status="denied",
        resource_id=ref,
        details={
            "execution_id": str(ctx.execution.id),
            "kind": "close",
            "denied_by": "tracker_writes.close",
        },
    )
    return ReportActionError(
        "close_denied", "A Delivery Report never closes an issue.", 403
    )


def run_write_decision(
    db: Session,
    *,
    account_id: Any,
    user_id: Any,
    execution_id: Any,
    tool: str,
    target: Optional[str],
    status: Optional[str] = None,
) -> Tuple[bool, str]:
    """Whether a report run may make this tracker write, logged either way.

    ``create_issue`` needs ``create_follow_up: "allow"``. ``update_issue``
    may only reopen, and needs ``reopen: "allow"``; a closing status is
    always refused. ``add_comment`` rides on the reopen rule, since the run
    only comments to explain a reopen. ``approval`` and ``deny`` both keep
    the write a proposal in the report.

    Args:
        db: Session.
        account_id: The run's account.
        user_id: The run's acting user, for the audit row.
        execution_id: The Delivery Report run.
        tool: ``create_issue``, ``update_issue`` or ``add_comment``.
        target: The issue or project the call names.
        status: The status ``update_issue`` sets, if any.

    Returns:
        ``(allowed, reason)``; the reason names the rule that decided.
    """
    writes = account_policy(db, account_id=account_id).tracker_writes
    normalized = (status or "").strip().lower()
    if tool == "create_issue":
        rule = writes.create_follow_up
        allowed, reason = rule == "allow", f"tracker_writes.create_follow_up={rule}"
    elif tool == "update_issue" and normalized in CLOSING_STATUSES:
        allowed, reason = False, "tracker_writes.close=deny"
    elif tool == "update_issue" and normalized in REOPEN_STATUSES:
        allowed, reason = (
            writes.reopen == "allow",
            f"tracker_writes.reopen={writes.reopen}",
        )
    elif tool == "add_comment":
        allowed, reason = (
            writes.reopen == "allow",
            f"tracker_writes.reopen={writes.reopen}",
        )
    else:
        allowed, reason = False, "a report run may only open follow-ups and reopen"
    _audit(
        db,
        account_id=account_id,
        user_id=user_id,
        status="success" if allowed else "denied",
        resource_id=target,
        details={
            "execution_id": str(execution_id),
            "kind": tool,
            "status": status,
            "allowed_by" if allowed else "denied_by": reason,
        },
    )
    return allowed, reason
