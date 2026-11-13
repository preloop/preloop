"""Gateway errors say whether Preloop or the upstream provider decided (#2288).

A provider's ``401 User not found`` used to reach the agent at Preloop's URL
as the bare provider sentence, indistinguishable from Preloop rejecting the
agent's own key. These tests pin the four cases apart: the gateway's own
client-auth 401, an upstream 401 with a misleading sentence, an upstream
routing 404, and a run stopped after a different first failure.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from preloop.models.crud import crud_api_usage
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService

# What litellm raises for OpenRouter's 401: the provider's sentence nested in
# its JSON body behind litellm's own prefixes.
_OPENROUTER_401 = (
    "litellm.AuthenticationError: AuthenticationError: OpenrouterException - "
    '{"error":{"message":"User not found.","code":401}}'
)
_OPENROUTER_404 = (
    "litellm.NotFoundError: OpenrouterException - "
    '{"error":{"message":"No allowed providers are available for the '
    'selected model.","code":404,"metadata":{"requested_providers":'
    '["alibaba"]}}}'
)
_SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"
_ORIGIN_KEYS = {"origin", "provider", "category", "upstream_status", "request_id"}

_EXPECTED_401_MESSAGE = (
    "Upstream provider openrouter rejected the provider credential configured "
    "for this model (HTTP 401). Provider message: User not found. This is not "
    "a rejection of your Preloop credentials."
)


@pytest.fixture(autouse=True)
def _isolate_gateway_alert_state() -> Iterator[None]:
    from preloop.services.gateway_error_alerts import reset_alert_state_for_tests

    reset_alert_state_for_tests()
    yield
    reset_alert_state_for_tests()


def _upstream_401() -> Exception:
    import litellm

    return litellm.AuthenticationError(
        message=_OPENROUTER_401, llm_provider="openrouter", model="m"
    )


def _upstream_404() -> Exception:
    import litellm

    return litellm.NotFoundError(
        message=_OPENROUTER_404, llm_provider="openrouter", model="m"
    )


def _model() -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider_name="openrouter",
        model_identifier="deepseek/deepseek-chat",
        account_id="account-1",
        api_endpoint=None,
        credential_type=None,
    )


def _service() -> OpenAIGatewayService:
    auth_context = ModelGatewayAuthContext(
        token="token", user=SimpleNamespace(id="user-1", account_id="account-1")
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)
    service._gateway_request_id = "req-2288"
    return service


def _raise_through_gateway(
    exc: Exception, provider: str = "openai"
) -> ModelGatewayAPIError:
    """Run a failing upstream call through the gateway's retry wrapper."""
    service = _service()

    def attempt() -> None:
        raise exc

    with pytest.raises(ModelGatewayAPIError) as caught:
        service._run_with_upstream_retries(provider, attempt, ai_model=_model())
    return caught.value


def _sse_data(frame: str) -> dict[str, Any]:
    return json.loads(frame.split("data:", 1)[1])


# (a) the gateway's own client-auth 401 ------------------------------------


@pytest.mark.parametrize(
    ("call", "protocol"),
    [
        (
            lambda: __import__(
                "preloop.api.gateway_auth_dependency", fromlist=["x"]
            ).authenticate_request(
                None, MagicMock(), allow_ended_runtime_session=False
            ),
            "openai",
        ),
        (
            lambda: __import__(
                "preloop.api.endpoints.anthropic_gateway", fromlist=["x"]
            ).get_anthropic_gateway_auth_context(
                MagicMock(),
                x_api_key=None,
                authorization=None,
                anthropic_version="2023-06-01",
                db=MagicMock(),
            ),
            "anthropic",
        ),
        (
            lambda: __import__(
                "preloop.api.endpoints.gemini_gateway", fromlist=["x"]
            ).get_gemini_gateway_auth_context(
                x_goog_api_key=None,
                api_key=None,
                authorization=None,
                key=None,
                db=MagicMock(),
            ),
            "gemini",
        ),
    ],
    ids=["openai", "anthropic", "gemini"],
)
def test_gateway_client_auth_401_says_origin_gateway(call, protocol):
    with pytest.raises(ModelGatewayAPIError) as caught:
        asyncio.run(call())
    err = caught.value
    assert err.status_code == 401
    assert err.origin == "gateway"
    assert "upstream" not in err.message.lower()
    payload = err.to_payload()
    if protocol == "gemini":
        (info,) = payload["error"]["details"]
        assert info["metadata"] == {"origin": "gateway"}
    else:
        assert payload["error"]["origin"] == "gateway"
        assert "provider" not in payload["error"]
        assert "upstream_status" not in payload["error"]


# (b) an upstream 401 whose sentence names a "user" ----------------------


def test_upstream_401_states_origin_and_keeps_classification():
    err = _raise_through_gateway(_upstream_401())
    # Status, class and terminal semantics are unchanged.
    assert err.status_code == 401
    assert err.error_class == "upstream_auth"
    assert err.terminal is True
    assert err.message == _EXPECTED_401_MESSAGE
    body = err.to_payload()["error"]
    assert body["message"] == _EXPECTED_401_MESSAGE
    assert body["origin"] == "upstream"
    assert body["provider"] == "openrouter"
    assert body["category"] == "upstream_auth"
    assert body["upstream_status"] == 401
    assert body["request_id"] == "req-2288"


def test_upstream_401_message_does_not_interpret_the_provider_sentence():
    message = _raise_through_gateway(_upstream_401()).message.lower()
    for guess in ("expired", "rotate", "revoked", "missing account", "billing"):
        assert guess not in message


def test_upstream_401_provider_message_is_still_scrubbed():
    import litellm

    exc = litellm.AuthenticationError(
        message=(
            "OpenrouterException - "
            f'{{"error":{{"message":"User not found for key {_SECRET}"}}}}'
        ),
        llm_provider="openrouter",
        model="m",
    )
    err = _raise_through_gateway(exc)
    assert _SECRET not in err.message
    assert _SECRET not in json.dumps(err.to_payload())
    assert "[REDACTED]" in err.message
    assert err.message.startswith("Upstream provider openrouter rejected")


def test_upstream_401_streaming_responses_event_carries_origin():
    service = _service()
    error = service._stream_error("openai", _upstream_401(), ai_model=_model())
    event = _sse_data(service._responses_stream_error_event(_upstream_401(), error))
    # Terminal auth failures still end the turn with response.failed.
    assert event["type"] == "response.failed"
    failed = event["response"]["error"]
    assert failed["message"] == _EXPECTED_401_MESSAGE
    assert failed["origin"] == "upstream"
    assert failed["provider"] == "openrouter"
    assert failed["upstream_status"] == 401
    assert failed["request_id"] == "req-2288"


def test_upstream_401_streaming_chat_event_carries_origin():
    service = _service()
    error = service._stream_error("openai", _upstream_401(), ai_model=_model())
    body = _sse_data(service._openai_stream_error_event(_upstream_401(), error))
    assert body["error"]["origin"] == "upstream"
    assert body["error"]["message"] == _EXPECTED_401_MESSAGE


def test_upstream_401_anthropic_body_and_stream_carry_origin():
    err = _raise_through_gateway(_upstream_401(), provider="anthropic")
    body = err.to_payload()
    assert body["type"] == "error"
    assert body["error"]["type"] == "authentication_error"
    assert body["error"]["message"] == _EXPECTED_401_MESSAGE
    assert _ORIGIN_KEYS <= set(body["error"])

    service = _service()
    error = service._stream_error("anthropic", _upstream_401(), ai_model=_model())
    frame = service._anthropic_stream_error_event(_upstream_401(), error)
    streamed = _sse_data(frame)
    assert streamed["error"]["origin"] == "upstream"
    assert streamed["error"]["provider"] == "openrouter"


def test_upstream_401_gemini_body_carries_origin_as_error_info():
    err = _raise_through_gateway(_upstream_401(), provider="gemini")
    body = err.to_payload()["error"]
    assert body["code"] == 401
    assert body["status"] == "UNAUTHENTICATED"
    assert body["message"] == _EXPECTED_401_MESSAGE
    (info,) = body["details"]
    assert info["@type"] == "type.googleapis.com/google.rpc.ErrorInfo"
    assert info["reason"] == "UPSTREAM_AUTH"
    assert info["metadata"] == {
        "origin": "upstream",
        "provider": "openrouter",
        "category": "upstream_auth",
        "upstream_status": "401",
        "request_id": "req-2288",
    }


def test_gemini_stream_relays_origin_from_responses_error_event():
    from preloop.services.gemini_gateway import _gemini_origin_details

    details = _gemini_origin_details(
        {
            "type": "error",
            "code": "authentication_error",
            "message": _EXPECTED_401_MESSAGE,
            "origin": "upstream",
            "provider": "openrouter",
            "category": "upstream_auth",
            "upstream_status": 401,
        }
    )
    (info,) = details["details"]
    assert info["metadata"]["origin"] == "upstream"
    assert info["metadata"]["upstream_status"] == "401"
    assert _gemini_origin_details({"type": "error", "message": "x"}) == {}


def test_gemini_403_conversion_keeps_origin_fields():
    from preloop.services.gemini_gateway import _as_gemini_denial

    source = ModelGatewayAPIError(
        provider="openai",
        status_code=403,
        message="m",
        error_class="upstream_auth",
        origin="upstream",
        upstream_provider="openrouter",
        upstream_status=403,
        request_id="req-2288",
    )
    rendered = _as_gemini_denial(source)
    assert rendered.provider == "gemini"
    meta = rendered.to_payload()["error"]["details"][0]["metadata"]
    assert meta["origin"] == "upstream"
    assert meta["upstream_status"] == "403"


def test_principal_bound_oauth_401_keeps_preloop_recovery_text():
    """The passthrough's own recovery text must not be quoted as the provider's."""
    from preloop.services.openai_gateway import (
        PRINCIPAL_BOUND_OAUTH_CREDENTIAL_TYPES,
    )

    model = _model()
    model.provider_name = "anthropic"
    model.credential_type = next(iter(PRINCIPAL_BOUND_OAUTH_CREDENTIAL_TYPES))
    err = OpenAIGatewayService._anthropic_passthrough_upstream_error(
        401,
        '{"type":"error","error":{"type":"authentication_error",'
        '"message":"OAuth token revoked"}}',
        ai_model=model,
    )
    assert err.status_code == 401
    assert "Provider message" not in err.message
    assert err.origin == "upstream"


# (c) an upstream routing 404 ------------------------------------------------


def test_upstream_routing_404_states_origin_without_reclassifying():
    err = _raise_through_gateway(_upstream_404())
    assert err.status_code == 404
    assert err.message == (
        "Upstream provider openrouter returned HTTP 404 for this request. "
        "Provider message: No allowed providers are available for the selected "
        "model. This error came from the upstream provider, not from Preloop."
    )
    body = err.to_payload()["error"]
    assert body["origin"] == "upstream"
    assert body["upstream_status"] == 404
    # The 404 is not an auth failure and does not claim to be one.
    assert body.get("category") != "upstream_auth"
    assert "credential" not in err.message


def test_gateway_inferred_5xx_wording_unchanged():
    """Only provider-reported 401-404 gain the label; 5xx keep theirs."""

    class _NoStatusError(Exception):
        pass

    err = OpenAIGatewayService._normalize_upstream_error(
        "openai", _NoStatusError("upstream exploded"), ai_model=_model()
    )
    assert "Provider message" not in err.message


# (d) a run stopped after a different first failure ------------------------


def _flow_and_execution(db_session: Session, test_user) -> tuple[Any, Any]:
    from tests.services.test_flow_execution_limits import (
        _create_execution,
        _create_flow,
    )

    flow = _create_flow(db_session, test_user)
    return flow, _create_execution(db_session, flow)


def _log_attempt(
    db_session: Session,
    test_user,
    flow,
    execution,
    *,
    status_code: int,
    error_class: str | None,
    detail: str | None,
    request_id: str,
) -> None:
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=status_code,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        flow_id=str(flow.id),
        flow_execution_id=str(execution.id),
        model_alias="openrouter/deepseek",
        provider_name="openrouter",
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        error_class=error_class,
        meta_data={
            "request_fingerprint": "fp-a",
            "error_class": error_class,
            "error_detail": detail,
            "gateway_request_id": request_id,
        },
    )
    db_session.flush()


def _log_404_then_401s(db_session: Session, test_user, flow, execution) -> None:
    _log_attempt(
        db_session,
        test_user,
        flow,
        execution,
        status_code=404,
        error_class="upstream_routing",
        detail="No allowed providers are available for the selected model.",
        request_id="req-first",
    )
    for index in range(2):
        _log_attempt(
            db_session,
            test_user,
            flow,
            execution,
            status_code=401,
            error_class="upstream_auth",
            detail="User not found.",
            request_id=f"req-later-{index}",
        )


def test_repeated_failure_reports_the_first_cause_not_the_last(
    db_session: Session, test_user
):
    from preloop.services.flow_execution_limits import (
        RepeatedUpstreamFailureError,
        enforce_failure_streak_for_id,
    )
    from preloop.services.flow_failure_category import (
        FAILURE_CATEGORY_REPEATED_UPSTREAM_FAILURE,
        derive_failure_category,
    )

    flow, execution = _flow_and_execution(db_session, test_user)
    _log_404_then_401s(db_session, test_user, flow, execution)

    with pytest.raises(RepeatedUpstreamFailureError) as caught:
        enforce_failure_streak_for_id(db_session, execution_id=execution.id)
    exc = caught.value
    # The stop reason is still the trailing 401 streak...
    assert exc.streak.error_class == "upstream_auth"
    assert exc.streak.status_code == 401
    # ...but the first cause is the 404, not the later 401.
    assert exc.first_cause is not None
    assert exc.first_cause.to_dict() == {
        "origin": "upstream",
        "category": "upstream_routing",
        "upstream_status": 404,
        "provider": "openrouter",
        "message": "No allowed providers are available for the selected model.",
        "request_id": "req-first",
    }
    assert exc.message.startswith("Repeated upstream failure: 2 identical")
    assert (
        "First failure in this run: upstream_routing (HTTP 404 from upstream "
        "provider openrouter): No allowed providers" in exc.message
    )
    assert "User not found" not in exc.message

    db_session.refresh(execution)
    assert execution.error_message == exc.message
    assert (
        derive_failure_category(status="FAILED", error_message=execution.error_message)
        == FAILURE_CATEGORY_REPEATED_UPSTREAM_FAILURE
    )
    from preloop.models.models import FlowExecutionLog

    (milestone,) = (
        db_session.query(FlowExecutionLog)
        .filter(
            FlowExecutionLog.execution_id == execution.id,
            FlowExecutionLog.log_type == "milestone",
        )
        .all()
    )
    assert milestone.metadata_["first_cause"]["upstream_status"] == 404


def test_first_cause_restarts_after_a_success(db_session: Session, test_user):
    from preloop.services.flow_execution_limits import (
        RepeatedUpstreamFailureError,
        enforce_failure_streak_for_id,
    )

    flow, execution = _flow_and_execution(db_session, test_user)
    _log_attempt(
        db_session,
        test_user,
        flow,
        execution,
        status_code=404,
        error_class="upstream_routing",
        detail="old routing failure",
        request_id="req-old",
    )
    _log_attempt(
        db_session,
        test_user,
        flow,
        execution,
        status_code=200,
        error_class=None,
        detail=None,
        request_id="req-ok",
    )
    for index in range(2):
        _log_attempt(
            db_session,
            test_user,
            flow,
            execution,
            status_code=401,
            error_class="upstream_auth",
            detail="User not found.",
            request_id=f"req-{index}",
        )
    with pytest.raises(RepeatedUpstreamFailureError) as caught:
        enforce_failure_streak_for_id(db_session, execution_id=execution.id)
    cause = caught.value.first_cause
    assert cause is not None
    assert cause.status_code == 401
    assert cause.request_id == "req-0"


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_424_body_carries_first_cause(db_session: Session, test_user, protocol):
    """The 424 refusal (also what a streaming request gets before any
    event) keeps its status and code and nests the first cause."""
    from preloop.models.crud import crud_api_key

    flow, execution = _flow_and_execution(db_session, test_user)
    _log_404_then_401s(db_session, test_user, flow, execution)
    runtime_key, _ = crud_api_key.create_runtime_key(
        db_session,
        name="origin test",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={"flow_execution_id": str(execution.id)},
    )
    service = OpenAIGatewayService(
        db_session,
        ModelGatewayAuthContext(token="t", user=test_user, api_key=runtime_key),
    )
    service._gateway_request_id = "req-refusal"
    with pytest.raises(ModelGatewayAPIError) as caught:
        service._check_repeated_upstream_failures(
            execution.id, gateway_provider=protocol
        )
    err = caught.value
    assert err.status_code == 424
    assert err.code == "repeated_upstream_failure"
    assert err.terminal is True
    body = err.to_payload()["error"]
    assert body["origin"] == "gateway"
    assert body["category"] == "upstream_auth"
    assert body["request_id"] == "req-refusal"
    assert body["cause"]["upstream_status"] == 404
    assert body["cause"]["provider"] == "openrouter"
    assert body["cause"]["request_id"] == "req-first"
    assert "First failure in this run: upstream_routing" in body["message"]
