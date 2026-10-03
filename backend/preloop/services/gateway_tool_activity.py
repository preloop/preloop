"""Structured tool activity recovered from captured gateway request/response bodies.

The live console needs to answer "which tool is running, with what arguments,
and did it work" without re-deriving it from prose. A gateway event carries the
capped raw request/response bodies and a conversation preview that flattens
everything to text, so the structure is *there* but not *addressable*: two
identical calls are indistinguishable, an Anthropic ``tool_result`` looks like a
user turn, and a call with no result looks the same as one that never ran.

This module normalizes the three wire dialects the gateway accepts into one
entry list, emitted on the event as ``payload.tool_activity``:

  * OpenAI Chat Completions — ``message.tool_calls[]`` and ``role="tool"``
    messages carrying ``tool_call_id``.
  * OpenAI Responses — ``function_call`` / ``function_call_output`` items
    carrying ``call_id``.
  * Anthropic Messages — ``tool_use`` / ``tool_result`` content blocks
    correlated by ``id`` / ``tool_use_id``.

Design rules that the console depends on:

  * **Identity is never fabricated.** Every one of the three dialects stamps a
    provider id on both halves of a call, so that id is the entry identity. An
    entry whose provider id is missing keeps ``id = None`` and is flagged
    ``stable_id: False``; a consumer keys it by its own position instead of
    merging it with anything.
  * **Arguments and results are content.** When the gateway's capture policy is
    off, or a sanitizer already replaced the value with its redaction marker,
    the entry keeps the tool NAME and the ids but carries no argument/result
    text and sets ``redacted``. Capture policy is a promise about content, and
    this module is downstream of it, never around it.
  * **Bounded.** The request body replays the whole conversation history, so an
    unbounded extractor would re-emit every call the session ever made on every
    event. Entries are capped and ``truncated`` says so.
  * **Pure.** No database, no settings lookups beyond the flag the caller
    passes in, no prose scraping: only the documented structured fields are
    read.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Optional

#: Cap on entries per event. The request half of the payload is accumulated
#: history, so this bounds the worst case (a long session replayed on every
#: event) rather than a single turn's parallel calls.
MAX_TOOL_ACTIVITY_ENTRIES = 60
#: Cap on the rendered arguments preview. Tool arguments are the deciding
#: detail when an operator is deciding whether to approve, so this is generous
#: compared with the result preview.
MAX_TOOL_ARGUMENT_CHARS = 800
#: Cap on the rendered result preview.
MAX_TOOL_RESULT_CHARS = 1500
#: Cap on list/array items walked while flattening an arguments object.
MAX_NESTED_ITEMS = 8
#: Cap on nesting depth walked while flattening arguments/result structures.
MAX_NESTED_DEPTH = 4

#: Marker the gateway sanitizer substitutes when content capture is disabled.
REDACTION_MARKER = "***REDACTED***"

_TEXT_BLOCK_TYPES = frozenset({"input_text", "output_text", "text"})
_REDACTED = True
_NOT_REDACTED = False


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "… [truncated]", True


def _render_value(value: Any, depth: int = 0) -> str:
    """Flatten one arguments/result value to displayable text.

    Strings pass through untouched so a JSON-encoded argument string keeps its
    formatting; everything else is rendered as bounded JSON so a structured
    argument object is readable rather than Python's ``{'a': 1}``. Malformed
    or unrepresentable values fall back to ``str`` — never an exception, and
    never ``eval``.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if depth >= MAX_NESTED_DEPTH:
        return _truncate(str(value), 200)[0]
    try:
        return json.dumps(value, indent=2, default=str, sort_keys=False)
    except (TypeError, ValueError):
        return _truncate(str(value), 200)[0]


def _parse_json_text(text: str) -> tuple[str, bool]:
    """Pretty-print valid JSON text; keep anything else readable as-is.

    A gateway argument field is a JSON *string* on two of the three dialects.
    Pretty-printing it is the difference between ``{"path": "src"}`` and an
    escaped one-liner, and a truncated argument string is not valid JSON — that
    case must render as text rather than raise.
    """
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return text, False
    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError):
        return text, False
    if isinstance(parsed, str):
        # A JSON string literal: the inner value is what the operator reads.
        return parsed, False
    return json.dumps(parsed, indent=2, default=str), False


def _content_to_text(value: Any, depth: int = 0) -> str:
    """Flatten message/tool-result content to text, bounded.

    Mirrors the gateway emitter's own preview flattening so the two views of
    the same body agree on what the text is.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        if depth >= MAX_NESTED_DEPTH:
            return ""
        parts = [
            _content_to_text(item, depth + 1) for item in list(value)[:MAX_NESTED_ITEMS]
        ]
        return "\n".join(part for part in parts if part)
    if isinstance(value, Mapping):
        block_type = str(value.get("type") or "")
        if block_type in _TEXT_BLOCK_TYPES and value.get("text") is not None:
            return str(value.get("text"))
        if value.get("content") is not None:
            return _content_to_text(value.get("content"), depth + 1)
        if value.get("text") is not None:
            return str(value.get("text"))
        if value.get("output") is not None:
            return _content_to_text(value.get("output"), depth + 1)
        return ""
    return str(value)


def _bound_arguments(raw: Any) -> tuple[Optional[str], bool]:
    """Render tool arguments, honouring the caller's redactor."""
    if raw is None:
        return None, False
    text = _render_value(raw)
    if isinstance(raw, str):
        text, _ = _parse_json_text(text)
    if not text.strip():
        return None, False
    text, truncated = _truncate(text, MAX_TOOL_ARGUMENT_CHARS)
    return text, truncated


def _bound_result(raw: Any) -> tuple[Optional[str], bool]:
    text = _content_to_text(raw)
    if not text.strip():
        return None, False
    return _truncate(text, MAX_TOOL_RESULT_CHARS)


class _Collector:
    """Accumulates normalized entries, de-duplicating exact identities only.

    Entries are keyed by ``(direction, provider_id)`` when a provider id
    exists. Two calls that happen to carry identical arguments are different
    calls and both survive; a call and its result never merge because the
    direction is part of the key.
    """

    def __init__(
        self,
        *,
        capture_content: bool,
        redact_text: Optional[Callable[[str], str]],
    ) -> None:
        self.capture_content = capture_content
        self.redact_text = redact_text
        self.entries: list[dict[str, Any]] = []
        self._seen: set[tuple[str, str]] = set()
        self.dropped = 0

    def _guard_text(self, text: str) -> tuple[str, bool]:
        """Apply the redactor, then refuse to emit text capture withheld.

        Two different things produce a redaction marker here and they mean
        different things:

        * The sanitizer replaced the WHOLE value with the marker, because
          content capture is off or the field was classified as content. There
          is nothing left to show, so the entry keeps its name and ids and
          reports ``redacted``.
        * The secret redactor matched INSIDE the value. The rest of the
          arguments are still the deciding detail for an operator reading the
          card, so the text is kept with the secret replaced — the same
          treatment the conversation preview gets.
        """
        if not text:
            return text, _NOT_REDACTED
        if text.strip() == REDACTION_MARKER:
            return "", _REDACTED
        if self.redact_text is not None:
            text = self.redact_text(text)
        if text.strip() == REDACTION_MARKER:
            return "", _REDACTED
        if not self.capture_content:
            return "", _REDACTED
        return text, _NOT_REDACTED

    def add(
        self,
        *,
        direction: str,
        call_id: Any = None,
        name: Optional[str] = None,
        arguments: Any = None,
        result: Any = None,
        is_error: Optional[bool] = None,
        dialect: str,
    ) -> None:
        provider_id = str(call_id) if call_id not in (None, "") else None
        if provider_id is not None:
            key = (direction, provider_id)
            if key in self._seen:
                # A history replay of an entry this event already carries.
                return
            self._seen.add(key)

        redacted = _NOT_REDACTED
        truncated = False
        argument_text: Optional[str] = None
        result_text: Optional[str] = None

        if arguments is not None:
            rendered, was_truncated = _bound_arguments(arguments)
            truncated = truncated or was_truncated
            argument_text, argument_redacted = self._guard_text(rendered or "")
            redacted = redacted or argument_redacted
            if not argument_text:
                argument_text = None
        if result is not None:
            rendered, was_truncated = _bound_result(result)
            truncated = truncated or was_truncated
            result_text, result_redacted = self._guard_text(rendered or "")
            redacted = redacted or result_redacted
            if not result_text:
                result_text = None

        if len(self.entries) >= MAX_TOOL_ACTIVITY_ENTRIES:
            self.dropped += 1
            return

        self.entries.append(
            {
                "id": provider_id,
                "stable_id": provider_id is not None,
                "direction": direction,
                "name": str(name) if name else None,
                "dialect": dialect,
                "arguments": argument_text,
                "result": result_text,
                "is_error": is_error,
                "redacted": redacted,
                "truncated": truncated,
            }
        )


def _openai_chat_tool_calls(message: Any, collector: _Collector, dialect: str) -> None:
    """``{"id": ..., "type": "function", "function": {"name", "arguments"}}``."""
    if not isinstance(message, Mapping):
        return
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        function = call.get("function")
        if not isinstance(function, Mapping):
            continue
        name = function.get("name")
        collector.add(
            direction="call",
            call_id=call.get("id"),
            name=str(name) if name else None,
            arguments=function.get("arguments"),
            dialect=dialect,
        )


def _openai_chat_tool_result(message: Any, collector: _Collector, dialect: str) -> None:
    """``{"role": "tool", "tool_call_id": ..., "name": ..., "content": ...}``."""
    if not isinstance(message, Mapping):
        return
    collector.add(
        direction="result",
        call_id=message.get("tool_call_id"),
        name=str(message.get("name")) if message.get("name") else None,
        result=message.get("content"),
        dialect=dialect,
    )


def _anthropic_blocks(content: Any, collector: _Collector) -> None:
    """Content-block lists carrying ``tool_use`` and ``tool_result``."""
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, Mapping):
            continue
        block_type = str(block.get("type") or "").lower()
        if block_type == "tool_use":
            name = block.get("name")
            collector.add(
                direction="call",
                call_id=block.get("id"),
                name=str(name) if name else None,
                arguments=block.get("input"),
                dialect="anthropic",
            )
        elif block_type == "tool_result":
            collector.add(
                direction="result",
                call_id=block.get("tool_use_id"),
                result=block.get("content"),
                is_error=bool(block.get("is_error")) or None,
                dialect="anthropic",
            )


def _responses_items(items: Any, collector: _Collector) -> None:
    """``function_call`` / ``function_call_output`` / nested ``output`` lists."""
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, Mapping):
            continue
        item_type = str(item.get("type") or "").lower()
        if item_type in {"function_call", "tool_call", "custom_tool_call"}:
            name = item.get("name")
            arguments = item.get("arguments")
            if arguments is None:
                arguments = item.get("input")
            collector.add(
                direction="call",
                call_id=item.get("call_id") or item.get("id"),
                name=str(name) if name else None,
                arguments=arguments,
                dialect="openai_responses",
            )
        elif item_type == "function_call_output":
            collector.add(
                direction="result",
                call_id=item.get("call_id") or item.get("id"),
                result=item.get("output"),
                dialect="openai_responses",
            )
        elif item_type in {"message", "item"}:
            _responses_items(item.get("output") or item.get("content"), collector)


def _scan_message_list(messages: Any, collector: _Collector) -> None:
    """One pass over a Chat Completions / Messages ``messages`` array."""
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role") or "").lower()
        if role == "tool" or str(message.get("type") or "").lower() == "tool_result":
            _openai_chat_tool_result(message, collector, "openai_chat")
            continue
        _openai_chat_tool_calls(message, collector, "openai_chat")
        _anthropic_blocks(message.get("content"), collector)


def _scan_response_payload(payload: Any, collector: _Collector) -> None:
    """The completion for THIS request; its calls are not history repeats."""
    if not _is_mapping(payload):
        return
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, Mapping):
                _openai_chat_tool_calls(choice.get("message"), collector, "openai_chat")
    _responses_items(payload.get("output"), collector)
    _anthropic_blocks(payload.get("content"), collector)
    # Single-object Responses shape: {"type": "function_call", ...}.
    if str(payload.get("type") or "").lower() in {
        "function_call",
        "tool_call",
        "custom_tool_call",
    }:
        _responses_items([payload], collector)


def _scan_request_payload(payload: Any, collector: _Collector) -> None:
    """The accumulated history; tool results here may name no tool at all."""
    if not _is_mapping(payload):
        return
    _scan_message_list(payload.get("messages"), collector)
    _responses_items(payload.get("input"), collector)


def normalize_tool_activity(
    *,
    request_payload: Any,
    response_payload: Any,
    capture_content: bool,
    redact_text: Optional[Callable[[str], str]] = None,
) -> Optional[dict[str, Any]]:
    """Normalize one gateway exchange into structured tool activity.

    Args:
        request_payload: The (already sanitized) request body. Accumulated
            history: tool calls and results replayed from earlier turns.
        response_payload: The (already sanitized) response body for this
            request. Its tool calls belong to this turn only.
        capture_content: Whether the gateway's capture policy permits message
            content at all. When false, arguments and results are withheld
            and every entry is flagged ``redacted``.
        redact_text: Optional secret redactor applied to extracted text.
            Applied AFTER the sanitizer, so a secret the sanitizer missed
            because it was nested inside a tool argument is still caught.

    Returns:
        ``None`` when neither body carries any tool structure (so the event
        payload stays as small as it was), otherwise
        ``{"entries": [...], "truncated": bool, "dialect": str | None}``.

    The response is scanned first so the ``tool_activity`` list reads in the
    order the turn happened: this request's call, then the history that led to
    it. Anthropic names are back-filled onto results that arrive before their
    ``tool_use`` block in the same exchange.
    """
    collector = _Collector(capture_content=capture_content, redact_text=redact_text)
    _scan_response_payload(response_payload, collector)
    _scan_request_payload(request_payload, collector)

    if not collector.entries:
        return None

    # Name results whose tool_use block was not in this exchange. A second pass
    # (rather than remembering during the scan) keeps the collector's append
    # order, which is the chronological order the console renders.
    known_names: dict[str, str] = {}
    for entry in collector.entries:
        if entry["direction"] == "call" and entry["id"] and entry["name"]:
            known_names.setdefault(entry["id"], entry["name"])
    if known_names:
        for entry in collector.entries:
            if entry["direction"] == "result" and entry["id"] and not entry["name"]:
                entry["name"] = known_names.get(entry["id"])

    dialects = {entry["dialect"] for entry in collector.entries}
    return {
        "entries": collector.entries,
        "truncated": collector.dropped > 0,
        "dialect": next(iter(dialects)) if len(dialects) == 1 else None,
    }
