"""Self-hosted prompt loss remains visible in usage and execution outcomes."""

import json
from typing import Iterator
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_ai_model,
    crud_api_key,
    crud_api_usage,
    crud_flow,
    crud_flow_execution,
)
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService


@pytest.fixture
def flow_gateway(
    db_session: Session, test_user: models.User
) -> tuple[OpenAIGatewayService, models.FlowExecution, models.AIModel]:
    """Create synthetic model/flow identities; every upstream is mocked."""
    model = crud_ai_model.create_with_account(
        db_session,
        obj_in={
            "name": "Example local model",
            "model_identifier": "example-coder",
            "provider_name": "openai-compatible",
            "api_endpoint": "https://models.example.com/v1",
            "api_key": "synthetic-test-key",
            "model_parameters": {},
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "example-coder",
                    "provider_adapter": "preloop",
                },
                "pricing": {"input_price_per_1k": 0.01, "output_price_per_1k": 0.02},
            },
        },
        account_id=test_user.account_id,
    )
    flow = crud_flow.create(
        db_session,
        flow_in=FlowCreate(
            name="Example review",
            prompt_template="Review the change",
            agent_type="codex",
            trigger_event_source="github",
            trigger_event_types=["test"],
            account_id=test_user.account_id,
            ai_model_id=model.id,
            agent_config={},
        ),
        account_id=test_user.account_id,
    )
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )
    key, _ = crud_api_key.create_runtime_key(
        db_session,
        name="Example flow key",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={"flow_id": str(flow.id), "flow_execution_id": str(execution.id)},
    )
    return (
        OpenAIGatewayService(
            db_session,
            ModelGatewayAuthContext(token="test", user=test_user, api_key=key),
        ),
        execution,
        model,
    )


def upstream_stream(input_tokens: int, *, late_reset: bool = False) -> MagicMock:
    """Send a terminal usage event, then fail if the consumer reads again."""

    def chunks() -> Iterator[str]:
        yield (
            "data: "
            + json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_example",
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [
                                    {"type": "output_text", "text": "Review complete"}
                                ],
                            }
                        ],
                        "usage": {"input_tokens": input_tokens, "output_tokens": 49},
                    },
                }
            )
            + "\n\n"
        )
        if late_reset:
            raise httpx.RemoteProtocolError("Connection reset after completed response")
        raise httpx.ReadTimeout("Body never closes")

    response = MagicMock(status_code=200, headers={})
    response.iter_text.side_effect = chunks
    return response


def test_truncated_terminal_stream_persists_evidence_models_and_refuses_retry(
    db_session: Session,
    flow_gateway: tuple[OpenAIGatewayService, models.FlowExecution, models.AIModel],
) -> None:
    """Reproduce the never-closing stream without a late zero-token 502."""
    service, execution, model = flow_gateway
    response = upstream_stream(8194)
    client = MagicMock()
    client.send.return_value = response
    with patch(
        "preloop.services.openai_gateway._openai_passthrough_http_client",
        return_value=client,
    ):
        events = list(
            service.stream_response(
                {
                    "model": "example-coder",
                    "input": "Review this change carefully. " * 3600,
                    "tools": [
                        {
                            "type": "function",
                            "name": "read_change",
                            "parameters": {"type": "object"},
                        }
                    ],
                }
            )
        )
        service.flush_deferred_stream_record()
    assert "response.completed" in "".join(events)
    rows = (
        db_session.query(models.ApiUsage)
        .filter_by(flow_execution_id=execution.id)
        .all()
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.status_code == 200 and row.prompt_tokens == 8194
    assert row.error_class is None
    evidence = row.meta_data["context_truncated_suspected"]
    assert evidence["estimated_input_tokens"] > 24000
    assert evidence["reported_input_tokens"] == 8194
    db_session.refresh(execution)
    assert execution.failure_category == "context_truncated_suspected"
    assert execution.startup_receipt["context_truncated_suspected"] == evidence
    assert execution.status == "FAILED"
    used = crud_api_usage.get_models_used_for_executions(
        db_session, [execution.id], account_id=model.account_id
    )
    assert used[str(execution.id)][0]["model_alias"] == "example-coder"
    with pytest.raises(ModelGatewayAPIError, match="truncated"):
        service._check_context_window(
            model, {"input": "Short reminder"}, gateway_provider="openai"
        )


def test_complete_answer_followed_by_reset_keeps_success_and_usage(
    db_session: Session,
    flow_gateway: tuple[OpenAIGatewayService, models.FlowExecution, models.AIModel],
) -> None:
    """A complete answer remains successful even if the HTTP peer resets later."""
    service, execution, model = flow_gateway
    response = upstream_stream(8194, late_reset=True)
    client = MagicMock()
    client.send.return_value = response
    with patch(
        "preloop.services.openai_gateway._openai_passthrough_http_client",
        return_value=client,
    ):
        events = list(
            service.stream_response(
                {"model": "example-coder", "input": "Review the change"}
            )
        )
        service.flush_deferred_stream_record()
        service.flush_deferred_stream_record()
    assert len(events) == 1
    assert json.loads(events[0].removeprefix("data: "))["type"] == "response.completed"
    rows = (
        db_session.query(models.ApiUsage)
        .filter_by(flow_execution_id=execution.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].status_code == 200
    assert rows[0].error_class is None
    assert rows[0].prompt_tokens == 8194
    assert rows[0].completion_tokens == 49
    db_session.refresh(execution)
    assert execution.status == "RUNNING"
    assert execution.failure_category is None
    response.close.assert_called_once()


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": 10000, "output_tokens": 10},
        {"input_tokens": 100, "cache_read_input_tokens": 9900, "output_tokens": 10},
    ],
)
def test_plateau_uses_previous_call_and_cache_inclusive_input(
    db_session: Session,
    flow_gateway: tuple[OpenAIGatewayService, models.FlowExecution, models.AIModel],
    usage: dict[str, int],
) -> None:
    """A repeated count is a second signal, even without the strong deficit."""
    service, execution, model = flow_gateway
    for estimate in (16000, 16500):
        service._begin_request_accounting()
        service._context_input_estimate = estimate
        service._record_gateway_request(
            endpoint="/openai/v1/responses",
            method="POST",
            status_code=200,
            duration=1,
            ai_model=model,
            requested_model="example-coder",
            response_payload=None,
            upstream_response={"usage": usage},
            endpoint_kind="responses_stream",
        )
        db_session.refresh(execution)
        if estimate == 16000:
            assert execution.status == "RUNNING"
    assert execution.failure_category == "context_truncated_suspected"
    assert (
        execution.startup_receipt["context_truncated_suspected"]["signal"]
        == "unchanged_input_tokens_with_growing_history"
    )


def test_estimated_usage_never_marks_truncation(
    db_session: Session,
    flow_gateway: tuple[OpenAIGatewayService, models.FlowExecution, models.AIModel],
) -> None:
    """A fallback usage estimate is not provider evidence."""
    service, execution, model = flow_gateway
    service._begin_request_accounting()
    service._context_input_estimate = 26000
    service._record_gateway_request(
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=499,
        duration=1,
        ai_model=model,
        requested_model="example-coder",
        response_payload=None,
        upstream_response={"usage": {"input_tokens": 8194, "output_tokens": 49}},
        endpoint_kind="responses_stream",
        usage_source="estimated",
    )
    db_session.refresh(execution)
    assert execution.status == "RUNNING"


def test_preflight_known_window_never_calls_upstream(
    db_session: Session,
    flow_gateway: tuple[OpenAIGatewayService, models.FlowExecution, models.AIModel],
) -> None:
    """The same oversized first prompt fails before any provider work."""
    service, execution, model = flow_gateway
    crud_ai_model.update(
        db_session, db_obj=model, obj_in={"model_parameters": {"context_window": 8192}}
    )
    client = MagicMock()
    with (
        patch(
            "preloop.services.openai_gateway._openai_passthrough_http_client",
            return_value=client,
        ),
        pytest.raises(ModelGatewayAPIError, match="registered context window of 8192"),
    ):
        service.stream_response(
            {"model": "example-coder", "input": "Review this change. " * 6000}
        )
    client.send.assert_not_called()
    db_session.refresh(execution)
    assert execution.status == "FAILED"
    assert execution.failure_category == "context_truncated_suspected"
    assert (
        execution.startup_receipt["context_truncated_suspected"]["context_window"]
        == 8192
    )
    assert (
        crud_api_usage.get_gateway_usage_for_execution(db_session, str(execution.id))[
            "api_requests"
        ]
        == 0
    )


def test_estimated_previous_call_is_not_a_plateau(
    db_session: Session,
    flow_gateway: tuple[OpenAIGatewayService, models.FlowExecution, models.AIModel],
) -> None:
    """A provider report cannot be compared with an earlier estimated count."""
    service, execution, model = flow_gateway
    for estimate, source in ((16000, "estimated"), (16500, "provider")):
        service._begin_request_accounting()
        service._context_input_estimate = estimate
        service._record_gateway_request(
            endpoint="/openai/v1/responses",
            method="POST",
            status_code=200,
            duration=1,
            ai_model=model,
            requested_model="example-coder",
            response_payload=None,
            upstream_response={"usage": {"input_tokens": 10000, "output_tokens": 10}},
            endpoint_kind="responses_stream",
            usage_source=source,
        )
    db_session.refresh(execution)
    assert execution.status == "RUNNING"


def test_detection_uses_outbound_estimate_after_context_optimization(
    db_session: Session,
    flow_gateway: tuple[OpenAIGatewayService, models.FlowExecution, models.AIModel],
) -> None:
    """Removed request context must not be mistaken for server-side truncation."""
    service, execution, model = flow_gateway
    service._begin_request_accounting()
    service._context_input_estimate = 10000
    service._record_gateway_request(
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=1,
        ai_model=model,
        requested_model="example-coder",
        response_payload=None,
        upstream_response={"usage": {"input_tokens": 8194, "output_tokens": 49}},
        endpoint_kind="responses_stream",
        request_payload={"input": "Review this change. " * 6000},
    )
    db_session.refresh(execution)
    assert execution.status == "RUNNING"
