"""Observability for gateway streams the client never consumed.

A proxy in front of the gateway (ingress, sidecar) can time a streaming
request out and drop the connection before the ASGI layer pulls a single
chunk from the response generator. The upstream model call has already been
made and billed by the provider at that point, but the generator body never
runs, so its ``finally``-based abort accounting never fires and the request
leaves no trace at all: no usage row, no status code, no error class.

These tests pin the "never consumed" path down for every streaming gateway
entrypoint, and pin the already-working paths (clean completion, mid-stream
disconnect) so the new accounting cannot start double-counting spend.
"""

from __future__ import annotations

import gc
from typing import Any, Iterator
from unittest.mock import patch

from preloop.models.crud import crud_ai_model
from preloop.models.models.api_usage import ApiUsage
from preloop.services.gemini_gateway import GeminiGatewayService
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.upstream_errors import (
    ERROR_CLASS_CLIENT_CANCELLED,
    ERROR_CLASS_STREAM_ABANDONED,
)


def _create_gateway_model(db_session, account_id, alias: str) -> Any:
    """Create a gateway-enabled, priced model so usage rows carry a cost."""
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"Gateway Model {alias}",
            "provider_name": "openai",
            "model_identifier": "test-model",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": alias,
                    "provider_adapter": "preloop",
                },
                "pricing": {
                    "input_price_per_1k": 0.01,
                    "output_price_per_1k": 0.02,
                },
            },
            "is_default": True,
        },
        account_id=account_id,
    )


def _upstream_chunks() -> Iterator[dict]:
    """A well-formed upstream stream that ends with a usage chunk."""
    yield {"choices": [{"delta": {"content": "Hel"}}]}
    yield {"choices": [{"delta": {"content": "lo"}}]}
    yield {
        "choices": [{"delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 5,
            "total_tokens": 16,
        },
    }


def _service(db_session, test_user) -> OpenAIGatewayService:
    return OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )


def _rows_for(db_session, ai_model) -> list[ApiUsage]:
    """Gateway usage rows for exactly this test's model, oldest first."""
    return (
        db_session.query(ApiUsage)
        .filter(
            ApiUsage.action_type == "model_gateway",
            ApiUsage.ai_model_id == ai_model.id,
        )
        .order_by(ApiUsage.timestamp.asc())
        .all()
    )


def test_abandoned_responses_stream_is_recorded(db_session, test_user):
    """A responses stream closed before its first chunk must leave a row.

    This is the shape of a proxy read-timeout: the upstream call has already
    happened (the gateway prefetches the first chunk to surface early
    failures), then the client is gone before consuming anything.
    """
    ai_model = _create_gateway_model(db_session, test_user.account_id, "abandon-resp")
    service = _service(db_session, test_user)

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=_upstream_chunks(),
    ) as mock_completion:
        stream = service.stream_response({"model": "abandon-resp", "input": "Hello"})
        # The upstream provider has already been called and is billing us.
        assert mock_completion.called
        stream.close()

    rows = _rows_for(db_session, ai_model)
    assert len(rows) == 1, f"expected exactly one usage row, got {len(rows)}"
    assert rows[0].status_code == 499
    assert rows[0].error_class == ERROR_CLASS_STREAM_ABANDONED
    assert rows[0].endpoint == "/openai/v1/responses"


def test_abandoned_chat_completions_stream_is_recorded(db_session, test_user):
    """The chat-completions streaming entrypoint has the same blind spot."""
    ai_model = _create_gateway_model(db_session, test_user.account_id, "abandon-chat")
    service = _service(db_session, test_user)

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=_upstream_chunks(),
    ):
        stream = service.stream_chat_completion(
            {
                "model": "abandon-chat",
                "messages": [{"role": "user", "content": "Hello"}],
            }
        )
        stream.close()

    rows = _rows_for(db_session, ai_model)
    assert len(rows) == 1
    assert rows[0].status_code == 499
    assert rows[0].error_class == ERROR_CLASS_STREAM_ABANDONED
    assert rows[0].endpoint == "/openai/v1/chat/completions"


def test_abandoned_anthropic_stream_is_recorded(db_session, test_user):
    """The Anthropic messages streaming entrypoint has the same blind spot."""
    ai_model = _create_gateway_model(db_session, test_user.account_id, "abandon-anth")
    service = _service(db_session, test_user)

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=_upstream_chunks(),
    ):
        stream = service.stream_message(
            {
                "model": "abandon-anth",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 64,
            }
        )
        stream.close()

    rows = _rows_for(db_session, ai_model)
    assert len(rows) == 1
    assert rows[0].status_code == 499
    assert rows[0].error_class == ERROR_CLASS_STREAM_ABANDONED
    assert rows[0].endpoint == "/anthropic/v1/messages"


def test_abandoned_gemini_stream_is_recorded(db_session, test_user):
    """Gemini streaming (the reported incident's surface) must record too.

    Gemini wraps the shared responses stream in a translating generator, so
    an abandoned Gemini request abandons a *nested* generator. Closing must
    be deterministic: no ``gc.collect()`` here on purpose, because by the time
    the collector runs the request's database session can already be closed
    and the usage row would be lost for good.
    """
    ai_model = _create_gateway_model(db_session, test_user.account_id, "abandon-gem")
    service = GeminiGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=_upstream_chunks(),
    ):
        stream = service.stream_generate_content(
            "abandon-gem",
            {"contents": [{"role": "user", "parts": [{"text": "Hello"}]}]},
        )
        stream.close()

    rows = _rows_for(db_session, ai_model)
    assert len(rows) == 1
    assert rows[0].status_code == 499
    assert rows[0].error_class == ERROR_CLASS_STREAM_ABANDONED


def test_fully_consumed_stream_records_one_success_row(db_session, test_user):
    """The happy path must stay a single 200 row with provider tokens.

    Guards against the abandonment accounting adding a second, phantom row
    (which would double-count spend) once the stream completed normally.
    """
    ai_model = _create_gateway_model(db_session, test_user.account_id, "ok-resp")
    service = _service(db_session, test_user)

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=_upstream_chunks(),
    ):
        stream = service.stream_response({"model": "ok-resp", "input": "Hello"})
        events = list(stream)
        stream.close()
        del stream
        gc.collect()

    assert events, "expected SSE events from a completed stream"
    rows = _rows_for(db_session, ai_model)
    assert len(rows) == 1, f"expected exactly one usage row, got {len(rows)}"
    assert rows[0].status_code == 200
    assert rows[0].error_class is None
    assert rows[0].prompt_tokens == 11
    assert rows[0].completion_tokens == 5


def test_midstream_disconnect_still_records_client_cancelled_once(
    db_session, test_user
):
    """A stream dropped after partial consumption keeps its existing shape.

    This path already worked (the generator body ran, so its ``finally``
    fired). It must keep recording exactly one ``client_cancelled`` row and
    must not also gain an abandonment row.
    """
    ai_model = _create_gateway_model(db_session, test_user.account_id, "midstream")
    service = _service(db_session, test_user)

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=_upstream_chunks(),
    ):
        stream = service.stream_response({"model": "midstream", "input": "Hello"})
        next(stream)
        stream.close()
        del stream
        gc.collect()

    rows = _rows_for(db_session, ai_model)
    assert len(rows) == 1, f"expected exactly one usage row, got {len(rows)}"
    assert rows[0].status_code == 499
    assert rows[0].error_class == ERROR_CLASS_CLIENT_CANCELLED


class _Closeable:
    """Records how many times it was closed."""

    def __init__(self) -> None:
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


def test_observer_closes_owned_resources_only_when_abandoned():
    """``closes`` resources are owned by the generator once it has started.

    The wrapped generator's ``finally`` closes its own upstream response, so
    the observer must not close it again — a double close on a real httpx
    response is what broke the Anthropic passthrough when this was first
    written.
    """
    from preloop.services.model_gateway_stream_observer import ObservedGatewayStream

    resource = _Closeable()
    consumed = ObservedGatewayStream(iter(["a", "b"]), closes=(resource,))
    next(consumed)
    consumed.close()
    assert resource.close_count == 0

    resource = _Closeable()
    abandoned = ObservedGatewayStream(iter(["a", "b"]), closes=(resource,))
    abandoned.close()
    assert resource.close_count == 1


def test_observer_reports_abandonment_once_and_never_raises():
    """Teardown is idempotent, and a failing recorder must not propagate."""
    from preloop.services.model_gateway_stream_observer import ObservedGatewayStream

    calls: list[int] = []
    stream = ObservedGatewayStream(iter(["a"]), on_abandoned=lambda: calls.append(1))
    stream.close()
    stream.close()
    assert calls == [1]

    def _boom() -> None:
        raise RuntimeError("recording failed")

    exploding = ObservedGatewayStream(iter(["a"]), on_abandoned=_boom)
    exploding.close()  # must not raise
