"""Versioned tool observations, independent of bounded console previews.

A model request is not proof that a tool executed. Request-side tool results
are separately identified observations. Hashes cover canonical data, never
hidden credentials, and cannot recover previously uncaptured bytes.

Text is kept exactly. Control characters (form feeds, ESC colour sequences,
U+0003 sent to a terminal, U+0000) are stored as they arrived: canonical JSON
escapes the C0 range, and C1 and DEL are valid JSON. Only lone surrogates,
which have no UTF-8 encoding, are removed, and the observation says so in
``removed`` (category and count, never content).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from preloop.utils.secret_scrubbing import scrub_structure
from preloop.utils.jsonb_sanitize import remove_unencodable

EVIDENCE_TYPE = "execution_tool_evidence"
MAX_EVIDENCE_BYTES = 2 * 1024 * 1024
MAX_OBSERVATIONS = 512

#: Why a stored record is incomplete. Written at capture time. A record that
#: is incomplete only because ``masking`` fired (a named credential rule) is
#: the one case ``platform_review.evidence = captured`` still accepts.
REASON_MISSING_ID = "missing_id"
REASON_INVALID_NAME = "invalid_name"
#: The stored value differs from the wire value and no rule below explains
#: it. A safety net: no known capture path sets it any more.
REASON_UNSAFE_TEXT = "unsafe_text"
#: Lone surrogates were removed; the observation's ``removed`` counts them.
REASON_LONE_SURROGATES = "lone_surrogates"
#: The known-token credential scrubber masked text no named rule matched.
REASON_CREDENTIAL_SCRUB = "credential_scrub"
#: An account sensitive-data storage rule changed the record.
REASON_STORAGE_RULE = "storage_rule"
REASON_TRUNCATED = "truncated"
REASON_CAPPED = "capped"
REASON_SCAN_DEPTH = "scan_depth"
REASON_MASKING = "masking"
#: A value nested deeper than :data:`MAX_VALUE_DEPTH` was left out.
REASON_VALUE_DEPTH = "value_depth"
#: Nesting bound for one observation value, nested argument JSON included.
#: Masking walks the value, so a deeper one is left out rather than stored
#: unmasked or allowed to exhaust the stack.
MAX_VALUE_DEPTH = 64
INCOMPLETE_REASONS = frozenset(
    {
        REASON_MISSING_ID,
        REASON_INVALID_NAME,
        REASON_UNSAFE_TEXT,
        REASON_LONE_SURROGATES,
        REASON_CREDENTIAL_SCRUB,
        REASON_STORAGE_RULE,
        REASON_TRUNCATED,
        REASON_CAPPED,
        REASON_SCAN_DEPTH,
        REASON_MASKING,
        REASON_VALUE_DEPTH,
    }
)


class ValueTooDeepError(ValueError):
    """An observation value nests deeper than :data:`MAX_VALUE_DEPTH`."""


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


def _reject_constant(name: str) -> Any:
    """``NaN`` and ``Infinity`` are not JSON; such text is kept as text."""
    raise ValueError(name)


def _reencode(value: Any) -> str:
    """Canonical text of re-encoded arguments, ASCII-escaped if it must be."""
    try:
        return canonical_bytes(value).decode("utf-8")
    except UnicodeEncodeError:
        # A ``\udXXX`` escape in the wire text decoded to a lone surrogate.
        # The escaped form round-trips exactly and is plain ASCII.
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


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


def _redact_value(value: Any, depth: int = 0) -> tuple[Any, list[dict[str, Any]], bool]:
    """Scrub one observation value.

    Control characters are not touched: masking is the only change.

    Args:
        value: The wire value.
        depth: Nesting depth of ``value``, nested argument JSON included.

    Returns:
        The stored value, the rule counts that fired while producing it, and
        whether the known-token credential scrubber also changed it beyond
        the named rules (:data:`REASON_CREDENTIAL_SCRUB`). Pattern rules run
        on the original string before the known-token scrubber, so a match is
        counted even when both would mask it.

    Raises:
        ValueTooDeepError: The value nests deeper than :data:`MAX_VALUE_DEPTH`.
    """
    from preloop.services.model_gateway_events import (
        _REDACTION_MARKERS,
        merge_redactions,
        redact_sensitive_text,
    )

    unexplained = False
    if isinstance(value, (dict, list)) and depth >= MAX_VALUE_DEPTH:
        raise ValueTooDeepError("observation value nests too deeply")
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
            child, child_facts, child_unexplained = _redact_value(item, depth + 1)
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
            child, child_facts, child_unexplained = _redact_value(item, depth + 1)
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
            parsed = json.loads(text, parse_constant=_reject_constant)
        except ValueError:
            return text, facts, unexplained
        except RecursionError:
            raise ValueTooDeepError("observation value nests too deeply") from None
        if isinstance(parsed, (dict, list)):
            clean, nested, nested_unexplained = _redact_value(parsed, depth)
            facts = merge_redactions(facts, nested)
            unexplained = unexplained or nested_unexplained
            if clean != parsed:
                return _reencode(clean), facts, unexplained
        return text, facts, unexplained
    return value, [], unexplained


def removed_facts(counts: dict[str, int]) -> list[dict[str, Any]]:
    """``{"category", "count"}`` entries for removed text, sorted by category."""
    return [
        {"category": category, "count": count}
        for category, count in sorted(counts.items())
    ]


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
        identity, removed = remove_unencodable(identity)
        name, removed_name = remove_unencodable(name)
        for category, count in removed_name.items():
            removed[category] = removed.get(category, 0) + count
        facts: list[dict[str, Any]] = []
        too_deep = False
        if capture_content:
            from preloop.services.model_gateway_events import (
                merge_redactions,
                redact_sensitive_text,
            )

            try:
                value, facts, scrubbed = _redact_value(raw)
            except ValueTooDeepError:
                value, facts, scrubbed, too_deep = None, [], False, True
            if isinstance(value, str):
                # Keep exact wire argument strings. This redacts recognized
                # secrets without reserializing JSON or applying the preview cap.
                value, more = redact_sensitive_text(value)
                facts = merge_redactions(facts, more)
            value, removed_value = remove_unencodable(value)
            for category, count in removed_value.items():
                removed[category] = removed.get(category, 0) + count
        else:
            value = None
            scrubbed = False
        if too_deep:
            storage_redacted = True
            mark(REASON_VALUE_DEPTH)
        redacted = not capture_content or too_deep or value != raw or bool(removed)
        if removed:
            storage_redacted = True
            mark(REASON_LONE_SURROGATES)
        if capture_content and value != raw and not too_deep:
            storage_redacted = True
            # Each change is named by what made it, and several can apply.
            # A change nothing here accounts for is still recorded.
            if facts:
                mark(REASON_MASKING)
            if scrubbed:
                mark(REASON_CREDENTIAL_SCRUB)
            if not (facts or scrubbed or removed):
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
        if removed:
            record["removed"] = removed_facts(removed)
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
