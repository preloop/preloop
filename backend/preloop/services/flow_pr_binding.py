"""Bind a flow execution to the pull request it opened.

Issue-implementation flows persist the opened PR URL on
``FlowExecution.result`` so a later ``comment_created`` on that PR can start
a new execution of the same flow with ``_resume`` metadata. Native CLI
session resume (``--resume``) is a separate follow-up.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from preloop.models.crud import crud_flow_execution
from preloop.models.models.flow_execution import FlowExecution

logger = logging.getLogger(__name__)

_RESUME_LOOKBACK = 50


def normalize_pr_url(url: Optional[str]) -> str:
    """Canonicalize a GitHub PR or GitLab MR HTML URL for matching."""

    if not url or not isinstance(url, str):
        return ""
    parsed = urlparse(url.strip())
    if not parsed.netloc or not parsed.path:
        return ""
    path = parsed.path.rstrip("/")
    host = parsed.netloc.lower()
    if host.endswith("github.com"):
        parts = path.split("/")
        # GitHub issue comments on PRs often use /issues/N in html_url.
        if len(parts) >= 5 and parts[3] == "issues":
            parts[3] = "pull"
            path = "/".join(parts)
    scheme = parsed.scheme or "https"
    return f"{scheme}://{host}{path}"


def extract_pr_url_from_comment_event(event_data: Dict[str, Any]) -> Optional[str]:
    """Return the PR/MR HTML URL for a comment event, or None for issue comments."""

    payload = event_data.get("payload") or event_data
    if not isinstance(payload, dict):
        return None

    issue = payload.get("issue")
    if isinstance(issue, dict):
        pr_stub = issue.get("pull_request")
        if isinstance(pr_stub, dict):
            url = pr_stub.get("html_url") or issue.get("html_url") or pr_stub.get("url")
            normalized = normalize_pr_url(url)
            return normalized or None

    pr = payload.get("pull_request")
    if isinstance(pr, dict):
        normalized = normalize_pr_url(pr.get("html_url") or pr.get("url"))
        if normalized:
            return normalized

    mr = payload.get("merge_request")
    if isinstance(mr, dict):
        normalized = normalize_pr_url(mr.get("url") or mr.get("web_url"))
        if normalized:
            return normalized

    return None


def merge_result_preserving_pr_binding(
    existing: Optional[Any], incoming: Optional[Any]
) -> Optional[Any]:
    """Keep ``pr_url`` / ``pr_source_branch`` when the agent result overwrites."""

    if incoming is None:
        return existing
    if not isinstance(incoming, dict):
        return incoming
    if not isinstance(existing, dict):
        return incoming
    merged = dict(existing)
    merged.update(incoming)
    for key in ("pr_url", "pr_source_branch"):
        if not merged.get(key) and existing.get(key):
            merged[key] = existing[key]
    return merged


def record_opened_pr(
    db: Session,
    execution_id: Any,
    pr_url: str,
    source_branch: Optional[str] = None,
) -> None:
    """Merge the opened PR URL onto the execution result. Never raises."""

    try:
        if not execution_id or not pr_url:
            return
        execution = crud_flow_execution.get(db, id=execution_id)
        if execution is None:
            logger.warning(
                "Cannot record opened PR: execution %s not found", execution_id
            )
            return
        current: Dict[str, Any]
        if isinstance(execution.result, dict):
            current = dict(execution.result)
        else:
            current = {}
        current["pr_url"] = pr_url
        if source_branch:
            current["pr_source_branch"] = source_branch
        execution.result = current
        flag_modified(execution, "result")
        db.commit()
        logger.info(
            "Recorded opened PR %s (branch=%s) on execution %s",
            pr_url,
            source_branch,
            execution_id,
        )
    except Exception:
        logger.warning(
            "Failed to record opened PR on execution %s",
            execution_id,
            exc_info=True,
        )
        try:
            db.rollback()
        except Exception:
            pass


def find_bound_execution(
    db: Session, flow_id: Any, pr_url: str
) -> Optional[FlowExecution]:
    """Return the most recent execution of this flow that opened ``pr_url``."""

    needle = normalize_pr_url(pr_url)
    if not needle:
        return None
    executions = crud_flow_execution.get_by_flow(
        db, flow_id=flow_id, skip=0, limit=_RESUME_LOOKBACK
    )
    for execution in executions:
        result = execution.result if isinstance(execution.result, dict) else {}
        stored = normalize_pr_url(result.get("pr_url"))
        if stored and stored == needle:
            return execution
    return None


def flow_requires_pr_comment_resume(flow: Any) -> bool:
    """True when comment_created on this flow must correlate to an opened PR.

    Flows that listen to both issue labeling and comments would otherwise start
    a cold agent on every issue comment. Comment-only flows are unchanged.
    """

    types = getattr(flow, "trigger_event_types", None)
    if not isinstance(types, (list, tuple, set)):
        return False
    type_set = set(types)
    if "comment_created" not in type_set:
        return False
    return bool(type_set & {"issue_labeled", "issue_opened"})


def bind_resume_or_skip(
    db: Session, flow: Any, event_data: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Attach ``_resume`` when this comment belongs to a PR this flow opened.

    Returns the resume dict, or None when the event should not start a run.
    """

    pr_url = extract_pr_url_from_comment_event(event_data)
    if not pr_url:
        return None
    execution = find_bound_execution(db, flow.id, pr_url)
    if execution is None:
        return None
    result = execution.result if isinstance(execution.result, dict) else {}
    resume = {
        "execution_id": str(execution.id),
        "pr_url": result.get("pr_url") or pr_url,
        "source_branch": result.get("pr_source_branch"),
    }
    event_data["_resume"] = resume
    return resume
