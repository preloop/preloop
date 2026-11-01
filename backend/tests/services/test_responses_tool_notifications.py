"""Several outputs for one tool call (preloop/preloop#2173).

Codex code-mode ``exec`` cells that yield and later ``notify()`` record a
running acknowledgement and then the async result under the same call id.
Chat-completions allows one tool message per ``tool_call_id``, so later
outputs are appended to that call's tool message, in arrival order. Orphans
and mismatched ids still 400.
"""

from __future__ import annotations

import time

import pytest

from preloop.services import codex_upstream_ws as cw
from preloop.services.model_gateway_errors import ModelGatewayAPIError

from tests.services.test_codex_crosschat import _normalize
from tests.services.test_codex_upstream_ws import _chained_entry, _payload
from tests.services.test_responses_tool_pairing import (
    _assert_tool_pairing,
    _call,
    _output,
    _user,
)

ACK = "Script running with cell ID example-cell\nWall time 1.0 seconds\nOutput:\n"
RESULT = '{"build":{"exit_code":0,"output":"Build completed"}}'


def _custom_call(call_id="call_example"):
    return {
        "type": "custom_tool_call",
        "name": "exec",
        "call_id": call_id,
        "input": "// exec cell that yields and later calls notify()",
    }


def _custom_output(call_id="call_example", output=ACK, **extra):
    return {
        "type": "custom_tool_call_output",
        "call_id": call_id,
        "output": output,
        **extra,
    }


def _assistant(text):
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def _tool_messages(messages):
    return [m for m in messages if m["role"] == "tool"]


def test_issue_fragment_acknowledgement_then_notification_merges():
    messages = _normalize(
        [
            _user("go"),
            _custom_call(),
            _custom_output(),
            _custom_output(output=RESULT, name="exec"),
            _user("next"),
        ]
    )
    _assert_tool_pairing(messages)
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "user"]
    assert messages[2]["tool_call_id"] == "call_example"
    assert messages[2]["content"] == f"{ACK}\n{RESULT}"


def test_multiple_notifications_for_one_call_keep_arrival_order():
    messages = _normalize(
        [
            _user("go"),
            _custom_call(),
            _custom_output(output="ack"),
            _custom_output(output="first", name="exec"),
            _custom_output(output="second", name="exec"),
        ]
    )
    tools = _tool_messages(messages)
    assert len(tools) == 1
    assert tools[0]["content"] == "ack\nfirst\nsecond"


def test_many_notifications_merge_in_linear_time():
    """Repeats are joined once, not re-copied per output (review on #2174)."""
    count, chunk = 16000, "x" * 2048  # ~32 MB, the default body cap
    started = time.perf_counter()
    messages = _normalize(
        [_user("go"), _custom_call()]
        + [_custom_output(output=chunk) for _ in range(count)]
    )
    elapsed = time.perf_counter() - started
    tools = _tool_messages(messages)
    assert len(tools) == 1
    assert tools[0]["content"] == "\n".join([chunk] * count)
    # Quadratic merging took ~10.5 s here; linear takes well under 1 s.
    assert elapsed < 5.0


def test_function_call_output_repeat_merges_too():
    messages = _normalize(
        [_user("go"), _call(), _output(output="ack"), _output(output="done")]
    )
    _assert_tool_pairing(messages)
    assert _tool_messages(messages)[0]["content"] == "ack\ndone"


def test_parallel_calls_with_interleaved_acks_and_notifications():
    messages = _normalize(
        [
            _user("go"),
            _custom_call("call_a"),
            _custom_call("call_b"),
            _custom_output("call_a", "ack a"),
            # Notification for a while call_b is still pending.
            _custom_output("call_a", "result a", name="exec"),
            _custom_output("call_b", "ack b"),
            _custom_output("call_b", "result b", name="exec"),
            _user("next"),
        ]
    )
    _assert_tool_pairing(messages)
    tools = _tool_messages(messages)
    assert [(m["tool_call_id"], m["content"]) for m in tools] == [
        ("call_a", "ack a\nresult a"),
        ("call_b", "ack b\nresult b"),
    ]
    assert messages[-1]["role"] == "user"


def test_single_result_call_unchanged():
    messages = _normalize([_user("go"), _custom_call(), _custom_output(output="ok")])
    assert _tool_messages(messages) == [
        {"role": "tool", "tool_call_id": "call_example", "content": "ok"}
    ]


def test_notification_after_assistant_message_in_same_turn_merges():
    messages = _normalize(
        [
            _user("go"),
            _custom_call(),
            _custom_output(),
            _assistant("waiting for the build"),
            _custom_output(output=RESULT, name="exec"),
        ]
    )
    _assert_tool_pairing(messages)
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[2]["content"] == f"{ACK}\n{RESULT}"


def test_orphan_call_id_still_400s():
    with pytest.raises(ModelGatewayAPIError) as caught:
        _normalize(
            [_user("go"), _custom_call(), _custom_output(), _custom_output("call_x")]
        )
    assert caught.value.status_code == 400
    assert (
        "call_id does not match any earlier tool call awaiting a result"
        in caught.value.message
    )


def test_mismatched_call_id_with_pending_sibling_still_400s():
    with pytest.raises(ModelGatewayAPIError) as caught:
        _normalize(
            [
                _custom_call("call_a"),
                _custom_call("call_b"),
                _custom_output("call_a"),
                _custom_output("call_x"),
            ]
        )
    assert caught.value.status_code == 400
    assert "call_id does not match a pending tool call" in caught.value.message
    assert "call_b" in caught.value.message


def test_output_for_call_answered_in_previous_turn_still_400s():
    with pytest.raises(ModelGatewayAPIError) as caught:
        _normalize(
            [
                _user("go"),
                _custom_call("call_a"),
                _custom_output("call_a"),
                _custom_call("call_b"),
                _custom_output("call_b"),
                _custom_output("call_a", RESULT, name="exec"),
            ]
        )
    assert caught.value.status_code == 400
    assert "index=5" in caught.value.message


def test_ws_passthrough_forwards_repeated_outputs_verbatim():
    """The native Responses WS path diffs the raw input and does not pair
    outputs; a notification is just another appended item."""
    first = [_user("go")]
    entry = _chained_entry(first, [_custom_call()])
    note = _custom_output(output=RESULT, name="exec")
    current = first + [_custom_call(), _custom_output(), note]
    plan = cw.plan_request(entry, _payload(current))
    assert plan.mode == "incremental"
    assert plan.items == [_custom_output(), note]
