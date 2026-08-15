"""Tests for matrix/batch fan-out triggers.

A trigger carrying a ``matrix`` fans out to one execution per
(agent_type, ai_model_id) cell, all sharing a batch_id, so one flow
definition can drive a model x harness evaluation grid without cloning
the flow per cell.
"""

import pytest
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models.crud import crud_account, crud_flow
from preloop.models.models import Flow
from preloop.models.models.ai_model import AIModel
from preloop.models.models.flow_execution import MATRIX_OVERRIDES_KEY, FlowExecution
from preloop.models.models.user import User
from preloop.models.schemas.flow import FlowCreate
from preloop.services.flow_trigger_service import (
    MATRIX_MAX_ENTRIES,
    FlowTriggerService,
)


@pytest.fixture
def test_flow(db_session: Session, test_user: User) -> Flow:
    """Create a flow owned by the authenticated test user's account."""
    flow_in = FlowCreate(
        name=f"Matrix Flow {uuid4().hex[:8]}",
        description="Flow used for matrix fan-out tests",
        trigger_event_source="github",
        trigger_event_types=["push"],
        prompt_template="Evaluate: {{payload.message}}",
        agent_type="openhands",
        agent_config={"max_iterations": 10},
        account_id=test_user.account_id,
    )
    return crud_flow.create(
        db=db_session, flow_in=flow_in, account_id=test_user.account_id
    )


@pytest.fixture
def test_ai_model(db_session: Session, test_user: User) -> AIModel:
    """Create an account-owned AI model usable as a matrix override."""
    model = AIModel(
        name=f"Matrix Model {uuid4().hex[:8]}",
        provider_name="openai",
        model_identifier="gpt-4o",
        account_id=test_user.account_id,
    )
    db_session.add(model)
    db_session.flush()
    db_session.refresh(model)
    return model


def _patch_dispatch():
    """Patch NATS + dispatch so no execution actually runs."""
    return (
        patch(
            "preloop.services.flow_trigger_service.get_nats_client",
            new_callable=AsyncMock,
        ),
        patch.object(
            FlowTriggerService, "_start_flow_execution", new_callable=AsyncMock
        ),
    )


class TestTriggerFlowMatrixService:
    @pytest.mark.asyncio
    async def test_matrix_creates_one_execution_per_cell(
        self,
        db_session: Session,
        test_flow: Flow,
        test_ai_model: AIModel,
    ):
        service = FlowTriggerService(db_session)
        matrix = [
            {},  # flow defaults (baseline cell)
            {"agent_type": "opencode"},
            {"agent_type": "codex", "ai_model_id": str(test_ai_model.id)},
        ]

        nats_patch, dispatch_patch = _patch_dispatch()
        with nats_patch, dispatch_patch as mock_dispatch:
            result = await service.trigger_flow_matrix(
                flow_id=test_flow.id,
                matrix=matrix,
                test_mode=True,
                trigger_event_data={"payload": {"message": "hello"}},
            )

        assert result["flow_id"] == str(test_flow.id)
        batch_id = UUID(result["batch_id"])
        assert len(result["executions"]) == 3
        # Superset of the webhook trigger response: execution_id per cell.
        for index, cell in enumerate(result["executions"]):
            assert cell["index"] == index
            assert cell["execution_id"] == cell["id"]
            assert cell["status"] == "PENDING"
        assert result["executions"][1]["agent_type"] == "opencode"
        assert result["executions"][2]["ai_model_id"] == str(test_ai_model.id)

        # One dispatch per cell.
        assert mock_dispatch.await_count == 3

        # All rows share the batch_id and carry self-describing overrides.
        rows = (
            db_session.query(FlowExecution)
            .filter(FlowExecution.batch_id == batch_id)
            .all()
        )
        assert len(rows) == 3
        by_index = {
            row.trigger_event_details[MATRIX_OVERRIDES_KEY]["index"]: row
            for row in rows
        }
        assert (
            by_index[0].trigger_event_details[MATRIX_OVERRIDES_KEY].get("agent_type")
            is None
        )
        assert (
            by_index[1].trigger_event_details[MATRIX_OVERRIDES_KEY]["agent_type"]
            == "opencode"
        )
        assert by_index[2].trigger_event_details[MATRIX_OVERRIDES_KEY][
            "ai_model_id"
        ] == str(test_ai_model.id)
        # Shared trigger event data is preserved on every cell.
        for row in rows:
            assert row.trigger_event_details["payload"] == {"message": "hello"}
            assert row.trigger_event_details["test_mode"] is True

    @pytest.mark.asyncio
    async def test_matrix_rejects_empty_and_oversized(
        self, db_session: Session, test_flow: Flow
    ):
        service = FlowTriggerService(db_session)

        with pytest.raises(ValueError, match="at least one entry"):
            await service.trigger_flow_matrix(flow_id=test_flow.id, matrix=[])

        oversized = [{} for _ in range(MATRIX_MAX_ENTRIES + 1)]
        with pytest.raises(ValueError, match="at most"):
            await service.trigger_flow_matrix(flow_id=test_flow.id, matrix=oversized)


class TestOrchestratorMatrixOverrides:
    def test_overrides_applied_from_trigger_details(
        self, db_session: Session, test_flow: Flow, test_ai_model: AIModel
    ):
        from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

        orchestrator = FlowExecutionOrchestrator(
            db_session,
            flow_id=test_flow.id,
            trigger_event_data={
                MATRIX_OVERRIDES_KEY: {
                    "batch_id": str(uuid4()),
                    "index": 1,
                    "agent_type": "opencode",
                    "ai_model_id": str(test_ai_model.id),
                }
            },
            nats_client=AsyncMock(),
        )
        orchestrator._get_flow_details()

        assert orchestrator.agent_type == "opencode"
        assert orchestrator.ai_model is not None
        assert orchestrator.ai_model.id == test_ai_model.id
        # The shared flow row itself is untouched.
        assert orchestrator.flow.agent_type == "openhands"

    def test_no_overrides_falls_back_to_flow(
        self, db_session: Session, test_flow: Flow
    ):
        from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

        orchestrator = FlowExecutionOrchestrator(
            db_session,
            flow_id=test_flow.id,
            trigger_event_data={"payload": {}},
            nats_client=AsyncMock(),
        )
        orchestrator._get_flow_details()

        assert orchestrator.agent_type == "openhands"
        assert orchestrator.ai_model is None


class TestMatrixTriggerEndpoint:
    def _trigger(self, client: TestClient, flow_id, body):
        return client.post(f"/api/v1/flows/{flow_id}/trigger", json=body)

    def test_trigger_with_matrix_fans_out(
        self,
        client: TestClient,
        db_session: Session,
        test_flow: Flow,
        test_ai_model: AIModel,
    ):
        nats_patch, dispatch_patch = _patch_dispatch()
        with nats_patch, dispatch_patch:
            response = self._trigger(
                client,
                test_flow.id,
                {
                    "payload": {"message": "hi"},
                    "matrix": [
                        {"agent_type": "opencode"},
                        {"ai_model_id": str(test_ai_model.id)},
                    ],
                },
            )

        assert response.status_code == 200, response.text
        data = response.json()
        assert "batch_id" in data
        assert len(data["executions"]) == 2
        assert all("execution_id" in cell for cell in data["executions"])

        # The reserved matrix key never leaks into the stored event data.
        rows = (
            db_session.query(FlowExecution)
            .filter(FlowExecution.batch_id == UUID(data["batch_id"]))
            .all()
        )
        assert len(rows) == 2
        for row in rows:
            assert "matrix" not in row.trigger_event_details
            assert row.trigger_event_details["payload"] == {"message": "hi"}

    def test_trigger_without_matrix_is_unchanged(
        self, client: TestClient, test_flow: Flow
    ):
        nats_patch, dispatch_patch = _patch_dispatch()
        with nats_patch, dispatch_patch:
            response = self._trigger(client, test_flow.id, {"payload": {"x": 1}})

        assert response.status_code == 200, response.text
        data = response.json()
        assert set(data) == {"id", "status", "flow_id"}

    @pytest.mark.parametrize(
        "matrix, detail_fragment",
        [
            ([], "non-empty"),
            ("not-a-list", "non-empty"),
            ([{"agent_type": "not-a-real-agent"}], "not supported"),
            ([{"ai_model_id": "not-a-uuid"}], "must be a UUID"),
            ([{"prompt": "per-cell prompts are v2"}], "unsupported keys"),
            ([{} for _ in range(MATRIX_MAX_ENTRIES + 1)], "at most"),
        ],
    )
    def test_trigger_matrix_validation_rejects(
        self, client: TestClient, test_flow: Flow, matrix, detail_fragment
    ):
        response = self._trigger(client, test_flow.id, {"matrix": matrix})
        assert response.status_code == 422
        assert detail_fragment in response.json()["detail"]

    def test_trigger_matrix_rejects_foreign_model(
        self, client: TestClient, db_session: Session, test_flow: Flow
    ):
        other_account = crud_account.create(
            db_session,
            obj_in={"organization_name": f"Other Org {uuid4().hex[:8]}"},
        )
        foreign_model = AIModel(
            name="Foreign Model",
            provider_name="openai",
            model_identifier="gpt-4o",
            account_id=other_account.id,
        )
        db_session.add(foreign_model)
        db_session.flush()

        response = self._trigger(
            client,
            test_flow.id,
            {"matrix": [{"ai_model_id": str(foreign_model.id)}]},
        )
        assert response.status_code == 422
        assert "not found" in response.json()["detail"]


class TestBatchExecutionsEndpoint:
    def test_list_batch_with_rollup(
        self,
        client: TestClient,
        db_session: Session,
        test_flow: Flow,
        test_ai_model: AIModel,
    ):
        nats_patch, dispatch_patch = _patch_dispatch()
        with nats_patch, dispatch_patch:
            response = client.post(
                f"/api/v1/flows/{test_flow.id}/trigger",
                json={
                    "matrix": [
                        {},
                        {
                            "agent_type": "opencode",
                            "ai_model_id": str(test_ai_model.id),
                        },
                    ]
                },
            )
        batch_id = response.json()["batch_id"]

        # Simulate one finished cell with metrics.
        row = (
            db_session.query(FlowExecution)
            .filter(FlowExecution.batch_id == UUID(batch_id))
            .all()
        )[1]
        row.status = "SUCCEEDED"
        row.total_tokens = 1200
        row.estimated_cost = 0.05
        row.tool_calls_count = 7
        db_session.flush()

        list_response = client.get(f"/api/v1/flows/batches/{batch_id}/executions")
        assert list_response.status_code == 200, list_response.text
        data = list_response.json()

        assert data["batch_id"] == batch_id
        assert data["flow_id"] == str(test_flow.id)
        rollup = data["rollup"]
        assert rollup["total"] == 2
        assert rollup["by_status"] == {"PENDING": 1, "SUCCEEDED": 1}
        assert rollup["completed"] == 1
        assert rollup["total_tokens"] == 1200
        assert rollup["total_estimated_cost"] == 0.05
        assert rollup["total_tool_calls"] == 7

        executions = data["executions"]
        assert len(executions) == 2
        # Sorted by matrix index; cells are self-describing.
        assert executions[0]["matrix"]["index"] == 0
        assert executions[0]["matrix"]["agent_type"] is None
        assert executions[1]["matrix"]["index"] == 1
        assert executions[1]["matrix"]["agent_type"] == "opencode"
        assert executions[1]["matrix"]["ai_model_id"] == str(test_ai_model.id)
        assert all(e["batch_id"] == batch_id for e in executions)
        assert all(e["flow_name"] == test_flow.name for e in executions)

    def test_unknown_batch_404(self, client: TestClient):
        response = client.get(f"/api/v1/flows/batches/{uuid4()}/executions")
        assert response.status_code == 404

    def test_foreign_batch_404(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """A batch belonging to another account is invisible (404, not leak)."""
        other_account = crud_account.create(
            db_session,
            obj_in={"organization_name": f"Other Org {uuid4().hex[:8]}"},
        )
        other_flow = crud_flow.create(
            db=db_session,
            flow_in=FlowCreate(
                name=f"Other Flow {uuid4().hex[:8]}",
                prompt_template="p",
                trigger_event_source="github",
                trigger_event_types=["push"],
                agent_type="openhands",
                agent_config={},
                account_id=other_account.id,
            ),
            account_id=other_account.id,
        )
        foreign_batch = uuid4()
        db_session.add(
            FlowExecution(
                flow_id=other_flow.id,
                status="PENDING",
                batch_id=foreign_batch,
            )
        )
        db_session.flush()

        response = client.get(f"/api/v1/flows/batches/{foreign_batch}/executions")
        assert response.status_code == 404


def test_matrix_overrides_key_is_reserved():
    """The reserved key must stay in sync between model and consumers."""
    assert MATRIX_OVERRIDES_KEY == "_matrix"


class TestMatrixOverrideOnRebuiltExecutors:
    """Regression: every path that (re)builds an agent executor must honour
    the cell's agent_type override, not the flow default (resume / monitor /
    recovery all run against interrupted matrix cells)."""

    def _matrix_execution(self, db_session, flow, **kwargs) -> FlowExecution:
        execution = FlowExecution(
            flow_id=flow.id,
            status="RUNNING",
            batch_id=uuid4(),
            agent_session_reference="session-ref-123",
            trigger_event_details={
                MATRIX_OVERRIDES_KEY: {
                    "batch_id": str(uuid4()),
                    "index": 0,
                    "agent_type": "opencode",
                }
            },
            **kwargs,
        )
        db_session.add(execution)
        db_session.flush()
        db_session.refresh(execution)
        return execution

    def test_resolve_matrix_agent_selection_helper(self):
        from preloop.models.models.flow_execution import (
            resolve_matrix_agent_selection,
        )

        assert resolve_matrix_agent_selection(
            {MATRIX_OVERRIDES_KEY: {"agent_type": "opencode"}},
            flow_agent_type="openhands",
            flow_ai_model_id="model-1",
        ) == ("opencode", "model-1")
        # No overrides / no details -> flow defaults.
        assert resolve_matrix_agent_selection(
            {"payload": {}}, flow_agent_type="openhands"
        ) == ("openhands", None)
        assert resolve_matrix_agent_selection(
            None, flow_agent_type="openhands", flow_ai_model_id="model-1"
        ) == ("openhands", "model-1")

    @pytest.mark.asyncio
    async def test_resume_uses_cell_agent_type(
        self, db_session: Session, test_flow: Flow
    ):
        from preloop.services.flow_execution_runner import (
            resume_existing_execution,
        )
        from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

        execution = self._matrix_execution(db_session, test_flow)
        orchestrator = FlowExecutionOrchestrator(
            db_session,
            flow_id=test_flow.id,
            trigger_event_data=execution.trigger_event_details,
            nats_client=AsyncMock(),
        )
        orchestrator.execution_log = execution

        executor = AsyncMock()
        with (
            patch(
                "preloop.agents.create_agent_executor", return_value=executor
            ) as create_mock,
            patch.object(
                orchestrator,
                "_monitor_agent_execution",
                new_callable=AsyncMock,
                return_value={"status": "SUCCEEDED"},
            ),
            patch.object(orchestrator, "_update_execution_log", new_callable=AsyncMock),
        ):
            await resume_existing_execution(orchestrator, "session-ref-123")

        create_mock.assert_called_once()
        assert create_mock.call_args[0][0] == "opencode"

    @pytest.mark.asyncio
    async def test_monitor_uses_cell_agent_type(
        self, db_session: Session, test_flow: Flow
    ):
        from datetime import datetime, timedelta, timezone

        from preloop.agents import AgentStatus
        from preloop.services.execution_monitor import ExecutionMonitor

        stale_start = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            hours=3
        )
        execution = self._matrix_execution(
            db_session, test_flow, start_time=stale_start
        )

        executor = AsyncMock()
        executor.get_status.return_value = AgentStatus.RUNNING
        with patch(
            "preloop.services.execution_monitor.create_agent_executor",
            return_value=executor,
        ) as create_mock:
            await ExecutionMonitor()._check_execution(db_session, execution)

        create_mock.assert_called_once()
        assert create_mock.call_args[0][0] == "opencode"

    @pytest.mark.asyncio
    async def test_recovery_uses_cell_agent_type(
        self, db_session: Session, test_flow: Flow
    ):
        from preloop.agents import AgentStatus
        from preloop.services.execution_recovery import ExecutionRecoveryService

        execution = self._matrix_execution(db_session, test_flow)

        executor = AsyncMock()
        # Terminal container state: recovery updates the row and returns
        # before spawning monitoring tasks, keeping the test self-contained.
        executor.get_status.return_value = AgentStatus.SUCCEEDED
        with patch(
            "preloop.agents.create_agent_executor", return_value=executor
        ) as create_mock:
            await ExecutionRecoveryService()._resume_execution_monitoring(
                db_session, execution, nats_client=AsyncMock()
            )

        create_mock.assert_called_once()
        assert create_mock.call_args[0][0] == "opencode"
        assert execution.status == "SUCCEEDED"
