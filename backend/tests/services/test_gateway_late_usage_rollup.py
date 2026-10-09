"""A gateway usage row for a run refreshes that run's stored cost rollup (#1275).

The orchestrator writes ``flow_execution.estimated_cost`` when the run ends.
A request in flight at that moment records its usage row afterwards; the
gateway then asks the rollup helper to resync. The helper's terminal-status
guard is covered against a real database in
``tests/endpoints/test_execution_cost_single_source.py``; this pins the
wiring on the recording path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from preloop.models import models
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService


def _record(
    context_data: dict, *, hosted: bool = False
) -> tuple[MagicMock, OpenAIGatewayService]:
    account_id = uuid4()
    user = SimpleNamespace(id=uuid4(), account_id=account_id)
    api_key = SimpleNamespace(
        id=uuid4(),
        user_id=user.id,
        account_id=account_id,
        name="flow-execution-key",
        context_data=context_data,
    )
    service = OpenAIGatewayService(
        db=MagicMock(),
        auth_context=ModelGatewayAuthContext(token="token", user=user, api_key=api_key),
    )
    service._begin_request_accounting()
    usage_row = SimpleNamespace(
        id=uuid4(),
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        timestamp=datetime.now(timezone.utc),
        cost_source="provider",
        estimated_cost=0.1,
        auth_subject_type="api_key",
        runtime_session_id=None,
        runtime_principal_type=None,
        runtime_principal_id=None,
        runtime_principal_name=None,
        flow_id=context_data.get("flow_id"),
        flow_execution_id=context_data.get("flow_execution_id"),
        upstream_request_id=None,
        meta_data={},
    )
    model = models.AIModel(
        id=uuid4(),
        account_id=None if hosted else user.account_id,
        meta_data={"hosted": hosted},
        name="probe-model",
        provider_name="openai",
        model_identifier="gpt-5",
        api_key="provider-secret",
    )
    sync = MagicMock(return_value=True)
    with (
        patch.object(service, "_resolve_runtime_session", return_value=None),
        patch.object(service, "_resolve_managed_agent_id", return_value=None),
        patch.object(service, "_pricing_override_for_request", return_value=None),
        patch("preloop.services.openai_gateway.crud_api_usage") as usage_crud,
        patch("preloop.services.openai_gateway.crud_runtime_session"),
        patch("preloop.services.openai_gateway.crud_runtime_session_activity"),
        patch("preloop.services.openai_gateway.log_model_gateway_request"),
        patch("preloop.services.openai_gateway.ModelGatewayEventEmitter"),
        patch("preloop.services.gateway_usage_search.GatewayUsageSearchService"),
        patch("preloop.services.session_search_index.index_gateway_interaction"),
        patch(
            "preloop.services.openai_gateway.estimate_ai_model_usage_cost_detailed",
            return_value=SimpleNamespace(
                cost=0.1, source="provider", pricing_snapshot=None
            ),
        ),
        patch(
            "preloop.services.execution_metrics.sync_finished_execution_cost_rollup",
            sync,
        ),
    ):
        usage_crud.log_gateway_request.return_value = usage_row
        usage_crud.get_gateway_attempt_summary.return_value = {}
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
    service._captured_billing_metadata = (
        usage_crud.log_gateway_request.call_args.kwargs["meta_data"]
    )
    return sync, service


def test_usage_row_for_a_run_resyncs_its_rollup():
    execution_id = str(uuid4())
    sync, service = _record({"flow_execution_id": execution_id})
    sync.assert_called_once_with(
        service.db, execution_id, account_id=service.auth_context.account_id
    )


def test_usage_row_without_a_run_does_not_touch_rollups():
    sync, _ = _record({})
    sync.assert_not_called()


def test_recorded_request_names_actual_model_and_billing_path() -> None:
    for hosted, expected in [(False, "your_key"), (True, "allowance")]:
        _sync, service = _record({}, hosted=hosted)
        metadata = service._captured_billing_metadata
        assert metadata["billing_path"] == expected
        assert metadata["billing_model_name"] == "probe-model"
        assert metadata["billing_model_id"]
        assert "provider-secret" not in str(metadata)
