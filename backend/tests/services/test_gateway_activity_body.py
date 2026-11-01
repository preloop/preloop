"""Slim storage of model gateway call rows."""

import random

import pytest

from preloop.config import settings
from preloop.services.gateway_activity_body import (
    BODY_KEYS,
    COMPACTION_KEY,
    payload_bytes,
    slim_gateway_payload,
    strip_gateway_bodies,
    truncate_middle,
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
    assert payload_bytes(slim) <= 16384
    marker = slim[COMPACTION_KEY]
    assert "request_messages_omitted" not in marker
    assert marker["messages_shortened"]["strings"] >= 1
    # The new tool result survives, cut in the middle, not dropped.
    [tool_message] = slim["request"]["messages"]
    assert tool_message["role"] == "tool"
    assert "truncated" in tool_message["content"]
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


def test_byte_cap_holds_with_many_tool_entries():
    """Short strings alone cannot fit hundreds of entries; keys go instead."""
    payload = _call_payload(1)
    payload["tool_activity"] = {
        "entries": [
            {"id": None, "direction": "call", "arguments": "a" * 2000}
            for _ in range(400)
        ]
    }
    payload["tools"] = [
        {"kind": "call", "source": "response", "text": "t" * 2000} for _ in range(400)
    ]
    slim = slim_gateway_payload(payload, max_bytes=32768)
    assert payload_bytes(slim) <= 32768
    assert "tool_activity" in slim["body_compaction"]["keys_omitted"]
    assert "request" not in slim["body_compaction"]["keys_omitted"]
    assert "response" not in slim["body_compaction"]["keys_omitted"]
    assert slim["response"] == payload["response"]
    assert slim["total_tokens"] == 120


def test_reader_window_wider_than_slim_row_keeps_stored_boundary():
    payload = slim_gateway_payload(_call_payload(10), max_bytes=0)
    [event] = elide_replayed_history(
        [{"id": "1", "timestamp": "t", "type": "x", "payload": payload}], window=40
    )
    metadata = event["payload"]["conversation_preview"]["metadata"]
    assert metadata["history_window_start"] == 21
    assert metadata["history_window_elided_count"] == 20


def _prod_like_payload(
    rng: random.Random,
    *,
    history: int,
    new_results: int,
    result_chars: int,
    untracked_entries: int,
    response_chars: int,
) -> dict:
    """A long tool-loop call shaped like the rows the 32 KB cap emptied.

    ``history`` replayed messages, then ``new_results`` tool results added by
    this call, tool entries the slimming cannot attribute to a call id, and a
    response with text and a tool call.
    """
    raw: list[dict] = [{"role": "user", "content": "refactor the parser"}]
    preview: list[dict] = [
        {"source": "request", "role": "user", "text": "refactor the parser"}
    ]
    for turn in range(history // 2):
        raw.append(
            {
                "role": "assistant",
                "content": f"step {turn}",
                "tool_calls": [{"id": f"h{turn}", "function": {"name": "read"}}],
            }
        )
        raw.append({"role": "tool", "tool_call_id": f"h{turn}", "content": "x" * 900})
        preview.append({"source": "request", "role": "assistant", "text": "s"})
        preview.append({"source": "request", "role": "tool", "text": "x" * 900})
    for index in range(new_results):
        size = rng.randint(result_chars // 4, result_chars)
        body = "".join(rng.choice("abcdef \n") for _ in range(64)) * (size // 64)
        raw.append({"role": "tool", "tool_call_id": f"n{index}", "content": body})
        preview.append({"source": "request", "role": "tool", "text": body})
    response_text = "R" * response_chars
    preview.append({"source": "response", "role": "assistant", "text": response_text})
    return {
        "api_usage_id": "u1",
        "prompt_tokens": 150_000,
        "completion_tokens": 900,
        "total_tokens": 150_900,
        "estimated_cost": 0.42,
        "finish_reason": "tool_calls",
        "request": {
            "model": "m",
            "system": [{"type": "text", "text": "p" * 8192} for _ in range(3)],
            "messages": raw,
            "tools": [
                {
                    "type": "function",
                    "function": {"name": f"tool_{n}", "description": "d" * 1500},
                }
                for n in range(40)
            ],
        },
        "response": {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": response_text,
                        "tool_calls": [
                            {
                                "id": "next",
                                "function": {
                                    "name": "edit",
                                    "arguments": '{"path": "parser.py"}',
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 150_000, "completion_tokens": 900},
        },
        "conversation_preview": {"messages": preview, "metadata": {}},
        "tools": [
            {"kind": "result", "source": "request", "text": "t" * 400}
            for _ in range(untracked_entries)
        ],
        "tool_activity": {
            "entries": [
                {"direction": "result", "output": "o" * 400}
                for _ in range(untracked_entries)
            ]
        },
    }


def _assert_call_content_kept(payload: dict, slim: dict, cap: int) -> None:
    marker = slim[COMPACTION_KEY]
    assert payload_bytes(slim) <= cap
    assert marker["stored_bytes"] == payload_bytes(slim)
    assert "request" in slim and "response" in slim
    for key in ("request", "response", "conversation_preview"):
        assert key not in marker.get("keys_omitted", [])
    # The response keeps its structure: tool calls, finish reason, usage.
    choice = slim["response"]["choices"][0]
    assert choice["finish_reason"] == payload["response"]["choices"][0]["finish_reason"]
    assert (
        choice["message"]["tool_calls"]
        == payload["response"]["choices"][0]["message"]["tool_calls"]
    )
    assert slim["response"]["usage"] == payload["response"]["usage"]
    # Every message this call added is still there, in order.
    start = marker["request_history_start"]["messages"]
    added = payload["request"]["messages"][start:]
    kept = slim["request"]["messages"]
    assert [m.get("tool_call_id") for m in kept] == [
        m.get("tool_call_id") for m in added
    ]
    for original, stored in zip(added, kept, strict=False):
        text = stored["content"]
        if text != original["content"]:
            assert "[... truncated " in text
            assert original["content"].startswith(text.split("\n[... truncated")[0])
    # The last user message is kept for the call title.
    preview = slim["conversation_preview"]["messages"]
    assert any(m["role"] == "user" for m in preview)
    assert any(m["source"] == "response" for m in preview)
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        assert slim[key] == payload[key]


@pytest.mark.parametrize("seed", range(60))
@pytest.mark.parametrize("cap", [32_768, 131_072, 262_144])
def test_any_call_keeps_response_and_new_messages_under_cap(seed: int, cap: int):
    rng = random.Random(seed)
    payload = _prod_like_payload(
        rng,
        history=rng.choice([20, 120, 290, 580]),
        new_results=rng.randint(1, 12),
        result_chars=rng.choice([2_000, 8_192, 32_768]),
        untracked_entries=rng.choice([0, 50, 300]),
        response_chars=rng.choice([10, 2_000, 30_000]),
    )
    slim = slim_gateway_payload(payload, max_bytes=cap)
    _assert_call_content_kept(payload, slim, cap)


def _prod_regression_payload() -> dict:
    return _prod_like_payload(
        random.Random(7),
        history=290,
        new_results=160,
        result_chars=8_192,
        untracked_entries=300,
        response_chars=1_200,
    )


def test_regression_prod_marker_pattern_no_longer_drops_request_and_response():
    """The 32 KB cascade dropped request and response; now both survive.

    Before this change this call stored ``keys_omitted ==
    ["tool_activity", "tools", "request", "response"]`` and
    ``strings_capped_at == 256``. 161 new messages cannot all fit in 32 KB,
    so the newest one and the last user message are kept and the elision is
    recorded; the response is stored whole.
    """
    payload = _prod_regression_payload()
    slim = slim_gateway_payload(payload, max_bytes=32_768)
    marker = slim[COMPACTION_KEY]
    assert marker.get("keys_omitted") == ["tool_activity", "tools"]
    assert "strings_capped_at" not in marker
    assert marker["request_history_start"]["messages"] == 290
    assert marker["delta_messages_elided"] > 0
    assert payload_bytes(slim) <= 32_768
    assert slim["response"] == payload["response"]
    assert slim["request"]["messages"][-1]["tool_call_id"] == "n159"
    assert any(m["role"] == "user" for m in slim["conversation_preview"]["messages"])


def test_prod_pattern_at_default_cap_keeps_every_new_message():
    payload = _prod_regression_payload()
    cap = settings.model_gateway_activity_max_stored_bytes
    assert cap == 8_388_608
    slim = slim_gateway_payload(payload, max_bytes=cap)
    assert "delta_messages_elided" not in slim[COMPACTION_KEY]
    _assert_call_content_kept(payload, slim, cap)
    assert slim["response"] == payload["response"]


def test_marker_records_what_was_shortened():
    payload = _prod_like_payload(
        random.Random(1),
        history=40,
        new_results=6,
        result_chars=32_768,
        untracked_entries=0,
        response_chars=50_000,
    )
    slim = slim_gateway_payload(payload, max_bytes=65_536, keep_chars=1024)
    marker = slim[COMPACTION_KEY]
    assert marker["tool_definitions_omitted"] is True
    assert marker["messages_shortened"]["keep_chars"] == 1024
    assert marker["messages_shortened"]["chars_removed"] > 0
    truncated = [
        m for m in slim["conversation_preview"]["messages"] if m.get("truncated")
    ]
    assert truncated
    assert all(m["original_length"] > len(m["text"]) for m in truncated)
    assert payload_bytes(slim) <= 65_536


def test_response_is_cut_last_and_keeps_a_generous_head():
    payload = _prod_like_payload(
        random.Random(2),
        history=4,
        new_results=1,
        result_chars=1_000,
        untracked_entries=0,
        response_chars=200_000,
    )
    slim = slim_gateway_payload(payload, max_bytes=32_768, keep_chars=2048)
    marker = slim[COMPACTION_KEY]
    assert marker["response_shortened"]["head_chars"] == 8192
    content = slim["response"]["choices"][0]["message"]["content"]
    assert content.startswith("R" * 8192)
    assert "[... truncated " in content
    assert payload_bytes(slim) <= 32_768


def test_tiny_cap_still_bounded_and_keeps_keys():
    payload = _prod_like_payload(
        random.Random(3),
        history=10,
        new_results=400,
        result_chars=2_000,
        untracked_entries=10,
        response_chars=100,
    )
    slim = slim_gateway_payload(payload, max_bytes=8192)
    assert payload_bytes(slim) <= 8192
    assert "request" in slim and "response" in slim


def test_truncate_middle_keeps_head_and_tail():
    text = "a" * 5000 + "b" * 5000
    cut = truncate_middle(text, head=100, tail=50)
    assert cut.startswith("a" * 100)
    assert cut.endswith("b" * 50)
    assert "[... truncated 9850 chars ...]" in cut
    assert truncate_middle("short", head=100, tail=50) == "short"


def test_text_less_assistant_turns_still_bound_the_preview():
    """Tool-call-only assistant turns have no preview text; history stays out."""
    raw: list[dict] = [{"role": "user", "content": "go"}]
    preview: list[dict] = [{"source": "request", "role": "user", "text": "go"}]
    for turn in range(60):
        raw.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"c{turn}", "function": {"name": "read"}}],
            }
        )
        raw.append({"role": "tool", "tool_call_id": f"c{turn}", "content": "r" * 5000})
        # The preview builder skips the empty assistant message.
        preview.append({"source": "request", "role": "tool", "text": "r" * 5000})
    preview.append({"source": "response", "role": "assistant", "text": "done"})
    payload = {
        "total_tokens": 1,
        "request": {"model": "m", "messages": raw},
        "response": {"choices": [{"message": {"content": "done"}}]},
        "conversation_preview": {"messages": preview, "metadata": {}},
    }
    slim = slim_gateway_payload(payload, max_bytes=8_388_608)
    messages = slim["conversation_preview"]["messages"]
    assert [m["role"] for m in messages] == ["user", "tool", "assistant"]
    assert [m["position"] for m in messages] == [0, 60, 61]
    metadata = slim["conversation_preview"]["metadata"]
    assert metadata["history_window_start"] == 60
    assert metadata["history_window_elided_count"] == 59
    assert payload_bytes(slim) < 20_000


def test_long_tool_call_arguments_are_never_cut():
    arguments = '{"path": "a.py", "content": "' + "x" * 40_000 + '"}'
    payload = _call_payload(2)
    payload["response"] = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "y" * 40_000,
                    "tool_calls": [
                        {
                            "id": "w",
                            "function": {"name": "write", "arguments": arguments},
                        }
                    ],
                },
            }
        ],
        "content": [{"type": "tool_use", "id": "w2", "input": {"body": "z" * 9_000}}],
    }
    slim = slim_gateway_payload(payload, max_bytes=65_536)
    assert payload_bytes(slim) <= 65_536
    message = slim["response"]["choices"][0]["message"]
    assert message["tool_calls"][0]["function"]["arguments"] == arguments
    assert slim["response"]["content"] == payload["response"]["content"]
    assert "[... truncated " in message["content"]
