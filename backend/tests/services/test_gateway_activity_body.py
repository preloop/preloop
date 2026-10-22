"""Slim storage of model gateway call rows."""

from preloop.services.gateway_activity_body import (
    BODY_KEYS,
    COMPACTION_KEY,
    payload_bytes,
    slim_gateway_payload,
    strip_gateway_bodies,
)
from preloop.services.gateway_event_history import elide_replayed_history


def _call_payload(turns: int, *, text_size: int = 4000) -> dict:
    """A payload for call ``turns`` of a tool loop that replays its history."""
    raw: list[dict] = [{"role": "system", "content": "s" * text_size}]
    preview: list[dict] = [
        {"source": "request", "role": "system", "text": "s" * text_size}
    ]
    raw.append({"role": "user", "content": "fix the bug"})
    preview.append({"source": "request", "role": "user", "text": "fix the bug"})
    tools: list[dict] = []
    for turn in range(turns):
        call_id = f"call_{turn}"
        raw.append(
            {
                "role": "assistant",
                "content": f"step {turn}",
                "tool_calls": [{"id": call_id, "function": {"name": "read"}}],
            }
        )
        raw.append(
            {"role": "tool", "tool_call_id": call_id, "content": "r" * text_size}
        )
        preview.append(
            {"source": "request", "role": "assistant", "text": f"step {turn}"}
        )
        preview.append(
            {"source": "request", "role": "tool", "text": f"{turn}" + "r" * text_size}
        )
        tools.append({"kind": "call", "call_id": call_id, "source": "request"})
        tools.append({"kind": "result", "call_id": call_id, "source": "request"})
    preview.append({"source": "response", "role": "assistant", "text": "done"})
    return {
        "api_usage_id": "u1",
        "prompt_tokens": 100,
        "total_tokens": 120,
        "estimated_cost": 0.01,
        "request": {
            "model": "m",
            "messages": raw,
            "tools": [{"type": "function", "function": {"name": "read"}}],
        },
        "response": {"choices": [{"message": {"role": "assistant", "content": "x"}}]},
        "conversation_preview": {"messages": preview, "metadata": {}},
        "tools": tools,
        "tool_activity": {
            "entries": [{"id": t["call_id"], "direction": t["kind"]} for t in tools]
        },
    }


def test_slim_keeps_only_the_new_messages_with_positions():
    payload = _call_payload(30)
    slim = slim_gateway_payload(payload, max_bytes=0)

    messages = slim["conversation_preview"]["messages"]
    # The last user message (title), the latest tool result and the response.
    assert [m["role"] for m in messages] == ["user", "tool", "assistant"]
    assert [m["position"] for m in messages] == [1, 61, 62]
    metadata = slim["conversation_preview"]["metadata"]
    assert metadata["history_window_start"] == 61
    assert metadata["history_window_elided_count"] == 60
    assert [m["role"] for m in slim["request"]["messages"]] == ["tool"]
    assert slim["request"]["tools"] == payload["request"]["tools"]
    assert {t["call_id"] for t in slim["tools"]} == {"call_29"}
    assert {e["id"] for e in slim["tool_activity"]["entries"]} == {"call_29"}
    # Accounting fields survive untouched.
    for key in ("api_usage_id", "prompt_tokens", "total_tokens", "estimated_cost"):
        assert slim[key] == payload[key]
    assert payload_bytes(slim) < payload_bytes(payload) / 20


def test_first_call_keeps_the_whole_conversation():
    payload = _call_payload(0)
    slim = slim_gateway_payload(payload, max_bytes=0)
    assert len(slim["conversation_preview"]["messages"]) == 3
    assert slim["request"]["messages"] == payload["request"]["messages"]


def test_slim_is_idempotent_and_does_not_mutate():
    payload = _call_payload(5)
    before = payload_bytes(payload)
    slim = slim_gateway_payload(payload, max_bytes=8192)
    assert payload_bytes(payload) == before
    assert slim_gateway_payload(slim, max_bytes=8192) is slim
    assert slim[COMPACTION_KEY]["original_bytes"] == before


def test_byte_cap_bounds_the_row():
    payload = _call_payload(3, text_size=200_000)
    slim = slim_gateway_payload(payload, max_bytes=16384)
    assert payload_bytes(slim) <= 16384 + 512  # marker added after fitting
    marker = slim[COMPACTION_KEY]
    assert marker["request_messages_omitted"] is True
    assert "strings_capped_at" in marker
    assert slim["prompt_tokens"] == 100


def test_strip_removes_content_keeps_accounting():
    stripped = strip_gateway_bodies(_call_payload(2))
    assert not set(BODY_KEYS) & set(stripped)
    assert stripped["total_tokens"] == 120
    assert stripped[COMPACTION_KEY]["bodies_stripped"] is True
    assert strip_gateway_bodies(stripped) is stripped


def test_reader_rebuilds_the_conversation_from_slim_rows():
    """The gateway-events reader still yields every message once, in order."""
    events = [
        {
            "id": str(n),
            "timestamp": f"2026-10-22T00:00:{n:02d}",
            "type": "model_gateway_call",
            "payload": slim_gateway_payload(_call_payload(n), max_bytes=0),
        }
        for n in range(4)
    ]
    full = elide_replayed_history(
        [{**e, "payload": _call_payload(n)} for n, e in enumerate(events)]
    )
    slim = elide_replayed_history(events)

    def positions(page: list[dict]) -> set[tuple[int, str]]:
        return {
            (m["position"], m["text"])
            for e in page
            for m in e["payload"]["conversation_preview"]["messages"]
            if m["source"] == "request"
        }

    # Every request message the unslimmed rows render is in the slim rows,
    # except replayed assistant turns, which the previous row's response holds.
    missing = positions(full) - positions(slim)
    assert all(text.startswith("step ") for _, text in missing)


def test_reader_window_reports_original_positions():
    payload = slim_gateway_payload(_call_payload(10), max_bytes=0)
    [event] = elide_replayed_history(
        [{"id": "1", "timestamp": "t", "type": "x", "payload": payload}], window=1
    )
    metadata = event["payload"]["conversation_preview"]["metadata"]
    assert metadata["history_window_start"] == 22
    assert metadata["history_window_elided_count"] >= 20
