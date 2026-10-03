"""A live console pairs a gateway start with its completion by id, not by order.

``model_gateway_request_started`` is published before the usage row exists, so
it cannot carry the ApiUsage primary key. It mints its own id instead, and the
completion event carries the same one. Pairing by arrival order ("the next
completion closes the open start") is wrong the moment two requests overlap,
which is the normal shape of an agent doing parallel tool work.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from preloop.models import models
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService


@pytest.fixture
def emit():
    return MagicMock()


@pytest.fixture
def service():
    account_id = uuid4()
    user = SimpleNamespace(id=uuid4(), account_id=account_id)
    return OpenAIGatewayService(
        db=MagicMock(),
        auth_context=ModelGatewayAuthContext(token="token", user=user),
    )


def _started_event(service: OpenAIGatewayService, emit: MagicMock) -> dict:
    model = SimpleNamespace(id=uuid4(), provider_name="openai")
    with (
        patch.object(service, "_resolve_runtime_session", return_value="sess-1"),
        patch.object(service, "_resolve_managed_agent_id", return_value=None),
        patch("preloop.services.openai_gateway._emit_account_event_nonblocking", emit),
    ):
        service._emit_gateway_request_started(
            ai_model=model,
            requested_model="openai/gpt-5",
            request_payload={"messages": [{"role": "user", "content": "hi"}]},
            endpoint_kind="chat_completions",
        )
    return emit.call_args.args[0]


def test_started_event_carries_a_correlation_id(service, emit):
    service._begin_request_accounting()

    event = _started_event(service, emit)

    assert event["payload"]["gateway_request_id"] == service._gateway_request_id
    assert event["payload"]["outcome"] == "pending"


def test_overlapping_requests_get_different_ids(service, emit):
    """Two concurrent turns must never claim the same identity."""
    service._begin_request_accounting()
    first = _started_event(service, emit)["payload"]["gateway_request_id"]
    service._begin_request_accounting()
    second = _started_event(service, emit)["payload"]["gateway_request_id"]

    assert first != second


def test_new_request_rearms_the_id_even_if_the_previous_one_never_recorded(service):
    """A request that dies before usage recording must not lend its id onward."""
    service._begin_request_accounting()
    first = service._gateway_request_id

    service._begin_request_accounting()

    assert first != service._gateway_request_id


def test_unstarted_request_has_no_id(service):
    """Constructed outside a request (tests, factories) must not claim one."""
    assert service._gateway_request_id is None


def test_completion_event_reuses_the_start_identity(service):
    """The usage row the completion event is built from carries the same id.

    This is the link that lets a consumer pair the two halves. Without it the
    console is back to guessing from arrival order.
    """
    service._begin_request_accounting()
    started_id = service._gateway_request_id

    usage_row = SimpleNamespace(
        id=uuid4(),
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        timestamp=datetime.now(timezone.utc),
        cost_source="provider",
        estimated_cost=0.0,
        auth_subject_type="api_key",
        runtime_session_id="sess-1",
        runtime_principal_type=None,
        runtime_principal_id=None,
        runtime_principal_name=None,
        flow_id=None,
        flow_execution_id=None,
        upstream_request_id=None,
        meta_data={},
    )
    model = models.AIModel(
        name="probe-model",
        provider_name="openai",
        model_identifier="gpt-5",
        api_key="provider-secret",
    )

    with (
        patch.object(service, "_resolve_runtime_session", return_value="sess-1"),
        patch.object(service, "_resolve_managed_agent_id", return_value=None),
        patch.object(service, "_pricing_override_for_request", return_value=None),
        patch("preloop.services.openai_gateway.crud_api_usage") as usage_crud,
        patch(
            "preloop.services.openai_gateway.crud_api_usage.get_gateway_attempt_summary",
            return_value={},
        ),
        patch("preloop.services.openai_gateway.crud_runtime_session"),
        patch("preloop.services.openai_gateway.crud_runtime_session_activity"),
        patch("preloop.services.openai_gateway.log_model_gateway_request"),
        patch("preloop.services.openai_gateway.ModelGatewayEventEmitter"),
        patch("preloop.services.gateway_usage_search.GatewayUsageSearchService"),
        patch("preloop.services.session_search_index.index_gateway_interaction"),
        patch(
            "preloop.services.openai_gateway.estimate_ai_model_usage_cost_detailed",
            return_value=SimpleNamespace(
                cost=0.0, source="provider", pricing_snapshot=None
            ),
        ),
    ):
        usage_crud.log_gateway_request.return_value = usage_row
        service._record_gateway_request_inner(
            endpoint="/openai/v1/chat/completions",
            method="POST",
            status_code=200,
            duration=0.1,
            ai_model=model,
            requested_model="openai/gpt-5",
            response_payload=None,
            upstream_response=None,
            endpoint_kind="chat_completions",
        )

    recorded_meta = usage_crud.log_gateway_request.call_args.kwargs["meta_data"]
    assert recorded_meta["gateway_request_id"] == started_id
