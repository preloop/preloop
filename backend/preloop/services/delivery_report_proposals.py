"""Shared liveness rules for Delivery Report tracker-write claims."""

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

# A process may die after claiming a proposal but before recording the
# tracker's answer. A retry must not duplicate a write that may have landed.
STUCK_PENDING_AFTER = timedelta(minutes=15)
UNRESOLVED_STATUSES = ("unconfirmed", "pending")


def needs_confirmation(existing: Mapping[str, Any]) -> bool:
    """Whether a person must check the tracker before dismissing the claim."""
    status = existing.get("status")
    if status == "unconfirmed":
        return True
    if status != "pending":
        return False
    try:
        at = datetime.fromisoformat(str(existing.get("at")))
    except ValueError:
        return True
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - at >= STUCK_PENDING_AFTER
