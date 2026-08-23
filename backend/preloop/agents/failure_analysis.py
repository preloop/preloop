"""Turn raw agent-container logs into an actionable failure summary.

When an agent container fails, the only thing a user ever sees is
``FlowExecution.error_message``. Historically that was "the last few lines of
the log that happened to contain the word error", which for the common case of
an upstream model-provider outage produced text like::

    "  status: 504\\n}\\nAn unexpected critical error occurred:[object Object]"

That names no cause and suggests no action. The real signal — an agent CLI
retrying an upstream call and exhausting its attempts — sat hundreds of lines
earlier and was discarded.

This module scans the whole log for the *meaningful* failure signal instead of
the last error-shaped line, and classifies it using the shared upstream-error
taxonomy in :mod:`preloop.services.upstream_errors` so retry decisions and
recorded error classes speak the same vocabulary as the model gateway.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from preloop.services.upstream_errors import (
    ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED,
    classify_recorded_error,
)
from preloop.utils.secret_scrubbing import scrub_secrets

# Same cap the gateway uses when surfacing upstream text to end users
# (``model_gateway_errors._MAX_SURFACED_MESSAGE_CHARS``). Agent logs embed
# whole HTML error pages and multi-KB JSON blobs; the user needs the sentence.
MAX_SURFACED_MESSAGE_CHARS = 400

# Status/metadata lines the agent wrapper emits; never a failure cause.
_STATUS_PREFIXES = (
    "[Agent Status]",
    "[Status Update]",
    "Status:",
    '{"status":',
)

# A line that matches one of these carries no information on its own. They are
# skipped when choosing *which* line to surface, but a status code embedded in
# one is still harvested as a hint. ``[object Object]`` is the classic case: a
# JS runtime stringifying an error object instead of serialising it.
_LOW_VALUE_LINE_RES = (
    # "[object Object]", with or without a leading "...error occurred:" preamble
    re.compile(r"\[object \w+\]\s*$", re.IGNORECASE),
    # Bare "status: 504" / "code: 502" fragments from a dumped object
    re.compile(
        r"^\W*(?:status|statuscode|status_code|code)\W*\d{3}\W*$", re.IGNORECASE
    ),
    # Structural punctuation left over from a pretty-printed object
    re.compile(r"^[\s{}\[\](),;:'\"]*$"),
    # Proxy/server HTML error pages
    re.compile(r"^\s*<"),
    re.compile(r"^\s*\w+/[\d.]+\s*$"),
    # A "critical error" preamble with no payload after the colon
    re.compile(r"error occurred[:\s]*$", re.IGNORECASE),
)

# Lines where an agent CLI reports one failed attempt of its internal retry
# loop. Deliberately generic: any "attempt N ... fail" phrasing, so this is not
# tied to one provider or one CLI's wording.
_ATTEMPT_LINE_RE = re.compile(
    r"\battempt\b[^\n]{0,40}?\bfail(?:ed|ure)?\b|\bfail(?:ed|ure)?\b[^\n]{0,40}?\battempt\b",
    re.IGNORECASE,
)
_ATTEMPT_NUMBER_RE = re.compile(
    r"\battempt\s*#?\s*(\d{1,3})(?:\s*(?:/|of)\s*(\d{1,3}))?", re.IGNORECASE
)

# "Max attempts reached", "max retries exceeded", "all attempts failed", …
_EXHAUSTED_RE = re.compile(
    r"\b(?:max(?:imum)?\s+(?:attempts?|retries|retry\s+attempts)\s+"
    r"(?:reached|exceeded|exhausted)"
    r"|all\s+(?:\d+\s+)?(?:attempts?|retries)\s+(?:failed|exhausted)"
    r"|retr(?:y|ies)\s+(?:limit\s+)?exhausted"
    r"|giving\s+up)\b",
    re.IGNORECASE,
)

# An HTTP status attached to a status-ish keyword. Requires the keyword so
# arbitrary three-digit numbers in agent prose are not mistaken for statuses.
_STATUS_RE = re.compile(
    r"\b(?:status(?:\s*_?\s*code)?|http(?:\s*status)?|response\s*code|returned)\b"
    r"\W{0,4}(\d{3})\b",
    re.IGNORECASE,
)

# Transport-level failures that never produced an HTTP status but are just as
# retryable as a 504. The undici entries are what Node's fetch reports when
# the server side of an in-flight stream goes away (observed when a gateway
# pod was cycled mid-response during a rollout): ``SocketError: other side
# closed`` and ``TypeError: terminated``. The latter is anchored to the
# ``TypeError:`` prefix on purpose — a bare "terminated" also appears in
# terminal failures ("run terminated by policy") that must never retry.
#
# These patterns are matched against *any* content line, not only "attempt N
# failed" lines: a CLI that crashes on a severed stream without running its
# own retry loop leaves nothing but a raw stack (``TypeError: terminated`` /
# ``[cause]: SocketError: other side closed``), and that shape must still be
# classified transient. The asymmetry is deliberate — a false positive costs
# one bounded, side-effect-safe flow retry, while a false negative terminally
# fails a recoverable run.
_TRANSIENT_NETWORK_RE = re.compile(
    r"\b(?:connection\s+(?:reset|refused|closed|aborted|error)"
    r"|econnreset|econnrefused|epipe|etimedout"
    r"|socket\s+hang\s+up"
    r"|other\s+side\s+closed"
    r"|typeerror:\s*terminated"
    r"|fetch\s+(?:failed|terminated)"
    r"|network\s+error"
    # "operation timed out" is OpenCode's client-side LLM abort: its provider
    # options.timeout fires AbortSignal.timeout and the CLI dies with
    # `error="The operation timed out."` while the request was still
    # completing upstream — the canonical recoverable shape.
    r"|(?:request|read|socket|operation)\s+timed?\s*out"
    r"|timeout\s+(?:of\s+)?\d+\s*ms\s+exceeded"
    # OpenRouter / LiteLLM mid-stream provider drop. The gateway remaps this
    # to HTTP 502 upstream_disconnect; agent logs often carry the raw
    # signature without a "status: 502" token.
    r"|provider_unavailable"
    r"|upstream_disconnect"
    r"|disconnected mid-stream"
    r"|midstreamfallbackerror"
    r"|json error injected into sse stream)\b",
    re.IGNORECASE,
)

# HTTP statuses worth another attempt. 5xx gateway/overload family plus the
# throttling and request-timeout codes. 401/403/404/400/422 are deliberately
# absent: retrying them burns money and time and can never succeed.
_TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529})

# Opening of the sentence this module generates for an upstream failure.
# The generated message is the only thing that survives into
# ``FlowExecution.error_message``. The full analysis (including the
# transient verdict) now travels on the agent result itself, but legacy
# results carry only the message — recognising our own phrasing keeps
# re-analysis of that message as faithful as possible. Note the message alone
# cannot encode every verdict (a no-status sentence loses the
# transient/terminal distinction), which is exactly why the attached verdict
# is authoritative when present.
_UPSTREAM_SENTENCE_PREFIX = "Upstream model provider"
_GENERATED_ATTEMPTS_RE = re.compile(r"after\s+(\d{1,3})\s+attempts?\b", re.IGNORECASE)
_GENERATED_EXHAUSTED_RE = re.compile(r"exhausted its retries", re.IGNORECASE)

# Human phrasing per status family. Generic across providers on purpose.
_STATUS_PHRASES = {
    400: "rejected the request as invalid",
    401: "rejected our credentials",
    403: "denied access",
    404: "could not find the requested model",
    408: "timed out",
    413: "rejected the request as too large",
    422: "rejected the request as unprocessable",
    429: "rate-limited the request",
    500: "returned an internal error",
    502: "returned a bad gateway error",
    503: "was unavailable",
    504: "timed out",
    529: "was overloaded",
}


def _status_phrase(status: int) -> str:
    if status in _STATUS_PHRASES:
        return _STATUS_PHRASES[status]
    if 500 <= status < 600:
        return "returned a server error"
    if 400 <= status < 500:
        return "rejected the request"
    return "failed the request"


@dataclass(frozen=True)
class AgentFailureAnalysis:
    """What actually went wrong inside an agent container.

    Attributes:
        message: The user-facing sentence stored on the execution. Always
            secret-scrubbed and length-capped; never empty unless the logs
            were.
        upstream_status: HTTP status reported by the upstream model provider,
            when the logs named one.
        attempts: How many attempts the agent's own retry loop made, when it
            reported them.
        retry_exhausted: True when the agent reported giving up after
            exhausting its internal retries.
        transient: True when another attempt could plausibly succeed. Drives
            flow-level retry; never True for auth, quota or request errors.
        error_class: Shared taxonomy value from
            :mod:`preloop.services.upstream_errors`, or ``None`` when the
            failure was not upstream-related.
    """

    message: str
    upstream_status: Optional[int] = None
    attempts: Optional[int] = None
    retry_exhausted: bool = False
    transient: bool = False
    error_class: Optional[str] = None
    # Raw log lines the classification was derived from (scrubbed, capped).
    # Travels with the agent result so the retry decision can be audited.
    evidence: str = ""


def _content_lines(logs_text: str) -> list[str]:
    """Log lines with wrapper status/metadata noise removed."""
    return [
        line
        for line in logs_text.split("\n")
        if line.strip() and not line.strip().startswith(_STATUS_PREFIXES)
    ]


def _is_low_value(line: str) -> bool:
    """True when a line cannot, on its own, tell a human what broke."""
    stripped = line.strip()
    if not stripped:
        return True
    return any(pattern.search(stripped) for pattern in _LOW_VALUE_LINE_RES)


def _finalize(message: str) -> str:
    """Scrub secrets and cap length on anything surfaced to a user."""
    return (scrub_secrets(message.strip()) or "")[:MAX_SURFACED_MESSAGE_CHARS]


@dataclass(frozen=True)
class _RetrySignal:
    """What the agent's own retry loop reported, lifted out of the logs."""

    status: Optional[int] = None
    attempts: Optional[int] = None
    exhausted: bool = False
    saw_network_failure: bool = False
    # Raw text of the matching log lines. Classification runs against this,
    # not against the sentence we generate, so provider wording like
    # "you exceeded your current quota" still reaches the taxonomy.
    evidence: str = ""

    @property
    def found(self) -> bool:
        return self.status is not None or self.attempts is not None or self.exhausted


def _scan_upstream_retry_signal(lines: list[str]) -> _RetrySignal:
    """Find the agent's internal retry loop in the logs."""
    statuses: list[int] = []
    attempts: Optional[int] = None
    exhausted = False
    saw_network_failure = False
    evidence: list[str] = []

    for line in lines:
        is_attempt_line = bool(_ATTEMPT_LINE_RE.search(line))
        is_exhausted_line = bool(_EXHAUSTED_RE.search(line))
        if is_exhausted_line:
            exhausted = True
            evidence.append(line)

        if is_attempt_line:
            evidence.append(line)
            number_match = _ATTEMPT_NUMBER_RE.search(line)
            if number_match:
                seen = int(number_match.group(1))
                total = number_match.group(2)
                if total:
                    seen = max(seen, int(total))
                attempts = seen if attempts is None else max(attempts, seen)
            status_match = _STATUS_RE.search(line)
            if status_match:
                statuses.append(int(status_match.group(1)))

        # Checked on every content line, not only attempt lines: a severed
        # stream that crashed the CLI outright surfaces as a raw stack with
        # no retry-loop phrasing (see the _TRANSIENT_NETWORK_RE comment).
        if _TRANSIENT_NETWORK_RE.search(line):
            saw_network_failure = True
            if not is_attempt_line and not is_exhausted_line:
                evidence.append(line)

    return _RetrySignal(
        # The last reported attempt is the one that ended the run.
        status=statuses[-1] if statuses else None,
        attempts=attempts,
        exhausted=exhausted,
        saw_network_failure=saw_network_failure,
        evidence="\n".join(evidence),
    )


def _scan_any_upstream_status(lines: list[str]) -> Optional[int]:
    """Last upstream HTTP status mentioned anywhere, including in dumps.

    A bare ``status: 504`` fragment is useless *as a message*, but the number
    in it is still the best clue about what happened, so it is harvested even
    from lines that are otherwise discarded.
    """
    found: Optional[int] = None
    for line in lines:
        match = _STATUS_RE.search(line)
        if match:
            status = int(match.group(1))
            if 400 <= status < 600:
                found = status
    return found


def _build_upstream_message(
    status: Optional[int], attempts: Optional[int], exhausted: bool
) -> str:
    """Compose the user-facing sentence for an upstream provider failure."""
    if status is not None:
        subject = f"Upstream model provider {_status_phrase(status)} (HTTP {status})"
    else:
        subject = "Upstream model provider was unreachable"

    if attempts and attempts > 1:
        return f"{subject} after {attempts} attempts."
    if exhausted:
        return f"{subject}; the agent exhausted its retries."
    return f"{subject}."


def _fallback_message(lines: list[str]) -> str:
    """Legacy best-effort extraction, skipping information-free lines.

    Priority: explicit ``ERROR:`` lines, then exceptions/tracebacks, then any
    error-shaped line, then the log tail. Unlike the original implementation,
    lines that carry no information (``[object Object]``, bare ``status: NNN``,
    HTML error pages, stray braces) are never selected as the answer.
    """

    def window(index: int, before: int, after: int) -> str:
        start = max(0, index - before)
        end = min(len(lines), index + after)
        return "\n".join(lines[start:end])

    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip().upper().startswith("ERROR:"):
            return window(index, 2, 3)

    for index in range(len(lines) - 1, -1, -1):
        lowered = lines[index].lower()
        if any(marker in lowered for marker in ("traceback", "exception:", "raise ")):
            return window(index, 0, 10)

    for index in range(len(lines) - 1, -1, -1):
        if _is_low_value(lines[index]):
            continue
        lowered = lines[index].lower()
        if any(marker in lowered for marker in ("error", "failed", "fatal")):
            return window(index, 2, 3)

    informative = [line for line in lines if not _is_low_value(line)]
    return "\n".join((informative or lines)[-5:])


def _reanalyze_generated_message(text: str) -> Optional[AgentFailureAnalysis]:
    """Re-derive an analysis from a message this module previously generated.

    This is the *legacy* retry-decision path: agent results that predate the
    attached first-pass verdict carry only the stored sentence, so the
    orchestrator falls back to re-analysing it. Parsing our own output keeps
    that fallback as close as possible to the full-log classification —
    though not identical: a no-status sentence ("was unreachable") cannot
    distinguish a severed stream from a terminal failure that also exhausted
    attempts, so it is treated as transient here. Results carrying the
    first-pass verdict do not take this path.
    """
    stripped = text.strip()
    if not stripped.startswith(_UPSTREAM_SENTENCE_PREFIX):
        return None

    status_match = re.search(r"\bHTTP\s+(\d{3})\b", stripped)
    status = int(status_match.group(1)) if status_match else None

    attempts_match = _GENERATED_ATTEMPTS_RE.search(stripped)
    attempts = int(attempts_match.group(1)) if attempts_match else None
    exhausted = bool(_GENERATED_EXHAUSTED_RE.search(stripped)) or bool(attempts)

    error_class = (
        classify_recorded_error(status, stripped) if status is not None else None
    )
    if status is not None:
        transient = (
            status in _TRANSIENT_STATUSES
            and error_class != ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED
        )
    else:
        # "Upstream model provider was unreachable" — a transport failure.
        transient = True

    return AgentFailureAnalysis(
        message=_finalize(stripped),
        upstream_status=status,
        attempts=attempts,
        retry_exhausted=exhausted,
        transient=transient,
        error_class=error_class,
    )


def analyze_agent_failure(logs_text: str) -> AgentFailureAnalysis:
    """Explain why an agent container failed, from its logs.

    Args:
        logs_text: Full (or tailed) container log text.

    Returns:
        An :class:`AgentFailureAnalysis`. ``message`` is always safe to store
        on ``FlowExecution.error_message`` and show to a user.
    """
    lines = _content_lines(logs_text)
    if not lines:
        return AgentFailureAnalysis(message="")

    already_analyzed = _reanalyze_generated_message(logs_text)
    if already_analyzed is not None:
        return already_analyzed

    signal = _scan_upstream_retry_signal(lines)
    status = signal.status

    # No retry loop reported: an upstream status may still be the only real
    # clue, hiding inside an otherwise information-free object dump.
    if not signal.found:
        tail_is_noise = all(_is_low_value(line) for line in lines[-3:])
        harvested = _scan_any_upstream_status(lines)
        if harvested is not None and tail_is_noise:
            status = harvested

    if signal.found or status is not None:
        message = _build_upstream_message(status, signal.attempts, signal.exhausted)
        # Classify against the original log text, not the sentence we just
        # generated: provider wording ("exceeded your current quota") is what
        # separates a hard quota exhaustion from a transient 429.
        error_class = (
            classify_recorded_error(status, signal.evidence or logs_text)
            if status is not None
            else None
        )
        if status is not None:
            transient = (
                status in _TRANSIENT_STATUSES
                and error_class != ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED
            )
        else:
            # Attempts were reported but no status: only treat as transient
            # when the logs showed a transport-level failure.
            transient = signal.saw_network_failure
        return AgentFailureAnalysis(
            message=_finalize(message),
            upstream_status=status,
            attempts=signal.attempts,
            retry_exhausted=signal.exhausted,
            transient=transient,
            error_class=error_class,
            evidence=_finalize(signal.evidence),
        )

    # Fallback path: no retry loop and no HTTP status anywhere. A raw
    # transport-failure stack (undici "TypeError: terminated" with
    # "[cause]: SocketError: other side closed") lands here, and it is
    # exactly as retryable as the attempt-line shapes above — the verdict
    # comes from the same _TRANSIENT_NETWORK_RE scan of the full log.
    return AgentFailureAnalysis(
        message=_finalize(_fallback_message(lines)),
        transient=signal.saw_network_failure,
        evidence=_finalize(signal.evidence),
    )
