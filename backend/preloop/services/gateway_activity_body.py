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
* If the result is still larger than ``max_bytes``, it is shrunk without
  ever dropping the response, the messages this call added or the last user
  message, and without removing the ``request`` or ``response`` keys: tool
  definitions go to names, ``tool_activity`` and ``tools`` entries are
  dropped, then long strings are cut in the middle (head and tail kept,
  with a ``[... truncated N chars ...]`` marker), request context first,
  then the new messages longest first, and only then the response. See
  :func:`_fit` for the full order. ``body_compaction`` records every step.

The function is pure and idempotent: a payload that already carries the
``body_compaction`` marker is returned unchanged. Live events published to the
console are not touched; only the stored row is slimmed.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Optional

from preloop.config import settings

COMPACTION_VERSION = 2
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

# Bytes kept free for the ``body_compaction`` marker itself.
_MARKER_RESERVE = 1024
# Middle truncation marker. Readers detect it (``TRUNCATION_MARKER_RE`` in the
# console) and show a "Truncated" indicator next to the text.
_MIDDLE_MARKER = "\n[... truncated {dropped} chars ...]\n"
DEFAULT_KEEP_CHARS = 2048
# The response keeps a larger head than any other string: it is the one part
# of a call no earlier or later row repeats.
_RESPONSE_HEAD_FACTOR = 4
_MIN_KEEP_CHARS = 64
_DELTA_KEYS = ("messages", "input")


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


def slim_gateway_payload(
    payload: Any, *, max_bytes: int, keep_chars: Optional[int] = None
) -> Any:
    """Return the stored form of one model gateway call payload.

    Args:
        payload: The ``model_gateway_call`` event payload (or a stored row's
            metadata). Not mutated.
        max_bytes: Upper bound for the serialized result. ``0`` or less keeps
            the history slimming but applies no byte cap.
        keep_chars: Characters kept at each end of a string cut in the
            middle to fit the cap. Defaults to
            ``MODEL_GATEWAY_ACTIVITY_TRUNCATE_KEEP_CHARS``.

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
        result = _fit(
            result,
            marker,
            max_bytes=max(1, max_bytes - _MARKER_RESERVE),
            keep_chars=(
                settings.model_gateway_activity_truncate_keep_chars
                if keep_chars is None
                else keep_chars
            ),
        )
    marker["original_bytes"] = original_bytes
    marker["stored_bytes"] = 0
    result[COMPACTION_KEY] = marker
    # The size includes its own digits; settle it (two rounds at most).
    for _ in range(3):
        stored = payload_bytes(result)
        if marker["stored_bytes"] == stored:
            break
        marker["stored_bytes"] = stored
    return result


def truncate_middle(value: str, *, head: int, tail: int) -> str:
    """Return ``value`` with its middle replaced by a truncation marker.

    Args:
        value: The string to shorten.
        head: Characters kept from the start.
        tail: Characters kept from the end.

    Returns:
        ``value`` unchanged when the marker would not make it shorter,
        otherwise head, marker and tail.
    """
    dropped = len(value) - head - tail
    marker = _MIDDLE_MARKER.format(dropped=dropped)
    if dropped <= len(marker):
        return value
    return value[:head] + marker + (value[len(value) - tail :] if tail else "")


class _Slot:
    """One string leaf inside a payload, addressable for in-place rewrite."""

    __slots__ = ("container", "key", "preview_message")

    def __init__(
        self,
        container: Any,
        key: Any,
        preview_message: Optional[dict[str, Any]] = None,
    ) -> None:
        self.container = container
        self.key = key
        self.preview_message = preview_message

    @property
    def value(self) -> str:
        return self.container[self.key]


def _string_slots(
    value: Any, *, preview_message: Optional[dict[str, Any]] = None
) -> list[_Slot]:
    slots: list[_Slot] = []
    stack: list[Any] = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            items: Any = node.items()
        elif isinstance(node, list):
            items = enumerate(node)
        else:
            continue
        for key, item in items:
            if isinstance(item, str):
                slots.append(_Slot(node, key, preview_message))
            elif isinstance(item, (dict, list)):
                stack.append(item)
    return slots


def _preview_slots(result: dict[str, Any], *, response: bool) -> list[_Slot]:
    preview = result.get("conversation_preview")
    if not isinstance(preview, dict) or not isinstance(preview.get("messages"), list):
        return []
    slots: list[_Slot] = []
    for message in preview["messages"]:
        if not isinstance(message, dict):
            continue
        if (message.get("source") == "response") != response:
            continue
        if isinstance(message.get("text"), str):
            slots.append(_Slot(message, "text", message))
    return slots


def _json_len(value: str) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _shorten(
    slots: list[_Slot],
    *,
    size: int,
    max_bytes: int,
    head: int,
    tail: int,
    stats: dict[str, int],
) -> int:
    """Middle-truncate ``slots``, longest first, until ``size`` fits.

    Returns the new estimated size. Each rewrite updates the estimate by the
    exact change in that string's JSON encoding, so no full re-serialization
    is needed per string.
    """
    floor = head + tail + len(_MIDDLE_MARKER) + 8
    candidates = [slot for slot in slots if len(slot.value) > floor]
    candidates.sort(key=lambda slot: len(slot.value), reverse=True)
    for slot in candidates:
        if size <= max_bytes:
            break
        before = slot.value
        after = truncate_middle(before, head=head, tail=tail)
        if after == before:
            continue
        slot.container[slot.key] = after
        size += _json_len(after) - _json_len(before)
        stats["strings"] = stats.get("strings", 0) + 1
        stats["chars_removed"] = stats.get("chars_removed", 0) + (
            len(before) - len(after)
        )
        if slot.preview_message is not None:
            message = slot.preview_message
            message["truncated"] = True
            message.setdefault("original_length", len(before))
    return size


def _record(
    marker: dict[str, Any], key: str, stats: dict[str, int], **extra: Any
) -> None:
    if stats.get("strings"):
        marker[key] = {**stats, **extra}


def _delta_slots(result: dict[str, Any]) -> list[_Slot]:
    slots = _preview_slots(result, response=False)
    request = result.get("request")
    if isinstance(request, dict):
        for key in _DELTA_KEYS:
            if isinstance(request.get(key), list):
                slots.extend(_string_slots(request[key]))
    return slots


def _response_slots(result: dict[str, Any]) -> list[_Slot]:
    slots = _preview_slots(result, response=True)
    slots.extend(_string_slots(result.get("response")))
    return slots


def _request_context_slots(result: dict[str, Any]) -> list[_Slot]:
    """Strings of the request outside its new messages (system prompt etc.)."""
    request = result.get("request")
    if not isinstance(request, dict):
        return []
    slots: list[_Slot] = []
    for key, value in request.items():
        if key in _DELTA_KEYS:
            continue
        if isinstance(value, str):
            slots.append(_Slot(request, key))
        else:
            slots.extend(_string_slots(value))
    return slots


def _other_slots(result: dict[str, Any]) -> list[_Slot]:
    """Strings outside request, response and preview (accounting excluded)."""
    slots: list[_Slot] = []
    for key, value in result.items():
        if key in ("request", "response", "conversation_preview", COMPACTION_KEY):
            continue
        if isinstance(value, (dict, list)):
            slots.extend(_string_slots(value))
    return slots


def _elide_delta_messages(result: dict[str, Any], marker: dict[str, Any]) -> None:
    """Keep only the newest delta message and the last user message.

    Reached only when the delta has so many messages that their shortest
    form still exceeds the cap. The request and response keys stay.
    """
    elided = 0
    request = result.get("request")
    if isinstance(request, dict):
        for key in _DELTA_KEYS:
            items = request.get(key)
            if isinstance(items, list) and len(items) > 1:
                elided += len(items) - 1
                request[key] = items[-1:]
    preview = result.get("conversation_preview")
    if isinstance(preview, dict) and isinstance(preview.get("messages"), list):
        messages = preview["messages"]
        keep_user = _last_user_index(messages)
        kept = []
        last_request = max(
            (
                index
                for index, message in enumerate(messages)
                if not isinstance(message, dict) or message.get("source") != "response"
            ),
            default=None,
        )
        for index, message in enumerate(messages):
            is_response = (
                isinstance(message, dict) and message.get("source") == "response"
            )
            if is_response or index in (keep_user, last_request):
                kept.append(message)
            else:
                elided += 1
        preview["messages"] = kept
    marker["delta_messages_elided"] = elided


def _cut_harder(
    result: dict[str, Any],
    slots: Any,
    *,
    max_bytes: int,
    keep: int,
    stats: dict[str, int],
) -> int:
    """Halve the kept ends of ``slots()`` strings until the result fits.

    Returns the last ``keep`` used (64 at the least).
    """
    size = payload_bytes(result)
    while size > max_bytes and keep > _MIN_KEEP_CHARS:
        keep = max(_MIN_KEEP_CHARS, keep // 2)
        _shorten(
            slots(),
            size=size,
            max_bytes=max_bytes,
            head=keep,
            tail=keep,
            stats=stats,
        )
        size = payload_bytes(result)
    return keep


def _fit(
    result: dict[str, Any],
    marker: dict[str, Any],
    *,
    max_bytes: int,
    keep_chars: int = DEFAULT_KEEP_CHARS,
) -> Any:
    """Shrink ``result`` until it serializes to at most ``max_bytes``.

    The model response, the messages this call added and the last user
    message are never dropped, and the ``request`` and ``response`` keys are
    never removed. Steps, cheapest loss first:

    1. Tool definitions are reduced to their names.
    2. ``tool_activity`` is dropped (it is derived from request and response).
    3. The ``tools`` entries are dropped (also derived), and long strings of
       the request outside its new messages (system prompt, instructions) are
       cut in the middle.
    4. Long strings of the new messages are cut in the middle, longest first
       (large tool outputs and file contents), keeping ``keep_chars`` at
       each end.
    5. Long strings of the response are cut in the middle, keeping a head of
       ``4 * keep_chars``.
    6. Only if all of that is not enough (a cap far below the call's own
       size): the strings of step 3 and 4 are cut harder, halving the kept
       ends down to 64 characters; then the new messages are reduced to the
       newest one plus the last user message; then the response is cut
       harder. A cap smaller than the bare structure replaces the request
       and response contents with ``{"omitted_for_size": true}``.

    ``body_compaction`` records each step that changed something.
    """
    size = payload_bytes(result)
    if size <= max_bytes:
        return result
    result = copy.deepcopy(result)
    keep = max(_MIN_KEEP_CHARS, keep_chars)

    request = result.get("request")
    if isinstance(request, dict) and isinstance(request.get("tools"), list):
        request["tools"] = _tool_names(request["tools"])
        marker["tool_definitions_omitted"] = True
        size = payload_bytes(result)
        if size <= max_bytes:
            return result

    omitted: list[str] = []
    for key in ("tool_activity", "tools"):
        if key in result and size > max_bytes:
            del result[key]
            omitted.append(key)
            marker["keys_omitted"] = omitted
            size = payload_bytes(result)
    if size <= max_bytes:
        return result

    stats: dict[str, int] = {}
    size = _shorten(
        _request_context_slots(result) + _other_slots(result),
        size=size,
        max_bytes=max_bytes,
        head=keep,
        tail=keep,
        stats=stats,
    )
    _record(marker, "request_context_shortened", stats, keep_chars=keep)
    size = payload_bytes(result)
    if size <= max_bytes:
        return result

    stats = {}
    size = _shorten(
        _delta_slots(result),
        size=size,
        max_bytes=max_bytes,
        head=keep,
        tail=keep,
        stats=stats,
    )
    _record(marker, "messages_shortened", stats, keep_chars=keep)
    size = payload_bytes(result)
    if size <= max_bytes:
        return result

    stats = {}
    response_head = keep * _RESPONSE_HEAD_FACTOR
    size = _shorten(
        _response_slots(result),
        size=size,
        max_bytes=max_bytes,
        head=response_head,
        tail=keep // 2,
        stats=stats,
    )
    _record(marker, "response_shortened", stats, head_chars=response_head)
    size = payload_bytes(result)
    if size <= max_bytes:
        return result

    # Last resorts for pathological rows. The response is touched last.
    stats = {}
    keep = _cut_harder(
        result,
        lambda: _request_context_slots(result)
        + _other_slots(result)
        + _delta_slots(result),
        max_bytes=max_bytes,
        keep=keep,
        stats=stats,
    )
    _record(marker, "messages_shortened_further", stats, keep_chars=keep)
    if payload_bytes(result) <= max_bytes:
        return result

    _elide_delta_messages(result, marker)
    if payload_bytes(result) <= max_bytes:
        return result

    stats = {}
    keep = _cut_harder(
        result,
        lambda: _response_slots(result),
        max_bytes=max_bytes,
        keep=response_head,
        stats=stats,
    )
    _record(marker, "response_shortened_further", stats, head_chars=keep)
    if payload_bytes(result) <= max_bytes:
        return result
    # Only a cap smaller than the structure itself gets here. Keep the keys
    # so readers can tell the content was removed, not absent.
    for key in ("request", "response"):
        if key in result:
            result[key] = {"omitted_for_size": True}
    result.pop("conversation_preview", None)
    marker["content_omitted_for_size"] = True
    return result


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
