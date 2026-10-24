"""What a pull request review run finished after submitting its review (#1683).

A reviewer flow submits its review (``update_pull_request`` with a
``review_action``) and then has post-review work left: the description
summary, removing the eyes reaction, and the completion result. When the run
then ends without succeeding (stopped, timed out, failed), the commit status
must still say that a review was submitted and which step never completed,
instead of a bare "execution was stopped" that hides the review outcome.

The run stays a failure: a submitted review is not a completed run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# Statuses a recorded gateway tool call carries when it ran.
_EXECUTED_STATUSES = frozenset({"executed", "success", "completed"})

# A JSON ``null`` serializes to four bytes, so a key whose recorded size is at
# most this carried no value.
_EMPTY_VALUE_SIZE = 4

_REVIEW_LABELS = {
    "approved": "approved",
    "changes_requested": "changes requested",
    "commented": "commented",
}

# GitHub caps a commit status description at 140 characters.
COMMIT_STATUS_DESCRIPTION_LIMIT = 140

STEP_DESCRIPTION = "PR description summary"
STEP_REACTION = "eyes reaction removal"
STEP_RESULT = "completion result"


@dataclass
class ReviewProgress:
    """A submitted review and the post-review steps observed after it.

    Attributes:
        review_state: ``approved``, ``changes_requested`` or ``commented``.
        description_updated: The description was written after the review.
        reaction_removed: The eyes reaction was removed after the review.
        remaining: Post-review steps not observed, in flow order.
    """

    review_state: str
    description_updated: bool = False
    reaction_removed: bool = False
    remaining: list[str] = field(default_factory=list)


def _metadata(activity: Any) -> dict[str, Any]:
    metadata = getattr(activity, "metadata_", None)
    return metadata if isinstance(metadata, dict) else {}


def _executed(activity: Any) -> bool:
    status = str(getattr(activity, "status", "") or "").lower()
    return status in _EXECUTED_STATUSES


def _has_value(summary: dict[str, Any], key: str) -> bool:
    size = summary.get(key)
    return isinstance(size, int) and size > _EMPTY_VALUE_SIZE


def summarize_review_progress(
    activities: Iterable[Any], *, result_confirmed: bool
) -> Optional[ReviewProgress]:
    """Find the last submitted review and what the run did after it.

    Args:
        activities: The execution's recorded tool calls, oldest first. Each
            has ``tool_name``, ``status`` and ``metadata_`` (with
            ``work_ref`` and ``arguments_summary``).
        result_confirmed: Whether the agent delivered its completion result.

    Returns:
        The review and its remaining steps, or None when no review was
        submitted.
    """
    progress: Optional[ReviewProgress] = None
    for activity in activities:
        if getattr(activity, "tool_name", None) != "update_pull_request":
            continue
        if not _executed(activity):
            continue
        metadata = _metadata(activity)
        work_ref = metadata.get("work_ref")
        summary = metadata.get("arguments_summary")
        summary = summary if isinstance(summary, dict) else {}
        if isinstance(work_ref, dict) and work_ref.get("action") == "reviewed":
            # A later review supersedes an earlier one; the steps after it
            # are what count.
            progress = ReviewProgress(
                review_state=str(work_ref.get("review_state") or "commented")
            )
            continue
        if progress is None:
            continue
        if _has_value(summary, "description"):
            progress.description_updated = True
        if _has_value(summary, "remove_reaction"):
            progress.reaction_removed = True
    if progress is None:
        return None
    if not progress.description_updated:
        progress.remaining.append(STEP_DESCRIPTION)
    if not progress.reaction_removed:
        progress.remaining.append(STEP_REACTION)
    if not result_confirmed:
        progress.remaining.append(STEP_RESULT)
    return progress


def describe_incomplete_review(progress: ReviewProgress, stored_status: str) -> str:
    """Commit status description for a run that reviewed but did not succeed.

    Args:
        progress: The submitted review and its remaining steps.
        stored_status: The execution's terminal status.

    Returns:
        A description of at most :data:`COMMIT_STATUS_DESCRIPTION_LIMIT`
        characters naming the review outcome, how the run ended, and the
        steps that did not complete.
    """
    label = _REVIEW_LABELS.get(progress.review_state, progress.review_state)
    ended = str(stored_status or "failed").lower()
    if progress.remaining:
        tail = "incomplete: " + ", ".join(progress.remaining)
    else:
        tail = "post-review steps done"
    text = f"Review submitted ({label}); run {ended}, {tail}"
    if len(text) > COMMIT_STATUS_DESCRIPTION_LIMIT:
        text = text[: COMMIT_STATUS_DESCRIPTION_LIMIT - 3].rstrip() + "..."
    return text
