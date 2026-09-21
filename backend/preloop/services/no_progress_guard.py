"""Settings and evidence for runs that never change the workspace.

Execution ``a50ba8ff`` (issue #851) read files for 54 minutes, sent 10.13M
prompt tokens over 86 gateway requests, edited nothing, committed nothing and
exited 0 mid-sentence. The platform recorded it as ``unknown`` and did not try
again. Nothing upstream had failed: the run simply never started implementing.

Two things were missing, and both live here.

*Evidence.* The container's post-execution git block already knows the answer:
it either pushes commits or prints "No commits on <branch>, skipping push".
That sentence is now accompanied by :data:`NO_COMMITS_MARKER`, a stable line
the orchestrator matches, so "the agent reported failure and produced no
commit" is a fact read off the run rather than a guess made from prose. A run
whose block never ran (a harness crash, a flow without git) prints no marker
and is therefore never classified this way: absence of evidence is not
evidence.

*Settings.* Three optional keys on ``flow.agent_config``, all off by default so
no existing flow changes behaviour:

``no_progress_after_seconds``
    How long a live run may go without any tracked or untracked change in its
    checkout before the guard nudges it. Null (the default) disables the guard.
``no_progress_grace_seconds``
    How much longer it gets after that nudge before the run is stopped.
    Defaults to :data:`DEFAULT_GRACE_SECONDS` when the guard is enabled.
``retry_on_no_progress``
    ``{"enabled": bool, "ai_model_id": str | null, "reasoning_effort": str |
    null}``. When a run ends ``agent_no_progress`` and this is enabled, exactly
    one retry is created, optionally escalated onto another model or a higher
    reasoning effort. A retry is never retried.

Presets may opt in; the defaults here never do it for a user.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

# Printed by the container's post-execution git block, on its own line,
# immediately after the human sentence, when the branch carried no commit.
# The branch name follows, separated by a space.
NO_COMMITS_MARKER = "PRELOOP_NO_COMMITS"

# agent_config keys.
NO_PROGRESS_AFTER_KEY = "no_progress_after_seconds"
NO_PROGRESS_GRACE_KEY = "no_progress_grace_seconds"
RETRY_ON_NO_PROGRESS_KEY = "retry_on_no_progress"

# Used when the guard is enabled without naming its own grace period.
DEFAULT_GRACE_SECONDS = 600

# Floors. A guard that fires after 10 seconds would nudge every run during its
# clone; one with no grace at all would stop a run the moment it was nudged.
MIN_AFTER_SECONDS = 60
MIN_GRACE_SECONDS = 60

# The efforts an escalation may ask for. "none" is not offered: escalating to
# no reasoning at all is not an escalation.
ALLOWED_REASONING_EFFORTS = ("low", "medium", "high")


@dataclass(frozen=True)
class NoProgressGuardConfig:
    """When to nudge a live run that has changed nothing, and when to stop it.

    Attributes:
        after_seconds: Seconds of no workspace change before the nudge.
        grace_seconds: Further seconds allowed after the nudge before the stop.
    """

    after_seconds: int
    grace_seconds: int


@dataclass(frozen=True)
class NoProgressRetryConfig:
    """Whether and how to retry a run that ended without touching anything.

    Attributes:
        enabled: Whether one retry may be created at all.
        ai_model_id: Model the retry runs on, or None to keep the flow default.
        reasoning_effort: Effort the retry runs with, or None to keep the
            model's own setting.
    """

    enabled: bool
    ai_model_id: Optional[str] = None
    reasoning_effort: Optional[str] = None


def _positive_int(value: Any) -> Optional[int]:
    """One positive whole number, or None for anything else.

    Booleans are rejected: ``True`` is not 1 second, it is a typo.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_guard_config(agent_config: Any) -> Optional[NoProgressGuardConfig]:
    """Read the live-guard settings off ``flow.agent_config``.

    Args:
        agent_config: The flow's ``agent_config`` value, any shape.

    Returns:
        The guard's deadlines, or None when the flow did not enable it (the
        default) or wrote something unusable. A malformed value disables the
        guard rather than failing the run: stopping runs is what this module
        does, and it must never do it on a misreading.
    """
    if not isinstance(agent_config, Mapping):
        return None
    after = _positive_int(agent_config.get(NO_PROGRESS_AFTER_KEY))
    if after is None:
        return None
    grace = _positive_int(agent_config.get(NO_PROGRESS_GRACE_KEY))
    return NoProgressGuardConfig(
        after_seconds=max(MIN_AFTER_SECONDS, after),
        grace_seconds=max(MIN_GRACE_SECONDS, grace or DEFAULT_GRACE_SECONDS),
    )


def parse_retry_config(agent_config: Any) -> NoProgressRetryConfig:
    """Read ``agent_config.retry_on_no_progress``.

    Args:
        agent_config: The flow's ``agent_config`` value, any shape.

    Returns:
        The retry policy. Disabled for anything that is not an explicit
        ``{"enabled": true}``, including a bare ``true``: an escalation that
        costs a second run is worth one unambiguous spelling.
    """
    if not isinstance(agent_config, Mapping):
        return NoProgressRetryConfig(enabled=False)
    raw = agent_config.get(RETRY_ON_NO_PROGRESS_KEY)
    if not isinstance(raw, Mapping):
        return NoProgressRetryConfig(enabled=False)
    if raw.get("enabled") is not True:
        return NoProgressRetryConfig(enabled=False)

    model_id = raw.get("ai_model_id")
    ai_model_id = (
        model_id.strip() if isinstance(model_id, str) and model_id.strip() else None
    )

    effort = raw.get("reasoning_effort")
    reasoning_effort = None
    if isinstance(effort, str) and effort.strip():
        candidate = effort.strip().lower()
        if candidate in ALLOWED_REASONING_EFFORTS:
            reasoning_effort = candidate
        else:
            logger.info(
                "Ignoring retry_on_no_progress.reasoning_effort %r: expected one of %s",
                candidate,
                ", ".join(ALLOWED_REASONING_EFFORTS),
            )

    return NoProgressRetryConfig(
        enabled=True,
        ai_model_id=ai_model_id,
        reasoning_effort=reasoning_effort,
    )
