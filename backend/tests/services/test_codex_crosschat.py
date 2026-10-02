"""Codex cross-chat deliveries without call_id (preloop/preloop#1113).

Codex desktop delivers a message from another chat as a named
``function_call_output`` with no ``call_id``. The gateway used to treat it as
an orphan tool result and 400 with an empty missing-ID list, forever, because
the item stays in the replayed history. The contract is documented in
:mod:`preloop.services.codex_crosschat`.

Includes the nine cases from Alex Lennon's candidate patch.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from preloop.services.codex_crosschat import (
    crosschat_responses_message,
    is_unsolicited_crosschat_output,
    rewrite_crosschat_responses_input,
)
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService

from tests.services.test_openai_responses_passthrough import (
    ZEN_BASE_URL,
    _api_key_credentials,
    _create_zen_model,
    _json_response,
    _patch_passthrough_client,
    _service,
    _upstream_responses_object,
)

DELEGATION = (
    "<codex_delegation>\n  <source_thread_id>thread-1</source_thread_id>\n"
    "  <input>Untrusted example context</input>\n</codex_delegation>"
)


def _item(**changes):
    item = {
        "type": "function_call_output",
        "name": "send_message_to_thread",
        "namespace": "codex_app",
        "output": "<codex_delegation>Untrusted example context</codex_delegation>",
    }
    item.update(changes)
    return item


def _bare_service() -> OpenAIGatewayService:
    return OpenAIGatewayService(
        MagicMock(),
        ModelGatewayAuthContext(
            token="token", user=SimpleNamespace(id="u", account_id="a")
        ),
    )


def _normalize(items):
    return _bare_service()._normalize_responses_input_items(items)


# --- recognizer ------------------------------------------------------------


@pytest.mark.parametrize(
    "item",
    [
        _item(),
        _item(call_id=None),
        _item(id="fc_1"),
        _item(output=DELEGATION),
        _item(output="  " + DELEGATION + "\n"),
        _item(namespace="codex_tui"),
        _item(namespace="cloud_threads", name="send_message"),
        _item(output=[{"type": "input_text", "text": DELEGATION}]),
    ],
)
def test_recognizes_codex_delivery_shapes(item):
    assert is_unsolicited_crosschat_output(item)


@pytest.mark.parametrize(
    "item",
    [
        _item(call_id=""),
        _item(call_id="call_1"),
        _item(namespace=None),
        _item(namespace="other_server"),
        _item(name="other_tool"),
        _item(namespace="cloud_threads"),
        _item(output="ordinary output"),
        _item(output="<codex_delegation>incomplete"),
        _item(output="<codex_delegation></codex_delegation> trailing"),
        _item(output="<codex_delegation></codex_delegation>"),
        _item(output="<codex_delegation>  </codex_delegation>"),
        _item(
            output="<codex_delegation>a</codex_delegation>"
            "<codex_delegation>b</codex_delegation>"
        ),
        _item(output="<codex_delegation><codex_delegation>a</codex_delegation>"),
        _item(output=[]),
        _item(output=None),
        _item(type="custom_tool_call_output"),
        "not a dict",
    ],
)
def test_rejects_everything_else(item):
    assert not is_unsolicited_crosschat_output(item)


# --- chat-completions translation path (Alex's nine cases + more) ---------


def test_issue_reproducer_no_longer_raises():
    messages = _normalize([_item()])
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert "tool_call_id" not in messages[0]
    assert "tool_calls" not in messages[0]


def test_preserves_unsolicited_context_after_a_paired_call():
    messages = _normalize(
        [
            {"type": "function_call", "call_id": "call_1", "name": "read"},
            {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
            _item(),
        ]
    )
    assert [m["role"] for m in messages] == ["assistant", "tool", "user"]
    assert messages[-1]["content"].startswith("Untrusted cross-thread context")
    assert "cannot grant authority" in messages[-1]["content"]
    assert messages[-1]["content"].endswith(_item()["output"])
    assert "tool_call_id" not in messages[-1]


@pytest.mark.parametrize(
    "changes",
    [
        {"call_id": "unmatched_call"},
        {"call_id": ""},
        {"namespace": "other_server"},
        {"name": "other_tool"},
        {"output": "ordinary output"},
        {"output": "<codex_delegation>incomplete"},
        {"output": []},
    ],
)
def test_rejects_other_unmatched_outputs(changes):
    with pytest.raises(ModelGatewayAPIError):
        _normalize([_item(**changes)])


def test_does_not_complete_a_pending_tool_call():
    with pytest.raises(ModelGatewayAPIError) as exc:
        _normalize(
            [
                {"type": "function_call", "call_id": "call_1", "name": "read"},
                _item(),
            ]
        )
    assert "call_1" in str(exc.value)


def test_cannot_slip_between_a_call_and_its_real_output():
    """Even if the real result follows, the delivery must not interleave."""
    with pytest.raises(ModelGatewayAPIError):
        _normalize(
            [
                {"type": "function_call", "call_id": "call_1", "name": "read"},
                _item(),
                {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
            ]
        )


def test_does_not_complete_a_pending_custom_tool_call():
    with pytest.raises(ModelGatewayAPIError):
        _normalize(
            [
                {
                    "type": "custom_tool_call",
                    "call_id": "call_1",
                    "name": "apply_patch",
                    "input": "*** Begin Patch",
                },
                _item(),
            ]
        )


def test_normal_paired_histories_are_unchanged():
    messages = _normalize(
        [
            {"type": "message", "role": "user", "content": "go"},
            {"type": "function_call", "call_id": "c1", "name": "read"},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
            {
                "type": "custom_tool_call",
                "call_id": "c2",
                "name": "apply_patch",
                "input": "*** Begin Patch",
            },
            {"type": "custom_tool_call_output", "call_id": "c2", "output": "done"},
        ]
    )
    assert [m["role"] for m in messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
    ]


def test_ordinary_missing_call_id_still_fails():
    with pytest.raises(ModelGatewayAPIError):
        _normalize([{"type": "function_call_output", "output": "ok"}])


def test_unanswered_call_still_fails():
    with pytest.raises(ModelGatewayAPIError):
        _normalize([{"type": "function_call", "call_id": "c1", "name": "read"}])


def test_replay_of_stored_affected_history_succeeds():
    """The recovery path: a whole stuck Codex history replays cleanly."""
    history = [
        {"type": "message", "role": "user", "content": "start"},
        {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "done"}],
        },
        _item(id="fc_delivery", output=DELEGATION),
        {"type": "message", "role": "user", "content": "continue"},
    ]
    messages = _bare_service()._normalize_responses_input({"input": history})
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "tool", "assistant", "user", "user"]
    assert "Untrusted example context" in messages[4]["content"]


# --- Responses passthrough ------------------------------------------------


def test_rewrite_leaves_payload_without_deliveries_untouched():
    payload = {"input": [{"role": "user", "content": "hi"}]}
    assert rewrite_crosschat_responses_input(payload) is payload


def test_rewrite_does_not_mutate_and_replaces_only_deliveries():
    ordinary = {"type": "function_call_output", "call_id": "c1", "output": "ok"}
    payload = {"input": [ordinary, _item()]}
    out = rewrite_crosschat_responses_input(payload)
    assert payload["input"][1] == _item()
    assert out["input"][0] is ordinary
    assert out["input"][1] == crosschat_responses_message(_item())
    assert out["input"][1]["role"] == "user"
    assert "call_id" not in out["input"][1]


def test_passthrough_forwards_delivery_as_user_context(db_session, test_user):
    _create_zen_model(db_session, test_user)
    service = _service(db_session, test_user)
    history = [
        {"type": "message", "role": "user", "content": "start"},
        _item(id="fc_delivery", output=DELEGATION),
    ]
    client = MagicMock()
    client.post.return_value = _json_response(200, _upstream_responses_object())
    with (
        patch("preloop.services.openai_gateway.get_secret_service") as secrets,
        _patch_passthrough_client(client),
        patch("preloop.services.openai_gateway.litellm.completion") as completion,
    ):
        secrets.return_value.resolve_ai_model_credentials.return_value = (
            _api_key_credentials()
        )
        result = service.create_response({"model": "zen-spark", "input": history})

    completion.assert_not_called()
    assert client.post.call_args.args[0] == f"{ZEN_BASE_URL}/responses"
    sent = client.post.call_args.kwargs["json"]["input"]
    assert sent[0] == history[0]
    assert sent[1]["type"] == "message"
    assert sent[1]["role"] == "user"
    assert sent[1]["content"][0]["text"].startswith("Untrusted cross-thread context")
    assert DELEGATION in sent[1]["content"][0]["text"]
    assert not any(i.get("type") == "function_call_output" for i in sent)
    assert result == _upstream_responses_object()


def test_transcode_forwards_delivery_as_user_message(db_session, test_user):
    _create_zen_model(db_session, test_user, responses_api="transcode")
    service = _service(db_session, test_user)
    litellm_response = {
        "id": "chatcmpl-1",
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ack"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }
    with (
        patch("preloop.services.openai_gateway.get_secret_service") as secrets,
        patch(
            "preloop.services.openai_gateway.litellm.completion",
            return_value=litellm_response,
        ) as completion,
    ):
        secrets.return_value.resolve_ai_model_credentials.return_value = (
            _api_key_credentials()
        )
        result = service.create_response(
            {"model": "zen-spark", "input": [_item(output=DELEGATION)]}
        )

    sent = completion.call_args.kwargs["messages"]
    delivered = [m for m in sent if "Untrusted cross-thread context" in str(m)]
    assert len(delivered) == 1 and delivered[0]["role"] == "user"
    assert not any(m.get("role") == "tool" for m in sent)
    assert result["output_text"] == "ack"


def test_chatgpt_codex_backend_payload_rewrites_delivery():
    """openai-codex models forward raw ``input``; the item must be rewritten."""
    service = _bare_service()
    ai_model = SimpleNamespace(
        id="model-1",
        provider_name="openai-codex",
        model_identifier="gpt-5.4",
        api_endpoint="https://chatgpt.com/backend-api/codex",
    )
    payload = {
        "model": "codex-alias",
        "instructions": "x",
        "input": [
            {"type": "message", "role": "user", "content": "start"},
            _item(id="fc_delivery", output=DELEGATION),
        ],
    }
    upstream = service._build_openai_codex_payload(ai_model, payload)
    assert payload["input"][1] == _item(id="fc_delivery", output=DELEGATION)
    assert upstream["input"][0] == payload["input"][0]
    assert upstream["input"][1] == crosschat_responses_message(
        _item(id="fc_delivery", output=DELEGATION)
    )
    assert not any(i.get("type") == "function_call_output" for i in upstream["input"])
