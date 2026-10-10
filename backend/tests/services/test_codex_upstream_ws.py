"""Codex upstream Responses WebSocket transport (issue #1454).

Covers the Codex incremental rule (incremental vs full and each mismatch
reason), the fallbacks (previous_response_not_found, closed socket, age cap,
auth change, handshake failure -> HTTP), turn-state capture and return,
transport parity with the HTTP path, the usage meta fields, and that the
flag off leaves the HTTP path untouched.
"""

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from preloop.services import codex_upstream_ws as cw
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import (
    ModelGatewayAPIError,
    OpenAIGatewayService,
)

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _user_msg(text: str) -> Dict[str, Any]:
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


def _tool_call(n: int) -> Dict[str, Any]:
    return {
        "id": f"ctc_{n}",
        "type": "custom_tool_call",
        "status": "completed",
        "call_id": f"call_{n}",
        "name": "exec",
        "input": f"run {n}",
    }


def _tool_output(n: int) -> Dict[str, Any]:
    return {
        "type": "custom_tool_call_output",
        "call_id": f"call_{n}",
        "output": f"out {n}",
    }


def _payload(input_items: List[Any], **overrides: Any) -> Dict[str, Any]:
    payload = {
        "model": "gpt-6.1-sol",
        "instructions": "Be terse.",
        "input": input_items,
        "tools": [{"type": "custom", "name": "exec"}],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "reasoning": {"effort": "medium"},
        "store": False,
        "stream": True,
        "include": [],
        "prompt_cache_key": "conv-1",
    }
    payload.update(overrides)
    return payload


def _entry(**chain: Any) -> cw.WsEntry:
    entry = cw.WsEntry(
        key=("acct", "sess"), socket=None, auth_digest="d", opened_at=0, last_used=0
    )
    for name, value in chain.items():
        setattr(entry, name, value)
    return entry


def _chained_entry(first_input: List[Any], output: List[Any]) -> cw.WsEntry:
    return _entry(
        fingerprint=cw.request_fingerprint(_payload(first_input)),
        last_input=first_input,
        last_response_id="resp_1",
        last_output=output,
    )


# --------------------------------------------------------------------------
# Diff rule
# --------------------------------------------------------------------------


def test_plan_full_without_previous_request():
    plan = cw.plan_request(None, _payload([_user_msg("a")]))
    assert (plan.mode, plan.reason) == ("full", "no_previous_request")
    plan = cw.plan_request(_entry(), _payload([_user_msg("a")]))
    assert (plan.mode, plan.reason) == ("full", "no_previous_request")


def test_plan_full_without_previous_response():
    entry = _entry(
        last_input=[_user_msg("a")],
        fingerprint=cw.request_fingerprint(_payload([])),
    )
    plan = cw.plan_request(entry, _payload([_user_msg("a")]))
    assert (plan.mode, plan.reason) == ("full", "no_previous_response")


def test_plan_incremental_sends_only_new_items():
    first = [_user_msg("a")]
    entry = _chained_entry(first, [_tool_call(1)])
    current = first + [_tool_call(1), _tool_output(1)]
    plan = cw.plan_request(entry, _payload(current))
    assert plan.mode == "incremental"
    assert plan.previous_response_id == "resp_1"
    assert plan.items == [_tool_output(1)]


def test_plan_ignores_ids_status_and_codex_passthrough_metadata():
    """The CLI resends output items with its own passthrough metadata."""
    first = [_user_msg("a")]
    entry = _chained_entry(first, [_tool_call(1)])
    resent = {key: value for key, value in _tool_call(1).items() if key not in ("id",)}
    resent["status"] = "in_progress"
    resent["internal_chat_message_metadata_passthrough"] = {"create_time": 1.5}
    plan = cw.plan_request(entry, _payload(first + [resent, _tool_output(1)]))
    assert plan.mode == "incremental"
    assert plan.items == [_tool_output(1)]


def test_plan_ignores_upstream_item_metadata_the_cli_drops():
    """Live gpt-6.1-sol output items carry ``metadata``; the CLI resends
    them without it (its typed item has no such field)."""
    first = [_user_msg("a")]
    upstream_item = {**_tool_call(1), "metadata": {"server": "annotation"}}
    entry = _chained_entry(first, [upstream_item])
    plan = cw.plan_request(entry, _payload(first + [_tool_call(1), _tool_output(1)]))
    assert plan.mode == "incremental"


@pytest.mark.parametrize(
    "override",
    [
        {"model": "gpt-other"},
        {"instructions": "Different."},
        {"tools": []},
        {"tool_choice": "none"},
        {"parallel_tool_calls": True},
        {"reasoning": {"effort": "high"}},
        {"include": ["reasoning.encrypted_content"]},
        {"service_tier": "priority"},
        {"prompt_cache_key": "conv-2"},
        {"text": {"verbosity": "low"}},
    ],
)
def test_plan_full_when_a_property_changes(override):
    first = [_user_msg("a")]
    entry = _chained_entry(first, [_tool_call(1)])
    current = first + [_tool_call(1), _tool_output(1)]
    plan = cw.plan_request(entry, _payload(current, **override))
    assert (plan.mode, plan.reason) == ("full", "properties_changed")
    assert plan.items == current


def test_client_metadata_change_keeps_incremental():
    first = [_user_msg("a")]
    entry = _chained_entry(first, [_tool_call(1)])
    current = first + [_tool_call(1), _tool_output(1)]
    plan = cw.plan_request(
        entry, _payload(current, client_metadata={"x-codex-turn-state": "t2"})
    )
    assert plan.mode == "incremental"


def test_plan_full_when_input_shortened():
    first = [_user_msg("a"), _user_msg("b")]
    entry = _chained_entry(first, [_tool_call(1)])
    plan = cw.plan_request(entry, _payload([_user_msg("a")]))
    assert (plan.mode, plan.reason) == ("full", "input_shortened")


def test_plan_full_when_history_was_rewritten():
    """E.g. an operator note or redaction changed an earlier item."""
    first = [_user_msg("a")]
    entry = _chained_entry(first, [_tool_call(1)])
    current = [_user_msg("a [redacted]"), _tool_call(1), _tool_output(1)]
    plan = cw.plan_request(entry, _payload(current))
    assert (plan.mode, plan.reason) == ("full", "input_mismatch")


def test_plan_full_when_response_items_were_not_resent():
    first = [_user_msg("a")]
    entry = _chained_entry(first, [_tool_call(1)])
    plan = cw.plan_request(entry, _payload(first + [_user_msg("next")]))
    assert (plan.mode, plan.reason) == ("full", "input_mismatch")


def test_build_frame_carries_turn_state_in_client_metadata():
    payload = _payload([_user_msg("a")], client_metadata={"k": "v"})
    plan = cw.IncrementalPlan(
        mode="incremental",
        reason="incremental",
        items=[_tool_output(1)],
        previous_response_id="resp_9",
    )
    frame = cw.build_frame(payload, plan, "ts-client")
    assert frame["type"] == "response.create"
    assert frame["previous_response_id"] == "resp_9"
    assert frame["input"] == [_tool_output(1)]
    assert frame["client_metadata"] == {"k": "v", "x-codex-turn-state": "ts-client"}
    assert frame["instructions"] == "Be terse."
    full = cw.build_frame(payload, cw.plan_request(None, payload), None)
    assert "previous_response_id" not in full
    assert full["client_metadata"] == {"k": "v"}


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_registry_lru_bound_and_sweep():
    clock = _Clock()
    registry = cw.CodexWsRegistry(max_entries=2, idle_timeout_s=60, clock=clock)
    sockets = [MagicMock() for _ in range(3)]
    for index, socket in enumerate(sockets):
        registry.put(
            cw.WsEntry(
                key=("a", str(index)),
                socket=socket,
                auth_digest="d",
                opened_at=clock.now,
                last_used=clock.now,
            )
        )
    assert len(registry) == 2
    sockets[0].close.assert_called_once()
    clock.now += 61
    assert registry.sweep() == 2
    assert len(registry) == 0


def test_registry_age_cap_and_http_only_ttl():
    clock = _Clock()
    registry = cw.CodexWsRegistry(http_only_ttl_s=30, clock=clock)
    entry = cw.WsEntry(
        key=("a", "s"),
        socket=MagicMock(),
        auth_digest="d",
        opened_at=clock.now,
        last_used=clock.now,
    )
    clock.now += 55 * 60
    entry.last_used = clock.now
    assert registry.expiry_reason(entry) == "age_cap"
    registry.mark_http_only(("a", "s"), "http_426")
    assert registry.http_only_reason(("a", "s")) == "http_426"
    clock.now += 31
    assert registry.http_only_reason(("a", "s")) is None


# --------------------------------------------------------------------------
# Service wiring with a fake upstream socket
# --------------------------------------------------------------------------


def _events(response_id: str, item: Dict[str, Any], turn_state: Optional[str] = None):
    events: List[Dict[str, Any]] = []
    if turn_state:
        events.append(
            {
                "type": "codex.response.metadata",
                "headers": {"X-Codex-Turn-State": turn_state},
            }
        )
    events += [
        {"type": "response.created", "response": {"id": response_id}},
        {"type": "response.output_item.added", "item": dict(item)},
        {"type": "response.output_item.done", "item": dict(item)},
        {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "usage": {
                    "input_tokens": 100,
                    "input_tokens_details": {"cached_tokens": 90},
                    "output_tokens": 5,
                },
            },
        },
    ]
    return events


class _FakeSocket:
    """Answers each sent frame with the next scripted list of events."""

    def __init__(self, scripts: List[Any]):
        self.scripts = list(scripts)
        self.sent: List[Dict[str, Any]] = []
        self.pending: List[Any] = []
        self.closed = False

    def send(self, text: str) -> None:
        self.sent.append(json.loads(text))
        script = self.scripts.pop(0)
        if isinstance(script, Exception):
            self.pending = [script]
        else:
            self.pending = [json.dumps(event) for event in script]

    def recv(self, timeout: Optional[float] = None) -> str:
        item = self.pending.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


class _Upstream:
    """Patches the connector, HTTP and the flag for one test."""

    def __init__(self, sockets: List[Any], *, flag: bool = True):
        self.sockets = list(sockets)
        self.handshakes: List[Dict[str, str]] = []
        self.http_requests: List[Any] = []
        self.flag = flag

    def connect(self, headers: Dict[str, str], *, open_timeout: float):
        self.handshakes.append(dict(headers))
        socket = self.sockets.pop(0)
        if isinstance(socket, Exception):
            raise socket
        return socket, getattr(socket, "handshake_turn_state", None)

    def urlopen(self, req, timeout=None):
        self.http_requests.append(req)
        return _HttpResponse()


_HTTP_SSE = (
    b'data: {"type":"response.created","response":{"id":"resp_http"}}\n\n'
    b'data: {"type":"response.output_item.added","item":{"id":"ctc_1",'
    b'"type":"custom_tool_call","status":"completed","call_id":"call_1",'
    b'"name":"exec","input":"run 1"}}\n\n'
    b'data: {"type":"response.output_item.done","item":{"id":"ctc_1",'
    b'"type":"custom_tool_call","status":"completed","call_id":"call_1",'
    b'"name":"exec","input":"run 1"}}\n\n'
    b'data: {"type":"response.completed","response":{"id":"resp_http",'
    b'"usage":{"input_tokens":100,"input_tokens_details":{"cached_tokens":90},'
    b'"output_tokens":5}}}\n\n'
)


class _HttpResponse:
    headers: Dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        return iter(_HTTP_SSE.splitlines(keepends=True))


@pytest.fixture(autouse=True)
def _fresh_registry():
    cw.REGISTRY.clear()
    cw.reset_metrics()
    yield
    cw.REGISTRY.clear()


def _service(headers: Optional[Dict[str, str]] = None) -> OpenAIGatewayService:
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    return OpenAIGatewayService(
        MagicMock(),
        auth_context,
        client_identity_headers=headers
        if headers is not None
        else {"session-id": "sess-1", "thread-id": "thread-1"},
    )


def _model() -> SimpleNamespace:
    return SimpleNamespace(
        id="model-1",
        provider_name="openai-codex",
        model_identifier="gpt-6.1-sol",
        api_endpoint="https://chatgpt.com/backend-api/codex",
    )


def _call(
    upstream: _Upstream,
    payload: Dict[str, Any],
    *,
    service: Optional[OpenAIGatewayService] = None,
    token: str = "oauth",
    account: str = "acct-1",
):
    service = service or _service()
    credentials = SimpleNamespace(value=token, payload={"account_id": account})
    with (
        patch.object(
            service, "_resolve_openai_codex_credentials", return_value=credentials
        ),
        patch.object(service, "_codex_upstream_ws_enabled", return_value=upstream.flag),
        patch.object(cw, "default_connect", side_effect=upstream.connect),
        patch(
            "preloop.services.openai_gateway.urllib_request.urlopen",
            side_effect=upstream.urlopen,
        ),
        patch(
            "preloop.services.hosted_spend_guard.guard_unmetered_hosted_call",
            return_value=None,
        ),
    ):
        return service, service._create_openai_codex_response(_model(), payload)


FIRST = [_user_msg("a")]
SECOND = FIRST + [_tool_call(1), _tool_output(1)]
THIRD = SECOND + [_tool_call(2), _tool_output(2)]


def test_flag_off_uses_http_and_never_connects():
    upstream = _Upstream([], flag=False)
    service, response = _call(upstream, _payload(FIRST))
    assert len(upstream.http_requests) == 1
    assert upstream.handshakes == []
    assert response["id"] == "resp_http"
    assert service._codex_transport_meta == {"transport": "http"}
    assert len(cw.REGISTRY) == 0


def test_warm_socket_goes_incremental_with_handshake_headers():
    socket = _FakeSocket(
        [
            _events("resp_1", _tool_call(1), turn_state="ts-up"),
            _events("resp_2", _tool_call(2)),
        ]
    )
    upstream = _Upstream([socket])
    headers = {
        "session-id": "sess-1",
        "thread-id": "thread-1",
        "x-codex-window-id": "win-1",
        "x-codex-turn-state": "ts-client",
    }
    first, r1 = _call(upstream, _payload(FIRST), service=_service(headers))
    second, r2 = _call(upstream, _payload(SECOND), service=_service(headers))

    assert len(upstream.handshakes) == 1
    handshake = upstream.handshakes[0]
    assert handshake["Authorization"] == "Bearer oauth"
    assert handshake["chatgpt-account-id"] == "acct-1"
    assert handshake["OpenAI-Beta"] == "responses_websockets=2026-02-06"
    assert handshake["originator"] == "preloop"
    assert handshake["User-Agent"] == "Preloop/1.0"
    assert handshake["session-id"] == "sess-1"
    assert handshake["x-codex-window-id"] == "win-1"
    # Turn state rides in client_metadata on WS, never as a handshake header.
    assert "x-codex-turn-state" not in handshake

    full_frame, incremental_frame = socket.sent
    assert full_frame["input"] == FIRST
    assert "previous_response_id" not in full_frame
    assert full_frame["client_metadata"]["x-codex-turn-state"] == "ts-client"
    assert incremental_frame["previous_response_id"] == "resp_1"
    assert incremental_frame["input"] == [_tool_output(1)]

    # Turn state from the metadata event is returned to the client.
    assert first.codex_turn_state == "ts-up"
    assert first._codex_transport_meta == {"transport": "ws", "ws_mode": "full"}
    assert second._codex_transport_meta == {
        "transport": "ws",
        "ws_mode": "incremental",
    }
    assert r2["usage"]["input_tokens_details"]["cached_tokens"] == 90
    metrics = cw.metrics_snapshot()
    assert metrics["mode_full"] == 1 and metrics["mode_incremental"] == 1
    assert metrics["socket_new"] == 1 and metrics["socket_reused"] == 1
    assert upstream.http_requests == []


def test_handshake_turn_state_is_captured():
    socket = _FakeSocket([_events("resp_1", _tool_call(1))])
    socket.handshake_turn_state = "ts-handshake"
    service, _ = _call(_Upstream([socket]), _payload(FIRST))
    assert service.codex_turn_state == "ts-handshake"


def test_previous_response_not_found_reconnects_and_resends_full():
    stale = _FakeSocket(
        [
            _events("resp_1", _tool_call(1)),
            [
                {
                    "type": "error",
                    "status": 400,
                    "error": {"code": "previous_response_not_found", "message": "x"},
                }
            ],
        ]
    )
    fresh = _FakeSocket([_events("resp_2", _tool_call(2))])
    upstream = _Upstream([stale, fresh])
    _call(upstream, _payload(FIRST))
    service, response = _call(upstream, _payload(SECOND))
    assert response["id"] == "resp_2"
    assert stale.closed
    assert fresh.sent[0]["input"] == SECOND
    assert "previous_response_id" not in fresh.sent[0]
    assert service._codex_transport_meta == {"transport": "ws", "ws_mode": "full"}
    assert cw.metrics_snapshot()["fallback_previous_response_not_found"] == 1


def test_closed_warm_socket_reconnects_and_resends_full():
    from websockets.exceptions import ConnectionClosedError

    stale = _FakeSocket(
        [_events("resp_1", _tool_call(1)), ConnectionClosedError(None, None)]
    )
    fresh = _FakeSocket([_events("resp_2", _tool_call(2))])
    upstream = _Upstream([stale, fresh])
    _call(upstream, _payload(FIRST))
    _, response = _call(upstream, _payload(SECOND))
    assert response["id"] == "resp_2"
    assert fresh.sent[0]["input"] == SECOND
    assert cw.metrics_snapshot()["fallback_socket_closed"] == 1


def test_fresh_socket_transport_failure_falls_back_to_http():
    from websockets.exceptions import ConnectionClosedError

    broken = _FakeSocket([ConnectionClosedError(None, None)])
    upstream = _Upstream([broken])
    service, response = _call(upstream, _payload(FIRST))
    assert response["id"] == "resp_http"
    assert len(upstream.http_requests) == 1
    assert service._codex_transport_meta == {"transport": "http"}


def test_age_cap_reconnects_and_resends_full():
    old = _FakeSocket([_events("resp_1", _tool_call(1))])
    new = _FakeSocket([_events("resp_2", _tool_call(2))])
    upstream = _Upstream([old, new])
    _call(upstream, _payload(FIRST))
    entry = cw.REGISTRY.get(("account-1", "sess-1"))
    entry.opened_at -= 55 * 60
    _call(upstream, _payload(SECOND))
    assert old.closed
    assert new.sent[0]["input"] == SECOND
    assert cw.metrics_snapshot()["fallback_age_cap"] == 1


def test_auth_change_reconnects():
    old = _FakeSocket([_events("resp_1", _tool_call(1))])
    new = _FakeSocket([_events("resp_2", _tool_call(2))])
    upstream = _Upstream([old, new])
    _call(upstream, _payload(FIRST), token="oauth-1")
    _call(upstream, _payload(SECOND), token="oauth-2")
    assert old.closed
    assert upstream.handshakes[1]["Authorization"] == "Bearer oauth-2"
    assert "previous_response_id" not in new.sent[0]
    assert cw.metrics_snapshot()["fallback_auth_changed"] == 1


@pytest.mark.parametrize("status", [426, 404, 403])
def test_handshake_refusal_falls_back_to_http_and_marks_http_only(status):
    upstream = _Upstream([cw.CodexWsHandshakeError(f"http_{status}", status=status)])
    service, response = _call(upstream, _payload(FIRST))
    assert response["id"] == "resp_http"
    assert service._codex_transport_meta == {"transport": "http"}
    assert cw.REGISTRY.http_only_reason(("account-1", "sess-1")) == f"http_{status}"
    # The next call of the session skips the handshake entirely.
    _call(upstream, _payload(SECOND))
    assert len(upstream.handshakes) == 1
    assert len(upstream.http_requests) == 2
    assert cw.metrics_snapshot()["handshake_failure"] == 1


def test_no_session_id_uses_http():
    payload = _payload(FIRST)
    payload.pop("prompt_cache_key")
    upstream = _Upstream([])
    _, response = _call(upstream, payload, service=_service({}))
    assert response["id"] == "resp_http"
    assert upstream.handshakes == []


def test_full_resend_error_surfaces_like_http():
    socket = _FakeSocket(
        [[{"type": "error", "status": 429, "error": {"message": "slow down"}}]]
    )
    with pytest.raises(ModelGatewayAPIError) as raised:
        _call(_Upstream([socket]), _payload(FIRST))
    assert raised.value.status_code == 429
    assert len(cw.REGISTRY) == 0


def test_busy_socket_falls_back_to_http():
    socket = _FakeSocket([_events("resp_1", _tool_call(1))])
    upstream = _Upstream([socket])
    _call(upstream, _payload(FIRST))
    entry = cw.REGISTRY.get(("account-1", "sess-1"))
    with entry.lock:
        _, response = _call(upstream, _payload(SECOND))
    assert response["id"] == "resp_http"


def test_ws_and_http_return_the_same_response_and_send_the_same_body():
    """Transport parity: identical post-hook body, identical result."""
    http = _Upstream([], flag=False)
    _, http_response = _call(http, _payload(FIRST))
    http_body = json.loads(http.http_requests[0].data)

    cw.REGISTRY.clear()
    socket = _FakeSocket(
        [
            [
                {"type": "response.created", "response": {"id": "resp_http"}},
                *_events("resp_http", _tool_call(1))[1:],
            ]
        ]
    )
    _, ws_response = _call(_Upstream([socket]), _payload(FIRST))
    frame = dict(socket.sent[0])
    frame.pop("type")
    frame.pop("client_metadata", None)
    assert frame == http_body
    assert ws_response == http_response


def test_account_override_wins_over_global_setting():
    service = _service()
    account = SimpleNamespace(meta_data={"codex_upstream_websocket": True})
    with (
        patch("preloop.models.crud.crud_account.get", return_value=account),
        patch("preloop.services.openai_gateway.settings") as fake_settings,
    ):
        fake_settings.codex_upstream_websocket = False
        assert service._codex_upstream_ws_enabled() is True
        account.meta_data = {"codex_upstream_websocket": False}
        fake_settings.codex_upstream_websocket = True
        assert service._codex_upstream_ws_enabled() is False
        account.meta_data = {}
        assert service._codex_upstream_ws_enabled() is True


# --------------------------------------------------------------------------
# Usage row meta (DB-backed)
# --------------------------------------------------------------------------


def test_usage_row_records_transport_and_raw_usage(db_session, test_user):
    from preloop.models.crud import crud_ai_model
    from preloop.models.models.api_usage import ApiUsage

    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Codex",
            "provider_name": "openai-codex",
            "model_identifier": "gpt-6.1-sol",
            "api_key": "unused",
            "meta_data": {"gateway": {"enabled": True, "model_alias": "gpt-6.1-sol"}},
        },
        account_id=test_user.account_id,
    )
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )
    service._codex_transport_meta = {"transport": "ws", "ws_mode": "incremental"}
    raw_usage = {
        "input_tokens": 100,
        "input_tokens_details": {"cached_tokens": 90},
        "output_tokens": 5,
    }
    service._record_gateway_request(
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.2,
        ai_model=ai_model,
        requested_model="gpt-6.1-sol",
        response_payload={"usage": {"input_tokens": 100, "output_tokens": 5}},
        upstream_response={"id": "resp_1", "usage": raw_usage},
        endpoint_kind="responses",
    )
    row = (
        db_session.query(ApiUsage)
        .filter(ApiUsage.account_id == test_user.account_id)
        .order_by(ApiUsage.created_at.desc())
        .first()
    )
    meta = row.meta_data or {}
    assert meta["transport"] == "ws"
    assert meta["ws_mode"] == "incremental"
    assert meta["usage_details"] == raw_usage
