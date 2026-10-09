"""Codex OAuth path keeps upstream prompt-cache affinity (issue #1440).

The ChatGPT Codex backend derives cache affinity from the ``session-id``
header and sticky-routes a turn with ``x-codex-turn-state``. These tests pin
what Preloop forwards upstream and relays back across two consecutive calls.
"""

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from preloop.api.endpoints.openai_gateway import (
    _streaming_with_gateway_warnings,
    _with_gateway_warnings,
)
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService

_SSE = (
    b'data: {"type":"response.output_item.added","item":'
    b'{"id":"msg_1","type":"message","role":"assistant"}}\n\n'
    b'data: {"type":"response.output_text.done","item_id":"msg_1","text":"OK"}\n\n'
    b'data: {"type":"response.completed","response":{"id":"resp_1",'
    b'"usage":{"input_tokens":1,"output_tokens":1}}}\n\n'
)


class _FakeResponse:
    def __init__(self, headers: Dict[str, str]):
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        return iter(_SSE.splitlines(keepends=True))


def _service(headers: Optional[Dict[str, str]] = None) -> OpenAIGatewayService:
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    return OpenAIGatewayService(
        MagicMock(), auth_context, client_identity_headers=headers
    )


def _model() -> SimpleNamespace:
    return SimpleNamespace(
        id="model-1",
        provider_name="openai-codex",
        model_identifier="gpt-5.6-sol",
        api_endpoint="https://chatgpt.com/backend-api/codex",
    )


def _payload(turns: int) -> Dict[str, Any]:
    items = []
    for i in range(turns):
        items.append(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": f"step {i}"}],
            }
        )
    return {
        "model": "preloop/openai/gpt-5.6-sol",
        "instructions": "Be terse.",
        "input": items,
        "prompt_cache_key": "conv-123",
        "stream": True,
    }


def _call(
    service: OpenAIGatewayService,
    payload: Dict[str, Any],
    upstream_headers: Dict[str, str],
    sent: List[Any],
) -> None:
    def fake_urlopen(req, timeout=None):
        sent.append(req)
        return _FakeResponse(upstream_headers)

    credentials = SimpleNamespace(value="oauth", payload={"account_id": "acct-1"})
    with (
        patch.object(
            service, "_resolve_openai_codex_credentials", return_value=credentials
        ),
        patch(
            "preloop.services.openai_gateway.urllib_request.urlopen",
            side_effect=fake_urlopen,
        ),
        patch(
            "preloop.services.hosted_spend_guard.guard_unmetered_hosted_call",
            return_value=None,
        ),
    ):
        service._create_openai_codex_response(_model(), payload)


def _hdr(req: Any, name: str) -> Optional[str]:
    # urllib.request.Request capitalizes header names ("Session-id").
    return {k.lower(): v for k, v in req.header_items()}.get(name)


def test_forwards_client_session_and_turn_state_and_keeps_prefix():
    client_headers = {
        "Session-Id": "sess-client",
        "thread-id": "thread-1",
        "x-client-request-id": "thread-1",
        "x-codex-window-id": "win-1",
        "x-codex-turn-metadata": '{"turn":1}',
        "x-codex-parent-thread-id": "parent-1",
        "x-openai-subagent": "review",
        "x-codex-turn-state": "ts-from-client",
        "Authorization": "Bearer preloop-key",
    }
    sent: List[Any] = []
    first = _service(client_headers)
    _call(first, _payload(2), {"x-codex-turn-state": "ts-upstream-1"}, sent)
    second = _service(client_headers)
    _call(second, _payload(4), {"x-codex-turn-state": "ts-upstream-2"}, sent)

    for req in sent:
        # (a) session-id is the client's, not the prompt_cache_key.
        assert _hdr(req, "session-id") == "sess-client"
        # (c) the client's turn-state reaches the upstream.
        assert _hdr(req, "x-codex-turn-state") == "ts-from-client"
        for name in (
            "thread-id",
            "x-client-request-id",
            "x-codex-window-id",
            "x-codex-turn-metadata",
            "x-codex-parent-thread-id",
            "x-openai-subagent",
        ):
            assert _hdr(req, name) == client_headers[name]
        # Honest identity is kept; the ingress credential is never relayed.
        assert _hdr(req, "originator") == "preloop"
        assert _hdr(req, "user-agent") == "Preloop/1.0"
        assert _hdr(req, "authorization") == "Bearer oauth"

    # (c) the upstream's turn-state is surfaced for the client.
    assert first.codex_turn_state == "ts-upstream-1"
    assert second.codex_turn_state == "ts-upstream-2"

    # (d) body prefix unchanged: call 1's input is a prefix of call 2's.
    body1 = json.loads(sent[0].data)
    body2 = json.loads(sent[1].data)
    assert body1["instructions"] == body2["instructions"]
    assert body2["input"][: len(body1["input"])] == body1["input"]
    assert body1["prompt_cache_key"] == body2["prompt_cache_key"] == "conv-123"
    assert "prompt_cache_retention" not in body1


def test_session_id_falls_back_to_prompt_cache_key():
    sent: List[Any] = []
    for turns in (2, 4):
        _call(_service({"User-Agent": "other"}), _payload(turns), {}, sent)
    for req in sent:
        assert _hdr(req, "session-id") == "conv-123"
        assert _hdr(req, "x-codex-turn-state") is None


def test_no_session_id_without_prompt_cache_key():
    sent: List[Any] = []
    payload = _payload(1)
    payload.pop("prompt_cache_key")
    service = _service()
    _call(service, payload, {}, sent)
    assert _hdr(sent[0], "session-id") is None
    assert service.codex_turn_state is None


def test_unprintable_or_oversized_routing_headers_are_dropped():
    sent: List[Any] = []
    service = _service({"session-id": "bad\nvalue", "thread-id": "x" * 300})
    _call(service, _payload(1), {"x-codex-turn-state": "bad\x01"}, sent)
    assert _hdr(sent[0], "session-id") == "conv-123"
    assert _hdr(sent[0], "thread-id") is None
    assert service.codex_turn_state is None


def _stub_service(turn_state: Optional[str]) -> SimpleNamespace:
    return SimpleNamespace(
        response_warning=None,
        last_usage_id=None,
        codex_turn_state=turn_state,
        flush_deferred_stream_record=lambda: None,
    )


def test_endpoint_relays_turn_state_non_streaming():
    response = _with_gateway_warnings({"id": "r"}, _stub_service("ts-up"))
    assert response.headers["x-codex-turn-state"] == "ts-up"
    assert _with_gateway_warnings({"id": "r"}, _stub_service(None)) == {"id": "r"}


def test_endpoint_relays_turn_state_streaming():
    response = _streaming_with_gateway_warnings(
        iter(["data: x\n\n"]), _stub_service("ts-up")
    )
    assert response.headers["x-codex-turn-state"] == "ts-up"
    plain = _streaming_with_gateway_warnings(iter([]), _stub_service(None))
    assert "x-codex-turn-state" not in plain.headers
