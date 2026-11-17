"""A terminal Responses event ends a turn even when HTTP never reaches EOF."""

import json
import time
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import httpx
import pytest

from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService


def service() -> OpenAIGatewayService:
    """Build a gateway without opening a database or provider connection."""
    return OpenAIGatewayService(
        MagicMock(),
        ModelGatewayAuthContext(
            token="test", user=SimpleNamespace(id="user", account_id="account")
        ),
    )


@pytest.mark.parametrize("client_closes", [False, True])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("snapshot_only", [False, True])
@pytest.mark.parametrize("late_error", [httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_native_terminal_records_usage_without_reading_eof(
    client_closes: bool,
    newline: str,
    snapshot_only: bool,
    late_error: type[Exception],
) -> None:
    """Reading past completed must not convert a delivered turn into a 502."""
    gateway = service()
    terminal = {
        "type": "response.completed",
        "response": {
            "id": "resp_example",
            "status": "completed",
            "output_text": "Result",
            "usage": {"input_tokens": 8194, "output_tokens": 49},
        },
    }
    if snapshot_only:
        terminal["response"].pop("output_text")
        terminal["response"]["output"] = [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Result"}],
            }
        ]

    def chunks() -> Iterator[str]:
        yield "data: " + json.dumps(terminal) + newline * 2
        raise late_error("Upstream reset the connection after completing the turn")

    upstream = MagicMock()
    upstream.hosted_call = None
    upstream.iter_text.side_effect = chunks
    with patch.object(gateway, "_record_gateway_request") as record:
        stream = gateway._openai_responses_passthrough_event_stream(
            upstream,
            ai_model=SimpleNamespace(provider_name="openai-compatible"),
            payload={"model": "example-coder", "input": "Review"},
            budget_result=None,
            started_at=time.perf_counter(),
        )
        if client_closes:
            next(stream)
            stream.close()
        else:
            events = list(stream)
            assert not any('"type": "error"' in event for event in events)
        gateway.flush_deferred_stream_record()
    record.assert_called_once()
    assert record.call_args.kwargs["status_code"] == 200
    assert record.call_args.kwargs["upstream_response"]["usage"]["input_tokens"] == 8194
    assert record.call_args.kwargs["accumulated_output_text"] == "Result"
    upstream.close.assert_called_once()


def test_codex_aggregator_stops_at_completed() -> None:
    """The Codex adapter must stop consuming when the model turn finishes."""

    def events() -> Iterator[dict[str, Any]]:
        yield {
            "type": "response.completed",
            "response": {
                "id": "resp_example",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 8194, "output_tokens": 49},
            },
        }
        raise httpx.ReadTimeout("The upstream never closes its body")

    assert service()._aggregate_codex_events(events())["usage"]["input_tokens"] == 8194


@pytest.mark.parametrize(
    "snapshot",
    [{"output": []}, {"output": [{"type": "reasoning"}]}, {"output_text": ""}],
)
def test_empty_terminal_snapshot_retains_streamed_text(
    snapshot: dict[str, Any],
) -> None:
    """An empty terminal summary cannot erase deltas already received."""
    gateway = service()
    upstream = MagicMock()
    upstream.hosted_call = None
    upstream.iter_text.return_value = iter(
        [
            "data: "
            + json.dumps(
                {"type": "response.output_text.delta", "delta": "Reviewed the change"}
            )
            + "\n\n",
            "data: "
            + json.dumps(
                {
                    "type": "response.completed",
                    "response": {"status": "completed", **snapshot},
                }
            )
            + "\n\n",
        ]
    )
    with patch.object(gateway, "_record_gateway_request") as record:
        list(
            gateway._openai_responses_passthrough_event_stream(
                upstream,
                ai_model=SimpleNamespace(provider_name="openai-compatible"),
                payload={"input": "Review"},
                budget_result=None,
                started_at=time.perf_counter(),
            )
        )
        gateway.flush_deferred_stream_record()
    assert record.call_args.kwargs["accumulated_output_text"] == "Reviewed the change"
