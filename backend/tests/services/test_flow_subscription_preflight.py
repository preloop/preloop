"""Agent-bound subscriptions must not launch an unrelated flow principal."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from preloop.models import models
from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.services.model_routing import ModelRoutingError, prepare_execution_routing


@pytest.fixture
def selection() -> tuple[SimpleNamespace, models.AIModel]:
    account_id, model_id = uuid4(), uuid4()
    model = models.AIModel(
        id=model_id,
        account_id=account_id,
        name="Subscription model",
        provider_name="openai-codex",
        model_identifier="gpt-5-codex",
        credentials_secret_id=uuid4(),
        credentials_secret=models.SecretReference(
            secret_kind="ai_model_credentials",
            meta_data={"credential_type": "oauth_openai_codex"},
            status="active",
        ),
        meta_data={"gateway": {"enabled": True, "alias": "openai/example"}},
    )
    flow = SimpleNamespace(
        id=uuid4(),
        name="Review",
        account_id=account_id,
        agent_type="codex",
        ai_model_id=model_id,
        agent_config={"execution_path": "ephemeral"},
        runner_pool="server",
        trigger_config={},
    )
    return flow, model


@pytest.mark.parametrize("path", ["default", "matrix", "rule", "retry", "continuation"])
def test_rejects_subscription_before_execution(
    selection: tuple[SimpleNamespace, models.AIModel], path: str
) -> None:
    flow, model = selection
    assert model.is_principal_bound_oauth
    kwargs = {}
    if path == "matrix":
        kwargs["authorized_matrix"] = {"ai_model_id": str(model.id)}
    if path == "rule":
        flow.agent_config["model_routing"] = {
            "version": 1,
            "rules": [
                {
                    "id": "review",
                    "labels": {"any": ["review"]},
                    "agent_type": "codex",
                    "ai_model_id": str(model.id),
                }
            ],
        }
    if path in {"retry", "continuation"}:
        kwargs.update(
            source_execution=SimpleNamespace(
                id=uuid4(),
                flow_id=flow.id,
                trigger_event_details={
                    "_model_routing": {
                        "schema_version": 1,
                        "agent_type": "codex",
                        "ai_model_id": str(model.id),
                        "source": "default",
                    }
                },
            ),
            pin_kind=path,
        )
    with patch("preloop.services.model_routing.crud_ai_model.get", return_value=model):
        with pytest.raises(ModelRoutingError, match="agent-bound subscription"):
            prepare_execution_routing(
                MagicMock(), flow, {"payload": {"labels": ["review"]}}, **kwargs
            )


@pytest.mark.parametrize("pool", ["server", "private"])
def test_runner_pool_and_target_id_do_not_transfer_agent_identity(
    selection: tuple[SimpleNamespace, models.AIModel], pool: str
) -> None:
    flow, model = selection
    flow.runner_pool = pool
    flow.agent_config["target_agent_id"] = str(uuid4())
    with patch("preloop.services.model_routing.crud_ai_model.get", return_value=model):
        with pytest.raises(ModelRoutingError, match="agent-bound subscription"):
            prepare_execution_routing(MagicMock(), flow, {})


def test_api_key_model_still_accepted(
    selection: tuple[SimpleNamespace, models.AIModel],
) -> None:
    flow, model = selection
    model.credentials_secret.meta_data = {"credential_type": "api_key"}
    with patch("preloop.services.model_routing.crud_ai_model.get", return_value=model):
        result = prepare_execution_routing(MagicMock(), flow, {})
    assert result["_model_routing"]["ai_model_id"] == str(model.id)


@pytest.mark.parametrize(
    "config",
    [
        {"execution_path": "persistent", "target_agent_id": str(uuid4())},
        {"host_exec_profile": "codex-local"},
        {
            "agent_config": {
                "execution_path": "persistent",
                "target_agent_id": str(uuid4()),
            }
        },
    ],
)
def test_existing_agent_and_local_cli_paths_keep_their_own_admission(
    selection: tuple[SimpleNamespace, models.AIModel], config: dict[str, Any]
) -> None:
    flow, model = selection
    flow.agent_config = config
    flow.runner_pool = "private"
    with patch("preloop.services.model_routing.crud_ai_model.get", return_value=model):
        prepare_execution_routing(MagicMock(), flow, {})


@pytest.mark.asyncio
async def test_invalid_later_matrix_cell_creates_nothing(
    selection: tuple[SimpleNamespace, models.AIModel],
) -> None:
    flow, model = selection
    flow.ai_model_id = None
    service = FlowTriggerService(MagicMock())
    with (
        patch("preloop.services.flow_trigger_service.crud_flow.get", return_value=flow),
        patch("preloop.services.flow_trigger_service.flows_halted", return_value=False),
        patch(
            "preloop.services.issue_triage_controller.is_triage_flow",
            return_value=False,
        ),
        patch.object(service, "_authorize_flow_run", new_callable=AsyncMock),
        patch("preloop.services.model_routing.crud_ai_model.get", return_value=model),
        patch(
            "preloop.services.flow_trigger_service.crud_flow_execution.create"
        ) as create,
        patch.object(
            service, "_start_flow_execution", new_callable=AsyncMock
        ) as dispatch,
    ):
        with pytest.raises(ModelRoutingError, match="agent-bound subscription"):
            await service.trigger_flow_matrix(
                flow.id, [{}, {"ai_model_id": str(model.id)}]
            )
    create.assert_not_called()
    dispatch.assert_not_awaited()


def test_employee_configuration_cannot_bypass_ephemeral_preflight(
    selection: tuple[SimpleNamespace, models.AIModel],
) -> None:
    flow, model = selection
    flow.trigger_config = {"employee_events": {}}
    flow.agent_config["target_agent_id"] = str(uuid4())
    with patch("preloop.services.model_routing.crud_ai_model.get", return_value=model):
        with pytest.raises(ModelRoutingError, match="agent-bound subscription"):
            prepare_execution_routing(MagicMock(), flow, {})


def test_restricted_ci_rejects_before_creating_execution(
    selection: tuple[SimpleNamespace, models.AIModel],
) -> None:
    from preloop.services.ci_execution import _prepare_execution

    flow, model = selection
    with (
        patch("preloop.services.ci_execution.crud.crud_ci_principal.authorize"),
        patch("preloop.services.ci_execution._review_flow", return_value=flow),
        patch("preloop.services.ci_execution.ci_review_event", return_value={}),
        patch("preloop.services.model_routing.crud_ai_model.get", return_value=model),
        patch("preloop.services.ci_execution.crud.crud_ci_execution.create") as create,
    ):
        with pytest.raises(ModelRoutingError, match="agent-bound subscription"):
            _prepare_execution(MagicMock(), MagicMock(), MagicMock())
    create.assert_not_called()


def test_no_progress_escalation_cannot_select_subscription(
    selection: tuple[SimpleNamespace, models.AIModel],
) -> None:
    from preloop.services.model_routing import apply_no_progress_escalation

    flow, model = selection
    with patch("preloop.services.model_routing.crud_ai_model.get", return_value=model):
        with pytest.raises(ModelRoutingError, match="agent-bound subscription"):
            apply_no_progress_escalation(
                MagicMock(),
                flow,
                {},
                escalation={"ai_model_id": str(model.id)},
                retry_of_execution_id=uuid4(),
            )


@pytest.mark.parametrize("matrix", [False, True])
def test_trigger_http_returns_422_without_creating_or_dispatching(
    client: Any, db_session: Any, test_user: models.User, matrix: bool
) -> None:
    from tests.services.test_model_routing import _flow, _usable_model

    model = _usable_model(db_session, test_user.account_id)
    model.credentials_secret.secret_kind = "ai_model_credentials"
    model.credentials_secret.meta_data = {"credential_type": "oauth_openai_codex"}
    db_session.flush()
    flow = _flow(db_session, test_user, ai_model_id=None if matrix else model.id)
    flow.runner_pool = "server"
    flow.agent_config = {"execution_path": "ephemeral"}
    db_session.flush()
    payload = {"matrix": [{}, {"ai_model_id": str(model.id)}]} if matrix else {}
    with (
        patch(
            "preloop.services.flow_trigger_service.crud_flow_execution.create"
        ) as create,
        patch.object(
            FlowTriggerService, "_start_flow_execution", new_callable=AsyncMock
        ) as dispatch,
    ):
        response = client.post(f"/api/v1/flows/{flow.id}/trigger", json=payload)
    assert response.status_code == 422, response.text
    assert "agent-bound subscription" in response.json()["detail"]
    assert "server-usable" in response.json()["detail"]
    create.assert_not_called()
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [True, False])
async def test_legacy_execution_fails_before_runtime_launch(
    db_session: Any, test_user: models.User, worker: bool
) -> None:
    from preloop.models import crud
    from preloop.models.schemas.flow_execution import FlowExecutionCreate
    from preloop.services import flow_execution_runner as runner
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
    from tests.services.test_model_routing import _flow, _usable_model

    model = _usable_model(db_session, test_user.account_id)
    model.credentials_secret.secret_kind = "ai_model_credentials"
    model.credentials_secret.meta_data = {"credential_type": "oauth_openai_codex"}
    flow = _flow(db_session, test_user, ai_model_id=model.id)
    flow.runner_pool = "server"
    execution = crud.crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id, status="PENDING", trigger_event_details={}
        ),
    )
    execution_id = execution.id
    db_session.commit()
    with (
        patch.object(runner, "get_db_session", return_value=iter([db_session])),
        patch.object(runner, "get_nats_client", new_callable=AsyncMock),
        patch.object(runner, "flows_halted", return_value=False),
        patch(
            "preloop.services.flow_execution_dispatcher.settings.flow_execution_worker_enabled",
            worker,
        ),
        patch.object(
            FlowExecutionOrchestrator,
            "_prepare_execution_context",
            new_callable=AsyncMock,
        ) as prepare,
        patch.object(
            FlowExecutionOrchestrator, "_run_agent_with_retries", new_callable=AsyncMock
        ) as launch,
        patch.object(
            FlowExecutionOrchestrator, "_publish_update", new_callable=AsyncMock
        ),
        patch.object(
            FlowExecutionOrchestrator, "_update_commit_status", new_callable=AsyncMock
        ),
        patch.object(
            FlowExecutionOrchestrator, "_notify_terminal", new_callable=AsyncMock
        ),
        patch.object(
            FlowExecutionOrchestrator, "_start_queued_followup", new_callable=AsyncMock
        ),
        patch.object(FlowExecutionOrchestrator, "_sync_runtime_session"),
        patch.object(FlowExecutionOrchestrator, "_cleanup_temporary_api_token"),
    ):
        if worker:
            result = await runner.claim_and_run_execution(str(execution_id))
            assert result["final_status"] == "FAILED"
        else:
            # The local dispatch callback uses this same shared entrypoint.
            orchestrator = FlowExecutionOrchestrator(
                db_session,
                flow_id=flow.id,
                trigger_event_data={},
                nats_client=AsyncMock(),
            )
            orchestrator.execution_log = execution
            await runner.run_existing_execution(orchestrator)
    stored = crud.crud_flow_execution.get(db_session, id=execution_id)
    assert stored.status == "FAILED"
    assert stored.failure_category == "model_config"
    assert "agent-bound subscription" in stored.error_message
    assert stored.end_time is not None
    assert stored.agent_session_reference is None
    assert (
        crud.crud_flow_execution.claim_execution(
            db_session,
            execution_id=execution_id,
            worker_id="another-worker",
            stale_after_seconds=120,
        )
        is None
    )
    prepare.assert_not_awaited()
    launch.assert_not_awaited()
