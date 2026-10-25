"""Drop replayed conversation history from a page of gateway events.

Every model call stores the conversation it sent, so call N repeats calls
1..N-1. A page of 25 events on a long session carried the same history 25
times (1.7 MB on prod for one page, #1586). The console already renders each
message once: it keys messages by ``(position, role, text)`` and skips one it
has seen (``utils/transcript.ts``, "growing-prefix delta"). This module does
the same on the server, so the repeats never cross the wire.

An elided message is left out, and every kept message carries its original
``position`` in the event's message list, since position is part of that key. Each event keeps its
last user message, the request the per-event views title it by. Tool entries
are dropped when an older event on the page already carried the same call id,
kind and content: the console merges tools by call id, so the first copy is
enough. The full payload of one event stays available from the event detail
endpoint.
"""

from __future__ import annotations

import json
from typing import Any


def _message_signature(index: int, message: dict[str, Any]) -> str | None:
    text = str(message.get("text") or "").strip()
    if not text:
        return None
    role = str(message.get("role") or "message").lower()
    return f"{index}::{role}::{text}"


def _tool_signature(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    call_id = entry.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return None
    return json.dumps(
        [call_id, entry.get("kind"), entry.get("name"), entry.get("text")],
        default=str,
    )


def _tool_activity_signature(entry: Any) -> str | None:
    """Identity of one ``tool_activity`` entry, or None when it has none.

    Only entries with the provider's call id are elided, and only when their
    content is identical: the console merges entries by that id across the
    events of a page (``normalizeToolCalls``), so a repeat adds nothing.
    """
    if not isinstance(entry, dict):
        return None
    call_id = entry.get("id")
    if not isinstance(call_id, str) or not call_id.strip():
        return None
    return json.dumps(
        [
            call_id,
            entry.get("direction"),
            entry.get("name"),
            entry.get("arguments"),
            entry.get("result"),
            entry.get("is_error"),
            entry.get("redacted"),
            entry.get("truncated"),
        ],
        default=str,
    )


def _last_user_index(messages: list[Any]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if (
            isinstance(message, dict)
            and str(message.get("role") or "").lower() == "user"
            and str(message.get("text") or "").strip()
        ):
            return index
    return None


def elide_replayed_history(
    events: list[dict[str, Any]], window: int | None = None
) -> list[dict[str, Any]]:
    """Return ``events`` with history already carried by an older event elided.

    ``events`` are ``{"id", "timestamp", "type", "payload"}`` dicts in any
    order. Payloads are copied before they are changed; the input is not
    mutated. The order of the returned list matches the input.

    Args:
        events: The page of gateway events.
        window: When set, each event keeps at most its last ``window``
            messages and tool entries (plus its last user message). Without
            it the oldest event on the page carries the whole conversation,
            which on a 400-call session is megabytes (#1689). Older turns
            stay reachable through earlier pages and the event detail
            endpoint; ``history_window_start`` in the preview metadata says
            where the kept messages begin.

    Returns:
        The events with replayed history elided.
    """
    seen_messages: set[str] = set()
    seen_tools: set[str] = set()
    seen_activity: set[str] = set()
    oldest_first = sorted(
        range(len(events)),
        key=lambda i: (str(events[i].get("timestamp") or ""), i),
    )
    result: list[dict[str, Any]] = list(events)
    for position in oldest_first:
        event = events[position]
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        new_payload = dict(payload)
        elided = 0
        window_elided = 0

        preview = payload.get("conversation_preview")
        messages = preview.get("messages") if isinstance(preview, dict) else None
        if isinstance(messages, list):
            keep_index = _last_user_index(messages)
            window_start = max(0, len(messages) - window) if window is not None else 0
            new_messages: list[Any] = []
            for index, message in enumerate(messages):
                if not isinstance(message, dict):
                    continue
                if index < window_start and index != keep_index:
                    window_elided += 1
                    continue
                # Stored rows keep only their own window and record each
                # message's original position; that position is the key.
                stored_position = message.get("position")
                signature = _message_signature(
                    stored_position if isinstance(stored_position, int) else index,
                    message,
                )
                if (
                    signature is not None
                    and signature in seen_messages
                    and index != keep_index
                ):
                    elided += 1
                    continue
                if signature is not None:
                    seen_messages.add(signature)
                new_messages.append({"position": index, **message})
            new_metadata = dict(preview.get("metadata") or {})
            # Tells a client this preview is partial and how many replayed
            # messages an older event on the page already carried; without
            # it a delta preview reads like a short conversation. A few bytes
            # per event, against the kilobytes elided. Messages dropped by
            # the window are counted apart, in history_window_elided_count,
            # with history_window_start as the boundary, so neither count
            # claims the other's messages were "already shown".
            new_metadata["history_elided_count"] = elided
            if window is not None:
                # A slim stored row already starts at its own window; report
                # positions in the original conversation, not list indexes.
                stored_start = new_metadata.get("history_window_start")
                stored_elided = new_metadata.get("history_window_elided_count")
                first = messages[window_start] if window_start < len(messages) else None
                first_position = (
                    first.get("position") if isinstance(first, dict) else None
                )
                if window_start == 0 and isinstance(stored_start, int):
                    # The read-time window dropped nothing: the stored
                    # boundary is the one the elided count refers to.
                    reported_start = stored_start
                elif isinstance(first_position, int):
                    reported_start = first_position
                else:
                    reported_start = max(window_start, stored_start or 0)
                new_metadata["history_window_start"] = reported_start
                new_metadata["history_window_elided_count"] = window_elided + (
                    stored_elided if isinstance(stored_elided, int) else 0
                )
            new_payload["conversation_preview"] = {
                **preview,
                "messages": new_messages,
                "metadata": new_metadata,
            }

        tools = payload.get("tools")
        if isinstance(tools, list):
            if window is not None:
                tools = tools[-window:] if window > 0 else []
            new_tools: list[Any] = []
            for entry in tools:
                signature = _tool_signature(entry)
                if signature is not None:
                    if signature in seen_tools:
                        continue
                    seen_tools.add(signature)
                new_tools.append(entry)
            new_payload["tools"] = new_tools

        # Structured tool activity is recovered from the request body, which
        # replays the conversation, so every event repeats the calls before
        # it: 662 of 762 KB on a page of the heaviest prod session (#1689).
        # A repeat of an entry an older event on the page carried is elided.
        activity = payload.get("tool_activity")
        entries = activity.get("entries") if isinstance(activity, dict) else None
        if isinstance(activity, dict) and isinstance(entries, list):
            new_entries: list[Any] = []
            activity_elided = 0
            for entry in entries:
                signature = _tool_activity_signature(entry)
                if signature is not None:
                    if signature in seen_activity:
                        activity_elided += 1
                        continue
                    seen_activity.add(signature)
                new_entries.append(entry)
            new_payload["tool_activity"] = {
                **activity,
                "entries": new_entries,
                "history_elided_count": activity_elided,
            }

        result[position] = {**event, "payload": new_payload}
    return result
