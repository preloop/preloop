"""Tests for flow-level retry of transient upstream failures.

An upstream model provider having a bad minute used to kill an entire flow
permanently: the agent CLI exhausted its own internal retries and the run was
marked FAILED with no second chance. These tests pin the retry policy and,
more importantly, the safety boundary that keeps a retry from double-posting
external side effects (pull-request comments, commit statuses, pushes).
"""

import pytest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy.orm import Session

from preloop.agents.base import AgentExecutionResult, AgentStatus
from preloop.models.crud import crud_account, crud_flow, crud_user
from preloop.models.models import Account, Flow
from preloop.models.models.user import User
from preloop.models.schemas.flow import FlowCreate
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator


@pytest.fixture
def test_account(db_session: Session) -> Account:
    return crud_account.create(
        db_session,
        obj_in={"organization_name": f"Test Org {uuid4().hex[:8]}", "is_active": True},
    )


@pytest.fixture
def test_user(db_session: Session, test_account: Account) -> User:
    user = crud_user.create(
        db_session,
        obj_in={
            "account_id": test_account.id,
            "email": f"retry_test_{uuid4().hex[:8]}@example.com",
            "username": f"retry_test_user_{uuid4().hex[:8]}",
            "full_name": "Retry Test User",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "test_password",
            "user_source": "local",
        },
    )
    db_session.flush()
    test_account.primary_user_id = user.id
    db_session.add(test_account)
    db_session.commit()
    return user


@pytest.fixture
def test_flow(db_session: Session, test_account: Account, test_user: User) -> Flow:
    flow_in = FlowCreate(
        name="Test Retry Flow",
        description="A test flow for retry behaviour",
        trigger_event_source="github",
        trigger_event_types=["pull_request_updated"],
        prompt_template="Review: {{payload.pull_request.title}}",
        agent_type="codex",
        agent_config={},
        account_id=test_account.id,
    )
    return crud_flow.create(db=db_session, flow_in=flow_in, account_id=test_account.id)


@pytest.fixture
def mock_nats_client():
    client = AsyncMock()
    client.is_connected = True
    client.publish = AsyncMock()
    return client


@pytest.fixture
def event_data():
    return {
        "source": "github",
        "type": "pull_request_updated",
        "event_id": "evt_retry_1",
        "payload": {
            "pull_request": {"title": "Add a feature", "number": 7},
            "repository": "example-org/example-repo",
        },
        "account_id": str(uuid4()),
    }


UPSTREAM_TIMEOUT_LOGS = "\n".join(
    [
        "Attempt 1 failed with status 504. Retrying...",
        "Attempt 2 failed with status 504. Retrying...",
        "Attempt 3 failed with status 504. Max attempts reached.",
        "An unexpected critical error occurred:[object Object]",
    ]
)


def _failed_result(logs: str, exit_code: int = 1) -> AgentExecutionResult:
    """An agent result carrying an upstream-failure log tail."""
    from preloop.agents.failure_analysis import analyze_agent_failure

    return AgentExecutionResult(
        status=AgentStatus.FAILED,
        session_reference="session-under-test",
        output_summary=logs,
        error_message=analyze_agent_failure(logs).message,
        exit_code=exit_code,
    )


def _build_orchestrator(db_session, test_flow, event_data, mock_nats_client):
    return FlowExecutionOrchestrator(
        db=db_session,
        flow_id=test_flow.id,
        trigger_event_data=event_data,
        nats_client=mock_nats_client,
    )


@pytest.mark.asyncio
class TestTransientRetry:
    """Transient upstream failures get another attempt; others do not."""

    async def test_transient_upstream_failure_is_retried(
        self, db_session, test_flow, event_data, mock_nats_client
    ):
        attempts = []

        async def monitor(session_reference, agent_executor):
            attempts.append(session_reference)
            if len(attempts) == 1:
                return {
                    "status": "FAILED",
                    "error_message": "Upstream model provider timed out (HTTP 504) "
                    "after 3 attempts.",
                    "exit_code": 1,
                    "actions_taken": [],
                    "mcp_usage_logs": [],
                }
            return {
                "status": "SUCCEEDED",
                "output_summary": "review posted",
                "error_message": None,
                "exit_code": 0,
                "actions_taken": [],
                "mcp_usage_logs": [],
            }

        executor = AsyncMock()
        executor.start = AsyncMock(return_value="session-under-test")
        executor.cleanup = AsyncMock()

        with (
            patch(
                "preloop.services.flow_orchestrator.create_agent_executor",
                return_value=executor,
            ),
            patch.object(
                FlowExecutionOrchestrator,
                "_monitor_agent_execution",
                side_effect=monitor,
            ),
            patch("preloop.services.flow_orchestrator.asyncio.sleep", AsyncMock()),
        ):
            orchestrator = _build_orchestrator(
                db_session, test_flow, event_data, mock_nats_client
            )
            await orchestrator.run()

        assert len(attempts) == 2, "transient upstream failure should be retried once"
        assert orchestrator.execution_log.status == "SUCCEEDED"

    async def test_retry_is_visible_in_the_execution_record(
        self, db_session, test_flow, event_data, mock_nats_client
    ):
        """Flakiness must never be hidden: the retry has to be recorded."""
        calls = []

        async def monitor(session_reference, agent_executor):
            calls.append(session_reference)
            if len(calls) == 1:
                return {
                    "status": "FAILED",
                    "error_message": "Upstream model provider timed out (HTTP 504).",
                    "exit_code": 1,
                    "actions_taken": [],
                    "mcp_usage_logs": [],
                }
            return {
                "status": "SUCCEEDED",
                "output_summary": "ok",
                "error_message": None,
                "exit_code": 0,
                "actions_taken": [],
                "mcp_usage_logs": [],
            }

        executor = AsyncMock()
        executor.start = AsyncMock(return_value="session-under-test")
        executor.cleanup = AsyncMock()

        with (
            patch(
                "preloop.services.flow_orchestrator.create_agent_executor",
                return_value=executor,
            ),
            patch.object(
                FlowExecutionOrchestrator,
                "_monitor_agent_execution",
                side_effect=monitor,
            ),
            patch("preloop.services.flow_orchestrator.asyncio.sleep", AsyncMock()),
        ):
            orchestrator = _build_orchestrator(
                db_session, test_flow, event_data, mock_nats_client
            )
            await orchestrator.run()

        milestones = [
            entry["milestone"]
            for entry in orchestrator.execution_logger.get_milestones()
        ]
        assert "execution_retry_scheduled" in milestones

    async def test_non_transient_failure_is_not_retried(
        self, db_session, test_flow, event_data, mock_nats_client
    ):
        """Auth failures can never succeed on retry: do not burn money."""
        calls = []

        async def monitor(session_reference, agent_executor):
            calls.append(session_reference)
            return {
                "status": "FAILED",
                "error_message": "Upstream model provider rejected our credentials "
                "(HTTP 401).",
                "exit_code": 1,
                "actions_taken": [],
                "mcp_usage_logs": [],
            }

        executor = AsyncMock()
        executor.start = AsyncMock(return_value="session-under-test")
        executor.cleanup = AsyncMock()

        with (
            patch(
                "preloop.services.flow_orchestrator.create_agent_executor",
                return_value=executor,
            ),
            patch.object(
                FlowExecutionOrchestrator,
                "_monitor_agent_execution",
                side_effect=monitor,
            ),
            patch("preloop.services.flow_orchestrator.asyncio.sleep", AsyncMock()),
        ):
            orchestrator = _build_orchestrator(
                db_session, test_flow, event_data, mock_nats_client
            )
            await orchestrator.run()

        assert len(calls) == 1, "a 401 must never be retried"
        assert orchestrator.execution_log.status == "FAILED"

    async def test_user_stop_is_not_retried(
        self, db_session, test_flow, event_data, mock_nats_client
    ):
        calls = []

        async def monitor(session_reference, agent_executor):
            calls.append(session_reference)
            return {
                "status": "STOPPED",
                "error_message": "Execution stopped by user request after 45 seconds.",
                "actions_taken": [],
                "mcp_usage_logs": [],
            }

        executor = AsyncMock()
        executor.start = AsyncMock(return_value="session-under-test")
        executor.cleanup = AsyncMock()

        with (
            patch(
                "preloop.services.flow_orchestrator.create_agent_executor",
                return_value=executor,
            ),
            patch.object(
                FlowExecutionOrchestrator,
                "_monitor_agent_execution",
                side_effect=monitor,
            ),
            patch("preloop.services.flow_orchestrator.asyncio.sleep", AsyncMock()),
        ):
            orchestrator = _build_orchestrator(
                db_session, test_flow, event_data, mock_nats_client
            )
            await orchestrator.run()

        assert len(calls) == 1, "a user-requested stop must never be retried"

    async def test_retries_are_bounded(
        self, db_session, test_flow, event_data, mock_nats_client
    ):
        """A permanently-down provider must not loop forever."""
        calls = []

        async def monitor(session_reference, agent_executor):
            calls.append(session_reference)
            return {
                "status": "FAILED",
                "error_message": "Upstream model provider timed out (HTTP 504).",
                "exit_code": 1,
                "actions_taken": [],
                "mcp_usage_logs": [],
            }

        executor = AsyncMock()
        executor.start = AsyncMock(return_value="session-under-test")
        executor.cleanup = AsyncMock()

        with (
            patch(
                "preloop.services.flow_orchestrator.create_agent_executor",
                return_value=executor,
            ),
            patch.object(
                FlowExecutionOrchestrator,
                "_monitor_agent_execution",
                side_effect=monitor,
            ),
            patch("preloop.services.flow_orchestrator.asyncio.sleep", AsyncMock()),
        ):
            orchestrator = _build_orchestrator(
                db_session, test_flow, event_data, mock_nats_client
            )
            await orchestrator.run()

        assert 1 < len(calls) <= 3, f"expected bounded retries, got {len(calls)}"
        assert orchestrator.execution_log.status == "FAILED"

        # Exhaustion must be visible on the timeline: without a marker, the
        # final failure of a retried run looks identical to a first-attempt
        # failure.
        milestones = [
            entry["milestone"]
            for entry in orchestrator.execution_logger.get_milestones()
        ]
        assert "execution_retries_exhausted" in milestones


@pytest.mark.asyncio
class TestSideEffectSafety:
    """A retry must never double-post external side effects."""

    async def test_no_retry_once_the_agent_produced_external_effects(
        self, db_session, test_flow, event_data, mock_nats_client
    ):
        """If the agent already pushed or opened a PR, a retry would double-post.

        Post-execution git operations (push, PR/MR creation) run inside the
        container only when the agent exits 0. An agent that exits 0 but is
        judged failed may therefore already have posted; that case must not be
        retried even when the log mentions a transient upstream status.
        """
        calls = []

        async def monitor(session_reference, agent_executor):
            calls.append(session_reference)
            return {
                "status": "FAILED",
                "error_message": "Upstream model provider timed out (HTTP 504).",
                # Exit code 0 => the container ran its push / PR-creation block.
                "exit_code": 0,
                "actions_taken": [],
                "mcp_usage_logs": [],
            }

        executor = AsyncMock()
        executor.start = AsyncMock(return_value="session-under-test")
        executor.cleanup = AsyncMock()

        with (
            patch(
                "preloop.services.flow_orchestrator.create_agent_executor",
                return_value=executor,
            ),
            patch.object(
                FlowExecutionOrchestrator,
                "_monitor_agent_execution",
                side_effect=monitor,
            ),
            patch("preloop.services.flow_orchestrator.asyncio.sleep", AsyncMock()),
        ):
            orchestrator = _build_orchestrator(
                db_session, test_flow, event_data, mock_nats_client
            )
            await orchestrator.run()

        assert len(calls) == 1, (
            "an agent that exited 0 may already have pushed or opened a PR; "
            "retrying it risks double-posting"
        )

    async def test_commit_status_is_posted_once_per_outcome(
        self, db_session, test_flow, event_data, mock_nats_client
    ):
        """Retrying must not emit a failure status for the recovered attempt."""
        calls = []

        async def monitor(session_reference, agent_executor):
            calls.append(session_reference)
            if len(calls) == 1:
                return {
                    "status": "FAILED",
                    "error_message": "Upstream model provider timed out (HTTP 504).",
                    "exit_code": 1,
                    "actions_taken": [],
                    "mcp_usage_logs": [],
                }
            return {
                "status": "SUCCEEDED",
                "output_summary": "ok",
                "error_message": None,
                "exit_code": 0,
                "actions_taken": [],
                "mcp_usage_logs": [],
            }

        executor = AsyncMock()
        executor.start = AsyncMock(return_value="session-under-test")
        executor.cleanup = AsyncMock()

        with (
            patch(
                "preloop.services.flow_orchestrator.create_agent_executor",
                return_value=executor,
            ),
            patch.object(
                FlowExecutionOrchestrator,
                "_monitor_agent_execution",
                side_effect=monitor,
            ),
            patch.object(
                FlowExecutionOrchestrator, "_update_commit_status", AsyncMock()
            ) as mock_status,
            patch("preloop.services.flow_orchestrator.asyncio.sleep", AsyncMock()),
        ):
            orchestrator = _build_orchestrator(
                db_session, test_flow, event_data, mock_nats_client
            )
            await orchestrator.run()

        states = [call.kwargs.get("state") for call in mock_status.call_args_list]
        assert "failure" not in states, (
            "the intermediate transient failure must not be reported as a "
            f"final failure status; got {states}"
        )
        assert states[-1] == "success"
