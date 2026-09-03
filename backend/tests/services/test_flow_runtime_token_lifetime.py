"""Lifetime of a flow execution's runtime (gateway) token across a worker handoff.

A deploy drains the flow-execution worker: in-flight handlers are cancelled so
the claim can be released and the execution re-dispatched, while the agent Job
keeps running in Kubernetes and a peer worker resumes monitoring it. The
interrupted worker must therefore leave the runtime token alone (the live agent
is still authenticating with it against the model gateway), and the worker that
actually finishes the execution must retire it.
"""

import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models.crud import (
    crud_account,
    crud_api_key,
    crud_flow,
    crud_runtime_session,
    crud_user,
)
from preloop.models.models import Account, Flow
from preloop.models.models.flow_execution import FlowExecution
from preloop.models.models.user import User
from preloop.models.schemas.flow import FlowCreate
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.services.model_gateway_auth import authenticate_bearer_token


@pytest.fixture
def token_account(db_session: Session) -> Account:
    return crud_account.create(
        db_session,
        obj_in={
            "organization_name": f"Token Org {uuid4().hex[:8]}",
            "is_active": True,
        },
    )


@pytest.fixture
def token_user(db_session: Session, token_account: Account) -> User:
    user = crud_user.create(
        db_session,
        obj_in={
            "account_id": token_account.id,
            "email": f"token_{uuid4().hex[:8]}@example.com",
            "username": f"token_user_{uuid4().hex[:8]}",
            "full_name": "Runtime Token User",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "test_password",
            "user_source": "local",
        },
    )
    db_session.flush()
    db_session.refresh(user)
    token_account.primary_user_id = user.id
    db_session.add(token_account)
    db_session.commit()
    return user


@pytest.fixture
def token_flow(db_session: Session, token_account: Account, token_user: User) -> Flow:
    flow_in = FlowCreate(
        name="Runtime Token Flow",
        description="Flow used to exercise runtime token lifetime",
        trigger_event_source="github",
        trigger_event_types=["issue_created"],
        prompt_template="Fix issue: {{payload.issue.title}}",
        agent_type="openhands",
        agent_config={"max_iterations": 10},
        account_id=token_account.id,
    )
    return crud_flow.create(db=db_session, flow_in=flow_in, account_id=token_account.id)


@pytest.fixture
def trigger_data():
    return {
        "source": "github",
        "type": "issue_created",
        "event_id": "evt_runtime_token",
        "payload": {"issue": {"title": "Broken login", "number": 7}},
    }


def _new_orchestrator(db_session, flow, trigger_data):
    nats_client = AsyncMock()
    nats_client.is_connected = True
    return FlowExecutionOrchestrator(
        db=db_session,
        flow_id=flow.id,
        trigger_event_data=trigger_data,
        nats_client=nats_client,
    )


def _succeeding_executor():
    from preloop.agents.base import AgentExecutionResult, AgentStatus

    executor = AsyncMock()
    executor.start = AsyncMock(return_value="agent-session-ref")
    executor.get_status = AsyncMock(return_value=AgentStatus.SUCCEEDED)
    executor.get_result = AsyncMock(
        return_value=AgentExecutionResult(
            status=AgentStatus.SUCCEEDED,
            session_reference="agent-session-ref",
            output_summary="done",
            actions_taken=None,
            exit_code=0,
        )
    )
    executor.stop = AsyncMock()
    return executor


async def _run_until_drained(orchestrator) -> str:
    """Run an execution until a deploy drain cancels its monitoring task.

    Returns the plaintext runtime token minted for the execution.
    """
    minted = {}
    original = FlowExecutionOrchestrator._create_temporary_api_token

    def spy(self):
        token, key_id = original(self)
        minted["token"] = token
        return token, key_id

    with (
        patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=_succeeding_executor(),
        ),
        patch.object(FlowExecutionOrchestrator, "_create_temporary_api_token", spy),
        patch.object(
            orchestrator,
            "_monitor_agent_execution",
            new_callable=AsyncMock,
            side_effect=asyncio.CancelledError(),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await orchestrator.run()

    assert minted.get("token"), "no runtime token was minted"
    return minted["token"]


class TestRuntimeTokenSurvivesWorkerHandoff:
    """The interrupted worker must not disarm a still-running agent."""

    @pytest.mark.asyncio
    async def test_drained_worker_leaves_token_and_session_alive(
        self, db_session: Session, token_flow: Flow, token_user: User, trigger_data
    ):
        orchestrator = _new_orchestrator(db_session, token_flow, trigger_data)

        token = await _run_until_drained(orchestrator)

        key = crud_api_key.get(db_session, id=orchestrator.temporary_api_key_id)
        assert key is not None
        assert key.is_active is True, (
            "the drained worker revoked the token of an agent that is still running"
        )

        # The credential still authenticates: this is exactly the call the live
        # agent makes against the model gateway after the handoff.
        auth = await authenticate_bearer_token(token, db_session)
        assert auth is not None
        assert auth.user.id == token_user.id

        session = crud_runtime_session.get_by_source(
            db_session,
            account_id=token_flow.account_id,
            session_source_type="flow_execution",
            session_source_id=str(orchestrator.execution_log.id),
        )
        assert session is not None
        assert session.ended_at is None
        assert orchestrator.execution_log.status not in (
            "SUCCEEDED",
            "FAILED",
            "STOPPED",
            "CANCELLED",
        )

    @pytest.mark.asyncio
    async def test_resuming_worker_retires_token_and_session(
        self, db_session: Session, token_flow: Flow, trigger_data
    ):
        from preloop.services.flow_execution_runner import resume_existing_execution

        drained = _new_orchestrator(db_session, token_flow, trigger_data)
        token = await _run_until_drained(drained)
        execution_id = drained.execution_log.id
        key_id = drained.temporary_api_key_id

        # A peer worker picks the execution up: fresh orchestrator, no
        # temporary_api_key_id of its own, adopting the running agent.
        resuming = _new_orchestrator(db_session, token_flow, trigger_data)
        resuming.execution_log = drained.execution_log
        assert resuming.temporary_api_key_id is None

        with (
            patch("preloop.agents.create_agent_executor", return_value=AsyncMock()),
            patch.object(
                resuming,
                "_monitor_agent_execution",
                new_callable=AsyncMock,
                return_value={"status": "SUCCEEDED", "output_summary": "done"},
            ),
        ):
            await resume_existing_execution(resuming, "agent-session-ref")

        key = crud_api_key.get(db_session, id=key_id)
        assert key is not None
        assert key.is_active is False
        assert await authenticate_bearer_token(token, db_session) is None

        session = crud_runtime_session.get_by_source(
            db_session,
            account_id=token_flow.account_id,
            session_source_type="flow_execution",
            session_source_id=str(execution_id),
        )
        assert session is not None
        assert session.ended_at is not None

    @pytest.mark.asyncio
    async def test_completed_run_still_revokes_its_token(
        self, db_session: Session, token_flow: Flow, trigger_data
    ):
        orchestrator = _new_orchestrator(db_session, token_flow, trigger_data)
        minted = {}
        original = FlowExecutionOrchestrator._create_temporary_api_token

        def spy(self):
            token, key_id = original(self)
            minted["token"] = token
            return token, key_id

        with (
            patch(
                "preloop.services.flow_orchestrator.create_executor_for_execution",
                return_value=_succeeding_executor(),
            ),
            patch.object(FlowExecutionOrchestrator, "_create_temporary_api_token", spy),
        ):
            await orchestrator.run()

        assert orchestrator.execution_log.status in ("SUCCEEDED", "FAILED")
        key = crud_api_key.get(db_session, id=orchestrator.temporary_api_key_id)
        assert key is not None
        assert key.is_active is False
        assert await authenticate_bearer_token(minted["token"], db_session) is None

    def test_cleanup_is_a_no_op_while_the_execution_runs(
        self, db_session: Session, token_flow: Flow, trigger_data
    ):
        orchestrator = _new_orchestrator(db_session, token_flow, trigger_data)
        orchestrator.flow = token_flow
        execution = FlowExecution(flow_id=token_flow.id, status="RUNNING")
        db_session.add(execution)
        db_session.commit()
        db_session.refresh(execution)
        orchestrator.execution_log = execution

        token, key_id = orchestrator._create_temporary_api_token()
        assert token is not None

        orchestrator._cleanup_temporary_api_token()

        key = crud_api_key.get(db_session, id=key_id)
        assert key.is_active is True

        # Same call once the execution is terminal does revoke it.
        execution.status = "SUCCEEDED"
        db_session.add(execution)
        db_session.commit()

        orchestrator._cleanup_temporary_api_token()

        db_session.refresh(key)
        assert key.is_active is False

    def test_revocation_covers_every_key_of_the_execution(
        self, db_session: Session, token_flow: Flow, trigger_data
    ):
        """Two workers, two minted keys, one execution: both must be retired."""
        orchestrator = _new_orchestrator(db_session, token_flow, trigger_data)
        orchestrator.flow = token_flow
        execution = FlowExecution(flow_id=token_flow.id, status="RUNNING")
        db_session.add(execution)
        db_session.commit()
        db_session.refresh(execution)
        orchestrator.execution_log = execution

        _, first_key_id = orchestrator._create_temporary_api_token()
        _, second_key_id = orchestrator._create_temporary_api_token()
        assert first_key_id != second_key_id

        revoked = orchestrator._revoke_execution_runtime_tokens()

        assert revoked == 2
        for key_id in (first_key_id, second_key_id):
            assert crud_api_key.get(db_session, id=key_id).is_active is False

    def test_a_sibling_executions_token_is_untouched(
        self, db_session: Session, token_flow: Flow, trigger_data
    ):
        """Revocation is scoped to one execution, never to the flow."""
        sibling = _new_orchestrator(db_session, token_flow, trigger_data)
        sibling.flow = token_flow
        sibling_execution = FlowExecution(flow_id=token_flow.id, status="RUNNING")
        db_session.add(sibling_execution)
        db_session.commit()
        db_session.refresh(sibling_execution)
        sibling.execution_log = sibling_execution
        sibling_token, sibling_key_id = sibling._create_temporary_api_token()

        finishing = _new_orchestrator(db_session, token_flow, trigger_data)
        finishing.flow = token_flow
        finished_execution = FlowExecution(flow_id=token_flow.id, status="SUCCEEDED")
        db_session.add(finished_execution)
        db_session.commit()
        db_session.refresh(finished_execution)
        finishing.execution_log = finished_execution
        finishing._create_temporary_api_token()

        finishing._cleanup_temporary_api_token()

        assert crud_api_key.get(db_session, id=sibling_key_id).is_active is True


class TestRecoveryRetiresCredentials:
    """Recovery decides a run is over without running its orchestrator teardown."""

    @pytest.mark.asyncio
    async def test_startup_failure_recovery_revokes_token_and_ends_session(
        self, db_session: Session, token_flow: Flow, trigger_data
    ):
        from preloop.services.execution_recovery import ExecutionRecoveryService

        orchestrator = _new_orchestrator(db_session, token_flow, trigger_data)
        orchestrator.flow = token_flow
        execution = FlowExecution(flow_id=token_flow.id, status="RUNNING")
        db_session.add(execution)
        db_session.commit()
        db_session.refresh(execution)
        orchestrator.execution_log = execution

        token, key_id = orchestrator._create_temporary_api_token()
        assert execution.agent_session_reference is None

        await ExecutionRecoveryService()._resume_execution_monitoring(
            db_session, execution, AsyncMock()
        )

        db_session.refresh(execution)
        assert execution.status == "FAILED"
        assert crud_api_key.get(db_session, id=key_id).is_active is False
        assert await authenticate_bearer_token(token, db_session) is None

        session = crud_runtime_session.get_by_source(
            db_session,
            account_id=token_flow.account_id,
            session_source_type="flow_execution",
            session_source_id=str(execution.id),
        )
        assert session is not None
        assert session.ended_at is not None
