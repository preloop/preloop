"""Alert a flow's owners after N consecutive failed executions (#1421).

Unattended loops (a scheduled SLA check, an incident investigator, a nightly
memory-curation job) can fail on every run with nobody noticing. This module
counts the leading run of failures among a flow's terminal executions and
emails / pushes the account's owners once per streak.

The streak is read from the executions table, not process memory: the
orchestrator may finish a run on any replica. Cross-replica "already alerted"
dedup is therefore an ``audit_log`` row keyed by the flow, mirroring
``unpriced_model_alert``. The read of that marker and the claim are serialized
per flow with a transaction-scoped advisory lock, so two concurrent terminal
runs cannot both claim and both deliver. The marker is claimed before delivery
so a delivery failure cannot turn into a retry storm, and it is anchored to the
newest failure at alert time; while that execution is still part of the streak,
no second alert is sent.

Semantics pinned here:

* a ``SUCCEEDED`` run with no ``model_output_summary`` and an empty ``result``
  counts as a failure (a run that produced nothing is not a success);
* ``STOPPED`` / ``CANCELLED`` are operator actions: neutral, so they neither
  break nor extend a streak;
* the alert names the flow, the last N execution ids and the last error, and
  is best-effort: no failure here may change an execution's terminal status;
* the streak walks completion time, not start time, so a success that
  started late and finished early does not hide failures that finished after it.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from preloop.models.crud import (
    crud_audit_log,
    crud_flow_execution,
    notification_preferences,
)
from preloop.services.flow_execution_notifications import SUCCESS_STATUSES
from preloop.utils.secret_scrubbing import scrub_secrets

logger = logging.getLogger(__name__)

#: Audit action used as the persisted, cross-replica dedup marker.
FLOW_FAILURE_ALERT_ACTION = "flow_failure_streak_alert"

#: ``resource_type`` on the marker row. ``resource_id`` is the flow id.
FLOW_FAILURE_ALERT_RESOURCE_TYPE = "flow"

#: Threshold applied when the flow did not configure one.
DEFAULT_FAILURE_ALERT_THRESHOLD = 3

#: Hard ceiling for stored values that bypass schema validation.
MAX_FAILURE_ALERT_THRESHOLD = 100

#: Newest execution ids embedded in a push ``data`` payload. The email still
#: names up to ``threshold`` ids; push providers cap a payload near 4 KB, and
#: ``threshold`` is allowed up to 100 (~3.9 KB of UUIDs alone), so the push
#: list is bounded independently.
PUSH_EXECUTION_IDS_LIMIT = 5

#: How many recent executions to inspect when walking the streak.
STREAK_SCAN_LIMIT = 200

#: Terminal statuses that mean the run failed.
FAILURE_STATUSES = frozenset({"FAILED", "TIMEOUT", "TIMED_OUT", "ABORTED", "ERROR"})

#: Operator-ended runs: neutral for streak counting.
NEUTRAL_STATUSES = frozenset({"STOPPED", "CANCELLED", "CANCELED"})

CLASSIFY_FAILURE = "failure"
CLASSIFY_SUCCESS = "success"
CLASSIFY_NEUTRAL = "neutral"


@dataclass(frozen=True)
class FailureStreak:
    """The leading run of failures on a flow's terminal executions."""

    count: int = 0
    #: Failure execution ids, newest first.
    execution_ids: List[str] = field(default_factory=list)
    last_error: Optional[str] = None
    #: Oldest failure found in the scanned window.
    streak_start_execution_id: Optional[str] = None
    #: True when an earlier alert's anchor execution is inside this streak, so
    #: this streak already produced its one alert.
    already_alerted: bool = False


@dataclass
class FailureAlertOutcome:
    """What one terminal-path evaluation did (or skipped)."""

    alerted: bool = False
    skipped_reason: Optional[str] = None
    recipients: int = 0
    emails_sent: int = 0
    pushes_sent: int = 0


def execution_has_output(execution: Any) -> bool:
    """True when a terminal execution recorded any result artifact.

    The orchestrator turns an exit-0 run with no confirmation into ``FAILED``
    before it reaches the streak counter, so a ``SUCCEEDED`` row normally has
    output. This check pins the acceptance clause for the edge case: a
    ``SUCCEEDED`` row with neither a summary nor a non-empty result counts as
    a failure, not a success. Exit codes are deliberately not consulted.
    """
    summary = getattr(execution, "model_output_summary", None)
    if isinstance(summary, str) and summary.strip():
        return True
    result = getattr(execution, "result", None)
    if isinstance(result, dict) and result:
        return True
    return False


def classify_execution(execution: Any) -> str:
    """Classify one execution for streak counting.

    Args:
        execution: A ``FlowExecution`` row (or a stand-in with ``status``,
            ``model_output_summary`` and ``result`` attributes).

    Returns:
        :data:`CLASSIFY_FAILURE`, :data:`CLASSIFY_SUCCESS` or
        :data:`CLASSIFY_NEUTRAL`.
    """
    status = str(getattr(execution, "status", "") or "").strip().upper()
    if status in FAILURE_STATUSES:
        return CLASSIFY_FAILURE
    if status in NEUTRAL_STATUSES:
        return CLASSIFY_NEUTRAL
    if status in SUCCESS_STATUSES:
        return CLASSIFY_SUCCESS if execution_has_output(execution) else CLASSIFY_FAILURE
    # A non-terminal row is not part of a finished streak. Treat it as neutral
    # so a scan that somehow includes one does not invent a failure.
    return CLASSIFY_NEUTRAL


def failure_alert_threshold(notifications: Any) -> int:
    """Resolve the per-flow consecutive-failure alert threshold.

    Args:
        notifications: ``flow.notifications`` as a dict or pydantic model.

    Returns:
        The configured value, or :data:`DEFAULT_FAILURE_ALERT_THRESHOLD` when
        unset / invalid. Out-of-range stored values are clamped.
    """
    raw = _raw_failure_value(notifications, "alert_after_consecutive_failures")
    if raw is None or isinstance(raw, bool):
        return DEFAULT_FAILURE_ALERT_THRESHOLD
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_FAILURE_ALERT_THRESHOLD
    if value < 1:
        return DEFAULT_FAILURE_ALERT_THRESHOLD
    return min(value, MAX_FAILURE_ALERT_THRESHOLD)


def _raw_failure_value(notifications: Any, key: str) -> Any:
    """Read ``on_failure.<key>`` from a dict, pydantic model, or None."""
    if notifications is None:
        return None
    if hasattr(notifications, "model_dump"):
        notifications = notifications.model_dump()
    if not isinstance(notifications, dict):
        return None
    on_failure = notifications.get("on_failure")
    if not isinstance(on_failure, dict):
        return None
    return on_failure.get(key)


def compute_failure_streak(
    executions: Sequence[Any],
    *,
    anchor: Optional[str] = None,
    scan_limit: int = STREAK_SCAN_LIMIT,
) -> FailureStreak:
    """Walk the leading run of failures in ``executions`` (newest first).

    The walk stops at the first success, since a success resets the streak.
    Operator-ended runs are skipped without breaking the run. When ``anchor``
    is given (the newest failure at the previous alert), the walk also stops
    once it reaches that execution: the anchor being inside the run means this
    is the same streak and no second alert is due. A scan that exhausts
    ``scan_limit`` without finding a success or the anchor is treated as the
    same streak, so a very long streak cannot re-alert.

    Args:
        executions: Terminal executions ordered newest first.
        anchor: Execution id at which this flow last alerted, if any.
        scan_limit: Maximum number of rows to inspect.

    Returns:
        The streak count, the failure ids (newest first), the most recent
        error, and whether the streak already alerted.
    """
    all_rows = list(executions)
    rows = all_rows[:scan_limit]
    window_exhausted = len(all_rows) >= scan_limit

    failed: List[Any] = []
    reached_reset = False
    found_anchor = False
    for row in rows:
        kind = classify_execution(row)
        if kind == CLASSIFY_SUCCESS:
            reached_reset = True
            break
        if kind == CLASSIFY_NEUTRAL:
            continue
        failed.append(row)
        if anchor is not None and str(getattr(row, "id", "")) == str(anchor):
            found_anchor = True
            break

    already_alerted = found_anchor or (
        anchor is not None
        and not reached_reset
        and not found_anchor
        and window_exhausted
    )
    execution_ids = [str(getattr(row, "id", "")) for row in failed]
    last_error = getattr(failed[0], "error_message", None) if failed else None
    return FailureStreak(
        count=len(failed),
        execution_ids=execution_ids,
        last_error=last_error,
        streak_start_execution_id=execution_ids[-1] if execution_ids else None,
        already_alerted=already_alerted,
    )


def build_failure_alert_message(
    *,
    flow_name: str,
    streak: FailureStreak,
    threshold: int,
) -> Dict[str, str]:
    """Subject, plain text and HTML of the consecutive-failure alert."""
    safe_name = scrub_secrets(flow_name) or flow_name
    error = scrub_secrets(streak.last_error) if streak.last_error else None
    error_text = error or "(no error message was recorded)"
    ids = streak.execution_ids[: max(threshold, 1)] or ["(none recorded)"]
    headline = f"Flow '{safe_name}' failed {streak.count} times in a row"
    id_lines = "\n".join(f"  - {execution_id}" for execution_id in ids)
    lines = [
        f"{headline}.",
        "",
        f"The last {len(ids)} execution ids:",
        id_lines,
        "",
        "Last error:",
        error_text,
        "",
        "This alert is sent once per failure streak and clears after a successful run.",
    ]
    body_html = (
        f"<p>{html.escape(headline)}.</p>"
        f"<p>The last {len(ids)} execution ids:</p>"
        f"<ul>{''.join(f'<li>{html.escape(execution_id)}</li>' for execution_id in ids)}</ul>"
        "<p>Last error:</p>"
        f"<pre>{html.escape(error_text)}</pre>"
        "<p>This alert is sent once per failure streak and clears after a "
        "successful run.</p>"
    )
    return {
        "subject": headline,
        "headline": headline,
        "text": "\n".join(lines),
        "html": body_html,
    }


def _send_emails(db: Session, owners: List[Any], message: Dict[str, str]) -> int:
    """Email every owner whose preferences allow email. Returns the count."""
    from preloop.utils.email import send_email

    sent = 0
    for owner in owners:
        if not getattr(owner, "email", None):
            continue
        prefs = notification_preferences.get_by_user(db, owner.id)
        if prefs is not None and not prefs.enable_email:
            continue
        try:
            send_email(
                owner.email, message["subject"], message["text"], message["html"]
            )
            sent += 1
        except Exception:  # noqa: BLE001 - one bad address must not stop the rest
            logger.warning("Flow failure alert email failed", exc_info=True)
    return sent


def deliver_failure_alert(
    db: Session,
    *,
    flow: Any,
    streak: FailureStreak,
    threshold: int,
) -> Dict[str, int]:
    """Send one failure-streak alert on every enabled channel.

    Recipients are the account's owners, the same set policy notices use.
    Never raises: a channel failure is logged and leaves the terminal status
    untouched.

    Args:
        db: Database session.
        flow: The flow whose streak failed.
        streak: Computed streak.
        threshold: Resolved alert threshold (caps the named execution ids).

    Returns:
        ``recipients``, ``email`` and ``push`` counts, for logs and tests.
    """
    from preloop.services.policy_notice_delivery import (
        policy_owners,
        send_push_to_owners,
    )

    result = {"recipients": 0, "email": 0, "push": 0}
    message = build_failure_alert_message(
        flow_name=str(getattr(flow, "name", "flow")),
        streak=streak,
        threshold=threshold,
    )
    try:
        owners = policy_owners(db, getattr(flow, "account_id", None))
    except Exception:  # noqa: BLE001 - never let recipient lookup raise
        logger.warning("Flow failure alert recipient lookup failed", exc_info=True)
        return result
    result["recipients"] = len(owners)

    try:
        result["email"] = _send_emails(db, owners, message)
    except Exception:  # noqa: BLE001 - one channel must not stop the others
        logger.warning("Flow failure alert email channel failed", exc_info=True)
    try:
        result["push"] = send_push_to_owners(
            db,
            owners,
            title=message["subject"],
            body=message["text"],
            data={
                "type": "flow_failure_alert",
                "flow_id": str(getattr(flow, "id", "")),
                "execution_ids": streak.execution_ids[:PUSH_EXECUTION_IDS_LIMIT],
                "url": "/console/flows",
            },
            thread_id="flow-failures",
        )
    except Exception:  # noqa: BLE001 - never let a transport raise
        logger.warning("Flow failure alert push channel failed", exc_info=True)
    return result


def _lock_flow_failure_alerts(db: Session, *, flow_id: Any) -> None:
    """Serialize the read-then-claim sequence for one flow across replicas.

    The "already alerted" read (``_latest_alert_anchor``) and the claim
    (``_claim_alert``) are a check-then-act: without serialization two terminal
    runs finishing close together can both read ``already_alerted == False``,
    both claim, and both deliver duplicate owner notifications. A
    transaction-scoped advisory lock on the flow closes that window: the claim
    commits, which releases the lock, so the next replica's read sees the
    marker. Postgres only; other dialects (unit tests) keep the prior
    behaviour.
    """
    bind = getattr(db, "bind", None)
    if getattr(getattr(bind, "dialect", None), "name", None) != "postgresql":
        return
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"flow_failure_alert:{flow_id}"},
    )


def _latest_alert_anchor(
    db: Session, *, account_id: Any, flow_id: Any
) -> Optional[str]:
    """Execution id at which this flow last alerted, if a marker exists."""
    entries = crud_audit_log.get_by_account(
        db,
        account_id=account_id,
        action=FLOW_FAILURE_ALERT_ACTION,
        resource_type=FLOW_FAILURE_ALERT_RESOURCE_TYPE,
        resource_id=str(flow_id),
        limit=1,
    )
    if not entries:
        return None
    details = entries[0].details if isinstance(entries[0].details, dict) else {}
    anchor = details.get("anchor_execution_id")
    return str(anchor) if anchor else None


def _claim_alert(
    db: Session,
    *,
    account_id: Any,
    flow_id: Any,
    streak: FailureStreak,
    threshold: int,
) -> None:
    """Persist the dedup marker before delivery (cross-replica)."""
    crud_audit_log.log_action(
        db,
        account_id=account_id,
        action=FLOW_FAILURE_ALERT_ACTION,
        resource_type=FLOW_FAILURE_ALERT_RESOURCE_TYPE,
        resource_id=str(flow_id),
        status="success",
        details={
            "anchor_execution_id": streak.execution_ids[0]
            if streak.execution_ids
            else None,
            "streak_start_execution_id": streak.streak_start_execution_id,
            "consecutive_failures": streak.count,
            "threshold": threshold,
            "execution_ids": streak.execution_ids[:10],
        },
    )


def _completion_instant(row: Any) -> Optional[datetime]:
    """When this execution finished, or when it started if it has no end."""
    end = getattr(row, "end_time", None)
    if end is not None:
        return end
    start = getattr(row, "start_time", None)
    if start is not None:
        return start
    return None


def ordered_by_completion(rows: Sequence[Any]) -> List[Any]:
    """Return ``rows`` newest-completion first.

    ``end_time`` wins over ``start_time``. Rows with neither timestamp keep
    their incoming order and sort after every timestamped row, so a caller
    that already passed newest-first rows without times is unchanged. The
    sort is stable.

    Args:
        rows: Executions in any order.

    Returns:
        The same rows, newest completion first.
    """

    def sort_key(item: tuple[int, Any]) -> tuple[int, float, int]:
        index, row = item
        stamp = _completion_instant(row)
        if stamp is None:
            return (1, 0.0, index)
        return (0, -stamp.timestamp(), index)

    indexed = list(enumerate(rows))
    indexed.sort(key=sort_key)
    return [row for _, row in indexed]


def evaluate_failure_streak(
    db: Session,
    *,
    flow: Any,
    execution: Any = None,
    notifications: Any = None,
) -> FailureAlertOutcome:
    """Count the flow's current failure streak and alert once when it is due.

    Runs for every terminal execution, independently of the tracker-comment
    notifications, so a flow with no ``notifications`` blob still gets the
    safety net. A success resets the streak, so the next streak alerts again.
    Never raises.

    Args:
        db: Database session.
        flow: The flow that just finished a run.
        execution: The execution that just finished (context only).
        notifications: Override for ``flow.notifications``.

    Returns:
        What was sent, or why nothing was.
    """
    try:
        flow_id = getattr(flow, "id", None)
        account_id = getattr(flow, "account_id", None)
        if flow_id is None or account_id is None:
            return FailureAlertOutcome(skipped_reason="flow_unscoped")

        blob = (
            notifications
            if notifications is not None
            else getattr(flow, "notifications", None)
        )
        threshold = failure_alert_threshold(blob)

        # A run that just succeeded resets the streak, so there is nothing to
        # count and no reason to scan the flow's history on the common path.
        # A no-output "success" classifies as a failure and skips this.
        if execution is not None and classify_execution(execution) == CLASSIFY_SUCCESS:
            return FailureAlertOutcome(skipped_reason="latest_success")

        # Serialize the whole read-then-claim sequence per flow. Acquiring the
        # lock before the row scan is deliberate: a replica that reads the rows
        # and only then locks can scan a stale window that predates the winning
        # replica's failure row, miss the anchor inside it, and deliver a
        # duplicate. Under the lock the scan sees every row committed before
        # the previous claim.
        _lock_flow_failure_alerts(db, flow_id=flow_id)
        # get_by_flow's default is start time. Overlapping runs can finish
        # in a different order than they started, and a success that started
        # last but finished first would otherwise reset the streak.
        rows = crud_flow_execution.get_by_flow(
            db,
            flow_id=flow_id,
            limit=STREAK_SCAN_LIMIT,
            order_by_completion=True,
        )
        rows = ordered_by_completion(rows)
        anchor = _latest_alert_anchor(db, account_id=account_id, flow_id=flow_id)
        streak = compute_failure_streak(
            rows, anchor=anchor, scan_limit=STREAK_SCAN_LIMIT
        )

        if streak.already_alerted:
            return FailureAlertOutcome(skipped_reason="already_alerted")
        if streak.count < threshold:
            return FailureAlertOutcome(skipped_reason="below_threshold")
        if not streak.execution_ids:
            return FailureAlertOutcome(skipped_reason="no_failures")

        try:
            _claim_alert(
                db,
                account_id=account_id,
                flow_id=flow_id,
                streak=streak,
                threshold=threshold,
            )
        except Exception:  # noqa: BLE001 - no marker, no alert (avoid a storm)
            # Release the advisory lock held by a failed/aborted transaction.
            try:
                db.rollback()
            except Exception:  # noqa: BLE001 - rollback is best effort
                pass
            logger.warning(
                "Could not claim flow failure alert marker for flow %s",
                flow_id,
                exc_info=True,
            )
            return FailureAlertOutcome(skipped_reason="claim_failed")

        counts = deliver_failure_alert(
            db, flow=flow, streak=streak, threshold=threshold
        )
        logger.info(
            "Flow failure streak alert for flow %s: %s consecutive failures, "
            "%s email(s), %s push(es)",
            flow_id,
            streak.count,
            counts["email"],
            counts["push"],
        )
        return FailureAlertOutcome(
            alerted=True,
            recipients=counts["recipients"],
            emails_sent=counts["email"],
            pushes_sent=counts["push"],
        )
    except Exception:  # noqa: BLE001 - a notification must not fail a run
        logger.warning(
            "Flow failure streak evaluation failed for execution %s",
            getattr(execution, "id", "unknown"),
            exc_info=True,
        )
        return FailureAlertOutcome(skipped_reason="error")
