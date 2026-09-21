"""Returned reasoning is governed text, including buffered streaming replies."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from preloop.services.model_content_policy import (
    _approval_arguments,
    canonical_response_text,
    evaluate_model_io,
    wrap_stream_for_response_policy,
)
from preloop.services.policy.schema import ModelIORule

EMAIL = "alice@example.com"


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": [{"message": {"content": "VALID", "reasoning_content": EMAIL}}]},
        {"choices": [{"message": {"content": "VALID", "reasoning": EMAIL}}]},
        {
            "choices": [
                {
                    "message": {
                        "content": "VALID",
                        "reasoning_details": [
                            {"type": "reasoning.text", "text": EMAIL}
                        ],
                    }
                }
            ]
        },
        {
            "content": [
                {"type": "thinking", "thinking": EMAIL},
                {"type": "text", "text": "VALID"},
            ]
        },
        {
            "output": [
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": EMAIL}],
                },
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "VALID"}],
                },
            ]
        },
        {
            "candidates": [
                {
                    "content": {
                        "parts": [{"thought": True, "text": EMAIL}, {"text": "VALID"}]
                    }
                }
            ]
        },
        {
            "choices": [
                {"message": {"content": "VALID"}},
                {"message": {"content": EMAIL}},
            ]
        },
    ],
)
def test_reasoning_response_shapes_are_scanned(payload: dict) -> None:
    text = canonical_response_text(payload)
    assert EMAIL in text
    assert "VALID" in text


@pytest.mark.parametrize("include_reasoning", [True, False])
@pytest.mark.parametrize("summary", [[], [{"text": "Safe summary"}]])
def test_output_text_is_scanned_alongside_reasoning_items(
    include_reasoning: bool, summary: list[dict]
) -> None:
    payload = {
        "output": [{"type": "reasoning", "summary": summary}],
        "output_text": EMAIL,
    }
    text = canonical_response_text(payload, include_reasoning=include_reasoning)
    assert EMAIL in text
    if include_reasoning and summary:
        assert "Safe summary" in text


def test_opaque_reasoning_is_not_treated_as_plaintext() -> None:
    payload = {
        "content": [
            {"type": "redacted_thinking", "data": EMAIL},
            {"type": "text", "text": "VALID", "signature": EMAIL},
        ]
    }
    assert canonical_response_text(payload) == "VALID"


@pytest.mark.parametrize(
    "fragments",
    [
        [
            {
                "type": "response.completed",
                "response": {
                    "output": [{"type": "reasoning", "summary": []}],
                    "output_text": EMAIL,
                },
            }
        ],
        [
            {
                "type": "response.completed",
                "response": {"output": None, "output_text": EMAIL},
            }
        ],
        [
            {"choices": [{"delta": {"reasoning_content": "alice@"}}]},
            {"choices": [{"delta": {"reasoning_content": "example.com"}}]},
        ],
        [{"choices": [{"delta": {"reasoning": EMAIL}}]}],
        [{"choices": [{"delta": {"reasoning_details": [{"text": EMAIL}]}}]}],
        [
            {
                "type": "content_block_delta",
                "delta": {"type": "thinking_delta", "thinking": EMAIL},
            }
        ],
        [
            {
                "type": "content_block_start",
                "content_block": {"type": "thinking", "thinking": EMAIL},
            }
        ],
        [
            {"type": "response.reasoning_summary_text.delta", "delta": "alice@"},
            {"type": "response.reasoning_summary_text.delta", "delta": "example.com"},
            {"type": "response.completed", "response": {"output_text": "VALID"}},
        ],
        [
            {
                "type": "response.completed",
                "response": {
                    "output": [
                        {"type": "reasoning", "summary": [{"text": EMAIL}]},
                        {"type": "message", "content": [{"text": "VALID"}]},
                    ]
                },
            }
        ],
        [
            {"type": "response.reasoning_summary_text.delta", "delta": EMAIL},
            {
                "type": "response.completed",
                "response": {
                    "output": [
                        {"type": "reasoning", "summary": [{"text": "No identifiers"}]}
                    ]
                },
            },
        ],
    ],
)
@pytest.mark.parametrize("action", ["deny", "require_approval"])
def test_streamed_reasoning_cannot_escape_policy(
    fragments: list[dict], action: str
) -> None:
    gateway = SimpleNamespace(
        db=MagicMock(),
        auth_context=SimpleNamespace(user=SimpleNamespace(account_id="acct", id="u")),
        _openai_stream_error_event=lambda exc, _err: f"data: {exc.code}\n\n",
        _sse_done=lambda: "data: [DONE]\n\n",
    )
    rule = ModelIORule.model_validate(
        {
            "id": "pii",
            "target": "model.response",
            "conditions": [{"expression": "pii.found == true", "action": action}],
        }
    )
    events = [f"data: {json.dumps(fragment)}\n\n" for fragment in fragments]
    events.append('data: {"choices":[{"delta":{"content":"VALID"}}]}\n\n')
    with (
        patch(
            "preloop.services.model_content_policy.load_model_io_rules",
            return_value=[rule],
        ),
        patch(
            "preloop.services.model_content_policy.hold_for_model_io_approval",
            new=MagicMock(return_value=False),
        ) as hold,
    ):
        result = "".join(
            wrap_stream_for_response_policy(
                iter(events),
                gateway=gateway,
                payload={},
                ai_model=None,
                provider="openai",
            )
        )
    assert "content_policy_denied" in result
    assert EMAIL not in result
    assert "VALID" not in result
    if action == "require_approval":
        hold.assert_called_once()
        arguments = _approval_arguments(
            hold.call_args.kwargs["decision"], "model.response"
        )
        assert EMAIL not in json.dumps(arguments)
        assert len(arguments["text_sha256"]) == 64


def test_reasoning_approval_snapshot_stores_hash_without_raw_text() -> None:
    rule = ModelIORule.model_validate(
        {
            "id": "approve-pii",
            "target": "model.response",
            "conditions": [
                {"expression": "pii.found == true", "action": "require_approval"}
            ],
        }
    )
    text = canonical_response_text(
        {"choices": [{"message": {"content": "VALID", "reasoning_content": EMAIL}}]}
    )
    decision = evaluate_model_io(rules=[rule], target="model.response", text=text)
    assert decision.action == "require_approval"
    arguments = _approval_arguments(decision, "model.response")
    assert len(arguments["text_sha256"]) == 64
    assert EMAIL not in json.dumps(arguments)
    assert "VALID" not in json.dumps(arguments)
