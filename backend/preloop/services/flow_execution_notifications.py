"""Terminal-path notifications for flow executions.

Comments go through the tracker client (the same service MCP ``add_comment``
uses), never through the MCP HTTP endpoint. Attention items are the console
``flow`` kind already derived from failed executions; this module records
when a flow asked for one so tests and callers can assert the decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from preloop.models.models.flow_execution import TRIGGER_SUBJECT_KEY
from preloop.utils.secret_scrubbing import scrub_secret_lines, scrub_secrets

logger = logging.getLogger(__name__)

FAILURE_STATUSES = frozenset({"FAILED", "TIMEOUT", "TIMED_OUT"})
SUCCESS_STATUSES = frozenset({"SUCCEEDED", "SUCCESS"})
LOG_TAIL_LINES = 20


@dataclass(frozen=True)
class ParsedNotifications:
    """Resolved on/off flags for one terminal notification pass."""

    on_failure_comment: bool
    on_failure_attention: bool
    on_success_comment: bool


@dataclass
class NotificationOutcome:
    """What the terminal notifier did (or skipped)."""

    failure_comment_posted: bool = False
    success_comment_posted: bool = False
    attention_item_raised: bool = False
    skipped_reason: Optional[str] = None


def parse_notifications(raw: Any) -> Optional[ParsedNotifications]:
    """Return typed flags from a flow.notifications blob, or None if unset.

    Args:
        raw: ``flow.notifications`` (dict, pydantic model, or None).

    Returns:
        Parsed flags, or None when the flow has no notifications configured.
    """
    if raw is None:
        return None
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if not isinstance(raw, dict):
        return None
    if not raw:
        return None

    on_failure = raw.get("on_failure") or {}
    on_success = raw.get("on_success") or {}
    if not isinstance(on_failure, dict):
        on_failure = {}
    if not isinstance(on_success, dict):
        on_success = {}

    parsed = ParsedNotifications(
        on_failure_comment=bool(on_failure.get("comment_on_trigger_issue")),
        on_failure_attention=bool(on_failure.get("attention_item")),
        on_success_comment=bool(on_success.get("comment_on_trigger_issue")),
    )
    if not (
        parsed.on_failure_comment
        or parsed.on_failure_attention
        or parsed.on_success_comment
    ):
        return None
    return parsed


def is_failure_status(status: str, failure_category: Optional[str] = None) -> bool:
    """True for FAILED/TIMEOUT rows, including timeout-categorised FAILED."""
    normalized = (status or "").upper()
    if normalized in FAILURE_STATUSES:
        return True
    if normalized in SUCCESS_STATUSES or normalized in {"STOPPED", "CANCELLED"}:
        return False
    return (failure_category or "").lower() == "timeout"


def is_success_status(status: str) -> bool:
    """True for a successful terminal status."""
    return (status or "").upper() in SUCCESS_STATUSES


def needs_tracker_comment(
    notifications: Any,
    status: str,
    failure_category: Optional[str] = None,
) -> bool:
    """True when the terminal path should resolve a tracker client."""
    parsed = parse_notifications(notifications)
    if parsed is None:
        return False
    if parsed.on_failure_comment and is_failure_status(status, failure_category):
        return True
    if parsed.on_success_comment and is_success_status(status):
        return True
    return False


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
            ref = reference.strip()
            if ref.startswith("#"):
                return ref[1:]
            return ref

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


def extract_opened_pr_url(result: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return the PR/MR URL recorded on the execution result, if any."""
    if not isinstance(result, dict):
        return None
    for key in ("pr_url", "pull_request_url", "merge_request_url"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def tail_log_lines(lines: Sequence[str], *, tail: int = LOG_TAIL_LINES) -> List[str]:
    """Return the last ``tail`` lines with secrets redacted."""
    cleaned = [line if isinstance(line, str) else "" for line in lines]
    return scrub_secret_lines(cleaned[-tail:])


def format_failure_comment(
    *,
    status: str,
    execution_url: str,
    failure_category: Optional[str],
    log_lines: Sequence[str],
) -> str:
    """Build the single failure comment posted on the triggering issue.

    Args:
        status: Terminal execution status (FAILED, TIMEOUT, ...).
        execution_url: Console URL for the execution.
        failure_category: Closed-vocabulary category, or None.
        log_lines: Already-tailed, already-redacted log lines.

    Returns:
        Comment body. User-facing text uses ASCII punctuation only.
    """
    category = failure_category or "unknown"
    lines = list(log_lines)
    if lines:
        log_block = "\n".join(lines)
        logs_section = f"Last {len(lines)} log lines:\n```\n{log_block}\n```"
    else:
        logs_section = "No log lines were captured."

    display_status = (status or "FAILED").upper()
    return (
        f"Flow execution {display_status}\n"
        f"\n"
        f"Status: {display_status}\n"
        f"Execution: {execution_url}\n"
        f"Failure category: {category}\n"
        f"\n"
        f"{logs_section}"
    )


def format_success_comment(pr_url: str) -> str:
    """Build the short success comment posted when a PR was opened."""
    return f"PR opened: {pr_url}"


async def notify_terminal_execution(
    *,
    notifications: Any,
    status: str,
    failure_category: Optional[str],
    execution_id: str,
    execution_url: str,
    trigger_event_details: Optional[Dict[str, Any]],
    result: Optional[Dict[str, Any]],
    log_lines: Sequence[str],
    tracker_client: Any,
) -> NotificationOutcome:
    """Apply flow.notifications after a terminal status write.

    Args:
        notifications: Raw ``flow.notifications`` value.
        status: Terminal status written on the execution.
        failure_category: Derived category, if any.
        execution_id: Execution id (for logs).
        execution_url: Console URL embedded in comments.
        trigger_event_details: Execution trigger snapshot.
        result: Execution result (PR URL lives here).
        log_lines: In-memory or persisted agent log lines.
        tracker_client: Tracker client with ``add_comment``, or None.

    Returns:
        What was posted or raised. Never raises: tracker errors are logged.
    """
    parsed = parse_notifications(notifications)
    if parsed is None:
        return NotificationOutcome(skipped_reason="notifications_unset")

    outcome = NotificationOutcome()
    failed = is_failure_status(status, failure_category)
    succeeded = is_success_status(status)

    if failed and parsed.on_failure_attention:
        # Failed executions already surface as console attention items of
        # kind ``flow`` (see frontend/src/utils/attention.ts). Raising the
        # flag here is the backend half of that contract.
        outcome.attention_item_raised = True
        logger.info(
            "Raised console attention item for failed execution %s",
            execution_id,
        )

    if failed and parsed.on_failure_comment:
        posted = await _post_trigger_comment(
            tracker_client=tracker_client,
            trigger_event_details=trigger_event_details,
            body=format_failure_comment(
                status=status,
                execution_url=execution_url,
                failure_category=failure_category,
                log_lines=tail_log_lines(log_lines),
            ),
            execution_id=execution_id,
        )
        outcome.failure_comment_posted = posted

    if succeeded and parsed.on_success_comment:
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
