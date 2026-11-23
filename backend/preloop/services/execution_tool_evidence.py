"""Versioned tool observations, independent of bounded console previews.

A model request is not proof that a tool executed. Request-side tool results
are separately identified observations. Hashes cover canonical sanitized data,
never hidden credentials, and cannot recover previously uncaptured bytes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from preloop.utils.secret_scrubbing import scrub_structure
from preloop.utils.jsonb_sanitize import sanitize_for_jsonb

EVIDENCE_TYPE = "execution_tool_evidence"
MAX_EVIDENCE_BYTES = 2 * 1024 * 1024
MAX_OBSERVATIONS = 512

#: Why a stored record is incomplete. Written at capture time. A record that
#: is incomplete only because ``masking`` fired (a named credential rule) is
#: the one case ``platform_review.evidence = captured`` still accepts.
REASON_MISSING_ID = "missing_id"
REASON_INVALID_NAME = "invalid_name"
REASON_UNSAFE_TEXT = "unsafe_text"
REASON_TRUNCATED = "truncated"
REASON_CAPPED = "capped"
REASON_SCAN_DEPTH = "scan_depth"
REASON_MASKING = "masking"
INCOMPLETE_REASONS = frozenset(
    {
        REASON_MISSING_ID,
        REASON_INVALID_NAME,
        REASON_UNSAFE_TEXT,
        REASON_TRUNCATED,
        REASON_CAPPED,
        REASON_SCAN_DEPTH,
        REASON_MASKING,
    }
)


def note_incomplete(document: dict[str, Any], reason: str) -> None:
    """Record one incompleteness reason and clear ``capture_complete``.

    Args:
        document: Evidence document about to be stored. Mutated in place.
        reason: One of :data:`INCOMPLETE_REASONS`.
    """
    if reason not in INCOMPLETE_REASONS:
        raise ValueError("Unsupported incompleteness reason")
    current = document.get("incomplete_reasons")
    names = (
        [item for item in current if isinstance(item, str)]
        if isinstance(current, list)
        else []
    )
    if reason not in names:
        names.append(reason)
    document["incomplete_reasons"] = sorted(names)
    document["capture_complete"] = False


def canonical_bytes(value: Any) -> bytes:
    """Canonical v1 encoding: sorted keys, compact JSON, UTF-8, no ASCII escapes."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest(value: Any) -> str:
    """SHA-256 of the documented canonical representation."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _sensitive_field(key: Any) -> bool:
    """True when a structured key's value is replaced wholesale."""
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(
        word in lowered
        for word in (
            "api_key",
            "authorization",
            "password",
            "secret",
            "credential",
        )
    ) or ("token" in lowered and "tokens" not in lowered)


def _redact_value(value: Any) -> tuple[Any, list[dict[str, Any]], bool]:
    """Scrub one observation value.

    Returns:
        The stored value, the rule counts that fired while producing it, and
        whether the value also changed for a reason that is not a named
        masking rule. Pattern rules run on the original string before the
        known-token scrubber, so a match is counted even when both would
        mask it.
    """
    from preloop.services.model_gateway_events import (
        _REDACTION_MARKERS,
        merge_redactions,
        redact_sensitive_text,
    )

    sanitized = sanitize_for_jsonb(value)
    unexplained = sanitized != value
    value = sanitized
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        facts: list[dict[str, Any]] = []
        field_hits = 0
        for key, item in value.items():
            if _sensitive_field(key):
                # An already-masked string is not a new redaction. Both marker
                # spellings show up: field redaction and text redaction. Any
                # other value, including a dict or a list, is masked. Membership
                # on those types raises, which used to drop the whole document.
                if isinstance(item, str) and item in _REDACTION_MARKERS:
                    redacted[key] = item
                else:
                    redacted[key] = "[REDACTED]"
                    field_hits += 1
                continue
            child, child_facts, child_unexplained = _redact_value(item)
            redacted[key] = child
            facts = merge_redactions(facts, child_facts)
            unexplained = unexplained or child_unexplained
        if field_hits:
            facts = merge_redactions(
                [{"rule": "sensitive_field", "count": field_hits}], facts
            )
        return redacted, facts, unexplained
    if isinstance(value, list):
        items: list[Any] = []
        facts = []
        for item in value:
            child, child_facts, child_unexplained = _redact_value(item)
            items.append(child)
            facts = merge_redactions(facts, child_facts)
            unexplained = unexplained or child_unexplained
        return items, facts, unexplained
    if isinstance(value, str):
        redacted_text, facts = redact_sensitive_text(value)
        scrubbed = scrub_structure(redacted_text)
        text = scrubbed if isinstance(scrubbed, str) else redacted_text
        # A scrub that is not one of the named rules is not masking.
        unexplained = unexplained or text != redacted_text
        try:
            parsed = json.loads(text)
        except ValueError:
            return text, facts, unexplained
        if isinstance(parsed, (dict, list)):
            clean, nested, nested_unexplained = _redact_value(parsed)
            facts = merge_redactions(facts, nested)
            unexplained = unexplained or nested_unexplained
            if clean != parsed:
                return canonical_bytes(clean).decode("utf-8"), facts, unexplained
        return text, facts, unexplained
    return value, [], unexplained


def capture_tools(
    request: Any, response: Any, *, capture_content: bool
) -> dict[str, Any]:
    """Capture tool-only wire observations without prompt/history body fields."""
    observations: list[dict[str, Any]] = []
    incomplete = False
    reasons: set[str] = set()
    remaining = MAX_EVIDENCE_BYTES
    storage_redacted = False

    def mark(reason: str) -> None:
        nonlocal incomplete
        incomplete = True
        reasons.add(reason)

    def add(item: dict[str, Any], source: str, kind: str) -> None:
        nonlocal remaining, storage_redacted
        function = item.get("function")
        function = function if isinstance(function, dict) else item
        raw = (
            function.get("arguments", item.get("input"))
            if kind == "call"
            else item.get("output", item.get("content"))
        )
        identity = (
            item.get("call_id")
            or item.get("tool_call_id")
            or item.get("tool_use_id")
            or item.get("id")
        )
        name = function.get("name")
        # Enforce identifier bounds independently of argument content.
        if not isinstance(identity, str) or not 0 < len(identity) <= 256:
            mark(REASON_MISSING_ID)
            identity = None
        if name is not None and (not isinstance(name, str) or len(name) > 256):
            mark(REASON_INVALID_NAME)
            name = None
        safe_raw = sanitize_for_jsonb(raw)
        if safe_raw != raw:
            storage_redacted = True
            mark(REASON_UNSAFE_TEXT)
        facts: list[dict[str, Any]] = []
        if capture_content:
            from preloop.services.model_gateway_events import (
                merge_redactions,
                redact_sensitive_text,
            )

            value, facts, unexplained = _redact_value(safe_raw)
            if isinstance(value, str):
                # Keep exact wire argument strings. This redacts recognized
                # secrets without reserializing JSON or applying the preview cap.
                value, more = redact_sensitive_text(value)
                facts = merge_redactions(facts, more)
        else:
            value = None
            unexplained = False
        redacted = not capture_content or value != raw
        if capture_content and value != raw:
            storage_redacted = True
            # A named rule is masking. A change the rules do not explain
            # (removed unsafe text) is recorded as well, and both can apply.
            if facts:
                mark(REASON_MASKING)
            if unexplained or not facts:
                mark(REASON_UNSAFE_TEXT)
        record: dict[str, Any] = {
            "source": source,
            "kind": kind,
            "call_id": identity,
            "name": name,
            "value": value,
            "redacted": redacted,
            "truncated": False,
        }
        if facts:
            record["redactions"] = facts
        if kind == "result":
            record.update(_agent_clip(raw))
        safe_record = sanitize_for_jsonb(record)
        if safe_record != record:
            storage_redacted = True
            safe_record["redacted"] = True
            mark(REASON_UNSAFE_TEXT)
        record = safe_record
        encoded = canonical_bytes(record)
        if len(observations) >= MAX_OBSERVATIONS:
            mark(REASON_CAPPED)
            return
        if len(encoded) > remaining:
            mark(REASON_TRUNCATED)
            return
        remaining -= len(encoded)
        observations.append(record)

    def scan(items: Any, source: str, depth: int = 0) -> None:
        if not isinstance(items, list):
            return
        if depth > 12:
            mark(REASON_SCAN_DEPTH)
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind in {"function_call", "tool_use", "custom_tool_call"}:
                add(item, source, "call")
            elif (
                kind
                in {"function_call_output", "tool_result", "custom_tool_call_output"}
                or item.get("role") == "tool"
            ):
                add(item, source, "result")
            for call in item.get("tool_calls") or []:
                if isinstance(call, dict):
                    add(call, source, "call")
            scan(item.get("content"), source, depth + 1)

    if isinstance(response, dict):
        scan(response.get("output"), "response")
        scan(response.get("content"), "response")
        for choice in response.get("choices") or []:
            if isinstance(choice, dict):
                scan([choice.get("message")], "response")
    if isinstance(request, dict):
        scan(_latest_tool_exchange(request.get("messages")), "request")
        scan(_latest_tool_exchange(request.get("input")), "request")
    from preloop.services.model_gateway_events import REDACTION_RULESET

    document = {
        "version": 1,
        "request_scope": "latest_tool_exchange",
        "storage_redacted": storage_redacted,
        "redaction_ruleset": REDACTION_RULESET,
        "observations": observations,
        "capture_complete": not incomplete,
        "content_capture_enabled": capture_content,
        "response_available": isinstance(response, dict),
    }
    if reasons:
        document["incomplete_reasons"] = sorted(reasons)
    return document


def _agent_clip(raw: Any) -> dict[str, Any]:
    """Whether the agent CLI cut this result before the model saw it.

    Separate from ``truncated``, which means only that Preloop's own storage
    limit dropped data. A clip does not make the record incomplete.

    Args:
        raw: The tool result as sent to the model.

    Returns:
        ``agent_clipped`` and, when clipped, ``agent_original_tokens``.
    """
    from preloop.services.codex_tool_output import codex_clip

    count = codex_clip(raw)
    if count is None:
        return {"agent_clipped": False}
    return {"agent_clipped": True, "agent_original_tokens": count}


def _latest_tool_exchange(items: Any) -> list[dict[str, Any]]:
    """Select the latest call/result exchange, not replayed conversation history.

    Chat/Anthropic envelopes terminate at their latest assistant tool call.
    Flat Responses input keeps a contiguous call batch and its result batch.
    Older calls were observed in earlier responses; historical imports are not
    asserted complete by this per-request capture contract.
    """
    if not isinstance(items, list):
        return []
    selected = []
    saw_calls = False
    calls = {"function_call", "tool_use", "custom_tool_call"}
    results = {"function_call_output", "tool_result", "custom_tool_call_output"}
    for item in reversed(items):
        if not isinstance(item, dict):
            break
        content = item.get("content")
        blocks = content if isinstance(content, list) else []
        has_calls = bool(item.get("tool_calls")) or item.get("type") in calls
        has_calls |= any(isinstance(b, dict) and b.get("type") in calls for b in blocks)
        has_results = item.get("role") == "tool" or item.get("type") in results
        has_results |= any(
            isinstance(b, dict) and b.get("type") in results for b in blocks
        )
        if not (has_calls or has_results):
            if not selected:
                continue  # Trailing prose/reasoning does not erase new results.
            break
        if saw_calls and has_results:
            break
        selected.append(item)
        saw_calls |= has_calls
        if has_calls and item.get("role") == "assistant":
            break
    return list(reversed(selected))
