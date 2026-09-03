"""Tests for flow-level retry of transient upstream failures.

An upstream model provider having a bad minute used to kill an entire flow
permanently: the agent CLI exhausted its own internal retries and the run was
marked FAILED with no second chance. These tests pin the retry policy and,
more importantly, the safety boundary that keeps a retry from double-posting
external side effects (pull-request comments, commit statuses, pushes).
"""

import dataclasses

import pytest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy.orm import Session

from preloop.agents.base import AgentExecutionResult, AgentStatus
from preloop.agents.container import (
    AGENT_SESSION_SUFFIX_KEY,
    ContainerAgentExecutor,
    kubernetes_job_name,
)
from preloop.agents.errors import AgentStartError
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
                "preloop.services.flow_orchestrator.create_executor_for_execution",
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
                "preloop.services.flow_orchestrator.create_executor_for_execution",
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
                "preloop.services.flow_orchestrator.create_executor_for_execution",
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
                "preloop.services.flow_orchestrator.create_executor_for_execution",
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
                "preloop.services.flow_orchestrator.create_executor_for_execution",
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
                "preloop.services.flow_orchestrator.create_executor_for_execution",
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
                "preloop.services.flow_orchestrator.create_executor_for_execution",
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


# The raw log shape from the rollout incident: the agent CLI crashed on a
# severed LLM stream *without* running its own retry loop, so the logs carry
# no "attempt N failed" phrasing at all — only undici's stack. This is the
# one shape the stored error MESSAGE cannot classify on its own; the verdict
# has to travel with the agent result.
SEVERED_STREAM_STACK_LOGS = [
    "[Agent Status] running",
    "Streaming model response...",
    "TypeError: terminated",
    "    at Fetch.onAborted (node:internal/deps/undici/undici:11190:53)",
    "    at Fetch.emit (node:events:518:28)",
    "  [cause]: SocketError: other side closed",
    "      at TLSSocket.onSocketEnd (node:internal/deps/undici/undici:8117:26)",
]


async def _agent_result_via_container(logs: list[str]) -> dict:
    """Build the agent-result payload the way production actually does.

    The real ``ContainerAgentExecutor.get_result`` (Docker mocked away)
    produces the ``AgentExecutionResult`` from the logs; the returned dict
    mirrors the terminal-result payload ``_monitor_agent_execution`` hands to
    ``_retry_decision``. Going through the real extraction path is the point:
    these tests pin the *wiring* from full-log analysis to retry decision,
    not any single component.
    """
    executor = ContainerAgentExecutor(
        agent_type="codex",
        config={},
        image="test-image:latest",
        use_kubernetes=False,
    )

    mock_docker = AsyncMock()
    mock_container = AsyncMock()
    mock_container.show.return_value = {"State": {"ExitCode": 1, "Error": ""}}
    mock_docker.containers.get.return_value = mock_container

    with (
        patch.object(ContainerAgentExecutor, "get_logs", AsyncMock(return_value=logs)),
        patch.object(
            ContainerAgentExecutor,
            "get_status",
            AsyncMock(return_value=AgentStatus.FAILED),
        ),
        patch.object(
            ContainerAgentExecutor,
            "_get_docker_client",
            AsyncMock(return_value=mock_docker),
        ),
    ):
        result = await executor.get_result("container-under-test")

    # ``getattr`` keeps this helper runnable against pre-wiring results, the
    # same tolerance _retry_decision itself must have for legacy payloads.
    analysis = getattr(result, "failure_analysis", None)
    return {
        "status": result.status.value,
        "output_summary": result.output_summary,
        "error_message": result.error_message,
        "actions_taken": [],
        "mcp_usage_logs": [],
        "exit_code": result.exit_code,
        "failure_analysis": (
            dataclasses.asdict(analysis) if analysis is not None else None
        ),
    }


@pytest.mark.asyncio
class TestRetryVerdictWiring:
    """The full-log verdict, not the stored message, drives the retry.

    ``container.get_result`` analyses the complete container logs;
    ``FlowExecution.error_message`` keeps only the generated sentence. These
    tests pin the contract that the transient/terminal verdict survives that
    lossy step by travelling on the agent result itself.
    """

    async def test_severed_stream_stack_is_retried_end_to_end(
        self, db_session, test_flow, event_data, mock_nats_client
    ):
        """The incident shape: a raw undici stack must schedule a retry.

        The fallback error message is the stack text, which re-analysis of
        the message alone classifies as non-transient. Only the verdict
        attached to the agent result can carry "this was a severed stream —
        retry it" across the message bottleneck.
        """
        agent_result = await _agent_result_via_container(SEVERED_STREAM_STACK_LOGS)

        orchestrator = _build_orchestrator(
            db_session, test_flow, event_data, mock_nats_client
        )

        assert orchestrator._retry_decision(agent_result) is None, (
            "a stream severed mid-response (undici 'TypeError: terminated' / "
            "'other side closed') is transient and must be retried"
        )

    async def test_policy_terminated_attempts_are_not_retried_end_to_end(
        self, db_session, test_flow, event_data, mock_nats_client
    ):
        """A policy kill with retry-loop phrasing must never retry.

        The generated message for this shape is 'Upstream model provider was
        unreachable after 2 attempts.', which message-only re-analysis calls
        transient. The first-pass verdict (terminal: no transport failure in
        the logs) must win.
        """
        agent_result = await _agent_result_via_container(
            [
                "Attempt 1 failed: run terminated by policy",
                "Attempt 2 failed: run terminated by policy",
                "Giving up.",
            ]
        )

        orchestrator = _build_orchestrator(
            db_session, test_flow, event_data, mock_nats_client
        )

        assert orchestrator._retry_decision(agent_result) is not None, (
            "a run terminated by policy can never succeed on retry; the "
            "full-log verdict must override the message's phrasing"
        )

    async def test_plain_crash_stays_non_retryable_end_to_end(
        self, db_session, test_flow, event_data, mock_nats_client
    ):
        """Guard against over-matching: an ordinary crash must not retry."""
        agent_result = await _agent_result_via_container(
            [
                "Traceback (most recent call last):",
                '  File "agent.py", line 10, in run',
                "ValueError: prompt template rendered empty",
            ]
        )

        orchestrator = _build_orchestrator(
            db_session, test_flow, event_data, mock_nats_client
        )

        assert orchestrator._retry_decision(agent_result) is not None

    async def test_attached_verdict_wins_over_message_phrasing(
        self, db_session, test_flow, event_data, mock_nats_client
    ):
        """Through the retry loop: a terminal verdict beats a transient-looking message."""
        calls = []

        async def monitor(session_reference, agent_executor):
            calls.append(session_reference)
            return {
                "status": "FAILED",
                # Message-only re-analysis would call this transient
                # (no-status branch of _reanalyze_generated_message).
                "error_message": "Upstream model provider was unreachable "
                "after 2 attempts.",
                "exit_code": 1,
                "actions_taken": [],
                "mcp_usage_logs": [],
                "failure_analysis": {
                    "message": "Upstream model provider was unreachable "
                    "after 2 attempts.",
                    "transient": False,
                },
            }

        executor = AsyncMock()
        executor.start = AsyncMock(return_value="session-under-test")
        executor.cleanup = AsyncMock()

        with (
            patch(
                "preloop.services.flow_orchestrator.create_executor_for_execution",
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
            "the first-pass terminal verdict travels with the result and "
            "must not be overridden by re-analysing the message"
        )

    async def test_legacy_result_without_verdict_still_retries_on_message(
        self, db_session, test_flow, event_data, mock_nats_client
    ):
        """Backward compat: no attached verdict → fall back to the message."""
        agent_result = {
            "status": "FAILED",
            "error_message": "Upstream model provider timed out (HTTP 504) "
            "after 3 attempts.",
            "exit_code": 1,
            "actions_taken": [],
            "mcp_usage_logs": [],
        }

        orchestrator = _build_orchestrator(
            db_session, test_flow, event_data, mock_nats_client
        )

        assert orchestrator._retry_decision(agent_result) is None


@pytest.mark.asyncio
class TestPerAttemptSessionNames:
    """Each attempt must ask the runtime for its OWN session name.

    The staging regression: the Kubernetes Job name is derived from the
    execution id alone, so attempt 2 asked for the name attempt 1 still owned
    (its Job lingers for AGENT_JOB_TTL_SECONDS) and died with
    ``Failed to start agent Job: (409) Conflict``. The retry meant to rescue a
    transient provider failure became the cause of the failure — and its 409
    message overwrote the real one.
    """

    @staticmethod
    def _recording_executor(suffixes: list):
        executor = AsyncMock()

        async def start(context):
            suffixes.append(context.get(AGENT_SESSION_SUFFIX_KEY))
            return f"session-{len(suffixes)}"

        executor.start = AsyncMock(side_effect=start)
        executor.cleanup = AsyncMock()
        return executor

    async def test_retry_attempt_requests_a_distinct_session_name(
        self, db_session, test_flow, event_data, mock_nats_client
    ):
        suffixes: list = []
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

        with (
            patch(
                "preloop.services.flow_orchestrator.create_executor_for_execution",
                return_value=self._recording_executor(suffixes),
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

        assert len(suffixes) == 2
        # Attempt 1 keeps the historic unsuffixed name so an in-flight run
        # started before this change is still addressable by its stored
        # session reference; every later attempt gets its own.
        assert suffixes[0] is None
        assert suffixes[1] == "a2"

        execution_id = str(orchestrator.execution_log.id)
        names = [
            kubernetes_job_name(execution_id, session_suffix=suffix)
            for suffix in suffixes
        ]
        assert len(set(names)) == len(names), (
            f"attempts must not collide on a Kubernetes Job name; got {names}"
        )


@pytest.mark.asyncio
class TestFailureCategoryRecorded:
    """Every terminal failure carries a category you can group by."""

    @staticmethod
    def _executor():
        executor = AsyncMock()
        executor.start = AsyncMock(return_value="session-under-test")
        executor.cleanup = AsyncMock()
        return executor

    async def _run_with_result(
        self, db_session, test_flow, event_data, mock_nats_client, agent_result: dict
    ):
        async def monitor(session_reference, agent_executor):
            return agent_result

        with (
            patch(
                "preloop.services.flow_orchestrator.create_executor_for_execution",
                return_value=self._executor(),
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
        return orchestrator

    async def test_transient_upstream_failure_is_categorised(
        self, db_session, test_flow, event_data, mock_nats_client
    ):
        orchestrator = await self._run_with_result(
            db_session,
            test_flow,
            event_data,
            mock_nats_client,
            {
                "status": "FAILED",
                "error_message": "Upstream model provider timed out (HTTP 504).",
                "exit_code": 0,  # exit 0 => not retried, terminal on attempt 1
                "actions_taken": [],
                "mcp_usage_logs": [],
            },
        )
        assert orchestrator.execution_log.status == "FAILED"
        assert orchestrator.execution_log.failure_category == "model_transient"

    async def test_auth_failure_is_categorised_apart_from_transient(
        self, db_session, test_flow, event_data, mock_nats_client
    ):
        orchestrator = await self._run_with_result(
            db_session,
            test_flow,
            event_data,
            mock_nats_client,
            {
                "status": "FAILED",
                "error_message": "Upstream model provider rejected our credentials "
                "(HTTP 401).",
                "exit_code": 1,
                "actions_taken": [],
                "mcp_usage_logs": [],
            },
        )
        assert orchestrator.execution_log.failure_category == "model_auth"

    async def test_successful_run_has_no_category(
        self, db_session, test_flow, event_data, mock_nats_client
    ):
        orchestrator = await self._run_with_result(
            db_session,
            test_flow,
            event_data,
            mock_nats_client,
            {
                "status": "SUCCEEDED",
                "output_summary": "ok",
                "error_message": None,
                "exit_code": 0,
                "actions_taken": [],
                "mcp_usage_logs": [],
            },
        )
        assert orchestrator.execution_log.status == "SUCCEEDED"
        assert orchestrator.execution_log.failure_category is None

    async def test_runner_start_failure_is_categorised_as_runner(
        self, db_session, test_flow, event_data, mock_nats_client
    ):
        """A run killed by an unresolvable Job conflict must say so."""
        executor = AsyncMock()
        executor.start = AsyncMock(
            side_effect=AgentStartError(
                "Failed to start agent Job: name agent-x is already used by "
                "execution other",
                category="runner_conflict",
            )
        )
        executor.cleanup = AsyncMock()

        with (
            patch(
                "preloop.services.flow_orchestrator.create_executor_for_execution",
                return_value=executor,
            ),
            patch("preloop.services.flow_orchestrator.asyncio.sleep", AsyncMock()),
        ):
            orchestrator = _build_orchestrator(
                db_session, test_flow, event_data, mock_nats_client
            )
            await orchestrator.run()

        assert orchestrator.execution_log.status == "FAILED"
        assert orchestrator.execution_log.failure_category == "runner_conflict"
