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
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from preloop.models.crud import crud_api_usage, crud_runtime_session_activity
from preloop.models.models.account import Account
from preloop.models.models.api_usage import ApiUsage

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

# Provider prompt-cache idle TTLs used for idle-expiry detection.
# Sources (STEP 0, 2026-08-01):
# - Anthropic: 5-minute default ephemeral TTL, refresh-on-hit
#   https://platform.claude.com/docs/en/build-with-claude/prompt-caching
# - OpenAI: typical in-memory eviction after 5-10 minutes idle (pre-GPT-5.6);
#   GPT-5.6+ guarantees at least 30m. Use 10m as the conservative default and
#   require a measured cache_read collapse + cache_creation spike to confirm.
#   https://platform.openai.com/docs/guides/prompt-caching
# - Gemini: explicit cache default TTL 1 hour; implicit retention is opaque.
#   https://ai.google.dev/gemini-api/docs/caching
# - DeepSeek: disk KV cache persists for hours; use 2h.
DEFAULT_CACHE_IDLE_TTL_SECONDS = 300
PROVIDER_CACHE_IDLE_TTL_SECONDS: dict[str, int] = {
    "anthropic": 300,
    "claude": 300,
    "openai": 600,
    "azure": 600,
    "azure_openai": 600,
    "google": 3600,
    "gemini": 3600,
    "vertex_ai": 3600,
    "deepseek": 7200,
}

# Write/read multipliers vs base input when the catalog lacks explicit cache
# prices. Anthropic documents 1.25x write / 0.1x read for the 5m TTL.
PROVIDER_CACHE_WRITE_MULTIPLIER: dict[str, float] = {
    "anthropic": 1.25,
    "claude": 1.25,
}
PROVIDER_CACHE_READ_MULTIPLIER: dict[str, float] = {
    "anthropic": 0.1,
    "claude": 0.1,
    "openai": 0.1,
    "azure": 0.1,
    "azure_openai": 0.1,
    "google": 0.1,
    "gemini": 0.1,
    "vertex_ai": 0.1,
    "deepseek": 0.1,
}

# Current cache_read must fall below this fraction of the previous request's
# cache_read to count as a "collapse" after an idle gap.
_CACHE_READ_COLLAPSE_RATIO = 0.25
# Ignore tiny cache_creation spikes that cannot be meaningful waste.
_MIN_IDLE_EXPIRY_CREATION_TOKENS = 100


@dataclass(frozen=True)
class GatewayCallEvent:
    """One captured model-gateway call with its stored payload."""

    event_id: str
    payload: dict[str, Any]
    timestamp: Optional[datetime] = None
    # Authoritative cache accounting from ApiUsage when available. Payload
    # fallbacks are used only when these are unset (synthetic unit tests).
    api_usage_id: Optional[str] = None
    cache_read_tokens: Optional[int] = None
    cache_creation_tokens: Optional[int] = None
    provider_name: Optional[str] = None
    cache_write_price_per_1k: Optional[float] = None
    cache_read_price_per_1k: Optional[float] = None


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


class CacheIdleExpiryEvent(BaseModel):
    """One request that re-paid cache WRITE after an idle TTL lapse.

    Detected when consecutive requests share a stable content prefix, the
    inter-request gap exceeds the provider TTL, and the later request shows a
    measured cache_read collapse with a cache_creation spike. Token counts and
    ``api_usage_id`` are taken from real ApiUsage rows when available.
    """

    event_id: str
    previous_event_id: str
    api_usage_id: Optional[str] = None
    idle_seconds: float
    provider_ttl_seconds: int
    provider_name: Optional[str] = None
    rewritten_tokens: int = 0
    previous_cache_read_tokens: int = 0
    current_cache_read_tokens: int = 0
    measured_extra_cost_usd: Optional[float] = None


class CacheProfile(BaseModel):
    """Repeated-prefix / provider-cache alignment signals."""

    avg_repeated_prefix_tokens: int = 0
    repeated_prefix_share: float = 0.0
    prefix_stability: str = "stable"
    cache_breaking_events: list[CacheBreakingEvent] = Field(default_factory=list)
    measured_cache_read_tokens: int = 0
    idle_expiry_events: list[CacheIdleExpiryEvent] = Field(default_factory=list)
    measured_idle_expiry_tokens: int = 0
    measured_idle_expiry_extra_cost_usd: float = 0.0


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
    idle_expiry_tokens: int = 0
    total_tokens: int = 0
    scope_tokens: int = 0
    clamped: bool = False


def compute_profile_savings(
    profile: Optional[SessionContextProfile],
) -> ProfileSavingsBreakdown:
    """Roll measured waste up to one non-overlapping savings total.

    Dedupe rules, in order:

    * Unused tool-schema tokens are taken verbatim. ``analyze_tool_schema_overhead``
      already accumulates each advertised schema once per resend, so multiplying
      by ``resend_count`` again would be quadratic in the number of requests.
    * Tool-output waste takes ``max(compressible, largest oversized field)``:
      both measure the same tool-result bytes.
    * Cache-prefix waste overlaps the two above (the repeated prefix contains
      the schemas and results), so the in-context total is the ``max`` of the
      prefix estimate and the schema + tool-output sum, never their sum.
    * Retry waste is additive: those tokens were spent on requests that were
      discarded, so they are disjoint from the surviving context.
    * Idle-expiry tokens are additive: content-stable prefixes that were
      re-written after a TTL lapse (measured ``cache_creation_tokens``).

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

    idle_expiry_tokens = 0
    if profile.cache_profile is not None:
        idle_expiry_tokens = max(profile.cache_profile.measured_idle_expiry_tokens, 0)

    in_context_tokens = max(schema_tokens + tool_output_tokens, cache_prefix_tokens)
    raw_total = in_context_tokens + retry_waste_tokens + idle_expiry_tokens

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
        idle_expiry_tokens=idle_expiry_tokens,
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
    preview_messages_raw = (
        preview.get("messages") if isinstance(preview, dict) else None
    )
    preview_messages: list[Any] = (
        preview_messages_raw if isinstance(preview_messages_raw, list) else []
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
    usage_details = payload.get("usage_details")
    if isinstance(usage_details, dict):
        candidates.append(usage_details.get("cache_read_input_tokens"))
        details = usage_details.get("prompt_tokens_details")
        if isinstance(details, dict):
            candidates.append(details.get("cached_tokens"))
    details = payload.get("prompt_tokens_details")
    if isinstance(details, dict):
        candidates.append(details.get("cached_tokens"))
    for candidate in candidates:
        try:
            if candidate is not None:
                return int(candidate)
        except (TypeError, ValueError):
            continue
    return 0


def _payload_cache_creation_tokens(payload: dict[str, Any]) -> int:
    """Extract cache-creation (write) tokens from a stored gateway payload."""
    usage = payload.get("usage")
    candidates: list[Any] = []
    if isinstance(usage, dict):
        candidates.append(usage.get("cache_creation_input_tokens"))
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            candidates.append(details.get("cache_creation_tokens"))
    candidates.append(payload.get("cache_creation_input_tokens"))
    usage_details = payload.get("usage_details")
    if isinstance(usage_details, dict):
        candidates.append(usage_details.get("cache_creation_input_tokens"))
        details = usage_details.get("prompt_tokens_details")
        if isinstance(details, dict):
            candidates.append(details.get("cache_creation_tokens"))
    details = payload.get("prompt_tokens_details")
    if isinstance(details, dict):
        candidates.append(details.get("cache_creation_tokens"))
    for candidate in candidates:
        try:
            if candidate is not None:
                return int(candidate)
        except (TypeError, ValueError):
            continue
    return 0


def _event_cache_read_tokens(event: GatewayCallEvent) -> int:
    if event.cache_read_tokens is not None:
        return max(int(event.cache_read_tokens), 0)
    return _payload_cache_read_tokens(event.payload)


def _event_cache_creation_tokens(event: GatewayCallEvent) -> int:
    if event.cache_creation_tokens is not None:
        return max(int(event.cache_creation_tokens), 0)
    return _payload_cache_creation_tokens(event.payload)


def _normalize_provider_name(provider: Optional[str]) -> str:
    if not provider:
        return ""
    return str(provider).strip().lower().replace("-", "_").replace(" ", "_")


def provider_cache_idle_ttl_seconds(provider_name: Optional[str]) -> int:
    """Return the idle TTL used for idle-expiry detection for a provider.

    Args:
        provider_name: Provider identifier from ApiUsage / event payload.

    Returns:
        TTL in seconds. Defaults conservatively to Anthropic's 5-minute TTL.
    """
    normalized = _normalize_provider_name(provider_name)
    if normalized in PROVIDER_CACHE_IDLE_TTL_SECONDS:
        return PROVIDER_CACHE_IDLE_TTL_SECONDS[normalized]
    for key, ttl in PROVIDER_CACHE_IDLE_TTL_SECONDS.items():
        if key in normalized or normalized.startswith(f"{key}_"):
            return ttl
    return DEFAULT_CACHE_IDLE_TTL_SECONDS


def _event_provider_name(event: GatewayCallEvent) -> Optional[str]:
    if event.provider_name:
        return event.provider_name
    payload = event.payload
    for key in ("provider_name", "gateway_provider"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _event_timestamp(event: GatewayCallEvent) -> Optional[datetime]:
    if event.timestamp is not None:
        ts = event.timestamp
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts
    raw = event.payload.get("timestamp")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


@lru_cache(maxsize=1)
def _vendored_model_price_map() -> dict[str, Any]:
    path = Path(__file__).resolve().parent / "data" / "model_prices.json"
    try:
        loaded: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _prices_from_catalog_entry(
    entry: dict[str, Any], *, provider_name: Optional[str]
) -> tuple[Optional[float], Optional[float]]:
    """Convert one catalog price row into write/read USD per 1k tokens."""
    write_per_token = entry.get("cache_creation_input_token_cost")
    read_per_token = entry.get("cache_read_input_token_cost")
    input_per_token = entry.get("input_cost_per_token")
    write_per_1k: Optional[float] = None
    read_per_1k: Optional[float] = None
    if write_per_token is not None:
        write_per_1k = float(write_per_token) * 1000.0
    if read_per_token is not None:
        read_per_1k = float(read_per_token) * 1000.0
    if (write_per_1k is None or read_per_1k is None) and input_per_token is not None:
        input_per_1k = float(input_per_token) * 1000.0
        provider = _normalize_provider_name(provider_name)
        if write_per_1k is None:
            write_mult = PROVIDER_CACHE_WRITE_MULTIPLIER.get(provider, 1.0)
            write_per_1k = input_per_1k * write_mult
        if read_per_1k is None:
            read_mult = PROVIDER_CACHE_READ_MULTIPLIER.get(provider, 0.1)
            read_per_1k = input_per_1k * read_mult
    return write_per_1k, read_per_1k


def resolve_cache_prices_per_1k(
    *,
    model_alias: Optional[str],
    provider_name: Optional[str],
) -> tuple[Optional[float], Optional[float]]:
    """Resolve cache write/read USD prices per 1k tokens from the catalog.

    Args:
        model_alias: Gateway model alias (e.g. ``anthropic/claude-sonnet-4``).
        provider_name: Provider name used for multiplier fallbacks.

    Returns:
        ``(write_price_per_1k, read_price_per_1k)``. Either may be ``None``
        when the catalog has no usable entry.
    """
    price_map = _vendored_model_price_map()
    candidates: list[str] = []
    bare_model = ""
    if isinstance(model_alias, str) and model_alias.strip():
        alias = model_alias.strip()
        candidates.append(alias)
        if "/" in alias:
            bare_model = alias.split("/", 1)[1]
            candidates.append(bare_model)
            candidates.append(alias.replace("/", "."))
        else:
            bare_model = alias
        candidates.append(alias.replace("/", "."))
        provider = _normalize_provider_name(provider_name)
        if provider and bare_model:
            candidates.append(f"{provider}.{bare_model}")
    for candidate in candidates:
        entry = price_map.get(candidate)
        if not isinstance(entry, dict):
            continue
        write_per_1k, read_per_1k = _prices_from_catalog_entry(
            entry, provider_name=provider_name
        )
        if write_per_1k is not None or read_per_1k is not None:
            return write_per_1k, read_per_1k

    # Catalog keys often carry Bedrock-style suffixes
    # (``anthropic.claude-...-v2:0``). Fall back to the first entry whose key
    # contains the bare model id and has cache prices.
    if bare_model and len(bare_model) >= 8:
        for key, entry in price_map.items():
            if not isinstance(entry, dict) or bare_model not in key:
                continue
            write_per_1k, read_per_1k = _prices_from_catalog_entry(
                entry, provider_name=provider_name
            )
            if write_per_1k is not None and read_per_1k is not None:
                return write_per_1k, read_per_1k

    # No catalog hit: cannot invent an absolute input price.
    return None, None


def _measured_idle_expiry_extra_cost_usd(
    *,
    rewritten_tokens: int,
    write_price_per_1k: Optional[float],
    read_price_per_1k: Optional[float],
) -> Optional[float]:
    """Extra USD paid for re-writing tokens vs reading them from cache."""
    if rewritten_tokens <= 0:
        return 0.0
    if write_price_per_1k is None or read_price_per_1k is None:
        return None
    differential = float(write_price_per_1k) - float(read_price_per_1k)
    if differential <= 0:
        return 0.0
    return round((rewritten_tokens / 1000.0) * differential, 6)


def analyze_cache_profile(events: list[GatewayCallEvent]) -> CacheProfile:
    """Measure prefix stability and idle TTL cache-expiry waste.

    Content-breaking detection compares consecutive request prefixes and tool
    signatures. Idle-expiry detection additionally requires timestamps, a gap
    exceeding the provider TTL, a stable prefix, and a measured cache_read
    collapse with cache_creation spike on the later request.

    Args:
        events: Gateway call events ordered oldest-first.

    Returns:
        Cache alignment profile including cache-breaking and idle-expiry events.
    """
    if len(events) < 2:
        return CacheProfile()
    repeated_tokens: list[int] = []
    prefix_shares: list[float] = []
    breaking: list[CacheBreakingEvent] = []
    idle_expiry: list[CacheIdleExpiryEvent] = []
    measured_cache_read = 0
    previous_event: Optional[GatewayCallEvent] = None
    previous_messages: Optional[list[dict[str, Any]]] = None
    previous_tools: Optional[str] = None
    for event in events:
        payload = event.payload
        messages = _extract_messages(payload)
        tools_signature = json.dumps(
            _extract_tool_definitions(payload), sort_keys=True, default=str
        )
        measured_cache_read += _event_cache_read_tokens(event)
        if previous_messages is not None and previous_event is not None:
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
            else:
                idle_event = _detect_idle_expiry(
                    previous=previous_event,
                    current=event,
                    shared_prefix_messages=shared,
                )
                if idle_event is not None:
                    idle_expiry.append(idle_event)
        previous_event = event
        previous_messages = messages
        previous_tools = tools_signature
    avg_repeated = (
        round(sum(repeated_tokens) / len(repeated_tokens)) if repeated_tokens else 0
    )
    avg_share = sum(prefix_shares) / len(prefix_shares) if prefix_shares else 0.0
    idle_tokens = sum(event.rewritten_tokens for event in idle_expiry)
    idle_cost = sum(event.measured_extra_cost_usd or 0.0 for event in idle_expiry)
    return CacheProfile(
        avg_repeated_prefix_tokens=avg_repeated,
        repeated_prefix_share=round(avg_share, 4),
        prefix_stability="stable" if not breaking else "unstable",
        cache_breaking_events=breaking,
        measured_cache_read_tokens=measured_cache_read,
        idle_expiry_events=idle_expiry,
        measured_idle_expiry_tokens=idle_tokens,
        measured_idle_expiry_extra_cost_usd=round(idle_cost, 6),
    )


def _detect_idle_expiry(
    *,
    previous: GatewayCallEvent,
    current: GatewayCallEvent,
    shared_prefix_messages: int,
) -> Optional[CacheIdleExpiryEvent]:
    """Flag idle TTL expiry when usage shows a cache rewrite after a long gap.

    Args:
        previous: Prior gateway call.
        current: Current gateway call.
        shared_prefix_messages: Count of leading messages shared with previous.

    Returns:
        An idle-expiry event when the gap, stable prefix, and usage collapse
        criteria are all met; otherwise ``None``.
    """
    if shared_prefix_messages <= 0:
        return None
    prev_ts = _event_timestamp(previous)
    curr_ts = _event_timestamp(current)
    if prev_ts is None or curr_ts is None:
        return None
    idle_seconds = (curr_ts - prev_ts).total_seconds()
    if idle_seconds <= 0:
        return None
    provider = _event_provider_name(current) or _event_provider_name(previous)
    ttl = provider_cache_idle_ttl_seconds(provider)
    if idle_seconds <= ttl:
        return None

    prev_read = _event_cache_read_tokens(previous)
    curr_read = _event_cache_read_tokens(current)
    curr_creation = _event_cache_creation_tokens(current)
    if curr_creation < _MIN_IDLE_EXPIRY_CREATION_TOKENS:
        return None
    # Require evidence the prior turn was warm, then cold-rewrote.
    if prev_read < _MIN_IDLE_EXPIRY_CREATION_TOKENS:
        return None
    if curr_read > prev_read * _CACHE_READ_COLLAPSE_RATIO:
        return None

    write_price = current.cache_write_price_per_1k
    read_price = current.cache_read_price_per_1k
    if write_price is None or read_price is None:
        resolved_write, resolved_read = resolve_cache_prices_per_1k(
            model_alias=(
                str(current.payload.get("model_alias") or "")
                or str(previous.payload.get("model_alias") or "")
                or None
            ),
            provider_name=provider,
        )
        write_price = write_price if write_price is not None else resolved_write
        read_price = read_price if read_price is not None else resolved_read

    extra_cost = _measured_idle_expiry_extra_cost_usd(
        rewritten_tokens=curr_creation,
        write_price_per_1k=write_price,
        read_price_per_1k=read_price,
    )
    api_usage_id = current.api_usage_id
    if api_usage_id is None:
        raw_id = current.payload.get("api_usage_id")
        if isinstance(raw_id, str) and raw_id.strip():
            api_usage_id = raw_id.strip()

    return CacheIdleExpiryEvent(
        event_id=current.event_id,
        previous_event_id=previous.event_id,
        api_usage_id=api_usage_id,
        idle_seconds=round(idle_seconds, 3),
        provider_ttl_seconds=ttl,
        provider_name=provider,
        rewritten_tokens=curr_creation,
        previous_cache_read_tokens=prev_read,
        current_cache_read_tokens=curr_read,
        measured_extra_cost_usd=extra_cost,
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
                raw_name = (
                    function.get("name")
                    if isinstance(function, dict)
                    else call.get("name")
                )
                if isinstance(raw_name, str) and raw_name:
                    invoked.add(raw_name)
        tool_name = payload.get("tool_name")
        if isinstance(tool_name, str) and tool_name:
            invoked.add(tool_name)
    unused = sorted(set(advertised) - invoked)
    unused_tokens = sum(advertised[name] for name in unused)
    return ToolSchemaOverheadProfile(
        advertised_tools=len(advertised),
        invoked_tools=len(set(advertised) & invoked),
        unused_tool_names=unused,
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

    Enriches each event with authoritative ``ApiUsage`` cache token counts and
    catalog cache prices when ``metadata.api_usage_id`` is present, so idle
    expiry waste can be traced to ledger rows.

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
    selected = [
        row
        for row in reversed(rows)
        if isinstance(row.metadata_, dict) and (not allowed or str(row.id) in allowed)
    ]

    usage_ids: list[str] = []
    for row in selected:
        raw_id = row.metadata_.get("api_usage_id")
        if isinstance(raw_id, str) and raw_id.strip():
            usage_ids.append(raw_id.strip())
    usage_by_id: dict[str, ApiUsage] = {}
    if usage_ids:
        for usage_row in crud_api_usage.get_by_ids(
            db, ids=usage_ids, account_id=account.id
        ):
            usage_by_id[str(usage_row.id)] = usage_row

    events: list[GatewayCallEvent] = []
    for row in selected:
        payload = row.metadata_
        raw_usage_id = payload.get("api_usage_id")
        api_usage_id = (
            raw_usage_id.strip()
            if isinstance(raw_usage_id, str) and raw_usage_id.strip()
            else None
        )
        usage: Optional[ApiUsage] = (
            usage_by_id.get(api_usage_id) if api_usage_id else None
        )
        provider_name: Optional[str] = None
        cache_read_tokens: Optional[int] = None
        cache_creation_tokens: Optional[int] = None
        write_price: Optional[float] = None
        read_price: Optional[float] = None
        raw_model_alias = payload.get("model_alias")
        model_alias: Optional[str] = (
            raw_model_alias if isinstance(raw_model_alias, str) else None
        )
        if usage is not None:
            provider_name = usage.provider_name
            if usage.cache_read_tokens is not None:
                cache_read_tokens = int(usage.cache_read_tokens)
            if usage.cache_creation_tokens is not None:
                cache_creation_tokens = int(usage.cache_creation_tokens)
            if usage.model_alias:
                model_alias = usage.model_alias
            write_price, read_price = resolve_cache_prices_per_1k(
                model_alias=model_alias,
                provider_name=provider_name,
            )
        else:
            raw_provider = payload.get("provider_name")
            if isinstance(raw_provider, str):
                provider_name = raw_provider
            write_price, read_price = resolve_cache_prices_per_1k(
                model_alias=model_alias,
                provider_name=provider_name,
            )
        events.append(
            GatewayCallEvent(
                event_id=str(row.id),
                payload=payload,
                timestamp=row.timestamp,
                api_usage_id=api_usage_id,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens,
                provider_name=provider_name,
                cache_write_price_per_1k=write_price,
                cache_read_price_per_1k=read_price,
            )
        )
    return events


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
