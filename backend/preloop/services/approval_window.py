"""One place that answers "how long does the human have?".

Before this module the answer was a literal ``300`` in
``approval_service.create_approval_request``. Five minutes is a reasonable
default for an interactive tool call and a nonsense default for a compliance
decision: a CRA waiver is a documented risk acceptance whose reviewer may have
to check reachability first. On staging a human answered such a question in 175
seconds and was 27 seconds too late, and the risk acceptance attached to no
evidence at all.

Precedence, most specific first, each one bounded by the account cap:

1. ``timeout_seconds`` passed to ``ask_user`` / ``request_approval``
2. ``flow.approval_window_seconds`` (the flow that is running)
3. ``approval_workflow.timeout_seconds``
4. ``settings.approval_default_window_seconds`` (300, unchanged)

The cap is ``account.meta_data["approval_window_max_seconds"]`` when the
account sets one, else ``settings.approval_max_window_seconds`` (30 days).
Nothing may exceed it: an approval that never expires is a governance object
nobody ever closes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from preloop.config import settings

logger = logging.getLogger(__name__)

#: Nothing shorter is a decision window; it is a race the human loses.
APPROVAL_WINDOW_MIN_SECONDS = 60

#: Account metadata key holding a per-account ceiling (seconds).
ACCOUNT_WINDOW_CAP_KEY = "approval_window_max_seconds"


@dataclass(frozen=True)
class ApprovalWindow:
    """The resolved decision window for one approval request."""

    seconds: int
    #: ``tool_argument`` | ``flow`` | ``workflow`` | ``default``
    source: str
    #: True when the requested value was clamped to the account/deployment cap.
    capped: bool = False

    def describe(self) -> str:
        """Operator-facing sentence naming the setting that produced it."""
        names = {
            "tool_argument": "requested by the agent",
            "flow": "this flow's approval_window_seconds",
            "workflow": "the approval workflow timeout",
            "default": "the deployment default approval window",
        }
        label = names.get(self.source, self.source)
        text = f"{self.seconds}s ({label})"
        return f"{text}, capped" if self.capped else text


def _coerce(value: Any) -> Optional[int]:
    """Positive int or None; a garbage setting must not decide governance."""
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def account_window_cap(account: Any) -> int:
    """Ceiling for one account: its own metadata override, else the setting."""
    deployment_cap = max(
        APPROVAL_WINDOW_MIN_SECONDS, int(settings.approval_max_window_seconds)
    )
    meta = getattr(account, "meta_data", None)
    override = (
        _coerce(meta.get(ACCOUNT_WINDOW_CAP_KEY)) if isinstance(meta, dict) else None
    )
    if override is None:
        return deployment_cap
    # An account may only tighten. Letting a tenant raise the deployment
    # ceiling would make "expires_at" unbounded per tenant.
    return max(APPROVAL_WINDOW_MIN_SECONDS, min(deployment_cap, override))


def resolve_approval_window(
    *,
    requested_seconds: Any = None,
    flow: Any = None,
    workflow: Any = None,
    account: Any = None,
) -> ApprovalWindow:
    """Resolve the decision window from the most specific setting available.

    Every argument is optional and duck-typed on purpose: this is called from
    the MCP tool gate (which has a workflow and maybe a flow), from the
    approval service (which may have neither), and from tests.
    """
    cap = account_window_cap(account)

    candidates = (
        ("tool_argument", _coerce(requested_seconds)),
        ("flow", _coerce(getattr(flow, "approval_window_seconds", None))),
        ("workflow", _coerce(getattr(workflow, "timeout_seconds", None))),
        (
            "default",
            _coerce(settings.approval_default_window_seconds)
            or APPROVAL_WINDOW_MIN_SECONDS,
        ),
    )
    for source, seconds in candidates:
        if seconds is None:
            continue
        clamped = max(APPROVAL_WINDOW_MIN_SECONDS, min(cap, seconds))
        if clamped != seconds:
            logger.info(
                "Approval window %ss from %s clamped to %ss",
                seconds,
                source,
                clamped,
            )
        return ApprovalWindow(seconds=clamped, source=source, capped=clamped != seconds)
    # Unreachable: the default branch always yields a value.
    return ApprovalWindow(seconds=APPROVAL_WINDOW_MIN_SECONDS, source="default")


def should_park(
    window_seconds: int, *, park_after_seconds: Optional[int] = None
) -> bool:
    """True when waiting in place would cost more than parking the run.

    A window at or under the short in-process wait is answered before the
    park would even be observed, so parking it would add a container restart
    for nothing.
    """
    threshold = (
        park_after_seconds
        if park_after_seconds is not None
        else int(settings.approval_park_after_seconds)
    )
    if threshold <= 0:
        return False
    return int(window_seconds) > threshold
