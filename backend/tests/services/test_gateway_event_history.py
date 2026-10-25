"""history=delta on session gateway events (#1586)."""

from __future__ import annotations

import copy

from preloop.services.gateway_event_history import elide_replayed_history


def _event(index: int, history: list[dict], response: str, tools: list[dict]):
    return {
        "id": f"event-{index}",
        "timestamp": f"2026-10-13T10:{index:02d}:00+00:00",
        "type": "model_gateway_call",
        "payload": {
            "conversation_preview": {
                "messages": [
                    *history,
                    {"role": "assistant", "source": "response", "text": response},
                ],
                "metadata": {"message_count": len(history) + 1},
            },
            "tools": tools,
        },
    }


def _session(calls: int) -> list[dict]:
    """Newest first, like the endpoint: each call replays all earlier ones."""
    system = {"role": "system", "source": "request", "text": "You are an agent."}
    history = [system, {"role": "user", "source": "request", "text": "Fix the test"}]
    tools: list[dict] = []
    events = []
    for index in range(calls):
        response = f"step {index}"
        tools = [
            *tools,
            {"kind": "call", "call_id": f"c{index}", "name": "bash", "text": "ls"},
            {"kind": "result", "call_id": f"c{index}", "name": "bash", "text": "ok"},
        ]
        events.append(_event(index, history, response, list(tools)))
        history = [
            *history,
            {"role": "assistant", "source": "request", "text": response},
            {"role": "tool", "source": "request", "text": f"result {index}"},
        ]
    return list(reversed(events))


def _texts(event: dict) -> list[tuple[int, str]]:
    return [
        (message["position"], message["text"])
        for message in event["payload"]["conversation_preview"]["messages"]
    ]


def test_oldest_event_keeps_everything_newer_ones_only_what_is_new():
    events = _session(3)
    result = elide_replayed_history(events)

    oldest, middle, newest = result[2], result[1], result[0]
    assert _texts(oldest) == [
        (0, "You are an agent."),
        (1, "Fix the test"),
        (2, "step 0"),
    ]
    # The replayed system prompt and the history are gone; the user request
    # the per-event views title by stays; positions are the original ones.
    assert _texts(middle) == [(1, "Fix the test"), (3, "result 0"), (4, "step 1")]
    assert _texts(newest) == [
        (1, "Fix the test"),
        (5, "result 1"),
        (6, "step 2"),
    ]
    assert (
        newest["payload"]["conversation_preview"]["metadata"]["history_elided_count"]
        == 4
    )


def test_tools_already_carried_are_dropped():
    result = elide_replayed_history(_session(3))
    assert [entry["call_id"] for entry in result[0]["payload"]["tools"]] == [
        "c2",
        "c2",
    ]
    assert len(result[2]["payload"]["tools"]) == 2


def test_input_is_not_mutated_and_order_is_kept():
    events = _session(4)
    before = copy.deepcopy(events)
    result = elide_replayed_history(events)
    assert events == before
    assert [event["id"] for event in result] == [event["id"] for event in events]


def test_repeated_text_at_a_new_position_is_kept():
    first = _event(
        0, [{"role": "user", "source": "request", "text": "continue"}], "a", []
    )
    second = _event(
        1,
        [
            {"role": "user", "source": "request", "text": "continue"},
            {"role": "assistant", "source": "request", "text": "a"},
            {"role": "user", "source": "request", "text": "continue"},
        ],
        "b",
        [],
    )
    result = elide_replayed_history([second, first])
    assert _texts(result[0]) == [(2, "continue"), (3, "b")]


def test_events_without_preview_pass_through():
    event = {
        "id": "x",
        "timestamp": None,
        "type": "model_gateway_call",
        "payload": None,
    }
    assert elide_replayed_history([event]) == [event]


def test_window_bounds_the_oldest_event_to_its_last_messages():
    """#1689: a page on a long session must not carry the whole history."""
    events = _session(40)
    result = elide_replayed_history(events[:20], window=10)
    oldest = result[-1]
    preview = oldest["payload"]["conversation_preview"]
    positions = [message["position"] for message in preview["messages"]]
    total = len(events[19]["payload"]["conversation_preview"]["messages"])
    # The last user message is kept as the event's title even when old.
    tail = [p for p in positions if p >= total - 10]
    assert tail == list(range(total - 10, total))
    assert len(positions) <= 11
    assert preview["metadata"]["history_window_start"] == total - 10
    # The oldest event has nothing replayed from an older one; every message
    # it lost was dropped by the window, and is counted as such.
    assert preview["metadata"]["history_elided_count"] == 0
    assert preview["metadata"]["history_window_elided_count"] == total - len(positions)
    assert len(oldest["payload"]["tools"]) == 10


def test_window_keeps_newer_events_new_messages():
    events = _session(5)
    windowed = elide_replayed_history(events, window=10)
    plain = elide_replayed_history(events)
    assert [_texts(e) for e in windowed[:-1]] == [_texts(e) for e in plain[:-1]]


def test_no_window_leaves_the_oldest_event_whole():
    events = _session(10)
    result = elide_replayed_history(events)
    preview = result[-1]["payload"]["conversation_preview"]
    assert "history_window_start" not in preview["metadata"]


def _with_activity(events: list[dict]) -> list[dict]:
    """Give each event the structured tool activity its replayed history holds."""
    oldest_first = sorted(events, key=lambda event: event["timestamp"])
    entries: list[dict] = []
    for index, event in enumerate(oldest_first):
        entries = [
            *entries,
            {"id": f"c{index}", "direction": "call", "name": "bash", "arguments": "ls"},
            {"id": f"c{index}", "direction": "result", "name": "bash", "result": "ok"},
            # An entry without a provider id is never merged or elided.
            {"id": None, "direction": "call", "name": "bash", "arguments": "pwd"},
        ]
        event["payload"]["tool_activity"] = {
            "entries": list(entries),
            "truncated": False,
            "dialect": "openai",
        }
    return events


def test_tool_activity_already_carried_is_dropped():
    """Replayed tool activity was 662 of 762 KB on a heavy page (#1689)."""
    events = _with_activity(_session(4))
    result = elide_replayed_history(events)
    by_id = {event["id"]: event["payload"]["tool_activity"] for event in result}
    oldest, newest = by_id["event-0"], by_id["event-3"]
    assert [e["id"] for e in oldest["entries"]] == ["c0", "c0", None]
    assert oldest["history_elided_count"] == 0
    # The newest event keeps only its own call, plus every id-less entry.
    kept = [(e["id"], e["direction"]) for e in newest["entries"]]
    assert ("c3", "call") in kept and ("c3", "result") in kept
    assert not [k for k in kept if k[0] in ("c0", "c1", "c2")]
    assert sum(1 for k in kept if k[0] is None) == 4
    assert newest["history_elided_count"] == 6
    # Every call and result is still on the page exactly once.
    seen = [
        (e["id"], e["direction"])
        for activity in by_id.values()
        for e in activity["entries"]
        if e["id"]
    ]
    assert sorted(seen) == sorted(
        (f"c{i}", d) for i in range(4) for d in ("call", "result")
    )


def test_tool_activity_with_new_content_is_kept():
    events = _with_activity(_session(2))
    newest = max(events, key=lambda event: event["timestamp"])
    newest["payload"]["tool_activity"]["entries"][1] = {
        "id": "c0",
        "direction": "result",
        "name": "bash",
        "result": "a later, different result",
    }
    result = elide_replayed_history(events)
    newest_out = max(result, key=lambda event: event["timestamp"])
    results = [
        e["result"]
        for e in newest_out["payload"]["tool_activity"]["entries"]
        if e.get("direction") == "result" and e.get("id") == "c0"
    ]
    assert results == ["a later, different result"]


def test_talk_page_bounds_replayed_history_and_keeps_tool_pairs():
    """A 50-event Talk page stays small without losing page-local turns."""
    import gzip
    import json
    import random

    random_source = random.Random(42)
    history = [
        {
            "role": "assistant",
            "source": "request",
            "text": "".join(
                random_source.choices("abcdefghijklmnopqrstuvwxyz", k=1000)
            ),
        }
        for _ in range(1000)
    ]
    events = []
    for index in range(50):
        history = [
            *history,
            {"role": "user", "source": "request", "text": f"turn {index}"},
        ]
        tools = [
            {"kind": kind, "call_id": f"c{index}", "name": "test", "text": kind}
            for kind in ("call", "result")
        ]
        events.append(_event(index, history, f"response {index}", tools))
    result = elide_replayed_history(list(reversed(events)), window=40)
    encoded = json.dumps({"logs": result}).encode()
    assert len(encoded) < 3_000_000
    assert len(gzip.compress(encoded)) < 1_000_000
    assert {
        entry["call_id"] for event in result for entry in event["payload"]["tools"]
    } == {f"c{i}" for i in range(50)}
    for event in result:
        assert {entry["kind"] for entry in event["payload"]["tools"]} == {
            "call",
            "result",
        }
        messages = event["payload"]["conversation_preview"]["messages"]
        assert all("position" in message for message in messages)
        assert any(message["source"] == "response" for message in messages)
