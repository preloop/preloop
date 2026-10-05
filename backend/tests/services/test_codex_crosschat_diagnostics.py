"""Codex call_id-less deliveries, recurrence of preloop/preloop#1113.

The 2026-10-05 recurrence 400'd with an EMPTY missing-ID list and left no
audit row. These tests cover every normalizer path that used to produce that
empty list, the ``create_thread`` delivery found in current Codex source
(``codex-rs/tui/src/dynamic_tools.rs`` at ``rust-v0.162.0-alpha.16``), and
the audit row the rejection now writes.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from preloop.services.codex_crosschat import (
    describe_input_item,
    is_unsolicited_crosschat_output,
)
from preloop.services.model_gateway_errors import ModelGatewayAPIError

from tests.services.test_codex_crosschat import (
    DELEGATION,
    _bare_service,
    _item,
    _normalize,
)
from tests.services.test_openai_responses_passthrough import (
    _create_zen_model,
    _service,
)

SECRET = "TOP-SECRET-OUTPUT-CONTENT"


def _rejection(items) -> str:
    with pytest.raises(ModelGatewayAPIError) as caught:
        _normalize(items)
    assert caught.value.status_code == 400
    message = caught.value.message
    assert SECRET not in message
    assert "did not have response messages: ." not in message
    assert "Offending input item: " in message
    return message


# --- contract extension: create_thread -------------------------------------


@pytest.mark.parametrize("namespace", ["codex_tui", "codex_app"])
def test_create_thread_delivery_is_untrusted_user_context(namespace):
    item = _item(name="create_thread", namespace=namespace, output=DELEGATION)
    assert is_unsolicited_crosschat_output(item)
    messages = _normalize([item])
    assert messages == [
        {
            "role": "user",
            "content": messages[0]["content"],
        }
    ]
    assert messages[0]["content"].startswith("Untrusted cross-thread context")
    assert f"{namespace}/create_thread" in messages[0]["content"]


def test_create_thread_delivery_never_satisfies_a_pending_call():
    message = _rejection(
        [
            {"type": "function_call", "call_id": "call_1", "name": "spawn_agent"},
            _item(name="create_thread", namespace="codex_tui", output=DELEGATION),
        ]
    )
    assert "response messages: call_1." in message
    assert "name='create_thread'" in message


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "create_thread", "namespace": "cloud_threads"},
        {"name": "create_thread", "namespace": "codex_tui", "call_id": ""},
        {"name": "create_thread", "namespace": "codex_tui", "output": "plain"},
        {"name": "fork_thread", "namespace": "codex_tui"},
        {"name": "spawn_agent", "namespace": "multi_agent_v1"},
        {"name": "send_input", "namespace": "collaboration"},
    ],
)
def test_other_call_id_less_shapes_are_still_rejected(changes):
    assert not is_unsolicited_crosschat_output(_item(**changes))


# --- every path that used to produce an empty missing-ID list --------------


def test_unrecognised_call_id_less_output_names_the_item():
    message = _rejection(
        [
            {"type": "message", "role": "user", "content": "hi"},
            {
                "type": "function_call_output",
                "id": "fco_1",
                "name": "some_host_tool",
                "namespace": "codex_app",
                "output": SECRET,
            },
        ]
    )
    assert "response messages: (none)." in message
    assert (
        "index=1 type='function_call_output' name='some_host_tool' "
        "namespace='codex_app' call_id=missing id='fco_1'"
    ) in message
    assert "not a recognised Codex delivery" in message


@pytest.mark.parametrize(
    ("changes", "state"),
    [({"call_id": None}, "call_id=null"), ({"call_id": ""}, "call_id=empty")],
)
def test_null_and_empty_call_id_are_distinguished(changes, state):
    item = {"type": "function_call_output", "output": SECRET, **changes}
    assert state in _rejection([item])


def test_orphan_output_with_unknown_call_id():
    message = _rejection(
        [{"type": "function_call_output", "call_id": "call_x", "output": SECRET}]
    )
    assert "call_id='call_x'" in message
    assert "does not match any earlier tool call" in message


def test_duplicate_output_for_an_answered_call():
    message = _rejection(
        [
            {"type": "function_call", "call_id": "c1", "name": "read"},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
            {"type": "function_call_output", "call_id": "c1", "output": SECRET},
        ]
    )
    assert "index=2" in message


def test_output_for_a_call_dropped_for_missing_name():
    message = _rejection(
        [
            {"type": "function_call", "call_id": "c1"},
            {"type": "function_call_output", "call_id": "c1", "output": SECRET},
        ]
    )
    assert "index=1" in message and "call_id='c1'" in message


def test_output_before_its_call():
    message = _rejection(
        [
            {"type": "function_call_output", "call_id": "c1", "output": SECRET},
            {"type": "function_call", "call_id": "c1", "name": "read"},
        ]
    )
    assert "index=0" in message


def test_custom_tool_call_output_without_any_id():
    message = _rejection([{"type": "custom_tool_call_output", "output": SECRET}])
    assert "type='custom_tool_call_output'" in message
    assert "call_id=missing" in message


def test_output_answering_a_local_shell_call():
    message = _rejection(
        [
            {
                "type": "local_shell_call",
                "call_id": "ls1",
                "status": "completed",
                "action": {"type": "exec", "command": ["ls"]},
            },
            {"type": "function_call_output", "call_id": "ls1", "output": SECRET},
        ]
    )
    assert "index=1" in message and "call_id='ls1'" in message


def test_mismatched_output_while_another_call_is_pending():
    message = _rejection(
        [
            {"type": "function_call", "call_id": "c1", "name": "read"},
            {"type": "function_call_output", "call_id": "c2", "output": SECRET},
        ]
    )
    assert "response messages: c1." in message
    assert "does not match a pending tool call" in message


def test_reasoning_between_call_and_output_is_named():
    message = _rejection(
        [
            {"type": "function_call", "call_id": "c1", "name": "read"},
            {"type": "reasoning", "summary": [{"text": SECRET}]},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        ]
    )
    assert "index=1 type='reasoning'" in message


def test_unanswered_call_at_end_of_input():
    message = _rejection([{"type": "function_call", "call_id": "c9", "name": "x"}])
    assert "response messages: c9." in message
    assert "end of input" in message


def test_describe_never_includes_content_and_bounds_identifiers():
    text = describe_input_item(
        {
            "type": "function_call_output",
            "name": "n" * 500,
            "output": SECRET,
            "arguments": SECRET,
            "content": SECRET,
        },
        3,
    )
    assert SECRET not in text
    assert len(text) < 200


# --- audit row -------------------------------------------------------------


@pytest.mark.parametrize("stream", [False, True])
def test_rejection_writes_failed_audit_row(db_session, test_user, stream):
    model = _create_zen_model(db_session, test_user)
    service = _service(db_session, test_user)
    payload = {
        "model": model.model_identifier,
        "stream": stream,
        "input": [
            {
                "type": "function_call_output",
                "name": "some_host_tool",
                "namespace": "codex_app",
                "output": SECRET,
            }
        ],
    }
    with (
        patch("preloop.services.openai_gateway.log_model_gateway_request") as audit,
        patch.object(service, "_resolve_requested_model", return_value=model),
    ):
        with pytest.raises(ModelGatewayAPIError) as caught:
            if stream:
                list(service.stream_response(payload))
            else:
                service.create_response(payload)
    assert caught.value.status_code == 400
    audit.assert_called_once()
    row = audit.call_args.kwargs
    assert row["outcome"] == "failed"
    assert row["status_code"] == 400
    assert row["error_type"] == "validation_error"
    assert row["endpoint"] == "/openai/v1/responses"
    assert row["endpoint_kind"] == ("responses_stream" if stream else "responses")
    assert "name='some_host_tool'" in row["error_detail"]
    assert "call_id=missing" in row["error_detail"]
    assert SECRET not in row["error_detail"]


def test_reasoning_bridge_reports_the_clients_original_index():
    """DeepSeek's bridge drops reasoning items; the 400 must still name the
    position in the client's own ``input``."""
    model = SimpleNamespace(
        id="model-1",
        provider_name="deepseek",
        model_identifier="deepseek-v4-flash",
        api_endpoint=None,
    )
    payload = {
        "input": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "role": "user", "content": "hi"},
            {"type": "reasoning", "summary": []},
            {"type": "function_call_output", "call_id": "c7", "output": SECRET},
        ]
    }
    with pytest.raises(ModelGatewayAPIError) as caught:
        _bare_service()._normalize_responses_input(payload, ai_model=model)
    assert "Offending input item: index=3 type='function_call_output'" in (
        caught.value.message
    )
    assert SECRET not in caught.value.message
