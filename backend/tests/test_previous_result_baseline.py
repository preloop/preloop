"""A review run seeds its baseline from a previous execution id.

Covers the path end to end at the runner boundary: the trigger payload
names a previous execution, the orchestrator resolves it while building
the execution context, and the agent layer writes it into the workspace
before the agent starts. Also covers what a scheduled subscription needs
(the schedule's static payload) and what must never happen (a failed run,
or a marker that reveals another account's data).

The resolution rules live in tests/services/test_workspace_baseline.py;
the transport in tests/utils/test_workspace_baseline.py.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"

from preloop.agents.container import ContainerAgentExecutor
from preloop.models.crud import crud_account, crud_flow, crud_flow_execution
from preloop.models.models import Account, Flow
from preloop.models.models.user import User
from preloop.models.schemas.flow import CronSchedule, FlowCreate
from preloop.models.schemas.flow_execution import (
    FlowExecutionCreate,
    FlowExecutionUpdate,
)
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.utils.workspace_baseline import (
    BASELINE_MISMATCH_PATH,
    BASELINE_WORKSPACE_PATH,
    MAX_BASELINE_RESULT_BYTES,
    MISMATCH_NO_RESULT,
    MISMATCH_TOO_LARGE,
    MISMATCH_UNAVAILABLE,
    PREVIOUS_RESULT_EXECUTION_ID_KEY,
    BaselineDelivery,
    baseline_chunk_env_var,
    build_workspace_baseline_shell,
)
from preloop.utils.workspace_seed import (
    build_workspace_seed_shell,
    parse_workspace_files,
    workspace_seed_env,
)

PREVIOUS_RESULT = {
    "schema": "preloop.review.codehealth/v1",
    "verdict": "pass_with_findings",
    "findings": [
        {"id": "health:quality:app/main.py:long-function", "status": "open"},
        {"id": "health:dead-code:app/util.py:unused-export", "status": "open"},
    ],
}


def _flow(db_session: Session, account_id, **overrides) -> Flow:
    fields = {
        "name": f"Review Flow {uuid4().hex[:8]}",
        "prompt_template": "Review {{payload.repository_url}}",
        "agent_type": "openhands",
        "agent_config": {"max_iterations": 10},
        "trigger_event_source": "webhook",
        "trigger_event_types": ["webhook"],
        **overrides,
    }
    flow_in = FlowCreate(**fields)
    return crud_flow.create(db=db_session, flow_in=flow_in, account_id=account_id)


def _execution(db_session: Session, flow: Flow, result=None):
    execution = crud_flow_execution.create(
        db_session, obj_in=FlowExecutionCreate(flow_id=flow.id, status="COMPLETED")
    )
    if result is not None:
        crud_flow_execution.update(
            db_session, db_obj=execution, obj_in=FlowExecutionUpdate(result=result)
        )
    return execution


async def _context(db_session: Session, flow: Flow, payload: dict, mock_nats_client):
    """Prepare an execution context the way a real run does."""
    orchestrator = FlowExecutionOrchestrator(
        db=db_session,
        flow_id=flow.id,
        trigger_event_data={
            "source": "webhook",
            "type": "webhook",
            "payload": payload,
        },
        nats_client=mock_nats_client,
    )
    orchestrator._get_flow_details()
    orchestrator.execution_log = crud_flow_execution.create(
        db_session, obj_in=FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )
    return await orchestrator._prepare_execution_context(resolved_prompt="review")


def _executor_stub() -> SimpleNamespace:
    """The agent-layer methods under test, without a container executor."""
    return SimpleNamespace(
        logger=logging.getLogger(__name__),
        _workspace_baseline_delivery=(
            ContainerAgentExecutor._workspace_baseline_delivery
        ),
        _workspace_seed_payload=ContainerAgentExecutor._workspace_seed_payload,
    )


def _materialize(context: dict, root) -> subprocess.CompletedProcess:
    """Run the workspace prelude the agent layer builds, rooted at ``root``.

    Same delivery, same environment and same block order as
    ``_prepare_init_commands`` (baseline first, then the payload seeds);
    only the workspace root is redirected so the test can read the files.
    """
    stub = _executor_stub()
    env = ContainerAgentExecutor._workspace_baseline_env(stub, context)
    delivery = ContainerAgentExecutor._workspace_baseline_delivery(context)
    commands = []
    if ContainerAgentExecutor._prepare_workspace_baseline_commands(stub, context):
        commands.append(build_workspace_baseline_shell(delivery, workspace_root=root))

    seeds = parse_workspace_files(
        ContainerAgentExecutor._workspace_seed_payload(context)
    )
    if seeds:
        env.update(workspace_seed_env(seeds))
        commands.append(build_workspace_seed_shell(seeds, workspace_root=root))

    return subprocess.run(
        ["sh", "-c", " && ".join(commands)],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
    )


@pytest.fixture
def mock_nats_client() -> AsyncMock:
    """The orchestrator publishes progress; nothing here needs a broker."""
    return AsyncMock()


@pytest.fixture
def test_flow(db_session: Session, test_user: User) -> Flow:
    """A review flow owned by the test user's account."""
    return _flow(db_session, test_user.account_id)


class TestBaselineReachesTheWorkspace:
    """The file contents, not just the intent."""

    @pytest.mark.asyncio
    async def test_valid_execution_id_lands_at_the_documented_path(
        self, db_session: Session, test_flow: Flow, mock_nats_client, tmp_path
    ):
        previous = _execution(db_session, test_flow, result=PREVIOUS_RESULT)

        context = await _context(
            db_session,
            test_flow,
            {PREVIOUS_RESULT_EXECUTION_ID_KEY: str(previous.id)},
            mock_nats_client,
        )
        outcome = _materialize(context, str(tmp_path))

        assert outcome.returncode == 0, outcome.stderr
        written = tmp_path / BASELINE_WORKSPACE_PATH
        assert json.loads(written.read_text()) == PREVIOUS_RESULT
        assert not (tmp_path / BASELINE_MISMATCH_PATH).exists()
        assert context["baseline_delivery"]["source_execution_id"] == str(previous.id)

    @pytest.mark.asyncio
    async def test_the_baseline_never_enters_the_launch_command(
        self, db_session: Session, test_flow: Flow, mock_nats_client
    ):
        """Contents travel in the environment; the command is capped."""
        previous = _execution(
            db_session, test_flow, result={"findings": ["q" * 400] * 200}
        )

        context = await _context(
            db_session,
            test_flow,
            {PREVIOUS_RESULT_EXECUTION_ID_KEY: str(previous.id)},
            mock_nats_client,
        )
        stub = _executor_stub()
        shell = ContainerAgentExecutor._prepare_workspace_baseline_commands(
            stub, context
        )
        env = ContainerAgentExecutor._workspace_baseline_env(stub, context)

        assert "q" * 100 not in shell
        rejoined = "".join(
            env[baseline_chunk_env_var(index)] for index in range(len(env))
        )
        assert json.loads(base64.b64decode(rejoined)) == {"findings": ["q" * 400] * 200}

    @pytest.mark.asyncio
    async def test_no_key_delivers_nothing(
        self, db_session: Session, test_flow: Flow, mock_nats_client, tmp_path
    ):
        context = await _context(
            db_session, test_flow, {"depth": "quick"}, mock_nats_client
        )

        assert "baseline_delivery" not in context
        stub = _executor_stub()
        assert (
            ContainerAgentExecutor._prepare_workspace_baseline_commands(stub, context)
            == ""
        )


class TestBaselineDegradesInsteadOfFailing:
    """An unusable id is a run without drift, never a failed run."""

    @pytest.mark.asyncio
    async def test_foreign_execution_id_writes_a_marker_and_runs(
        self, db_session: Session, test_flow: Flow, mock_nats_client, tmp_path
    ):
        other_account = crud_account.create(
            db_session,
            obj_in={"organization_name": f"Org {uuid4().hex[:8]}", "is_active": True},
        )
        other_flow = _flow(db_session, other_account.id)
        foreign = _execution(db_session, other_flow, result=PREVIOUS_RESULT)

        context = await _context(
            db_session,
            test_flow,
            {PREVIOUS_RESULT_EXECUTION_ID_KEY: str(foreign.id)},
            mock_nats_client,
        )
        outcome = _materialize(context, str(tmp_path))

        assert outcome.returncode == 0, outcome.stderr
        marker = json.loads((tmp_path / BASELINE_MISMATCH_PATH).read_text())
        assert marker["baseline_mismatch"] is True
        assert marker["reason"] == MISMATCH_UNAVAILABLE
        assert not (tmp_path / BASELINE_WORKSPACE_PATH).exists()

    @pytest.mark.asyncio
    async def test_execution_without_a_result_writes_the_same_marker(
        self, db_session: Session, test_flow: Flow, mock_nats_client, tmp_path
    ):
        resultless = _execution(db_session, test_flow)

        context = await _context(
            db_session,
            test_flow,
            {PREVIOUS_RESULT_EXECUTION_ID_KEY: str(resultless.id)},
            mock_nats_client,
        )
        outcome = _materialize(context, str(tmp_path))

        assert outcome.returncode == 0, outcome.stderr
        marker = json.loads((tmp_path / BASELINE_MISMATCH_PATH).read_text())
        assert marker["baseline_mismatch"] is True
        assert marker["reason"] == MISMATCH_NO_RESULT

    @pytest.mark.asyncio
    async def test_oversized_result_is_refused_not_truncated(
        self, db_session: Session, test_flow: Flow, mock_nats_client, tmp_path
    ):
        oversized = _execution(
            db_session,
            test_flow,
            result={"findings": ["x" * 500] * (MAX_BASELINE_RESULT_BYTES // 500)},
        )

        context = await _context(
            db_session,
            test_flow,
            {PREVIOUS_RESULT_EXECUTION_ID_KEY: str(oversized.id)},
            mock_nats_client,
        )
        outcome = _materialize(context, str(tmp_path))

        assert outcome.returncode == 0, outcome.stderr
        marker = json.loads((tmp_path / BASELINE_MISMATCH_PATH).read_text())
        assert marker["reason"] == MISMATCH_TOO_LARGE
        assert not (tmp_path / BASELINE_WORKSPACE_PATH).exists()


class TestTheResponseKeepsForeignIdsSecret:
    """A foreign id must answer exactly like an id that never existed."""

    def test_trigger_responses_are_indistinguishable(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        other_account: Account = crud_account.create(
            db_session,
            obj_in={"organization_name": f"Org {uuid4().hex[:8]}", "is_active": True},
        )
        other_flow = _flow(db_session, other_account.id)
        foreign = _execution(db_session, other_flow, result=PREVIOUS_RESULT)
        flow = _flow(db_session, test_user.account_id)

        foreign_response = client.post(
            f"/api/v1/flows/{flow.id}/trigger",
            json={"payload": {PREVIOUS_RESULT_EXECUTION_ID_KEY: str(foreign.id)}},
        )
        unknown_response = client.post(
            f"/api/v1/flows/{flow.id}/trigger",
            json={"payload": {PREVIOUS_RESULT_EXECUTION_ID_KEY: str(uuid4())}},
        )

        assert foreign_response.status_code == unknown_response.status_code == 200
        assert sorted(foreign_response.json()) == sorted(unknown_response.json())
        assert foreign_response.json()["status"] == unknown_response.json()["status"]


class TestExplicitFileWins:
    """Documented precedence, asserted on what the workspace ends up with."""

    @pytest.mark.asyncio
    async def test_seeded_file_beats_the_execution_id(
        self, db_session: Session, test_flow: Flow, mock_nats_client, tmp_path
    ):
        previous = _execution(db_session, test_flow, result=PREVIOUS_RESULT)
        seeded = {"schema": "preloop.review.codehealth/v1", "findings": ["seeded"]}

        context = await _context(
            db_session,
            test_flow,
            {
                PREVIOUS_RESULT_EXECUTION_ID_KEY: str(previous.id),
                "previous_result_path": BASELINE_WORKSPACE_PATH,
                "workspace_files": [
                    {
                        "path": BASELINE_WORKSPACE_PATH,
                        "content_base64": base64.b64encode(
                            json.dumps(seeded).encode()
                        ).decode(),
                    }
                ],
            },
            mock_nats_client,
        )
        outcome = _materialize(context, str(tmp_path))

        assert outcome.returncode == 0, outcome.stderr
        assert "baseline_delivery" not in context
        written = tmp_path / BASELINE_WORKSPACE_PATH
        assert json.loads(written.read_text()) == seeded
        assert not (tmp_path / BASELINE_MISMATCH_PATH).exists()


class TestScheduledSubscription:
    """A schedule states the key once and every run diffs against the last."""

    @pytest.mark.asyncio
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    async def test_schedule_payload_carries_the_key_into_the_run(
        self, mock_nats, db_session: Session, test_user: User
    ):
        flow = _flow(
            db_session,
            test_user.account_id,
            trigger_event_source="schedule",
            trigger_event_types=["schedule"],
            schedule_config=CronSchedule(
                expr="0 7 * * 1",
                timezone="UTC",
                payload={PREVIOUS_RESULT_EXECUTION_ID_KEY: "last", "depth": "standard"},
            ),
        )
        mock_nats.return_value = AsyncMock()
        service = FlowTriggerService(db_session)

        with patch.object(
            service, "_start_flow_execution", new_callable=AsyncMock
        ) as started:
            outcome = await service.run_scheduled_tick(flow.id)

        assert outcome == "triggered"
        payload = started.call_args[1]["event_data"]["payload"]
        assert payload[PREVIOUS_RESULT_EXECUTION_ID_KEY] == "last"
        assert payload["depth"] == "standard"
        # The schedule's own fields still describe when it fired.
        assert payload["schedule"]["expr"] == "0 7 * * 1"
        assert "payload" not in payload["schedule"]
        assert payload["scheduled_at"]

    @pytest.mark.asyncio
    async def test_the_scheduled_run_diffs_against_its_own_last_run(
        self, db_session: Session, test_flow: Flow, mock_nats_client, tmp_path
    ):
        """``last`` resolves to this flow's most recent stored result."""
        _execution(db_session, test_flow, result={"schema": "older", "findings": []})
        latest = _execution(db_session, test_flow, result=PREVIOUS_RESULT)

        context = await _context(
            db_session,
            test_flow,
            {PREVIOUS_RESULT_EXECUTION_ID_KEY: "last", "scheduled_at": "2026-09-15"},
            mock_nats_client,
        )
        outcome = _materialize(context, str(tmp_path))

        assert outcome.returncode == 0, outcome.stderr
        assert (
            json.loads((tmp_path / BASELINE_WORKSPACE_PATH).read_text())
            == PREVIOUS_RESULT
        )
        assert context["baseline_delivery"]["source_execution_id"] == str(latest.id)

    @pytest.mark.asyncio
    async def test_the_first_scheduled_run_still_starts(
        self, db_session: Session, test_flow: Flow, mock_nats_client, tmp_path
    ):
        """Nothing to diff against yet is a marker, not a failure."""
        context = await _context(
            db_session,
            test_flow,
            {PREVIOUS_RESULT_EXECUTION_ID_KEY: "last"},
            mock_nats_client,
        )
        outcome = _materialize(context, str(tmp_path))

        assert outcome.returncode == 0, outcome.stderr
        marker = json.loads((tmp_path / BASELINE_MISMATCH_PATH).read_text())
        assert marker["reason"] == MISMATCH_UNAVAILABLE


class TestSeededBaselineBehaviourIsUnchanged:
    """The file based baseline still works exactly as before."""

    @pytest.mark.asyncio
    async def test_seeded_baseline_without_the_key(
        self, db_session: Session, test_flow: Flow, mock_nats_client, tmp_path
    ):
        seeded = {"schema": "preloop.review.arch/v1", "findings": ["seeded"]}

        context = await _context(
            db_session,
            test_flow,
            {
                "previous_result_path": BASELINE_WORKSPACE_PATH,
                "workspace_files": [
                    {
                        "path": BASELINE_WORKSPACE_PATH,
                        "content_base64": base64.b64encode(
                            json.dumps(seeded).encode()
                        ).decode(),
                    }
                ],
            },
            mock_nats_client,
        )
        outcome = _materialize(context, str(tmp_path))

        assert outcome.returncode == 0, outcome.stderr
        assert "baseline_delivery" not in context
        assert json.loads((tmp_path / BASELINE_WORKSPACE_PATH).read_text()) == seeded

    def test_a_context_without_the_key_transports_nothing(self):
        """Executions created before this feature carry no delivery."""
        stub = _executor_stub()
        context = {"trigger_event_data": {"payload": {}}}
        assert ContainerAgentExecutor._workspace_baseline_delivery(context) is None
        assert ContainerAgentExecutor._workspace_baseline_env(stub, context) == {}

    def test_a_delivery_round_trips_through_the_context(self):
        delivery = BaselineDelivery(content_base64="e30=", source_execution_id="x")
        context = {"baseline_delivery": delivery.model_dump()}
        assert ContainerAgentExecutor._workspace_baseline_delivery(context) == delivery
