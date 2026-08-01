"""Deterministic context analysis for runtime-session optimization.

Builds a :class:`SessionContextProfile` from captured model-gateway call
payloads so optimization suggestions are grounded in measured evidence
instead of aggregate token ratios. Analyzers are pure functions over
payload lists; database access happens only in
:func:`build_session_context_profile`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from preloop.models.crud import crud_runtime_session_activity
from preloop.models.models.account import Account

# Re-exported from core so the gateway and billing plugin share a single
# token-estimation implementation (DRY). ``estimate_tokens`` stays importable
# from this module path for existing callers.
from preloop.services.context_optimization import estimate_tokens

SEGMENT_KINDS = (
    "system_prompt",
    "tool_schemas",
    "tool_outputs",
    "conversation_history",
    "user_messages",
    "other",
)

_TOP_TOOL_OUTPUTS = 5
_MIN_HOMOGENEOUS_ARRAY_ITEMS = 5
_MIN_DUPLICATE_LINE_COUNT = 3
_SAMPLE_EXCERPT_CHARS = 160

# A top-level JSON field is treated as a droppable "oversized" candidate when
# its serialized form exceeds this many characters.
_OVERSIZED_FIELD_MIN_CHARS = 500
# Only inspect the bulkiest tool outputs for droppable fields.
_OVERSIZED_FIELD_SOURCE_OUTPUTS = 3
_MAX_OVERSIZED_FIELD_NAMES = 8


@dataclass(frozen=True)
class GatewayCallEvent:
    """One captured model-gateway call with its stored payload."""

    event_id: str
    payload: dict[str, Any]


class ContextSegment(BaseModel):
    """Token attribution for one kind of prompt content."""

    kind: str
    estimated_tokens: int = 0
    share: float = 0.0
    event_ids: list[str] = Field(default_factory=list)
    sample_excerpt: Optional[str] = None


class CacheBreakingEvent(BaseModel):
    """One request whose prefix diverged from the previous request."""

    event_id: str
    diverged_at_message_index: int
    reason_hint: str


class CacheProfile(BaseModel):
    """Repeated-prefix / provider-cache alignment signals."""

    avg_repeated_prefix_tokens: int = 0
    repeated_prefix_share: float = 0.0
    prefix_stability: str = "stable"
    cache_breaking_events: list[CacheBreakingEvent] = Field(default_factory=list)
    measured_cache_read_tokens: int = 0


class RetryProfile(BaseModel):
    """Token and cost waste attributable to failures and retries."""

    failed_requests: int = 0
    retry_requests: int = 0
    wasted_tokens: int = 0
    wasted_cost_estimate: float = 0.0
    failure_event_ids: list[str] = Field(default_factory=list)


class ToolOutputItem(BaseModel):
    """One notable tool output found in request context."""

    event_id: str
    tool_name: str
    estimated_tokens: int = 0
    content_kind: str = "text"


class OversizedOutputField(BaseModel):
    """A bulky top-level field in a tool's JSON result, a filter candidate.

    Identifies fields the agent pays tokens to carry but likely never uses
    downstream, so the operator can drop them with an output filter.
    """

    server_name: Optional[str] = None
    tool_name: str
    field_names: list[str] = Field(default_factory=list)
    estimated_tokens: int = 0


class ToolBloatProfile(BaseModel):
    """Oversized, duplicated, or compressible tool-output content."""

    largest_outputs: list[ToolOutputItem] = Field(default_factory=list)
    duplicate_output_tokens: int = 0
    duplicate_event_ids: list[str] = Field(default_factory=list)
    compressible_tokens_estimate: int = 0
    oversized_output_fields: list[OversizedOutputField] = Field(default_factory=list)


class ToolSchemaOverheadProfile(BaseModel):
    """Tool schemas advertised vs actually invoked."""

    advertised_tools: int = 0
    invoked_tools: int = 0
    unused_tool_names: list[str] = Field(default_factory=list)
    # Per-advertised-name resend-inclusive token totals for unused tools.
    # Used to carve disable-builtin-tools savings out of scope-tools without
    # double-counting (#146).
    unused_tool_tokens: dict[str, int] = Field(default_factory=dict)
    schema_tokens_estimate: int = 0
    unused_schema_tokens_estimate: int = 0
    resend_count: int = 0


class SessionContextProfile(BaseModel):
    """Evidence-grounded waste profile for one runtime session."""

    session_id: str
    analyzed_event_count: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    segments: list[ContextSegment] = Field(default_factory=list)
    cache_profile: Optional[CacheProfile] = None
    retry_profile: Optional[RetryProfile] = None
    tool_bloat: Optional[ToolBloatProfile] = None
    tool_schema_overhead: Optional[ToolSchemaOverheadProfile] = None


class ProfileSavingsBreakdown(BaseModel):
    """Deduplicated, profile-level roll-up of estimated recoverable tokens.

    Individual suggestions are separate *lenses* on the same measured bytes:
    the cache-prefix estimate re-measures the tool schemas and tool outputs it
    carries, and the oversized-field estimate re-measures part of the tool
    output the compressible estimate already covers. Summing suggestion
    estimates therefore double-counts. This breakdown is the single deduped
    total, bounded by the analyzed scope.
    """

    schema_overhead_tokens: int = 0
    tool_output_tokens: int = 0
    cache_prefix_tokens: int = 0
    retry_waste_tokens: int = 0
    total_tokens: int = 0
    scope_tokens: int = 0
    clamped: bool = False


def compute_profile_savings(
    profile: Optional[SessionContextProfile],
) -> ProfileSavingsBreakdown:
    """Roll measured waste up to one non-overlapping savings total.

    Dedupe rules, in order:

    * Unused tool-schema tokens are taken verbatim from the profile. Suggestion
      tiering (#146) may split those tokens across ``disable-builtin-tools`` and
      ``scope-tools`` for display, but this roll-up still uses the single
      profile figure so the headline total is unchanged by the split.
      ``analyze_tool_schema_overhead`` already accumulates each advertised
      schema once per resend, so multiplying by ``resend_count`` again would be
      quadratic in the number of requests.
    * Tool-output waste takes ``max(compressible, largest oversized field)``:
      both measure the same tool-result bytes.
    * Cache-prefix waste overlaps the two above (the repeated prefix contains
      the schemas and results), so the in-context total is the ``max`` of the
      prefix estimate and the schema + tool-output sum, never their sum.
    * Retry waste is additive: those tokens were spent on requests that were
      discarded, so they are disjoint from the surviving context.

    The total is finally clamped to the analyzed scope, since no optimization
    can recover more tokens than were analyzed.

    Args:
        profile: Measured context profile, or ``None`` when unavailable.

    Returns:
        The deduplicated breakdown. ``clamped`` is ``True`` when the raw total
        exceeded the analyzed scope and was capped.
    """
    if profile is None:
        return ProfileSavingsBreakdown()

    schema_tokens = 0
    if profile.tool_schema_overhead is not None:
        # Deliberately NOT multiplied by resend_count: the analyzer already
        # summed each advertised schema across every request that carried it.
        schema_tokens = max(
            profile.tool_schema_overhead.unused_schema_tokens_estimate, 0
        )

    tool_output_tokens = 0
    if profile.tool_bloat is not None:
        oversized = max(
            (
                field.estimated_tokens
                for field in profile.tool_bloat.oversized_output_fields
            ),
            default=0,
        )
        tool_output_tokens = max(
            profile.tool_bloat.compressible_tokens_estimate, oversized, 0
        )

    cache_prefix_tokens = 0
    if (
        profile.cache_profile is not None
        and profile.cache_profile.cache_breaking_events
    ):
        cache_prefix_tokens = max(
            profile.cache_profile.avg_repeated_prefix_tokens
            * len(profile.cache_profile.cache_breaking_events),
            0,
        )

    retry_waste_tokens = 0
    if profile.retry_profile is not None:
        retry_waste_tokens = max(profile.retry_profile.wasted_tokens, 0)

    in_context_tokens = max(schema_tokens + tool_output_tokens, cache_prefix_tokens)
    raw_total = in_context_tokens + retry_waste_tokens

    scope_tokens = max(
        int(profile.total_prompt_tokens) + int(profile.total_completion_tokens), 0
    )
    total = raw_total
    clamped = False
    if scope_tokens and raw_total > scope_tokens:
        total = scope_tokens
        clamped = True

    return ProfileSavingsBreakdown(
        schema_overhead_tokens=schema_tokens,
        tool_output_tokens=tool_output_tokens,
        cache_prefix_tokens=cache_prefix_tokens,
        retry_waste_tokens=retry_waste_tokens,
        total_tokens=total,
        scope_tokens=scope_tokens,
        clamped=clamped,
    )


def _message_content_to_text(value: Any) -> str:
    """Flatten message content (string or content blocks) to plain text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
                elif text is not None:
                    parts.append(json.dumps(text, ensure_ascii=False, default=str))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _extract_request(payload: dict[str, Any]) -> dict[str, Any]:
    request = payload.get("request")
    return request if isinstance(request, dict) else {}


def _extract_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return raw request messages, falling back to the conversation preview."""
    request = _extract_request(payload)
    messages = request.get("messages")
    if isinstance(messages, list) and messages:
        return [message for message in messages if isinstance(message, dict)]
    preview = payload.get("conversation_preview")
    preview_messages = (
        preview.get("messages")
        if isinstance(preview, dict) and isinstance(preview.get("messages"), list)
        else []
    )
    fallback: list[dict[str, Any]] = []
    for message in preview_messages:
        if not isinstance(message, dict):
            continue
        if message.get("source") == "response":
            continue
        fallback.append(
            {
                "role": str(message.get("role") or "user"),
                "content": str(message.get("text") or ""),
            }
        )
    return fallback


def _extract_tool_definitions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    request = _extract_request(payload)
    raw = request.get("tools") or request.get("functions")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _tool_definition_name(definition: dict[str, Any]) -> str:
    function = definition.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function["name"])
    if definition.get("name"):
        return str(definition["name"])
    return "unknown-tool"


def _is_tool_output_message(message: dict[str, Any]) -> bool:
    if message.get("role") == "tool":
        return True
    content = message.get("content")
    if isinstance(content, list):
        return any(
            isinstance(item, dict) and item.get("type") == "tool_result"
            for item in content
        )
    return False


def _classify_message(
    message: dict[str, Any], *, index: int, last_user_index: int
) -> str:
    """Classify one request message into a context segment kind."""
    role = str(message.get("role") or "")
    if role == "system":
        return "system_prompt"
    if _is_tool_output_message(message):
        return "tool_outputs"
    if role == "user":
        return "user_messages" if index == last_user_index else "conversation_history"
    if role == "assistant":
        return "conversation_history"
    return "other"


def _payload_prompt_tokens(payload: dict[str, Any]) -> int:
    value = payload.get("prompt_tokens")
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _payload_completion_tokens(payload: dict[str, Any]) -> int:
    value = payload.get("completion_tokens")
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _payload_total_tokens(payload: dict[str, Any]) -> int:
    value = payload.get("total_tokens")
    try:
        if value is not None:
            return int(value)
    except (TypeError, ValueError):
        # Unparseable total_tokens (provider quirk): fall through to the
        # prompt+completion sum below instead of dropping the event.
        pass
    return _payload_prompt_tokens(payload) + _payload_completion_tokens(payload)


def _payload_cost(payload: dict[str, Any]) -> float:
    value = payload.get("estimated_cost")
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def analyze_segments(events: list[GatewayCallEvent]) -> list[ContextSegment]:
    """Attribute prompt tokens to content segments across gateway calls.

    Args:
        events: Gateway call events ordered oldest-first.

    Returns:
        Segments with estimated tokens, shares, evidence event ids, and
        sample excerpts. Shares sum to ~1.0 over attributed tokens.
    """
    totals: dict[str, int] = {kind: 0 for kind in SEGMENT_KINDS}
    event_ids: dict[str, set[str]] = {kind: set() for kind in SEGMENT_KINDS}
    samples: dict[str, str] = {}
    for event in events:
        payload = event.payload
        messages = _extract_messages(payload)
        char_estimates: dict[str, int] = {kind: 0 for kind in SEGMENT_KINDS}
        last_user_index = max(
            (
                index
                for index, message in enumerate(messages)
                if str(message.get("role") or "") == "user"
            ),
            default=-1,
        )
        for index, message in enumerate(messages):
            kind = _classify_message(
                message, index=index, last_user_index=last_user_index
            )
            text = _message_content_to_text(message.get("content"))
            tokens = estimate_tokens(text)
            if tokens <= 0:
                continue
            char_estimates[kind] += tokens
            event_ids[kind].add(event.event_id)
            if kind not in samples and text.strip():
                samples[kind] = text.strip()[:_SAMPLE_EXCERPT_CHARS]
        tool_definitions = _extract_tool_definitions(payload)
        if tool_definitions:
            schema_text = json.dumps(tool_definitions, ensure_ascii=False, default=str)
            schema_tokens = estimate_tokens(schema_text)
            char_estimates["tool_schemas"] += schema_tokens
            event_ids["tool_schemas"].add(event.event_id)
            samples.setdefault("tool_schemas", schema_text[:_SAMPLE_EXCERPT_CHARS])
        estimated_total = sum(char_estimates.values())
        reported_prompt_tokens = _payload_prompt_tokens(payload)
        scale = (
            reported_prompt_tokens / estimated_total
            if reported_prompt_tokens > 0 and estimated_total > 0
            else 1.0
        )
        for kind, tokens in char_estimates.items():
            totals[kind] += round(tokens * scale)
    attributed_total = sum(totals.values())
    segments: list[ContextSegment] = []
    for kind in SEGMENT_KINDS:
        tokens = totals[kind]
        if tokens <= 0:
            continue
        segments.append(
            ContextSegment(
                kind=kind,
                estimated_tokens=tokens,
                share=tokens / attributed_total if attributed_total else 0.0,
                event_ids=sorted(event_ids[kind]),
                sample_excerpt=samples.get(kind),
            )
        )
    segments.sort(key=lambda segment: segment.estimated_tokens, reverse=True)
    return segments


def _normalize_message(message: dict[str, Any]) -> str:
    return json.dumps(
        {
            "role": message.get("role"),
            "content": _message_content_to_text(message.get("content")),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _diverged_reason(
    previous: list[dict[str, Any]],
    current: list[dict[str, Any]],
    diverged_at: int,
    *,
    tools_changed: bool,
) -> str:
    """Heuristic reason for a broken shared prefix between requests."""
    if tools_changed:
        return "tool_schema_changed"
    if diverged_at == 0 and current and str(current[0].get("role")) == "system":
        return "system_prompt_changed"
    if len(current) < len(previous):
        return "history_truncated"
    if diverged_at < len(previous):
        return "message_inserted"
    return "prefix_extended"


def _payload_cache_read_tokens(payload: dict[str, Any]) -> int:
    usage = payload.get("usage")
    candidates: list[Any] = []
    if isinstance(usage, dict):
        candidates.append(usage.get("cache_read_input_tokens"))
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            candidates.append(details.get("cached_tokens"))
    candidates.append(payload.get("cache_read_input_tokens"))
    for candidate in candidates:
        try:
            if candidate is not None:
                return int(candidate)
        except (TypeError, ValueError):
            continue
    return 0


def analyze_cache_profile(events: list[GatewayCallEvent]) -> CacheProfile:
    """Measure how much request prefix is re-sent verbatim across calls.

    Args:
        events: Gateway call events ordered oldest-first.

    Returns:
        Cache alignment profile including cache-breaking events.
    """
    if len(events) < 2:
        return CacheProfile()
    repeated_tokens: list[int] = []
    prefix_shares: list[float] = []
    breaking: list[CacheBreakingEvent] = []
    measured_cache_read = 0
    previous_messages: Optional[list[dict[str, Any]]] = None
    previous_tools: Optional[str] = None
    for event in events:
        payload = event.payload
        messages = _extract_messages(payload)
        tools_signature = json.dumps(
            _extract_tool_definitions(payload), sort_keys=True, default=str
        )
        measured_cache_read += _payload_cache_read_tokens(payload)
        if previous_messages is not None:
            shared = 0
            for prev_message, message in zip(previous_messages, messages, strict=False):
                if _normalize_message(prev_message) == _normalize_message(message):
                    shared += 1
                else:
                    break
            shared_tokens = sum(
                estimate_tokens(_message_content_to_text(message.get("content")))
                for message in messages[:shared]
            )
            total_tokens = sum(
                estimate_tokens(_message_content_to_text(message.get("content")))
                for message in messages
            )
            repeated_tokens.append(shared_tokens)
            prefix_shares.append(shared_tokens / total_tokens if total_tokens else 0.0)
            tools_changed = (
                previous_tools is not None and tools_signature != previous_tools
            )
            prefix_broken = shared < min(len(previous_messages), len(messages)) and (
                shared < len(previous_messages)
            )
            if tools_changed or prefix_broken:
                breaking.append(
                    CacheBreakingEvent(
                        event_id=event.event_id,
                        diverged_at_message_index=shared,
                        reason_hint=_diverged_reason(
                            previous_messages,
                            messages,
                            shared,
                            tools_changed=tools_changed,
                        ),
                    )
                )
        previous_messages = messages
        previous_tools = tools_signature
    avg_repeated = (
        round(sum(repeated_tokens) / len(repeated_tokens)) if repeated_tokens else 0
    )
    avg_share = sum(prefix_shares) / len(prefix_shares) if prefix_shares else 0.0
    return CacheProfile(
        avg_repeated_prefix_tokens=avg_repeated,
        repeated_prefix_share=round(avg_share, 4),
        prefix_stability="stable" if not breaking else "unstable",
        cache_breaking_events=breaking,
        measured_cache_read_tokens=measured_cache_read,
    )


def analyze_retry_profile(events: list[GatewayCallEvent]) -> RetryProfile:
    """Attribute token and cost waste to failed calls and their retries.

    Args:
        events: Gateway call events ordered oldest-first.

    Returns:
        Retry waste profile with failure evidence event ids.
    """
    failed_ids: list[str] = []
    failed_fingerprints: set[str] = set()
    wasted_tokens = 0
    wasted_cost = 0.0
    retry_count = 0
    for event in events:
        payload = event.payload
        outcome = str(payload.get("outcome") or "")
        status_code = payload.get("status_code")
        try:
            status = int(status_code) if status_code is not None else 0
        except (TypeError, ValueError):
            status = 0
        failed = outcome in {"error", "budget_denied"} or status >= 400
        is_retry = bool(payload.get("is_retry")) or bool(
            payload.get("retry_of_api_usage_id")
        )
        fingerprint = payload.get("request_fingerprint")
        retry_of_failure = (
            isinstance(fingerprint, str) and fingerprint in failed_fingerprints
        )
        if failed:
            failed_ids.append(event.event_id)
            wasted_tokens += _payload_total_tokens(payload)
            wasted_cost += _payload_cost(payload)
            if isinstance(fingerprint, str) and fingerprint:
                failed_fingerprints.add(fingerprint)
        elif is_retry or retry_of_failure:
            retry_count += 1
            wasted_tokens += _payload_prompt_tokens(payload)
            wasted_cost += _payload_cost(payload)
    return RetryProfile(
        failed_requests=len(failed_ids),
        retry_requests=retry_count,
        wasted_tokens=wasted_tokens,
        wasted_cost_estimate=round(wasted_cost, 6),
        failure_event_ids=failed_ids,
    )


def _content_kind(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            json.loads(stripped)
            return "json"
        except (ValueError, TypeError):
            pass
    if stripped.count("\n") >= 20:
        return "log"
    return "text"


def _homogeneous_array_compressible_tokens(text: str) -> int:
    """Estimate compressible tokens in a repeated-structure JSON array."""
    stripped = text.strip()
    if not stripped.startswith("["):
        return 0
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return 0
    if not isinstance(parsed, list) or len(parsed) <= _MIN_HOMOGENEOUS_ARRAY_ITEMS:
        return 0
    dict_items = [item for item in parsed if isinstance(item, dict)]
    if len(dict_items) < len(parsed) * 0.9:
        return 0
    key_sets = {tuple(sorted(item.keys())) for item in dict_items}
    if len(key_sets) > 2:
        return 0
    total_tokens = estimate_tokens(stripped)
    kept_items = min(3, len(dict_items))
    kept_share = kept_items / len(dict_items)
    return max(0, round(total_tokens * (1 - kept_share)))


def _duplicate_log_tokens(text: str) -> int:
    """Estimate tokens recoverable by collapsing repeated log lines."""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < _MIN_DUPLICATE_LINE_COUNT * 2:
        return 0
    counts: dict[str, int] = {}
    for line in lines:
        counts[line] = counts.get(line, 0) + 1
    return sum(
        estimate_tokens(line) * (count - 1)
        for line, count in counts.items()
        if count >= _MIN_DUPLICATE_LINE_COUNT
    )


def _resolve_tool_name(message: dict[str, Any], tool_call_names: dict[str, str]) -> str:
    tool_call_id = message.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id in tool_call_names:
        return tool_call_names[tool_call_id]
    if message.get("name"):
        return str(message["name"])
    return "tool"


def _split_server_tool_name(tool_name: str) -> tuple[Optional[str], str]:
    """Best-effort split of an MCP-style tool name into (server, tool).

    MCP tools are commonly namespaced as ``server__tool`` (double underscore)
    or ``server.tool``. When no separator is present the server is unknown.

    Args:
        tool_name: Raw resolved tool name.

    Returns:
        Tuple of optional server name and the bare tool name.
    """
    for separator in ("__", "."):
        if separator in tool_name:
            server, _, tool = tool_name.partition(separator)
            server = server.strip()
            tool = tool.strip() or tool_name
            return (server or None, tool)
    return (None, tool_name)


def _oversized_fields_in_tool_result(text: str) -> tuple[list[str], int]:
    """Find bulky top-level fields in a JSON tool result (best-effort).

    Parses ``text`` as JSON and measures the serialized size of each
    top-level object field; fields whose serialized form exceeds
    :data:`_OVERSIZED_FIELD_MIN_CHARS` are returned as droppable candidates.
    Never raises: non-JSON or non-object results yield no candidates.

    Args:
        text: Raw tool-result text.

    Returns:
        Tuple of the bulky field names (largest first) and the total
        estimated tokens attributable to those fields.
    """
    stripped = text.strip()
    if not (stripped.startswith("{") or stripped.startswith("[")):
        return ([], 0)
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return ([], 0)
    # A tool result is commonly one object OR a list of objects (e.g. a list of
    # records). For a list, a bulky field repeats per item, so its real cost is
    # the SUM of that field across every item — aggregate per field name.
    if isinstance(parsed, dict):
        items = [parsed]
    elif isinstance(parsed, list):
        items = [item for item in parsed if isinstance(item, dict)]
    else:
        return ([], 0)
    if not items:
        return ([], 0)
    # Unwrap MCP content-block envelopes ([{"type":"text","text":"<json>"}]) so we
    # analyze the real result fields rather than the wrapper's "text" field.
    if all(isinstance(item.get("text"), str) for item in items):
        inner: list[dict] = []
        for block in items:
            try:
                decoded = json.loads(block["text"])
            except (ValueError, TypeError):
                continue
            if isinstance(decoded, dict):
                inner.append(decoded)
            elif isinstance(decoded, list):
                inner.extend(d for d in decoded if isinstance(d, dict))
        if inner:
            items = inner
    field_chars: dict[str, int] = {}
    for item in items:
        for key, value in item.items():
            try:
                serialized = json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                serialized = str(value)
            field_chars[str(key)] = field_chars.get(str(key), 0) + len(serialized)
    sized: list[tuple[int, str]] = [
        (max(1, chars // 4), key)
        for key, chars in field_chars.items()
        if chars > _OVERSIZED_FIELD_MIN_CHARS
    ]
    if not sized:
        return ([], 0)
    sized.sort(key=lambda item: item[0], reverse=True)
    field_names = [name for _, name in sized][:_MAX_OVERSIZED_FIELD_NAMES]
    estimated_tokens = sum(tokens for tokens, _ in sized[:_MAX_OVERSIZED_FIELD_NAMES])
    return (field_names, estimated_tokens)


def analyze_tool_bloat(events: list[GatewayCallEvent]) -> ToolBloatProfile:
    """Find oversized, duplicated, and compressible tool outputs.

    Only the latest event is analyzed because agent gateway requests carry
    the full accumulated message history; earlier events are strict prefixes.
    Duplicates are detected within that request's message list (e.g. the
    same file read twice) rather than across requests, which is cache
    territory handled by :func:`analyze_cache_profile`.

    Args:
        events: Gateway call events ordered oldest-first.

    Returns:
        Tool bloat profile with a measured compressible-token estimate.
    """
    if not events:
        return ToolBloatProfile()
    latest = events[-1]
    messages = _extract_messages(latest.payload)
    tool_call_names: dict[str, str] = {}
    for message in messages:
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            name = (
                function.get("name") if isinstance(function, dict) else call.get("name")
            )
            call_id = call.get("id")
            if isinstance(call_id, str) and name:
                tool_call_names[call_id] = str(name)
    outputs: list[ToolOutputItem] = []
    seen_hashes: dict[str, int] = {}
    duplicate_tokens = 0
    duplicate_event_ids: set[str] = set()
    compressible = 0
    # Pair each output's raw text with its resolved tool name so the bulkiest
    # outputs can be re-inspected for droppable JSON fields afterwards.
    output_texts: list[tuple[str, str]] = []
    for message in messages:
        if not _is_tool_output_message(message):
            continue
        text = _message_content_to_text(message.get("content"))
        if not text.strip():
            continue
        tokens = estimate_tokens(text)
        tool_name = _resolve_tool_name(message, tool_call_names)
        outputs.append(
            ToolOutputItem(
                event_id=latest.event_id,
                tool_name=tool_name,
                estimated_tokens=tokens,
                content_kind=_content_kind(text),
            )
        )
        output_texts.append((tool_name, text))
        digest = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
        if digest in seen_hashes:
            duplicate_tokens += tokens
            duplicate_event_ids.add(latest.event_id)
        else:
            seen_hashes[digest] = tokens
        compressible += _homogeneous_array_compressible_tokens(text)
        compressible += _duplicate_log_tokens(text)
    outputs.sort(key=lambda item: item.estimated_tokens, reverse=True)
    # Oversized-field detection scans ALL events, not just the latest: multi-node
    # agents (e.g. LangGraph) place a tool's result in an intermediate request
    # rather than the final accumulated one. Dedup by content so a result that
    # reappears across nodes is counted once.
    all_output_texts: list[tuple[str, str]] = list(output_texts)
    seen_output_digests: set[str] = {
        hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
        for _, text in output_texts
    }
    for event in events[:-1]:
        event_messages = _extract_messages(event.payload)
        event_call_names: dict[str, str] = {}
        for message in event_messages:
            calls = message.get("tool_calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function")
                nm = fn.get("name") if isinstance(fn, dict) else call.get("name")
                cid = call.get("id")
                if isinstance(cid, str) and nm:
                    event_call_names[cid] = str(nm)
        for message in event_messages:
            if not _is_tool_output_message(message):
                continue
            text = _message_content_to_text(message.get("content"))
            if not text.strip():
                continue
            digest = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
            if digest in seen_output_digests:
                continue
            seen_output_digests.add(digest)
            all_output_texts.append(
                (_resolve_tool_name(message, event_call_names), text)
            )
    oversized_fields = _detect_oversized_output_fields(all_output_texts)
    return ToolBloatProfile(
        largest_outputs=outputs[:_TOP_TOOL_OUTPUTS],
        duplicate_output_tokens=duplicate_tokens,
        duplicate_event_ids=sorted(duplicate_event_ids),
        compressible_tokens_estimate=compressible + duplicate_tokens,
        oversized_output_fields=oversized_fields,
    )


def _detect_oversized_output_fields(
    output_texts: list[tuple[str, str]],
) -> list[OversizedOutputField]:
    """Find droppable bulky JSON fields among the largest tool outputs.

    Best-effort: only the bulkiest outputs are inspected and any output that
    is not a JSON object yields no candidates.

    Args:
        output_texts: ``(tool_name, raw_text)`` pairs for this request's
            tool outputs.

    Returns:
        Oversized-field candidates ordered by estimated tokens (largest
        first), one entry per qualifying tool output.
    """
    by_size = sorted(output_texts, key=lambda item: len(item[1]), reverse=True)[
        :_OVERSIZED_FIELD_SOURCE_OUTPUTS
    ]
    candidates: list[OversizedOutputField] = []
    for tool_name, text in by_size:
        field_names, estimated_tokens = _oversized_fields_in_tool_result(text)
        if not field_names:
            continue
        server_name, bare_tool = _split_server_tool_name(tool_name)
        candidates.append(
            OversizedOutputField(
                server_name=server_name,
                tool_name=bare_tool,
                field_names=field_names,
                estimated_tokens=estimated_tokens,
            )
        )
    candidates.sort(key=lambda item: item.estimated_tokens, reverse=True)
    return candidates


def analyze_tool_schema_overhead(
    events: list[GatewayCallEvent],
) -> ToolSchemaOverheadProfile:
    """Compare advertised tool schemas with tools actually invoked.

    Args:
        events: Gateway call events ordered oldest-first.

    Returns:
        Schema overhead profile with resend-aware unused-token estimate.
    """
    advertised: dict[str, int] = {}
    invoked: set[str] = set()
    schema_tokens_total = 0
    resend_count = 0
    for event in events:
        payload = event.payload
        definitions = _extract_tool_definitions(payload)
        if definitions:
            resend_count += 1
            schema_tokens_total += estimate_tokens(
                json.dumps(definitions, ensure_ascii=False, default=str)
            )
            for definition in definitions:
                name = _tool_definition_name(definition)
                advertised[name] = advertised.get(name, 0) + estimate_tokens(
                    json.dumps(definition, ensure_ascii=False, default=str)
                )
        for message in _extract_messages(payload):
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                name = (
                    function.get("name")
                    if isinstance(function, dict)
                    else call.get("name")
                )
                if name:
                    invoked.add(str(name))
        tool_name = payload.get("tool_name")
        if isinstance(tool_name, str) and tool_name:
            invoked.add(tool_name)
    unused = sorted(set(advertised) - invoked)
    unused_tool_tokens = {name: advertised[name] for name in unused}
    unused_tokens = sum(unused_tool_tokens.values())
    return ToolSchemaOverheadProfile(
        advertised_tools=len(advertised),
        invoked_tools=len(set(advertised) & invoked),
        unused_tool_names=unused,
        unused_tool_tokens=unused_tool_tokens,
        schema_tokens_estimate=schema_tokens_total,
        unused_schema_tokens_estimate=unused_tokens,
        resend_count=resend_count,
    )


def build_profile_from_events(
    session_id: str, events: list[GatewayCallEvent]
) -> SessionContextProfile:
    """Compose all analyzers into one profile from in-memory events.

    Args:
        session_id: Runtime session id the events belong to.
        events: Gateway call events ordered oldest-first.

    Returns:
        Complete session context profile.
    """
    return SessionContextProfile(
        session_id=session_id,
        analyzed_event_count=len(events),
        total_prompt_tokens=sum(
            _payload_prompt_tokens(event.payload) for event in events
        ),
        total_completion_tokens=sum(
            _payload_completion_tokens(event.payload) for event in events
        ),
        segments=analyze_segments(events),
        cache_profile=analyze_cache_profile(events),
        retry_profile=analyze_retry_profile(events),
        tool_bloat=analyze_tool_bloat(events),
        tool_schema_overhead=analyze_tool_schema_overhead(events),
    )


def load_session_gateway_events(
    db: Session,
    *,
    account: Account,
    runtime_session_id: str,
    event_ids: Optional[list[str]] = None,
    limit: int = 50,
) -> list[GatewayCallEvent]:
    """Load stored gateway calls for a session as in-memory events.

    Args:
        db: Database session.
        account: Owning account.
        runtime_session_id: Runtime session to analyze.
        event_ids: Optional scope filter; only these activity ids are analyzed.
        limit: Maximum number of gateway calls to load.

    Returns:
        Gateway call events ordered oldest-first.
    """
    rows = (
        crud_runtime_session_activity.list_full_model_gateway_call_payloads_for_session(
            db,
            account_id=account.id,
            runtime_session_id=runtime_session_id,
            limit=limit,
        )
    )
    allowed = set(event_ids or [])
    return [
        GatewayCallEvent(event_id=str(row.id), payload=row.metadata_)
        for row in reversed(rows)
        if isinstance(row.metadata_, dict) and (not allowed or str(row.id) in allowed)
    ]


def build_session_context_profile(
    db: Session,
    *,
    account: Account,
    runtime_session_id: str,
    event_ids: Optional[list[str]] = None,
    limit: int = 50,
) -> SessionContextProfile:
    """Load stored gateway calls for a session and build its context profile.

    Args:
        db: Database session.
        account: Owning account.
        runtime_session_id: Runtime session to analyze.
        event_ids: Optional scope filter; only these activity ids are analyzed.
        limit: Maximum number of gateway calls to analyze.

    Returns:
        Session context profile built from stored payloads.
    """
    events = load_session_gateway_events(
        db,
        account=account,
        runtime_session_id=runtime_session_id,
        event_ids=event_ids,
        limit=limit,
    )
    return build_profile_from_events(runtime_session_id, events)
