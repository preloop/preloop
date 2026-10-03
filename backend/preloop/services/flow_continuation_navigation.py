"""Shared issue, PR and execution links for a publication and its repairs."""

from typing import Any
from urllib.parse import urlsplit
import uuid

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_flow_execution


def _web_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _mapping(value: Any) -> dict[str, Any]:
    """Ignore malformed user-supplied trigger fields."""
    return value if isinstance(value, dict) else {}


def project_continuation_navigation(
    db: Session, execution: models.FlowExecution, *, account_id: uuid.UUID
) -> None:
    """Attach navigation to the detail row using only owned chain members."""
    # query_expression fields cannot be lazy-loaded on lightweight rows.
    resume = _mapping(
        _mapping(execution.__dict__.get("trigger_event_details")).get("_resume")
    )
    raw_root = execution.__dict__.get("resume_of") or resume.get("resume_root")
    try:
        root_id = uuid.UUID(str(raw_root or execution.id))
    except (TypeError, ValueError):
        root_id = uuid.UUID(str(execution.id))
    rows = crud_flow_execution.get_continuation_navigation(
        db, root_id=root_id, account_id=account_id
    )
    publisher = next((row for row in rows if row.id == root_id), None)
    # Never expose a foreign publisher referenced by malformed trigger JSON.
    if publisher is None:
        execution.resume_of = None
        execution.resume_totals = None
        return
    issue_url = None
    pr_url = None
    for row in [publisher, *rows]:
        details = _mapping(row.trigger_event_details)
        payload = _mapping(details.get("payload"))
        issue = payload.get("issue") or payload.get("object_attributes") or {}
        if isinstance(issue, dict):
            issue_url = issue_url or _web_url(
                issue.get("html_url") or issue.get("web_url") or issue.get("url")
            )
        result = _mapping(row.result)
        resume = _mapping(details.get("_resume"))
        feedback = _mapping(details.get("_feedback"))
        pr_url = pr_url or _web_url(
            result.get("pr_url") or resume.get("pr_url") or feedback.get("pr_url")
        )
    navigation = {
        "original_execution_id": root_id,
        "issue_url": issue_url,
        "pr_url": pr_url,
        "follow_ups_truncated": len(rows) > 101,
        "follow_ups": [
            {"id": row.id, "status": row.status, "start_time": row.start_time}
            for row in rows[:101]
            if row.id != root_id
        ],
    }
    execution.continuation_navigation = navigation
