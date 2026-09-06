"""Derive the stable failure category recorded on a failed flow execution.

``FlowExecution.error_message`` is free-form text: a Kubernetes API dump, a
provider stack trace, the tail of a shell script. It tells a user what
happened but nothing can *count* it, alert on it, or route it. Staging showed
the cost of that — 100 failed executions collapsing into nine distinct causes
that nobody could see without reading a hundred messages by hand.

``failure_category`` is the small, closed vocabulary those causes map onto.
It is derived once, at failure time, from the most authoritative signal
available and stored on the execution so the list/detail APIs and the console
can group by it. The precedence is: an explicit category named by the runner,
then structural message shapes that identify the failing layer regardless of
provider noise (a Job name conflict, a wall-clock timeout, a stop, the
completion contract), then the agent executor's full-log analysis verdict,
then provider-message patterns.

The vocabulary is deliberately about *where* a run broke and *who can fix
it*, not about severity:

``runner_conflict``
    The agent Job/container could not be created because its name was already
    taken (Kubernetes 409 AlreadyExists). Infrastructure, always transient.
``runner_error``
    Anything else that stopped the runtime from starting (bad manifest,
    quota, oversized entrypoint).
``model_transient``
    The model provider dropped, throttled or 5xx'd the request mid-run.
    Retryable, and the reason the run-level retry exists.
``model_auth``
    Credentials were rejected upstream (or by the gateway). Retrying is
    hopeless until a key/subscription is fixed.
``model_quota``
    Hard quota/credit exhaustion upstream. Also hopeless until refilled.
``model_config``
    The model rejected the request as configured (unsupported parameters,
    model not bound to the caller's credentials).
``no_confirmation``
    The agent exited 0 but never confirmed completion on either channel. The
    work may well have succeeded; this is Preloop's contract failing, not the
    provider's.
``setup_failed``
    A repository setup command (``git_clone_config.setup_commands``) failed
    after the clone/restore and before the agent ever started. Nothing the
    agent did caused it, and nothing it could have done would have avoided
    it.
``verification_failed``
    The publication gate (issue #428) refused to publish because a required
    check ran and failed. The implementation itself is the problem: the work
    goes back to the agent, not to the environment.
``verification_blocked``
    The publication gate refused to publish because a required check could
    not run (unavailable dependency, exit 126/127, exhausted gate budget).
    An environment gap, like ``setup_failed`` — runnable after bounded setup
    repair, never evidence of broken code.
``tool_error``
    A command or script the agent ran inside the workspace failed.
``agent_error``
    The agent process itself exited non-zero without a classifiable cause.
``timeout``
    The execution exceeded its wall-clock budget.
``cancelled``
    A human (or a policy) stopped the run.
``unknown``
    Nothing matched. A growing ``unknown`` share is the signal to extend this
    module, so it is never silently merged into ``agent_error``.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from preloop.services.upstream_errors import (
    ERROR_CLASS_NETWORK,
    ERROR_CLASS_STREAM_ABANDONED,
    ERROR_CLASS_UPSTREAM_AUTH,
    ERROR_CLASS_UPSTREAM_DISCONNECT,
    ERROR_CLASS_UPSTREAM_OVERLOADED,
    ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED,
    ERROR_CLASS_UPSTREAM_RATE_LIMITED,
)

FAILURE_CATEGORY_RUNNER_CONFLICT = "runner_conflict"
FAILURE_CATEGORY_RUNNER_ERROR = "runner_error"
FAILURE_CATEGORY_MODEL_TRANSIENT = "model_transient"
FAILURE_CATEGORY_MODEL_AUTH = "model_auth"
FAILURE_CATEGORY_MODEL_QUOTA = "model_quota"
FAILURE_CATEGORY_MODEL_CONFIG = "model_config"
FAILURE_CATEGORY_NO_CONFIRMATION = "no_confirmation"
FAILURE_CATEGORY_SETUP_FAILED = "setup_failed"
FAILURE_CATEGORY_VERIFICATION_FAILED = "verification_failed"
FAILURE_CATEGORY_VERIFICATION_BLOCKED = "verification_blocked"
FAILURE_CATEGORY_TOOL_ERROR = "tool_error"
FAILURE_CATEGORY_AGENT_ERROR = "agent_error"
FAILURE_CATEGORY_TIMEOUT = "timeout"
FAILURE_CATEGORY_CANCELLED = "cancelled"
FAILURE_CATEGORY_UNKNOWN = "unknown"

FAILURE_CATEGORIES = (
    FAILURE_CATEGORY_RUNNER_CONFLICT,
    FAILURE_CATEGORY_RUNNER_ERROR,
    FAILURE_CATEGORY_MODEL_TRANSIENT,
    FAILURE_CATEGORY_MODEL_AUTH,
    FAILURE_CATEGORY_MODEL_QUOTA,
    FAILURE_CATEGORY_MODEL_CONFIG,
    FAILURE_CATEGORY_NO_CONFIRMATION,
    FAILURE_CATEGORY_SETUP_FAILED,
    FAILURE_CATEGORY_VERIFICATION_FAILED,
    FAILURE_CATEGORY_VERIFICATION_BLOCKED,
    FAILURE_CATEGORY_TOOL_ERROR,
    FAILURE_CATEGORY_AGENT_ERROR,
    FAILURE_CATEGORY_TIMEOUT,
    FAILURE_CATEGORY_CANCELLED,
    FAILURE_CATEGORY_UNKNOWN,
)

# Column width; keep in sync with the migration.
FAILURE_CATEGORY_MAX_LENGTH = 32

# Statuses that get a category. Deliberately a positive list: a status this
# module does not recognise (a new in-flight stage, a future terminal state)
# yields no category rather than a fabricated one. An empty status is allowed
# so callers holding only an exception can still classify it.
_FAILURE_STATUSES = frozenset({"FAILED", "ERROR", "TIMEOUT", "TIMED_OUT"})
_CANCELLED_STATUSES = frozenset({"STOPPED", "CANCELLED", "CANCELED"})

# Upstream taxonomy -> category. Keeps the gateway's vocabulary and the
# execution's vocabulary from drifting apart.
_ERROR_CLASS_CATEGORIES = {
    ERROR_CLASS_NETWORK: FAILURE_CATEGORY_MODEL_TRANSIENT,
    ERROR_CLASS_UPSTREAM_OVERLOADED: FAILURE_CATEGORY_MODEL_TRANSIENT,
    ERROR_CLASS_UPSTREAM_RATE_LIMITED: FAILURE_CATEGORY_MODEL_TRANSIENT,
    ERROR_CLASS_UPSTREAM_DISCONNECT: FAILURE_CATEGORY_MODEL_TRANSIENT,
    ERROR_CLASS_STREAM_ABANDONED: FAILURE_CATEGORY_MODEL_TRANSIENT,
    ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED: FAILURE_CATEGORY_MODEL_QUOTA,
    ERROR_CLASS_UPSTREAM_AUTH: FAILURE_CATEGORY_MODEL_AUTH,
}

# --- Message patterns, most specific first -------------------------------
# Every pattern below is anchored on a real staging failure message; the
# comment names the shape it was taken from.

# "Failed to start agent Job: (409) ... jobs.batch "agent-<id>" already exists"
_RUNNER_CONFLICT_RE = re.compile(
    r"failed to start agent (?:job|container)[\s\S]{0,200}?"
    r"(?:\(409\)|already exists|alreadyexists|conflict)",
    re.IGNORECASE,
)
# Any other "Failed to start agent Job/container: ..." (e.g. the observed
# "exec /opt/entrypoint.sh: argument list too long").
_RUNNER_ERROR_RE = re.compile(
    r"failed to start agent (?:job|container|pod)"
    r"|failed to create (?:kubernetes )?job"
    r"|argument list too long"
    r"|imagepullbackoff|errimagepull|createcontainerconfigerror",
    re.IGNORECASE,
)
# "Execution timed out after 3600 seconds"
_TIMEOUT_RE = re.compile(
    r"execution timed out after|timed out after \d+ seconds|deadline exceeded",
    re.IGNORECASE,
)
# "Execution stopped by user" / agent status STOPPED
_CANCELLED_RE = re.compile(
    r"\b(?:stopped by (?:the )?user|cancelled by|canceled by|execution cancelled"
    r"|execution canceled|halt requested)\b",
    re.IGNORECASE,
)
# 'AI_APICallError: Invalid authentication credentials', 'unexpected status
# 401 Unauthorized: Invalid authentication credentials'
_MODEL_AUTH_RE = re.compile(
    r"invalid authentication credentials"
    r"|\b401\b[^\n]{0,40}unauthorized"
    r"|authenticationerror"
    r"|rejected our credentials"
    r"|invalid api key|incorrect api key",
    re.IGNORECASE,
)
# 'insufficient_quota', 'exceeded your current quota', 'credit balance'
_MODEL_QUOTA_RE = re.compile(
    r"insufficient_quota|exceeded your current quota|credit balance"
    r"|quota exhausted|out of credits",
    re.IGNORECASE,
)
# "zai does not support parameters: ['parallel_tool_calls']",
# "Model 'openai/gpt-5.4' is bound to another agent's subscription credentials"
_MODEL_CONFIG_RE = re.compile(
    r"does not support parameters"
    r"|is bound to another agent"
    r"|model metadata for [`'\"][^`'\"]+[`'\"] not found"
    r"|\bmodel not found\b|unsupported model|invalid model",
    re.IGNORECASE,
)
# 'stream error ... Upstream provider disconnected mid-stream',
# 'Reconnecting... 5/5', 'exceeded retry limit, last status: 429'
_MODEL_TRANSIENT_RE = re.compile(
    r"stream error"
    r"|stream disconnected before completion"
    r"|disconnected mid-stream"
    r"|peer closed connection"
    r"|apiconnectionerror"
    r"|reconnecting\.\.\."
    r"|exceeded retry limit"
    r"|upstream model provider"
    r"|\b429\b|too many requests|rate limit"
    r"|\b50[024]\b|bad gateway|service unavailable|overloaded"
    r"|connection reset|connection refused|socket hang up|other side closed"
    r"|typeerror:\s*terminated|fetch failed",
    re.IGNORECASE,
)
# The container-side setup block's marker, and the sentence
# analyze_agent_failure builds from it.
_SETUP_FAILED_RE = re.compile(
    r"PRELOOP_SETUP_FAILED|setup commands? failed",
    re.IGNORECASE,
)
# The publication gate's denial lines (issue #428). A required check that
# ran and failed puts the work back with the agent; a check that could not
# run (exit 126/127, missing dependency) is an environment gap and gets its
# own category so "needs a database" never reads as "broken code".
# Blocked is matched before failed: the VERDICT line carries the status.
_VERIFICATION_BLOCKED_RE = re.compile(
    r"PRELOOP_VERIFICATION_VERDICT DENY status=blocked"
    r"|verification gate refused publication.*verdict=DENY.*"
    r"status=blocked"
    r"|could not run \(unavailable dependency\)",
    re.IGNORECASE,
)
_VERIFICATION_FAILED_RE = re.compile(
    r"PRELOOP_VERIFICATION_DENIED"
    r"|PRELOOP_VERIFICATION_VERDICT DENY",
    re.IGNORECASE,
)
# The completion-contract message written by the orchestrator.
_NO_CONFIRMATION_RE = re.compile(
    r"success sentinel|flow_execution_success|did not confirm\s+success",
    re.IGNORECASE,
)
# A command the agent ran in the workspace blew up (python traceback, a
# non-zero `bash -lc` invocation, a failed CLI).
_TOOL_ERROR_RE = re.compile(
    r"traceback \(most recent call last\)"
    r"|\bexited \d+ in \d+"
    r"|systemexit|assertionerror|modulenotfounderror"
    r"|\berror:root:"
    r"|\bnpm err!|command not found",
    re.IGNORECASE,
)
# The agent harness itself failed without naming a cause.
_AGENT_ERROR_RE = re.compile(
    r"opencode command failed"
    r"|cli exited with code"
    r"|agent exited with"
    r"|monitoring error"
    r"|agent monitoring failed"
    r"|confirmation round that the original task did not complete",
    re.IGNORECASE,
)

# Messages Preloop itself writes, or shapes that identify the failing layer
# regardless of what the provider said. Matched BEFORE the provider verdict:
# a run killed by a Job name conflict, a wall-clock timeout, a human stop, or
# the completion contract is that thing even if the logs also contain a
# transient blip the executor's analyser latched onto.
_STRUCTURAL_MESSAGE_RULES = (
    (_SETUP_FAILED_RE, FAILURE_CATEGORY_SETUP_FAILED),
    (_VERIFICATION_BLOCKED_RE, FAILURE_CATEGORY_VERIFICATION_BLOCKED),
    (_VERIFICATION_FAILED_RE, FAILURE_CATEGORY_VERIFICATION_FAILED),
    (_RUNNER_CONFLICT_RE, FAILURE_CATEGORY_RUNNER_CONFLICT),
    (_RUNNER_ERROR_RE, FAILURE_CATEGORY_RUNNER_ERROR),
    (_TIMEOUT_RE, FAILURE_CATEGORY_TIMEOUT),
    (_CANCELLED_RE, FAILURE_CATEGORY_CANCELLED),
    (_NO_CONFIRMATION_RE, FAILURE_CATEGORY_NO_CONFIRMATION),
)

# Consulted only after the executor's failure analysis had its say, because
# for these the analysis (which saw the full logs) is the better witness and
# the message is a lossy summary of it.
_PROVIDER_MESSAGE_RULES = (
    (_MODEL_AUTH_RE, FAILURE_CATEGORY_MODEL_AUTH),
    (_MODEL_QUOTA_RE, FAILURE_CATEGORY_MODEL_QUOTA),
    (_MODEL_CONFIG_RE, FAILURE_CATEGORY_MODEL_CONFIG),
    (_MODEL_TRANSIENT_RE, FAILURE_CATEGORY_MODEL_TRANSIENT),
    (_TOOL_ERROR_RE, FAILURE_CATEGORY_TOOL_ERROR),
    (_AGENT_ERROR_RE, FAILURE_CATEGORY_AGENT_ERROR),
)


def _from_failure_analysis(analysis: Any) -> Optional[str]:
    """Category implied by an :class:`AgentFailureAnalysis`-shaped verdict."""
    if analysis is None:
        return None
    if isinstance(analysis, Mapping):
        error_class = analysis.get("error_class")
        transient = analysis.get("transient")
        status = analysis.get("upstream_status")
    else:
        error_class = getattr(analysis, "error_class", None)
        transient = getattr(analysis, "transient", None)
        status = getattr(analysis, "upstream_status", None)

    if error_class in _ERROR_CLASS_CATEGORIES:
        return _ERROR_CLASS_CATEGORIES[error_class]
    if isinstance(status, int):
        if status in (401, 403):
            return FAILURE_CATEGORY_MODEL_AUTH
        if status in (400, 404, 422):
            return FAILURE_CATEGORY_MODEL_CONFIG
    if transient:
        return FAILURE_CATEGORY_MODEL_TRANSIENT
    return None


def derive_failure_category(
    *,
    status: Optional[str] = None,
    error_message: Optional[str] = None,
    explicit_category: Optional[str] = None,
    failure_analysis: Any = None,
    exception: Optional[BaseException] = None,
) -> Optional[str]:
    """Classify one terminal execution outcome.

    Args:
        status: Terminal execution status (``FAILED``, ``STOPPED``, …).
        error_message: The message being stored on the execution.
        explicit_category: Category named by the code that raised, when it
            knew (see :class:`preloop.agents.errors.AgentStartError`). Wins
            over everything else.
        failure_analysis: The agent executor's log-analysis verdict
            (``AgentFailureAnalysis`` or its dict form), when present.
        exception: Exception that ended the run, when the failure came from
            an exception path rather than an agent result.

    Returns:
        A value from :data:`FAILURE_CATEGORIES`, or None when the outcome was
        not a failure. Never raises: a failure that cannot be classified is
        ``unknown``, and an execution is never left uncategorized because
        classification itself went wrong.
    """
    normalized_status = (status or "").strip().upper()
    if normalized_status in _CANCELLED_STATUSES:
        return FAILURE_CATEGORY_CANCELLED
    if normalized_status and normalized_status not in _FAILURE_STATUSES:
        # Success, or a run still in flight: there is nothing to categorise.
        return None
    if normalized_status in ("TIMEOUT", "TIMED_OUT"):
        return FAILURE_CATEGORY_TIMEOUT

    if explicit_category in FAILURE_CATEGORIES:
        return explicit_category

    exception_category = getattr(exception, "category", None)
    if exception_category in FAILURE_CATEGORIES:
        return exception_category

    text = " ".join(
        part
        for part in (error_message or "", str(exception) if exception else "")
        if part
    ).strip()

    for pattern, category in _STRUCTURAL_MESSAGE_RULES:
        if text and pattern.search(text):
            return category

    analysis_category = _from_failure_analysis(failure_analysis)
    if analysis_category is not None:
        return analysis_category

    for pattern, category in _PROVIDER_MESSAGE_RULES:
        if text and pattern.search(text):
            return category

    return FAILURE_CATEGORY_UNKNOWN
