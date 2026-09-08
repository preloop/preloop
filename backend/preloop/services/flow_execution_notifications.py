"""Terminal-path notifications for flow executions.

One notification survives: a short "PR opened: <url>" comment on the
triggering issue after a successful run that recorded a pull request URL.
Comments go through the tracker client (the same service MCP ``add_comment``
uses), never through the MCP HTTP endpoint.

The failure comment (``notifications.on_failure.comment_on_trigger_issue``)
was removed in 2026-09: a failed run already surfaces as a console attention
item of kind ``flow``, and a redacted log tail pasted onto someone's issue was
noise on the tracker rather than a notification. Stored flows may still carry
the key; it is parsed and ignored, like ``on_failure.attention_item``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from preloop.models.models.flow_execution import TRIGGER_SUBJECT_KEY
from preloop.utils.secret_scrubbing import scrub_secrets

logger = logging.getLogger(__name__)

SUCCESS_STATUSES = frozenset({"SUCCEEDED", "SUCCESS"})

# Issue / PR / MR identifiers the tracker comment APIs accept. Branch
# names, tags, and SHAs show up on ``_subject.reference`` for some events
# and must not become comment targets. GitLab MR refs are ``!123``.
_ISSUE_LIKE_REFERENCE = re.compile(r"^(?:\d+|[A-Za-z][A-Za-z0-9]+-\d+)$")


@dataclass(frozen=True)
class ParsedNotifications:
    """Resolved on/off flags for one terminal notification pass."""

    on_success_comment: bool


@dataclass
class NotificationOutcome:
    """What the terminal notifier did (or skipped)."""

    success_comment_posted: bool = False
    skipped_reason: Optional[str] = None


def parse_notifications(raw: Any) -> Optional[ParsedNotifications]:
    """Return typed flags from a flow.notifications blob, or None if unset.

    Args:
        raw: ``flow.notifications`` (dict, pydantic model, or None).

    Keys under ``on_failure`` are read and dropped: the failure comment was
    removed and the attention item was never optional.

    Returns:
        Parsed flags, or None when the flow asks for no comment.
    """
    if raw is None:
        return None
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if not isinstance(raw, dict):
        return None
    if not raw:
        return None

    on_success = raw.get("on_success") or {}
    if not isinstance(on_success, dict):
        on_success = {}

    if not bool(on_success.get("comment_on_trigger_issue")):
        return None
    return ParsedNotifications(on_success_comment=True)


def is_success_status(status: str) -> bool:
    """True for a successful terminal status."""
    return (status or "").upper() in SUCCESS_STATUSES


def needs_tracker_comment(notifications: Any, status: str) -> bool:
    """True when the terminal path should resolve a tracker client."""
    parsed = parse_notifications(notifications)
    if parsed is None:
        return False
    return parsed.on_success_comment and is_success_status(status)


def extract_trigger_comment_target(
    trigger_event_details: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Return the tracker issue/PR identifier to comment on.

    Prefers the denormalized ``_subject.reference`` written at execution
    create time, then falls back to common GitHub/GitLab/Jira payload keys.

    Args:
        trigger_event_details: Snapshot stored on the execution.

    Returns:
        Issue number, Jira key, or None when no triggering issue exists.
    """
    if not isinstance(trigger_event_details, dict):
        return None

    subject = trigger_event_details.get(TRIGGER_SUBJECT_KEY) or {}
    if isinstance(subject, dict):
        reference = subject.get("reference")
        if isinstance(reference, str) and reference.strip():
            canonical = _canonical_issue_reference(reference)
            if canonical:
                return canonical

    payload = trigger_event_details.get("payload")
    if not isinstance(payload, dict):
        payload = trigger_event_details

    for key in ("issue", "pull_request", "merge_request"):
        obj = payload.get(key)
        if not isinstance(obj, dict):
            continue
        if obj.get("number") is not None:
            return str(obj["number"])
        if obj.get("iid") is not None:
            return str(obj["iid"])
        if obj.get("key"):
            return str(obj["key"])

    obj_attrs = payload.get("object_attributes")
    if isinstance(obj_attrs, dict):
        if obj_attrs.get("iid") is not None:
            return str(obj_attrs["iid"])
        if obj_attrs.get("id") is not None and payload.get("object_kind") in (
            "issue",
            "merge_request",
        ):
            return str(obj_attrs["iid"] or obj_attrs["id"])

    return None


def _canonical_issue_reference(raw: str) -> Optional[str]:
    """Strip ``#`` / ``!`` and keep the rest only if it looks like an issue."""

    stripped = raw.strip()
    if stripped[:1] in {"#", "!"}:
        stripped = stripped[1:].strip()
    if _ISSUE_LIKE_REFERENCE.fullmatch(stripped):
        return stripped
    return None


def extract_opened_pr_url(result: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return the PR/MR URL recorded on the execution result, if any."""
    if not isinstance(result, dict):
        return None
    for key in ("pr_url", "pull_request_url", "merge_request_url"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def format_success_comment(pr_url: str) -> str:
    """Build the short success comment posted when a PR was opened."""
    return f"PR opened: {pr_url}"


async def notify_terminal_execution(
    *,
    notifications: Any,
    status: str,
    execution_id: str,
    trigger_event_details: Optional[Dict[str, Any]],
    result: Optional[Dict[str, Any]],
    tracker_client: Any,
) -> NotificationOutcome:
    """Apply flow.notifications after a terminal status write.

    Args:
        notifications: Raw ``flow.notifications`` value.
        status: Terminal status written on the execution.
        execution_id: Execution id (for logs).
        trigger_event_details: Execution trigger snapshot.
        result: Execution result (PR URL lives here).
        tracker_client: Tracker client with ``add_comment``, or None.

    Returns:
        What was posted or skipped. Never raises: tracker errors are logged.
    """
    parsed = parse_notifications(notifications)
    if parsed is None:
        return NotificationOutcome(skipped_reason="notifications_unset")

    outcome = NotificationOutcome()

    if is_success_status(status) and parsed.on_success_comment:
        pr_url = extract_opened_pr_url(result)
        if not pr_url:
            logger.info(
                "Success comment skipped for execution %s: no PR URL on result",
                execution_id,
            )
        else:
            posted = await _post_trigger_comment(
                tracker_client=tracker_client,
                trigger_event_details=trigger_event_details,
                body=format_success_comment(pr_url),
                execution_id=execution_id,
            )
            outcome.success_comment_posted = posted

    return outcome


async def _post_trigger_comment(
    *,
    tracker_client: Any,
    trigger_event_details: Optional[Dict[str, Any]],
    body: str,
    execution_id: str,
) -> bool:
    """Post ``body`` on the triggering issue via the tracker client."""
    if tracker_client is None:
        logger.warning(
            "Cannot comment on trigger issue for execution %s: no tracker client",
            execution_id,
        )
        return False

    target = extract_trigger_comment_target(trigger_event_details)
    if not target:
        logger.info(
            "Cannot comment on trigger issue for execution %s: no issue on trigger",
            execution_id,
        )
        return False

    comment = scrub_secrets(body) or body
    try:
        await tracker_client.add_comment(target, comment)
    except Exception:
        logger.warning(
            "Failed to comment on trigger issue %s for execution %s",
            target,
            execution_id,
            exc_info=True,
        )
        return False

    logger.info(
        "Posted terminal notification comment on %s for execution %s",
        target,
        execution_id,
    )
    return True
