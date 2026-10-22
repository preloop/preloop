"""Keep stored model gateway call rows small.

Every model call sends the whole conversation so far, and the gateway event
payload used to store all of it on the ``runtime_session_activity`` row:
the raw request, the conversation preview and the tool entries recovered from
both. Call N therefore repeated calls 1..N-1, so a session's storage grew
with the square of its length. A long agent session stored hundreds of
kilobytes per model call, most of it history that an earlier row of the same
session already carried.

:func:`slim_gateway_payload` rewrites one payload into its stored form:

* The conversation preview keeps only the messages new to this call: the
  request messages after the last assistant message (what the caller added
  since the previous response), plus the response. The last user message is
  kept even when it is older, because the per-call views title a call by it.
  Every kept message records its original ``position``, the same convention
  the gateway-events endpoint already uses when it elides replayed history,
  and ``history_window_start`` / ``history_window_elided_count`` in the
  preview metadata say where the stored window begins. The console merges
  messages across calls by position, so a session still reads end to end.
* The raw ``request.messages`` and ``request.input`` arrays are cut the same
  way. Every other request field (model, tool definitions, parameters) stays.
* Tool entries (``tools`` and ``tool_activity``) recovered from replayed
  history are dropped when their call id is not in the stored window; the
  older row that first carried them still has them.
* If the result is still larger than ``max_bytes``, tool definitions are
  reduced to their names, then the raw message arrays are dropped (the
  preview keeps the text), then long strings are cut until it fits.

The function is pure and idempotent: a payload that already carries the
``body_compaction`` marker is returned unchanged. Live events published to the
console are not touched; only the stored row is slimmed.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from preloop.utils.jsonb_sanitize import sanitize_for_jsonb

COMPACTION_VERSION = 1
COMPACTION_KEY = "body_compaction"

# Keys of a stored gateway call that carry message content. Retention strips
# exactly these; everything else (tokens, cost, model, outcome) is metadata.
BODY_KEYS: tuple[str, ...] = (
    "conversation_preview",
    "tools",
    "tool_activity",
    "request",
    "response",
    "messages",
)

_MIN_STRING_CHARS = 256


def payload_bytes(payload: Any) -> int:
    """Return the size of ``payload`` serialized as compact JSON, in bytes."""
    return len(
        json.dumps(
            payload, ensure_ascii=False, default=str, separators=(",", ":")
        ).encode("utf-8")
    )


def _is_assistant_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if str(item.get("role") or "").lower() == "assistant":
        return True
    # Responses API output items replayed as input carry no role.
    return item.get("type") in ("function_call", "reasoning")


def _tail_start(items: list[Any]) -> int:
    """Index of the first item after the last assistant item (0 if none)."""
    for index in range(len(items) - 1, -1, -1):
        if _is_assistant_item(items[index]):
            return index + 1
    return 0


def _last_user_index(messages: list[Any]) -> Optional[int]:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if (
            isinstance(message, dict)
            and str(message.get("role") or "").lower() == "user"
            and str(message.get("text") or "").strip()
        ):
            return index
    return None


def _slim_preview(preview: Any) -> Any:
    if not isinstance(preview, dict):
        return preview
    messages = preview.get("messages")
    if not isinstance(messages, list):
        return preview
    request_indexes = [
        index
        for index, message in enumerate(messages)
        if not isinstance(message, dict) or message.get("source") != "response"
    ]
    request_messages = [messages[index] for index in request_indexes]
    tail = _tail_start(request_messages)
    window_start = request_indexes[tail] if tail < len(request_indexes) else None
    if window_start is None:
        # Every request message is history; the response starts the window.
        window_start = next(
            (
                index
                for index, message in enumerate(messages)
                if isinstance(message, dict) and message.get("source") == "response"
            ),
            len(messages),
        )
    keep_user = _last_user_index(messages)
    kept: list[Any] = []
    elided = 0
    for index, message in enumerate(messages):
        if index < window_start and index != keep_user:
            elided += 1
            continue
        if isinstance(message, dict) and "position" not in message:
            message = {**message, "position": index}
        kept.append(message)
    metadata = dict(preview.get("metadata") or {})
    metadata["history_window_start"] = window_start
    metadata["history_window_elided_count"] = elided
    return {**preview, "messages": kept, "metadata": metadata}


def _call_ids(items: Any) -> set[str]:
    ids: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, str) and value:
            ids.add(value)

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(value, list):
            for item in value:
                walk(item, depth + 1)
        elif isinstance(value, dict):
            for key in ("call_id", "tool_call_id", "tool_use_id", "id"):
                add(value.get(key))
            for key in ("tool_calls", "content"):
                walk(value.get(key), depth + 1)

    walk(items)
    return ids


def _slim_request(request: Any) -> tuple[Any, dict[str, int]]:
    if not isinstance(request, dict):
        return request, {}
    starts: dict[str, int] = {}
    slimmed = dict(request)
    for key in ("messages", "input"):
        items = request.get(key)
        if isinstance(items, list):
            start = _tail_start(items)
            if start:
                slimmed[key] = items[start:]
            starts[key] = start
    return slimmed, starts


def _keep_tool_entry(entry: Any, *, id_key: str, live_ids: set[str]) -> bool:
    if not isinstance(entry, dict):
        return True
    if entry.get("source") == "response":
        return True
    call_id = entry.get(id_key)
    if not isinstance(call_id, str) or not call_id:
        return True
    return call_id in live_ids


def _tool_names(tools: list[Any]) -> list[dict[str, Any]]:
    names: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        name = function.get("name") if isinstance(function, dict) else tool.get("name")
        names.append({"name": name, "type": tool.get("type")})
    return names


def is_slim(payload: Any) -> bool:
    """Return True when ``payload`` is already in its stored, slim form."""
    return isinstance(payload, dict) and isinstance(payload.get(COMPACTION_KEY), dict)


def slim_gateway_payload(payload: Any, *, max_bytes: int) -> Any:
    """Return the stored form of one model gateway call payload.

    Args:
        payload: The ``model_gateway_call`` event payload (or a stored row's
            metadata). Not mutated.
        max_bytes: Upper bound for the serialized result. ``0`` or less keeps
            the history slimming but applies no byte cap.

    Returns:
        The slim payload, carrying a ``body_compaction`` marker that records
        what was removed. Non-dict input and already slim payloads are
        returned unchanged.
    """
    if not isinstance(payload, dict) or is_slim(payload):
        return payload
    original_bytes = payload_bytes(payload)
    result = dict(payload)
    marker: dict[str, Any] = {"version": COMPACTION_VERSION}

    request, starts = _slim_request(payload.get("request"))
    if "request" in payload:
        result["request"] = request
    if starts:
        marker["request_history_start"] = starts
    if "conversation_preview" in payload:
        result["conversation_preview"] = _slim_preview(payload["conversation_preview"])

    live_ids = _call_ids(request.get("messages") if isinstance(request, dict) else None)
    live_ids |= _call_ids(request.get("input") if isinstance(request, dict) else None)
    live_ids |= _call_ids(payload.get("response"))
    tools = payload.get("tools")
    if isinstance(tools, list):
        result["tools"] = [
            entry
            for entry in tools
            if _keep_tool_entry(entry, id_key="call_id", live_ids=live_ids)
        ]
    activity = payload.get("tool_activity")
    if isinstance(activity, dict) and isinstance(activity.get("entries"), list):
        result["tool_activity"] = {
            **activity,
            "entries": [
                entry
                for entry in activity["entries"]
                if _keep_tool_entry(entry, id_key="id", live_ids=live_ids)
            ],
        }

    if max_bytes > 0:
        result = _fit(result, marker, max_bytes=max_bytes)
    marker["original_bytes"] = original_bytes
    result[COMPACTION_KEY] = marker
    marker["stored_bytes"] = payload_bytes(result)
    return result


def _fit(result: dict[str, Any], marker: dict[str, Any], *, max_bytes: int) -> Any:
    if payload_bytes(result) <= max_bytes:
        return result
    request = result.get("request")
    if isinstance(request, dict) and isinstance(request.get("tools"), list):
        result["request"] = {**request, "tools": _tool_names(request["tools"])}
        marker["tool_definitions_omitted"] = True
        if payload_bytes(result) <= max_bytes:
            return result
    request = result.get("request")
    if isinstance(request, dict) and any(k in request for k in ("messages", "input")):
        result["request"] = {
            key: value
            for key, value in request.items()
            if key not in ("messages", "input")
        }
        marker["request_messages_omitted"] = True
        if payload_bytes(result) <= max_bytes:
            return result
    limit = max(_MIN_STRING_CHARS, max_bytes // 4)
    while True:
        candidate = sanitize_for_jsonb(result, max_string_chars=limit)
        if payload_bytes(candidate) <= max_bytes or limit <= _MIN_STRING_CHARS:
            marker["strings_capped_at"] = limit
            return candidate
        limit = max(_MIN_STRING_CHARS, limit // 2)


def strip_gateway_bodies(payload: Any) -> Any:
    """Return ``payload`` with every content key removed (retention).

    Token counts, cost, model, outcome and timing survive, so usage reporting
    over an old session still adds up; the messages themselves are gone.
    Idempotent.
    """
    if not isinstance(payload, dict):
        return payload
    marker = payload.get(COMPACTION_KEY)
    if isinstance(marker, dict) and marker.get("bodies_stripped"):
        return payload
    result = {key: value for key, value in payload.items() if key not in BODY_KEYS}
    result[COMPACTION_KEY] = {
        **(marker if isinstance(marker, dict) else {"version": COMPACTION_VERSION}),
        "bodies_stripped": True,
    }
    return result
