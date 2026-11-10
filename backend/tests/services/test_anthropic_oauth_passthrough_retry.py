"""Bounded retries for transient Anthropic OAuth passthrough rejections (#2267).

Everything here is mocked HTTP with a patched sleep and jitter: no network,
no provider calls.
"""

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, call, patch

import httpx
import pytest

from preloop.models.crud import crud_ai_model
from preloop.models.models.api_usage import ApiUsage
from preloop.services import gateway_error_alerts
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.upstream_errors import is_retryable_passthrough_rejection

URL = "https://api.anthropic.com/v1/messages"
HEADERS = {"authorization": "Bearer sk-ant-oat01-access-token"}
BODY = {"model": "claude-sonnet-4-5", "max_tokens": 16, "messages": []}
SUCCESS = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "hi"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 3, "output_tokens": 2},
}
SLEEP = "preloop.services.openai_gateway._sleep_before_upstream_retry"
ENQUEUE = "preloop.services.openai_gateway.enqueue_gateway_5xx_alert"
CLIENT = "preloop.services.openai_gateway._anthropic_passthrough_http_client"


def _error_body(error_type: str, message: str) -> str:
    return json.dumps(
        {"type": "error", "error": {"type": error_type, "message": message}}
    )


def _overloaded() -> str:
    return _error_body("overloaded_error", "Overloaded")


def _response(
    status: int,
    text: str = "",
    headers: Optional[Dict[str, str]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> MagicMock:
    """A mocked httpx response usable by both passthrough modes."""
    response = MagicMock()
    response.status_code = status
    response.text = text
    response.headers = httpx.Headers(headers or {})
    response.read.return_value = text.encode("utf-8")
    response.json.return_value = payload if payload is not None else SUCCESS
    return response


def _client(responses: List[Any]) -> MagicMock:
    client = MagicMock()
    client.post.side_effect = list(responses)
    client.send.side_effect = list(responses)
    return client


def _service() -> OpenAIGatewayService:
    context = ModelGatewayAuthContext(
        token="t", user=SimpleNamespace(id="user", account_id="account")
    )
    service = OpenAIGatewayService(MagicMock(), context)
    service.release_db_for_wait = MagicMock()  # type: ignore[method-assign]
    return service


def _model() -> SimpleNamespace:
    return SimpleNamespace(
        account_id="account",
        id="model",
        provider_name="anthropic",
        model_identifier="claude-sonnet-4-5",
        api_endpoint="",
        credential_type="oauth_anthropic_claude_code",
    )


@pytest.fixture(autouse=True)
def _isolate() -> Any:
    gateway_error_alerts.reset_alert_state_for_tests()
    with (
        patch("preloop.services.openai_gateway.random.uniform", return_value=0.0),
        patch(
            "preloop.services.hosted_spend_guard.guard_unmetered_hosted_call",
            return_value=None,
        ),
    ):
        yield
    gateway_error_alerts.reset_alert_state_for_tests()


def _complete(service: OpenAIGatewayService) -> Dict[str, Any]:
    return service._anthropic_oauth_passthrough_complete(
        url=URL, headers=HEADERS, body=BODY, ai_model=_model()
    )


def _open(service: OpenAIGatewayService) -> Any:
    return service._open_anthropic_oauth_passthrough_stream(
        url=URL, headers=HEADERS, body=BODY, ai_model=_model()
    )


@pytest.mark.parametrize("mode", ["complete", "stream"])
def test_529_then_success_retries_once_without_alert(mode: str) -> None:
    service = _service()
    failed = _response(529, _overloaded())
    ok = _response(200)
    client = _client([failed, ok])
    with (
        patch(CLIENT, return_value=client),
        patch(SLEEP) as sleep,
        patch(ENQUEUE) as enqueue,
    ):
        if mode == "complete":
            assert _complete(service) == SUCCESS
            calls = client.post.call_args_list
        else:
            assert _open(service) == (client, ok)
            calls = client.build_request.call_args_list
            assert client.send.call_count == 2
            ok.close.assert_not_called()
    assert len(calls) == 2
    assert calls[0] == calls[1]
    failed.close.assert_called_once()
    client.close.assert_not_called()
    sleep.assert_called_once()
    enqueue.assert_not_called()
    assert service._last_upstream_retry_count == 1


@pytest.mark.parametrize("mode", ["complete", "stream"])
def test_exhausted_529_surfaces_final_overload_and_alerts_once(mode: str) -> None:
    service = _service()
    failures = [_response(529, _overloaded()) for _ in range(3)]
    client = _client(failures)
    with (
        patch(CLIENT, return_value=client),
        patch(SLEEP) as sleep,
        patch(ENQUEUE) as enqueue,
    ):
        with pytest.raises(ModelGatewayAPIError) as exc_info:
            _complete(service) if mode == "complete" else _open(service)
    attempts = client.post if mode == "complete" else client.send
    assert attempts.call_count == 3
    for failure in failures:
        failure.close.assert_called_once()
    assert sleep.call_count == 2
    assert exc_info.value.status_code == 529
    assert exc_info.value.error_type == "overloaded_error"
    assert exc_info.value.error_class == "upstream_overloaded"
    assert enqueue.call_count == 1
    assert service._last_upstream_retry_count == 2


@pytest.mark.parametrize("status", [502, 503, 504, 529])
def test_eligible_statuses_are_retried(status: str) -> None:
    service = _service()
    client = _client(
        [_response(status, _error_body("api_error", "upstream hiccup")), _response(200)]
    )
    with patch(CLIENT, return_value=client), patch(SLEEP), patch(ENQUEUE):
        assert _complete(service) == SUCCESS
    assert client.post.call_count == 2


@pytest.mark.parametrize(
    ("status", "text", "headers"),
    [
        (529, _overloaded(), {"x-should-retry": "false"}),
        (503, _error_body("api_error", "Your credit balance is too low"), {}),
        (429, _error_body("rate_limit_error", "slow down"), {"x-should-retry": "true"}),
        (401, _error_body("authentication_error", "OAuth token revoked"), {}),
        (400, _error_body("invalid_request_error", "bad param"), {}),
        (402, _error_body("billing_error", "quota exhausted"), {}),
        (500, _error_body("api_error", "Internal server error"), {}),
    ],
)
def test_ineligible_rejections_are_not_retried(
    status: int, text: str, headers: Dict[str, str]
) -> None:
    service = _service()
    client = _client([_response(status, text, headers), _response(200)])
    with patch(CLIENT, return_value=client), patch(SLEEP) as sleep, patch(ENQUEUE):
        with pytest.raises(ModelGatewayAPIError) as exc_info:
            _complete(service)
    assert client.post.call_count == 1
    sleep.assert_not_called()
    # Existing mapping is unchanged (a billing 503 still maps to 402).
    assert exc_info.value.status_code >= 400
    assert service._last_upstream_retry_count == 0


def test_revoked_oauth_keeps_recovery_message() -> None:
    service = _service()
    client = _client(
        [_response(401, _error_body("authentication_error", "revoked")), _response(200)]
    )
    with patch(CLIENT, return_value=client), patch(SLEEP) as sleep:
        with pytest.raises(ModelGatewayAPIError) as exc_info:
            _complete(service)
    sleep.assert_not_called()
    assert exc_info.value.message != "revoked"


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")],
)
def test_transport_failures_are_not_retried(error: Exception) -> None:
    service = _service()
    client = _client([error, _response(200)])
    with patch(CLIENT, return_value=client), patch(SLEEP) as sleep:
        with pytest.raises(ModelGatewayAPIError) as exc_info:
            _complete(service)
    assert client.post.call_count == 1
    sleep.assert_not_called()
    assert exc_info.value.status_code == 502


def test_generic_exception_is_not_retried() -> None:
    service = _service()
    client = _client([RuntimeError("boom"), _response(200)])
    with patch(CLIENT, return_value=client), patch(SLEEP) as sleep:
        with pytest.raises(RuntimeError):
            _complete(service)
    assert client.post.call_count == 1
    sleep.assert_not_called()


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [("30", 8.0), ("3", 3.0), ("soon", 0.2), (None, 0.2)],
)
def test_retry_after_is_capped_and_bad_hints_use_backoff(
    retry_after: Optional[str], expected: float
) -> None:
    service = _service()
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    client = _client([_response(529, _overloaded(), headers), _response(200)])
    with patch(CLIENT, return_value=client), patch(SLEEP) as sleep:
        _complete(service)
    sleep.assert_called_once_with(pytest.approx(expected))


def test_single_configured_attempt_disables_retry() -> None:
    service = _service()
    client = _client([_response(529, _overloaded()), _response(200)])
    with (
        patch(CLIENT, return_value=client),
        patch(SLEEP) as sleep,
        patch(ENQUEUE),
        patch(
            "preloop.services.openai_gateway.settings."
            "model_gateway_upstream_retry_max_attempts",
            1,
        ),
    ):
        with pytest.raises(ModelGatewayAPIError):
            _complete(service)
    assert client.post.call_count == 1
    sleep.assert_not_called()


def test_recovery_drops_stale_failure_headers() -> None:
    service = _service()
    failed = _response(
        529,
        _overloaded(),
        {"x-should-retry": "true", "anthropic-ratelimit-unified-status": "rejected"},
    )
    client = _client([failed, _response(200)])
    with patch(CLIENT, return_value=client), patch(SLEEP):
        _complete(service)
    assert not service.upstream_response_headers


def test_exhaustion_keeps_last_attempt_headers() -> None:
    service = _service()
    client = _client(
        [
            _response(
                529, _overloaded(), {"anthropic-ratelimit-unified-status": str(n)}
            )
            for n in range(3)
        ]
    )
    with patch(CLIENT, return_value=client), patch(SLEEP), patch(ENQUEUE):
        with pytest.raises(ModelGatewayAPIError):
            _complete(service)
    assert service.upstream_response_headers == {
        "anthropic-ratelimit-unified-status": "2"
    }


def test_db_released_and_response_closed_before_backoff() -> None:
    service = _service()
    order = MagicMock()
    failed = _response(529, _overloaded())
    failed.close.side_effect = lambda: order.close()
    service.release_db_for_wait.side_effect = lambda *a: order.release()  # type: ignore[attr-defined]
    client = _client([failed, _response(200)])
    with (
        patch(CLIENT, return_value=client),
        patch(SLEEP, side_effect=lambda _s: order.sleep()),
    ):
        _complete(service)
    names = [entry[0] for entry in order.mock_calls]
    assert names.index("close") < names.index("sleep")
    assert names.index("release", names.index("close")) < names.index("sleep")
    assert order.mock_calls[-1] == call.sleep()


def _read_error() -> Any:
    raise httpx.ReadError("dropped")
    yield ""  # pragma: no cover - makes this a generator


def _in_band_error() -> Any:
    yield (
        "event: error\n"
        'data: {"type":"error","error":{"type":"overloaded_error",'
        '"message":"Overloaded"}}\n\n'
    )


def _partial_then_disconnect() -> Any:
    yield (
        "event: message_start\n"
        'data: {"type":"message_start","message":{"id":"msg_1",'
        '"usage":{"input_tokens":3}}}\n\n'
    )
    raise httpx.RemoteProtocolError("peer closed connection")


@pytest.mark.parametrize(
    "body", [_read_error, _in_band_error, _partial_then_disconnect]
)
def test_accepted_stream_is_never_reopened(
    db_session: Any, test_user: Any, body: Any
) -> None:
    """Drive the whole relay: a failure after acceptance never reopens."""
    _create_oauth_model(db_session, test_user)
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )
    accepted = _response(200)
    accepted.iter_text.side_effect = lambda *a, **k: body()
    client = _client([accepted, _response(200)])
    with (
        patch("preloop.services.openai_gateway.get_secret_service") as secrets,
        patch(CLIENT, return_value=client),
        patch(SLEEP) as sleep,
        patch(ENQUEUE),
    ):
        secrets.return_value.resolve_ai_model_credentials.return_value = (
            SimpleNamespace(
                credential_type="oauth_anthropic_claude_code",
                value="sk-ant-oat01-access-token",
            )
        )
        emitted: List[str] = []
        try:
            for chunk in service.stream_message({**_payload(), "stream": True}):
                emitted.append(chunk)
        except Exception:  # noqa: BLE001 - relay may re-raise the drop
            pass
    accepted.iter_text.assert_called_once()
    assert client.send.call_count == 1
    sleep.assert_not_called()
    accepted.close.assert_called()


def test_predicate_true_hint_does_not_broaden_allowlist() -> None:
    carrier = SimpleNamespace(status_code=500, message="boom", error_type=None)
    rejection = type("Rejection", (Exception,), {})()
    for name, value in vars(carrier).items():
        setattr(rejection, name, value)
    assert not is_retryable_passthrough_rejection(
        rejection, httpx.Headers({"x-should-retry": "true"})
    )


def _create_oauth_model(db_session: Any, test_user: Any) -> Any:
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Claude Code Subscription",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "api_key": "unused-placeholder",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "anthropic/claude-sonnet-4-5",
                    "provider_adapter": "preloop",
                },
            },
            "is_default": True,
        },
        account_id=test_user.account_id,
    )


def _payload() -> Dict[str, Any]:
    return {
        "model": "anthropic/claude-sonnet-4-5",
        "max_tokens": 16,
        "system": [{"type": "text", "text": "You are Claude Code."}],
        "messages": [{"role": "user", "content": "Hello"}],
    }


def _usage_rows(db_session: Any) -> List[ApiUsage]:
    return (
        db_session.query(ApiUsage)
        .filter(ApiUsage.endpoint == "/anthropic/v1/messages")
        .all()
    )


@pytest.mark.parametrize(
    ("responses", "status", "retries"),
    [
        ([529, 200], 200, 1),
        ([200], 200, 0),
        ([529, 529, 529], 529, 2),
    ],
)
def test_one_usage_row_records_upstream_retries(
    db_session: Any,
    test_user: Any,
    responses: List[int],
    status: int,
    retries: int,
) -> None:
    _create_oauth_model(db_session, test_user)
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )
    client = _client(
        [_response(code, _overloaded() if code >= 400 else "") for code in responses]
    )
    with (
        patch("preloop.services.openai_gateway.get_secret_service") as secrets,
        patch(CLIENT, return_value=client),
        patch(SLEEP),
        patch(ENQUEUE) as enqueue,
    ):
        secrets.return_value.resolve_ai_model_credentials.return_value = (
            SimpleNamespace(
                credential_type="oauth_anthropic_claude_code",
                value="sk-ant-oat01-access-token",
            )
        )
        if status == 200:
            service.create_message(_payload())
        else:
            with pytest.raises(ModelGatewayAPIError):
                service.create_message(_payload())
    rows = _usage_rows(db_session)
    assert len(rows) == 1
    assert rows[0].status_code == status
    assert rows[0].meta_data["upstream_retries"] == retries
    assert enqueue.call_count == (1 if status >= 500 else 0)
