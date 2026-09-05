"""The flow orchestrator must scope its model list to the flow credential.

``list_authorized_gateway_models`` without an auth context returns only the
fail-closed subset, so the orchestrator has to build a real
``ModelGatewayAuthContext`` from the runtime key it just minted. Otherwise the
generated agent config advertises models the flow principal cannot use and the
gateway rejects them with a 400 mid-session.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

pytestmark = pytest.mark.asyncio

TOKEN = "preloop-runtime-token"


@pytest.fixture
def orchestrator():
    orch = FlowExecutionOrchestrator(
        db=MagicMock(),
        flow_id="test-flow-id",
        trigger_event_data={},
        nats_client=AsyncMock(),
    )
    orch.flow = MagicMock()
    orch.flow.id = "test-flow-id"
    orch.flow.account_id = "test-account-id"
    orch.flow.git_clone_config = None
    orch.flow.trigger_project_ids = None
    orch.flow.agent_type = "opencode"
    orch.execution_log = MagicMock()
    orch.execution_log.id = "test-execution-id"
    orch.ai_model = MagicMock()
    return orch


def _gateway_runtime() -> MagicMock:
    runtime = MagicMock()
    runtime.model_gateway_enabled = True
    runtime.to_execution_context.return_value = {}
    return runtime


async def _prepare(orchestrator, *, list_models):
    """Run _prepare_execution_context with the model-list seam patched."""
    api_key_id = uuid4()
    with (
        patch.object(orchestrator, "_resolve_prompt", AsyncMock(return_value="do it")),
        patch.object(
            orchestrator,
            "_create_temporary_api_token",
            return_value=(TOKEN, api_key_id),
        ),
        patch.object(
            orchestrator,
            "_resolve_execution_model_runtime",
            return_value=_gateway_runtime(),
        ),
        patch(
            "preloop.services.agent_model_list.list_authorized_gateway_models",
            side_effect=list_models,
        ),
        patch(
            "preloop.services.model_gateway_auth.build_runtime_key_auth_context",
        ) as build_context,
    ):
        build_context.return_value = MagicMock()
        context = await orchestrator._prepare_execution_context()
    return context, build_context, api_key_id


class TestAuthorizedGatewayModelList:
    async def test_passes_the_flow_credential_auth_context(self, orchestrator):
        """The helper is called with the context built from the minted key."""
        captured = {}

        def list_models(db, account_id, auth_context=None):
            captured["auth_context"] = auth_context
            return [MagicMock(alias="openai/gpt-5", display_name="GPT 5")]

        context, build_context, api_key_id = await _prepare(
            orchestrator, list_models=list_models
        )

        build_context.assert_called_once()
        assert build_context.call_args.kwargs["token"] == TOKEN
        assert build_context.call_args.kwargs["api_key_id"] == str(api_key_id)
        assert captured["auth_context"] is build_context.return_value
        assert context["authorized_gateway_models"] == [
            {"alias": "openai/gpt-5", "display_name": "GPT 5"}
        ]

    async def test_resolution_failure_is_logged_at_warning(self, orchestrator, caplog):
        """A silent degradation to primary-only must be visible in prod logs."""

        def list_models(db, account_id, auth_context=None):
            raise RuntimeError("inventory unavailable")

        with caplog.at_level("WARNING", logger="preloop.services.flow_orchestrator"):
            context, _, _ = await _prepare(orchestrator, list_models=list_models)

        assert "authorized_gateway_models" not in context
        assert any(
            record.levelname == "WARNING"
            and "Could not resolve authorized gateway models" in record.getMessage()
            for record in caplog.records
        )
