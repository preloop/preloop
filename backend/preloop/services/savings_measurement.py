"""Deterministic input-token savings measurement for session optimization.

Computes the EXACT input-token delta of applying a candidate
session-optimization to a stored gateway request payload. There are NO model
calls here: tokenization is the same deterministic ``estimate_tokens``
heuristic the gateway uses, so the result is reproducible run-to-run and is
the precise "you wasted N tokens" figure for the input side of a request.

A separate re-execution harness layers output-side deltas on top of this; this
module owns only the deterministic input-side number. Following
``tool_cost_analysis.py`` conventions: a frozen result dataclass and pure
functions over the payload — no database access, no mutation of the caller's
input.

Tokenization mirrors ``openai_gateway._capture_tools_meta`` exactly: serialize
with ``json.dumps(obj, default=str, sort_keys=True)`` and call
``estimate_tokens`` on the resulting string.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Import the shared token estimator and tool-name resolver from preloop core so
# this measurement matches the gateway's attribution byte-for-byte. Both symbols
# are also re-exported from ``context_analysis`` for the billing plugin, but the
# core module is their canonical home.
from preloop.services.context_optimization import (
    estimate_tokens,
    tool_definition_name,
)


@dataclass(frozen=True)
class InputTokenSavings:
    """Exact input-token delta of an applied session-optimization.

    Attributes:
        original_tokens: Estimated input tokens of the request payload as-is.
        modified_tokens: Estimated input tokens after applying the candidate
            optimization (tool removal and/or tool-output field filtering).
        delta_tokens: ``original_tokens - modified_tokens``. Positive means
            tokens were saved; ``0`` means the optimization was a no-op.
        pct_saved: ``delta_tokens / original_tokens`` as a fraction, or ``0.0``
            when ``original_tokens`` is ``0``.
    """

    original_tokens: int
    modified_tokens: int
    delta_tokens: int
    pct_saved: float


def _count_tokens(obj: Any) -> int:
    """Estimate tokens for a payload fragment the way the gateway does.

    Mirrors ``openai_gateway._capture_tools_meta``: serialize deterministically
    with ``json.dumps(..., default=str, sort_keys=True)`` and run the shared
    ``estimate_tokens`` heuristic over the string.

    Args:
        obj: Any JSON-serializable object (or one coercible via ``default=str``).

    Returns:
        The estimated token count for the serialized object.
    """
    try:
        serialized = json.dumps(obj, default=str, sort_keys=True)
    except (TypeError, ValueError):
        serialized = ""
    return estimate_tokens(serialized)


def estimate_payload_tokens(payload: Mapping[str, Any]) -> int:
    """Estimate the input tokens of a full request payload.

    Public wrapper over the module's token heuristic so callers outside this
    module (e.g. the replay budget guard's per-run cost projection) share the
    same counting rules as the savings math instead of re-implementing them.

    Args:
        payload: The gateway request payload to size.

    Returns:
        The estimated input-token count for the serialized payload.
    """
    return _count_tokens(payload)


def _tool_call_id_names(messages: Sequence[Any]) -> dict[str, str]:
    """Map assistant ``tool_call`` ids to the tool name each invoked.

    A tool-result message (role ``"tool"``) references the originating call via
    ``tool_call_id`` rather than carrying the tool name directly, so we resolve
    names from the preceding assistant messages' ``tool_calls``.

    Args:
        messages: The request ``messages`` list (any shape; defended).

    Returns:
        Mapping of ``tool_call_id`` to the resolved tool name.
    """
    names: dict[str, str] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            call_id = call.get("id")
            function = call.get("function")
            name = ""
            if isinstance(function, dict):
                name = str(function.get("name") or "")
            if not name:
                name = str(call.get("name") or "")
            if isinstance(call_id, str) and name:
                names[call_id] = name
    return names


def _resolve_tool_result_name(
    message: Mapping[str, Any], tool_call_names: Mapping[str, str]
) -> str:
    """Resolve the tool name a tool-result message reports output for.

    Mirrors ``context_analysis._resolve_tool_name``: prefer the
    ``tool_call_id`` back-reference, then a direct ``name`` field.

    Args:
        message: A tool-result message.
        tool_call_names: Mapping of ``tool_call_id`` to tool name.

    Returns:
        The resolved tool name, or ``""`` when none can be determined.
    """
    tool_call_id = message.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id in tool_call_names:
        return tool_call_names[tool_call_id]
    name = message.get("name")
    if name:
        return str(name)
    return ""


def _is_tool_result_message(message: Mapping[str, Any]) -> bool:
    """Return whether a message carries tool output eligible for filtering.

    Recognizes the OpenAI chat tool-result shape (``role == "tool"``). Anthropic
    tool-result blocks live inside user-message content and are not the target
    of field filtering here.

    Args:
        message: A single request message.

    Returns:
        ``True`` when the message is a tool-result message.
    """
    return str(message.get("role") or "") == "tool"


def _drop_json_fields(value: Any, fields: Sequence[str]) -> Any:
    """Drop named top-level fields from a JSON tool-result value.

    Handles the common tool-result content encodings defensively and never
    raises: content that is not parseable JSON (or not a JSON object) is
    returned unchanged.

    Supported ``value`` forms:
        * A JSON string encoding an object: parsed, filtered, re-serialized.
        * A ``dict``: filtered in place on a copy.
        * A ``list`` of content blocks (e.g. ``[{"type": "text",
          "text": "<json>"}]``): each text block's payload is filtered.

    Args:
        value: The message ``content`` value.
        fields: Field names to remove from the decoded object.

    Returns:
        A new content value with the fields removed, or the original ``value``
        when nothing was parseable/filterable.
    """
    field_set = set(fields)
    if not field_set:
        return value

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return value
        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            return value
        if not isinstance(parsed, dict):
            return value
        filtered = {k: v for k, v in parsed.items() if k not in field_set}
        if filtered == parsed:
            return value
        return json.dumps(filtered)

    if isinstance(value, dict):
        filtered = {k: v for k, v in value.items() if k not in field_set}
        return filtered

    if isinstance(value, list):
        new_blocks: list[Any] = []
        changed = False
        for block in value:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                new_text = _drop_json_fields(block["text"], fields)
                if new_text != block["text"]:
                    new_block = dict(block)
                    new_block["text"] = new_text
                    new_blocks.append(new_block)
                    changed = True
                    continue
            new_blocks.append(block)
        return new_blocks if changed else value

    return value


def compute_input_token_savings(
    request_payload: dict[str, Any],
    *,
    removed_tool_names: set[str] | None = None,
    filtered_output_fields: Mapping[str, Sequence[str]] | None = None,
) -> InputTokenSavings:
    """Compute the exact input-token delta of an applied optimization.

    "Original" is ``request_payload`` as-is. "Modified" is a deep copy with the
    candidate optimization applied:

        * Any tool definition in ``request_payload["tools"]`` whose resolved
          name is in ``removed_tool_names`` is removed.
        * For every tool-result message (``role == "tool"``), the JSON fields
          named in ``filtered_output_fields`` — keyed by the tool name the
          message reports for — are dropped from its content.

    Both sides are tokenized identically to the gateway
    (``json.dumps(..., default=str, sort_keys=True)`` then ``estimate_tokens``),
    so the result is deterministic and reproducible run-to-run. The caller's
    dict is never mutated.

    Args:
        request_payload: The stored gateway request payload (OpenAI chat shape).
        removed_tool_names: Tool names to strip from the ``tools`` list. ``None``
            or empty leaves tools untouched.
        filtered_output_fields: Mapping of tool name to the JSON field names to
            drop from that tool's result messages. ``None`` or empty leaves
            tool outputs untouched.

    Returns:
        An :class:`InputTokenSavings` describing the input-token delta.
    """
    original_tokens = _count_tokens(request_payload)

    modified = apply_candidate(
        request_payload,
        removed_tool_names=removed_tool_names,
        filtered_output_fields=filtered_output_fields,
    )

    modified_tokens = _count_tokens(modified)
    delta_tokens = original_tokens - modified_tokens
    pct_saved = delta_tokens / original_tokens if original_tokens else 0.0
    return InputTokenSavings(
        original_tokens=original_tokens,
        modified_tokens=modified_tokens,
        delta_tokens=delta_tokens,
        pct_saved=pct_saved,
    )


def apply_candidate(
    request_payload: dict[str, Any],
    *,
    removed_tool_names: set[str] | None = None,
    filtered_output_fields: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Return a deep copy of ``request_payload`` with the candidate applied.

    This is the single source of truth for "what the modified request is": the
    deterministic input-token measurement tokenizes its output, and the
    re-execution replay harness re-runs it upstream. The caller's dict is never
    mutated.

    Args:
        request_payload: The stored gateway request payload (OpenAI chat shape).
        removed_tool_names: Tool names to strip from ``tools``; ``None``/empty
            leaves tools untouched.
        filtered_output_fields: Mapping of tool name to JSON field names to drop
            from that tool's result messages; ``None``/empty is a no-op.

    Returns:
        A new payload dict with the optimization applied.
    """
    modified = copy.deepcopy(request_payload)
    _apply_tool_removal(modified, removed_tool_names)
    _apply_output_filtering(modified, filtered_output_fields)
    return modified


def _apply_tool_removal(
    payload: dict[str, Any], removed_tool_names: set[str] | None
) -> None:
    """Remove named tool definitions from ``payload["tools"]`` in place.

    Args:
        payload: The (already-copied) modifiable payload.
        removed_tool_names: Tool names to strip; ``None``/empty is a no-op.
    """
    if not removed_tool_names:
        return
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return
    payload["tools"] = [
        definition
        for definition in tools
        if tool_definition_name(definition) not in removed_tool_names
    ]


def _apply_output_filtering(
    payload: dict[str, Any],
    filtered_output_fields: Mapping[str, Sequence[str]] | None,
) -> None:
    """Drop configured JSON fields from tool-result messages in place.

    Args:
        payload: The (already-copied) modifiable payload.
        filtered_output_fields: Mapping of tool name to droppable field names;
            ``None``/empty is a no-op.
    """
    if not filtered_output_fields:
        return
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    tool_call_names = _tool_call_id_names(messages)
    for message in messages:
        if not isinstance(message, dict) or not _is_tool_result_message(message):
            continue
        tool_name = _resolve_tool_result_name(message, tool_call_names)
        fields = filtered_output_fields.get(tool_name)
        if not fields:
            continue
        message["content"] = _drop_json_fields(message.get("content"), fields)
