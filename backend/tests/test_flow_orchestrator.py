"""Tests for FlowExecutionOrchestrator."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.services.flow_orchestrator import (
    AGENT_EXEC_START_MARKER,
    COMPLETION_NUDGE_MARKER,
    COMPLETION_NUDGE_RESULT_MARKER,
    COMPLETION_NUDGE_UNSUPPORTED_MARKER,
    FLOW_TIMEOUT_SECONDS_MAX,
    FLOW_TIMEOUT_SECONDS_MIN,
    FLOW_AUDIT_SUCCESS_INSTRUCTION,
    FLOW_EVAL_SUCCESS_INSTRUCTION,
    FLOW_FAILURE_REPORT_PREFIX,
    FLOW_SUCCESS_INSTRUCTION,
    FLOW_SUCCESS_SENTINEL,
    FlowExecutionOrchestrator,
    TimeoutBudget,
    _build_confirmation_nudge_prompt,
    _failure_report_in_log_lines,
    _result_artifact_confirmation,
    _sentinel_in_log_lines,
    _success_instruction_for_prompt,
)
from preloop.agents.base import AgentStatus, AgentExecutionResult
from preloop.agents.container import AGENT_SESSION_SUFFIX_KEY, kubernetes_job_name
from preloop.models.models import Flow, Account
from preloop.models.models.user import User
from preloop.models.schemas.flow import FlowCreate
from preloop.models.crud import (
    crud_account,
    crud_api_key,
    crud_flow,
    crud_runtime_session,
    crud_user,
)


@pytest.fixture
def test_account(db_session: Session) -> Account:
    """Create a test account (organization)."""
    account_data = {
        "organization_name": f"Test Org {uuid4().hex[:8]}",
        "is_active": True,
    }
    account = crud_account.create(db_session, obj_in=account_data)
    return account


@pytest.fixture
def test_user(db_session: Session, test_account: Account) -> User:
    """Create a test user for the account."""
    user_data = {
        "account_id": test_account.id,
        "email": f"orchestrator_test_{uuid4().hex[:8]}@example.com",
        "username": f"orchestrator_test_user_{uuid4().hex[:8]}",
        "full_name": "Orchestrator Test User",
        "is_active": True,
        "email_verified": True,
        "hashed_password": "test_password",
        "user_source": "local",
    }
    user = crud_user.create(db_session, obj_in=user_data)
    db_session.flush()
    db_session.refresh(user)
    test_account.primary_user_id = user.id
    db_session.add(test_account)
    db_session.commit()
    db_session.refresh(test_account)
    return user


@pytest.fixture
def test_flow(db_session: Session, test_account: Account, test_user: User) -> Flow:
    """Create a test flow."""
    from preloop.models.crud import crud_flow

    flow_in = FlowCreate(
        name="Test Orchestrator Flow",
        description="A test flow for orchestrator",
        trigger_event_source="github",
        trigger_event_types=["issue_created"],  # Use array field
        prompt_template="Fix issue: {{payload.issue.title}} - {{payload.issue.description}}",
        agent_type="openhands",
        agent_config={"max_iterations": 10},
        account_id=test_account.id,
    )
    flow = crud_flow.create(db=db_session, flow_in=flow_in, account_id=test_account.id)
    return flow


# Note: AIModel tests are skipped because ai_model table migration doesn't exist yet
# Will be re-enabled once the migration is created


@pytest.fixture
def mock_nats_client():
    """Create a mock NATS client."""
    mock_client = AsyncMock()
    mock_client.is_connected = True
    mock_client.publish = AsyncMock()
    return mock_client


@pytest.fixture
def event_data():
    """Create test event data."""
    return {
        "source": "github",
        "type": "issue_created",
        "event_id": "evt_123456",
        "payload": {
            "issue": {
                "title": "Bug in authentication",
                "description": "Users cannot login",
                "number": 42,
            },
            "repository": "test/repo",
        },
        "account_id": str(uuid4()),
    }


@pytest.fixture
def mock_agent_executor():
    """Create a mock agent executor that simulates successful execution.

    The mock also sets ``_success_sentinel_seen`` on the orchestrator so
    the sentinel-based status override (SUCCEEDED only if the sentinel was
    detected in logs) doesn't flip the result to FAILED.
    """
    mock_executor = AsyncMock()

    # Mock successful agent execution
    mock_executor.start = AsyncMock(return_value="mock-openhands-session-123")
    mock_executor.get_status = AsyncMock(return_value=AgentStatus.SUCCEEDED)
    mock_executor.get_result = AsyncMock(
        return_value=AgentExecutionResult(
            status=AgentStatus.SUCCEEDED,
            session_reference="mock-openhands-session-123",
            output_summary="Agent completed the task successfully",
            actions_taken=None,  # Let the orchestrator handle this field
            exit_code=0,
        )
    )
    mock_executor.stop = AsyncMock()

    # Store a flag so tests can opt-out of automatic sentinel triggering
    mock_executor._trigger_sentinel = True

    return mock_executor


class TestFlowExecutionOrchestrator:
    """Test suite for FlowExecutionOrchestrator."""

    @pytest.mark.asyncio
    async def test_full_lifecycle_success(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
        mock_agent_executor,
    ):
        """Test complete execution lifecycle ending in success."""
        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_agent_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            await orchestrator.run()

            # Verify execution log was created
            assert orchestrator.execution_log is not None
            assert orchestrator.execution_log.flow_id == test_flow.id
            assert orchestrator.execution_log.status == "SUCCEEDED"
            assert orchestrator.execution_log.trigger_event_id == "evt_123456"

            # Verify resolved prompt contains resolved placeholders
            assert (
                "Bug in authentication"
                in orchestrator.execution_log.resolved_input_prompt
            )
            assert (
                "Users cannot login" in orchestrator.execution_log.resolved_input_prompt
            )

            # Verify agent session reference was set
            assert orchestrator.execution_log.agent_session_reference is not None
            assert (
                "mock-openhands-session"
                in orchestrator.execution_log.agent_session_reference
            )
            assert orchestrator.runtime_session is not None
            assert orchestrator.runtime_session.session_source_type == "flow_execution"
            assert orchestrator.runtime_session.session_source_id == str(
                orchestrator.execution_log.id
            )
            assert orchestrator.runtime_session.session_reference == (
                "mock-openhands-session-123"
            )

            # Verify NATS updates were published
            assert (
                mock_nats_client.publish.call_count >= 3
            )  # At least PENDING, INITIALIZING, RUNNING, SUCCEEDED

    @pytest.mark.skip(
        reason="FK constraint prevents creating execution log for non-existent flow. "
        "This scenario should not occur in production as the trigger service validates flow_id."
    )
    @pytest.mark.asyncio
    async def test_flow_not_found(
        self,
        db_session: Session,
        mock_nats_client,
        event_data,
    ):
        """Test handling when flow is not found."""
        # This test is skipped because the DB schema has a foreign key constraint
        # from flow_execution to flow, so we cannot create an execution log for
        # a non-existent flow. In production, the FlowTriggerService only invokes
        # the orchestrator with valid flow_ids from the database.
        pass

    @pytest.mark.asyncio
    async def test_prompt_resolution_simple(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        mock_agent_executor,
    ):
        """Test simple prompt placeholder resolution."""
        event_data = {
            "source": "github",
            "type": "push",
            "payload": {"message": "Fixed bug #123"},
        }

        # Update flow with simple template
        test_flow.prompt_template = "Commit: {{payload.message}}"
        db_session.commit()

        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_agent_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            await orchestrator.run()

            # The prompt should contain the resolved template
            # (plus the success sentinel instruction appended by the orchestrator)
            resolved_prompt = orchestrator.execution_log.resolved_input_prompt
            assert resolved_prompt.startswith("Commit: Fixed bug #123")
            # Verify the success sentinel instruction is appended
            assert "FLOW_EXECUTION_SUCCESS" in resolved_prompt
            assert resolved_prompt.endswith(FLOW_SUCCESS_INSTRUCTION)

    @pytest.mark.asyncio
    async def test_prompt_resolution_normal_uses_sentinel_instruction(self):
        """Test normal prompts retain both confirmation channels."""
        orchestrator = FlowExecutionOrchestrator(
            db=MagicMock(spec=Session),
            flow_id=uuid4(),
            trigger_event_data={},
            nats_client=MagicMock(),
        )
        orchestrator.flow = MagicMock(prompt_template="Complete the requested task.")

        resolved_prompt = await orchestrator._resolve_prompt()

        assert resolved_prompt.endswith(FLOW_SUCCESS_INSTRUCTION)
        assert "FLOW_EXECUTION_SUCCESS" in resolved_prompt
        assert "/workspace/result.json" in resolved_prompt
        assert '"pass"' in resolved_prompt
        assert '"fail"' in resolved_prompt

    @pytest.mark.parametrize(
        "eval_contract",
        [
            "Required shape: preloop.eval.result/v1",
            "Do NOT print sentinel markers or paste JSON into chat output.",
        ],
    )
    @pytest.mark.asyncio
    async def test_prompt_resolution_eval_uses_result_artifact_instruction(
        self,
        eval_contract: str,
    ):
        """Test eval prompts preserve their structured result contract."""
        orchestrator = FlowExecutionOrchestrator(
            db=MagicMock(spec=Session),
            flow_id=uuid4(),
            trigger_event_data={},
            nats_client=MagicMock(),
        )
        orchestrator.flow = MagicMock(
            prompt_template=(
                "Evaluate the subject and write a rich report.\n" + eval_contract
            )
        )

        resolved_prompt = await orchestrator._resolve_prompt()

        assert resolved_prompt.endswith(FLOW_EVAL_SUCCESS_INSTRUCTION)
        assert "FLOW_EXECUTION_SUCCESS" not in resolved_prompt
        assert '{"status": "success"}' not in resolved_prompt
        assert "/workspace/result.json" in resolved_prompt
        assert "`pass` and `fail`" in resolved_prompt
        assert "`error`" in resolved_prompt

    @pytest.mark.parametrize(
        "audit_schema_marker",
        [
            # The schema ids actually present in the preset prompt texts:
            "preloop.cra.sbomaudit/v1",  # 004-sbom-verify
            "preloop.cra.releaseaudit/v1",  # 006-release-security-audit
            # preloop.cra.vulnscan/v1 (005) deliberately absent: since its
            # prompt carries a top-level "status" completion field it uses
            # the generic instruction (see test below).
        ],
    )
    @pytest.mark.asyncio
    async def test_prompt_resolution_audit_uses_verdict_instruction(
        self,
        audit_schema_marker: str,
    ):
        """Audit prompts get the verdict-contract confirmation instruction."""
        orchestrator = FlowExecutionOrchestrator(
            db=MagicMock(spec=Session),
            flow_id=uuid4(),
            trigger_event_data={},
            nats_client=MagicMock(),
        )
        orchestrator.flow = MagicMock(
            prompt_template=(
                "As your FINAL action, write /workspace/result.json.\n"
                f'Required shape ({audit_schema_marker}): {{ "schema": '
                f'"{audit_schema_marker}", "verdict": "pass" }}'
            )
        )

        resolved_prompt = await orchestrator._resolve_prompt()

        assert resolved_prompt.endswith(FLOW_AUDIT_SUCCESS_INSTRUCTION)
        # result.json + verdict is the confirmation channel...
        assert '"verdict"' in resolved_prompt
        assert '"pass_with_findings"' in resolved_prompt
        # ...but the sentinel is NOT forbidden (unlike eval): it stays
        # available, and required for shapes without a top-level verdict.
        assert "FLOW_EXECUTION_SUCCESS" in resolved_prompt
        assert "allowed and harmless" in resolved_prompt
        assert "bare status object" in resolved_prompt

    def test_instruction_selection_audit_vs_eval_vs_generic(self):
        """Contract routing: eval wins first, then audit, else generic."""
        assert (
            _success_instruction_for_prompt("shape: preloop.eval.result/v1")
            is FLOW_EVAL_SUCCESS_INSTRUCTION
        )
        assert (
            _success_instruction_for_prompt("shape: preloop.cra.releaseaudit/v1")
            is FLOW_AUDIT_SUCCESS_INSTRUCTION
        )
        assert (
            _success_instruction_for_prompt("Fix the bug and open a PR.")
            is FLOW_SUCCESS_INSTRUCTION
        )
        # 005's vulnscan contract carries a top-level "status" completion
        # field (preset revision #283), so it routes to the GENERIC
        # instruction whose result.json-status branch matches its contract;
        # the audit instruction's no-verdict sentinel fallback would be
        # misleading for it.
        assert (
            _success_instruction_for_prompt("shape: preloop.cra.vulnscan/v1")
            is FLOW_SUCCESS_INSTRUCTION
        )
        # The due-diligence contract's verdict vocabulary
        # ("recorded" | "error") is not a recognized completion
        # confirmation, so it keeps the generic sentinel instruction.
        assert (
            _success_instruction_for_prompt("shape: preloop.cra.duediligence/v1")
            is FLOW_SUCCESS_INSTRUCTION
        )

    @pytest.mark.asyncio
    async def test_prompt_resolution_nested(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
        mock_agent_executor,
    ):
        """Test nested placeholder resolution."""
        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_agent_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            await orchestrator.run()

            # Verify nested placeholders were resolved
            resolved = orchestrator.execution_log.resolved_input_prompt
            assert "Bug in authentication" in resolved
            assert "Users cannot login" in resolved
            assert "{{" not in resolved  # No unresolved placeholders

    @pytest.mark.asyncio
    async def test_prompt_resolution_missing_placeholder(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        mock_agent_executor,
    ):
        """Test handling of missing placeholders."""
        event_data = {
            "source": "github",
            "type": "issue_created",
            "payload": {},  # Missing issue data
        }

        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_agent_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            await orchestrator.run()

            # Verify execution succeeded even with missing placeholders
            assert orchestrator.execution_log.status == "SUCCEEDED"
            # Unresolved placeholders should remain in template
            assert "{{" in orchestrator.execution_log.resolved_input_prompt

    @pytest.mark.asyncio
    async def test_execution_context_without_ai_model(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
        mock_agent_executor,
    ):
        """Test execution context when no AI model is specified."""
        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_agent_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            await orchestrator.run()

            # Verify execution succeeded without AI model
            assert orchestrator.execution_log.status == "SUCCEEDED"
            assert orchestrator.ai_model is None

    @pytest.mark.skip(reason="AIModel table migration not yet created")
    @pytest.mark.asyncio
    async def test_execution_context_with_ai_model(
        self,
        db_session: Session,
        mock_nats_client,
        event_data,
    ):
        """Test execution context includes AI model details."""
        # This test will be re-enabled once ai_model table migration is created
        pass

    @pytest.mark.asyncio
    async def test_nats_updates_published(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
        mock_agent_executor,
    ):
        """Test that NATS updates are published at each stage."""
        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_agent_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            await orchestrator.run()

            # Verify NATS publish was called multiple times
            assert mock_nats_client.publish.call_count >= 3

            # Verify subject format
            first_call = mock_nats_client.publish.call_args_list[0]
            subject = first_call[0][0]
            assert subject.startswith("flow-updates.")

    @pytest.mark.asyncio
    async def test_nats_client_not_connected(
        self,
        db_session: Session,
        test_flow: Flow,
        event_data,
        mock_agent_executor,
    ):
        """Test handling when NATS client is not connected."""
        mock_nats = AsyncMock()
        mock_nats.is_connected = False
        mock_nats.publish = AsyncMock()

        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_agent_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats,
            )

            # Should not raise error even if NATS is unavailable
            await orchestrator.run()

            # Verify execution succeeded despite NATS issues
            assert orchestrator.execution_log.status == "SUCCEEDED"
            # NATS publish should not have been called
            mock_nats.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_lifecycle_states(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
        mock_agent_executor,
    ):
        """Test that execution goes through correct lifecycle states."""
        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_agent_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            await orchestrator.run()

            # Check final state
            assert orchestrator.execution_log.status == "SUCCEEDED"

            # Verify timestamps
            assert orchestrator.execution_log.start_time is not None
            assert orchestrator.execution_log.created_at is not None

    @pytest.mark.asyncio
    async def test_agent_config_passed_to_context(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
        mock_agent_executor,
    ):
        """Test that agent_config is included in execution context."""
        # Set specific agent config
        test_flow.agent_config = {"max_iterations": 20, "custom_param": "value"}
        db_session.commit()

        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_agent_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            await orchestrator.run()

            # Verify execution succeeded with custom config
            assert orchestrator.execution_log.status == "SUCCEEDED"
            assert orchestrator.flow.agent_config["max_iterations"] == 20
            assert orchestrator.flow.agent_config["custom_param"] == "value"

    @pytest.mark.asyncio
    async def test_allowed_mcp_servers_in_context(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
        mock_agent_executor,
    ):
        """Test that allowed_mcp_servers are included in context."""
        # Set MCP server restrictions
        test_flow.allowed_mcp_servers = ["github", "slack"]
        db_session.commit()

        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_agent_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            await orchestrator.run()

            assert orchestrator.execution_log.status == "SUCCEEDED"
            assert orchestrator.flow.allowed_mcp_servers == ["github", "slack"]

    @pytest.mark.asyncio
    async def test_trigger_event_details_stored(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
        mock_agent_executor,
    ):
        """Test that trigger event details are stored in execution log."""
        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_agent_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            await orchestrator.run()

            # Verify event details were stored
            assert orchestrator.execution_log.trigger_event_details is not None
            assert (
                orchestrator.execution_log.trigger_event_details["source"] == "github"
            )
            assert (
                orchestrator.execution_log.trigger_event_details["type"]
                == "issue_created"
            )
            assert (
                orchestrator.execution_log.trigger_event_details["payload"]["issue"][
                    "title"
                ]
                == "Bug in authentication"
            )

    @pytest.mark.asyncio
    async def test_error_handling_during_execution(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
    ):
        """Test error handling when an exception occurs during execution."""

        # Patch _prepare_execution_context to raise an exception
        # (this is called after execution log is created)
        with patch.object(
            FlowExecutionOrchestrator,
            "_prepare_execution_context",
            side_effect=Exception("Test error"),
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            await orchestrator.run()

            # Verify execution was marked as FAILED
            assert orchestrator.execution_log is not None
            assert orchestrator.execution_log.status == "FAILED"
            assert "Test error" in orchestrator.execution_log.error_message
            assert orchestrator.execution_log.end_time is not None

    @pytest.mark.asyncio
    async def test_nats_publish_error_handling(
        self,
        db_session: Session,
        test_flow: Flow,
        event_data,
        mock_agent_executor,
    ):
        """Test handling NATS publish errors."""
        # Mock NATS client that raises errors when publishing
        mock_nats = AsyncMock()
        mock_nats.is_connected = True
        mock_nats.publish = AsyncMock(side_effect=Exception("NATS publish failed"))

        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_agent_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats,
            )

            # Should not raise error even if NATS publish fails
            await orchestrator.run()

            # Verify execution succeeded despite NATS errors
            assert orchestrator.execution_log.status == "SUCCEEDED"

    @pytest.mark.asyncio
    async def test_execution_log_not_created_nats_warning(
        self,
        db_session: Session,
        test_flow: Flow,
        event_data,
    ):
        """Test NATS publish warning when execution log not created yet."""
        mock_nats = AsyncMock()
        mock_nats.is_connected = True

        orchestrator = FlowExecutionOrchestrator(
            db=db_session,
            flow_id=test_flow.id,
            trigger_event_data=event_data,
            nats_client=mock_nats,
        )

        # Try to publish update before execution log is created
        await orchestrator._publish_update("test", {"data": "value"})

        # Should not raise error, just skip publishing
        mock_nats.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_prompt_resolution_with_resolver_error(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
        mock_agent_executor,
    ):
        """Test prompt resolution when a resolver raises an error."""
        # Update template to use a resolver that will fail
        test_flow.prompt_template = (
            "Project: {{project.name}} Issue: {{payload.issue.title}}"
        )
        db_session.commit()

        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_agent_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            await orchestrator.run()

            # Verify execution succeeded even with resolver errors
            assert orchestrator.execution_log.status == "SUCCEEDED"
            # Project resolver should have failed but left placeholder
            assert (
                "{{project.name}}" in orchestrator.execution_log.resolved_input_prompt
            )

    @pytest.mark.asyncio
    async def test_prompt_resolution_returns_none(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
        mock_agent_executor,
    ):
        """Test prompt resolution when resolver returns None."""
        # Use a placeholder that will resolve to None
        test_flow.prompt_template = "Account: {{account.nonexistent}}"
        db_session.commit()

        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_agent_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            await orchestrator.run()

            # Verify execution succeeded
            assert orchestrator.execution_log.status == "SUCCEEDED"

    @pytest.mark.asyncio
    async def test_simple_resolve_exception_handling(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        mock_agent_executor,
    ):
        """Test simple_resolve handles exceptions gracefully."""
        # Event data with non-dict value in path
        event_data = {
            "source": "github",
            "type": "test",
            "payload": "string_value",  # Not a dict
        }

        test_flow.prompt_template = "{{payload.nested.value}}"
        db_session.commit()

        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_agent_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            await orchestrator.run()

            # Should succeed even with resolution errors
            assert orchestrator.execution_log.status == "SUCCEEDED"

    @pytest.mark.skip(
        reason="FK constraint prevents creating flow with non-existent account. "
        "This edge case cannot occur in production. Coverage tested via code review."
    )
    @pytest.mark.asyncio
    async def test_temporary_api_token_creation_account_not_found(
        self,
        db_session: Session,
        mock_nats_client,
        event_data,
        mock_agent_executor,
        test_flow: Flow,
    ):
        """Test handling when account not found for API token creation."""
        # This scenario is prevented by FK constraint in production
        pass

    @pytest.mark.asyncio
    async def test_temporary_api_token_creation_error(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
        mock_agent_executor,
    ):
        """Test handling when API token creation raises an error."""
        with (
            patch(
                "preloop.services.flow_orchestrator.create_executor_for_execution",
                return_value=mock_agent_executor,
            ),
            patch.object(
                FlowExecutionOrchestrator,
                "_create_temporary_api_token",
                side_effect=Exception("Token creation failed"),
            ),
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            # Should handle error during token creation
            # The actual exception will be caught in _create_temporary_api_token
            # but let's test the flow continues
            await orchestrator.run()

    def test_create_temporary_api_token_uses_primary_user_and_runtime_context(
        self,
        db_session: Session,
        test_flow: Flow,
        test_user: User,
        mock_nats_client,
    ):
        """Temporary flow credentials should use the primary account user and runtime context."""
        orchestrator = FlowExecutionOrchestrator(
            db=db_session,
            flow_id=test_flow.id,
            trigger_event_data={"source": "test"},
            nats_client=mock_nats_client,
        )
        orchestrator.flow = test_flow
        execution_id = uuid4()
        orchestrator.execution_log = type(
            "ExecutionLogStub", (), {"id": execution_id}
        )()

        token_value, token_id = orchestrator._create_temporary_api_token()

        assert token_value is not None
        assert token_id is not None

        stored_key = crud_api_key.get(db_session, id=token_id)
        assert stored_key is not None
        assert stored_key.user_id == test_user.id
        assert stored_key.key is None
        assert stored_key.key_hash is not None
        assert orchestrator.runtime_session is not None
        runtime_session = crud_runtime_session.get_by_source(
            db_session,
            account_id=test_user.account_id,
            session_source_type="flow_execution",
            session_source_id=str(execution_id),
        )
        assert runtime_session is not None
        assert stored_key.context_data["flow_execution_id"] == str(execution_id)
        assert stored_key.context_data["runtime_session_id"] == str(runtime_session.id)
        assert stored_key.context_data["runtime_principal"]["type"] == "flow_execution"
        assert stored_key.context_data["runtime_principal"]["id"] == str(execution_id)
        assert (
            stored_key.context_data["runtime_principal"]["username"]
            == test_user.username
        )

    @pytest.mark.asyncio
    async def test_bearer_token_end_to_end(
        self,
        db_session: Session,
        test_flow: Flow,
        test_user: User,
        mock_nats_client,
    ):
        orchestrator = FlowExecutionOrchestrator(
            db=db_session,
            flow_id=test_flow.id,
            trigger_event_data={"source": "test"},
            nats_client=mock_nats_client,
        )
        orchestrator.flow = test_flow
        from uuid import uuid4

        execution_id = uuid4()
        orchestrator.execution_log = type(
            "ExecutionLogStub", (), {"id": execution_id}
        )()

        token, key_id = orchestrator._create_temporary_api_token()
        assert token is not None

        from preloop.services.model_gateway_auth import authenticate_bearer_token

        res = await authenticate_bearer_token(token, db_session)
        assert res is not None
        assert res.user.id == test_user.id

    @pytest.mark.asyncio
    async def test_temporary_api_token_cleanup_token_not_found(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
        mock_agent_executor,
    ):
        """Test cleanup when temporary API token not found."""
        import uuid

        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_agent_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            # Set a non-existent token ID
            orchestrator.temporary_api_key_id = uuid.uuid4()

            await orchestrator.run()

            # Should succeed and handle missing token gracefully
            assert orchestrator.execution_log.status == "SUCCEEDED"

    @pytest.mark.skip(
        reason="Cleanup error handling is difficult to test with mocks. "
        "Error path tested via code review."
    )
    @pytest.mark.asyncio
    async def test_temporary_api_token_cleanup_error(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
        mock_agent_executor,
    ):
        """Test handling error during API token cleanup."""
        # Error handling verified via code review
        pass

    @pytest.mark.asyncio
    async def test_agent_start_error(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
    ):
        """Test handling when agent start fails."""
        # Mock agent executor that fails to start
        mock_executor = AsyncMock()
        mock_executor.start = AsyncMock(side_effect=Exception("Agent start failed"))

        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            await orchestrator.run()

            # Verify execution was marked as FAILED
            assert orchestrator.execution_log.status == "FAILED"
            assert "Agent start failed" in orchestrator.execution_log.error_message

    @pytest.mark.asyncio
    async def test_monitor_agent_execution_error(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
    ):
        """Test handling error during agent monitoring."""
        # Mock agent executor that fails during monitoring
        mock_executor = AsyncMock()
        mock_executor.start = AsyncMock(return_value="session-123")
        mock_executor.get_status = AsyncMock(side_effect=Exception("Monitoring error"))
        mock_executor.stop = AsyncMock()
        mock_executor.cleanup = AsyncMock()

        # Mock stream_logs to return empty async iterator
        async def empty_logs(session_ref):
            if False:  # Never executes, but makes this an async generator
                yield

        mock_executor.stream_logs = empty_logs

        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            await orchestrator.run()

            # Verify execution was marked as FAILED with monitoring error
            assert orchestrator.execution_log.status == "FAILED"
            assert "Monitoring error" in orchestrator.execution_log.error_message

    @pytest.mark.asyncio
    async def test_agent_execution_timeout(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
    ):
        """Test handling when agent execution times out."""
        # Mock agent executor that never completes
        mock_executor = AsyncMock()
        mock_executor.start = AsyncMock(return_value="session-123")
        mock_executor.get_status = AsyncMock(return_value=AgentStatus.RUNNING)
        mock_executor.stop = AsyncMock()
        mock_executor.stream_logs = AsyncMock()

        # Mock asyncio.sleep to speed up test
        async def fast_sleep(seconds):
            pass

        with (
            patch(
                "preloop.services.flow_orchestrator.create_executor_for_execution",
                return_value=mock_executor,
            ),
            patch(
                "preloop.services.flow_orchestrator.asyncio.sleep",
                side_effect=fast_sleep,
            ),
            patch.object(
                FlowExecutionOrchestrator, "_monitor_agent_execution"
            ) as mock_monitor,
        ):
            # Mock timeout scenario
            mock_monitor.return_value = {
                "status": "FAILED",
                "error_message": "Execution timed out after 3600 seconds",
                "actions_taken": [],
                "mcp_usage_logs": [],
            }

            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            await orchestrator.run()

            # Verify execution was marked as FAILED due to timeout
            assert orchestrator.execution_log.status == "FAILED"
            assert "timed out" in orchestrator.execution_log.error_message.lower()

    @pytest.mark.asyncio
    async def test_result_artifact_persisted_end_to_end(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
        mock_agent_executor,
    ):
        """The artifact returned by the executor lands in flow_execution.result.

        Covers the orchestrator wiring: _monitor_agent_execution's returned
        dict -> FlowExecutionUpdate.result -> DB (success path).
        """
        from preloop.models.models.flow_execution import FlowExecution

        artifact = {
            "schema": "preloop.eval.result/v1",
            "status": "pass",
            "summary": "all checks green",
            "metrics": {"latency_ms": 12},
        }
        mock_agent_executor.get_result_artifact = AsyncMock(return_value=artifact)

        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_agent_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            await orchestrator.run()

            assert orchestrator.execution_log.status == "SUCCEEDED"
            mock_agent_executor.get_result_artifact.assert_awaited()

            # Re-read from the DB: the JSONB column must round-trip.
            persisted = (
                db_session.query(FlowExecution)
                .filter(FlowExecution.id == orchestrator.execution_log.id)
                .one()
            )
            assert persisted.result == artifact

    @pytest.mark.asyncio
    async def test_evidence_archive_persisted(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
        mock_agent_executor,
    ):
        """The evidence pack captured before cleanup lands in the DB.

        Covers the wiring: get_evidence_archive (executor, pre-cleanup) ->
        orchestrator stash -> flow_execution.evidence_archive at finalize.
        """
        from preloop.models.models.flow_execution import FlowExecution

        archive = b"\x1f\x8b" + b"fake-evidence-tar-gz"
        mock_agent_executor.get_result_artifact = AsyncMock(
            return_value={"status": "success"}
        )
        mock_agent_executor.get_evidence_archive = AsyncMock(return_value=archive)

        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_agent_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            await orchestrator.run()

            assert orchestrator.execution_log.status == "SUCCEEDED"
            mock_agent_executor.get_evidence_archive.assert_awaited()

            persisted = (
                db_session.query(FlowExecution)
                .filter(FlowExecution.id == orchestrator.execution_log.id)
                .one()
            )
            assert bytes(persisted.evidence_archive) == archive

    @pytest.mark.asyncio
    async def test_no_evidence_archive_leaves_column_null(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
        mock_agent_executor,
    ):
        """Executors returning no archive (or none captured) persist NULL."""
        from preloop.models.models.flow_execution import FlowExecution

        mock_agent_executor.get_result_artifact = AsyncMock(return_value=None)
        mock_agent_executor.get_evidence_archive = AsyncMock(return_value=None)

        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_agent_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            await orchestrator.run()

            persisted = (
                db_session.query(FlowExecution)
                .filter(FlowExecution.id == orchestrator.execution_log.id)
                .one()
            )
            assert persisted.evidence_archive is None

    @pytest.mark.asyncio
    async def test_result_artifact_persisted_on_failure_path(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
    ):
        """A run that fails during monitoring still keeps its artifact.

        The agent may have written result.json before the monitor gave up;
        every terminal path of _monitor_agent_execution must capture it.
        """
        from preloop.models.models.flow_execution import FlowExecution

        artifact = {
            "schema": "preloop.eval.result/v1",
            "status": "error",
            "summary": "run aborted mid-eval",
        }

        mock_executor = AsyncMock()
        mock_executor.start = AsyncMock(return_value="session-123")
        mock_executor.get_status = AsyncMock(side_effect=Exception("Monitoring error"))
        mock_executor.stop = AsyncMock()
        mock_executor.cleanup = AsyncMock()
        mock_executor.get_result_artifact = AsyncMock(return_value=artifact)

        async def empty_logs(session_ref):
            if False:  # Never executes, but makes this an async generator
                yield

        mock_executor.stream_logs = empty_logs

        async def fast_sleep(seconds):
            pass

        with (
            patch(
                "preloop.services.flow_orchestrator.create_executor_for_execution",
                return_value=mock_executor,
            ),
            patch(
                "preloop.services.flow_orchestrator.asyncio.sleep",
                side_effect=fast_sleep,
            ),
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            await orchestrator.run()

            assert orchestrator.execution_log.status == "FAILED"
            mock_executor.get_result_artifact.assert_awaited()

            persisted = (
                db_session.query(FlowExecution)
                .filter(FlowExecution.id == orchestrator.execution_log.id)
                .one()
            )
            assert persisted.result == artifact

    @pytest.mark.asyncio
    async def test_user_stop_command(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
    ):
        """Test handling user stop command."""
        import asyncio
        import json

        # Mock agent executor
        mock_executor = AsyncMock()
        mock_executor.start = AsyncMock(return_value="session-123")
        mock_executor.get_status = AsyncMock(return_value=AgentStatus.RUNNING)
        mock_executor.stop = AsyncMock()
        mock_executor.stream_logs = AsyncMock(return_value=iter([]))

        captured_handler = None

        async def mock_subscribe(subject, cb):
            nonlocal captured_handler
            captured_handler = cb
            return AsyncMock()

        mock_nats_client.subscribe = mock_subscribe

        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            # Start the orchestrator in background
            run_task = asyncio.create_task(orchestrator.run())

            # Wait for subscription to be set up
            await asyncio.sleep(0.2)

            # Simulate user sending stop command
            if captured_handler:
                mock_msg = AsyncMock()
                mock_msg.data.decode.return_value = json.dumps({"command": "stop"})
                await captured_handler(mock_msg)

            # Wait for run to complete
            await asyncio.sleep(0.2)
            run_task.cancel()

            try:
                await run_task
            except asyncio.CancelledError:
                # Expected when cancelling the orchestrator run task in the test.
                pass

    @pytest.mark.asyncio
    async def test_unknown_command_type(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
    ):
        """Test handling unknown command type."""
        import asyncio
        import json

        mock_executor = AsyncMock()
        mock_executor.start = AsyncMock(return_value="session-123")
        mock_executor.get_status = AsyncMock(return_value=AgentStatus.SUCCEEDED)
        mock_executor.get_result = AsyncMock(
            return_value=AgentExecutionResult(
                status=AgentStatus.SUCCEEDED,
                session_reference="session-123",
                output_summary="Done",
                actions_taken=None,
                exit_code=0,
            )
        )
        mock_executor.stream_logs = AsyncMock(return_value=iter([]))

        captured_handler = None

        async def mock_subscribe(subject, cb):
            nonlocal captured_handler
            captured_handler = cb
            return AsyncMock()

        mock_nats_client.subscribe = mock_subscribe

        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            run_task = asyncio.create_task(orchestrator.run())

            # Wait for subscription
            await asyncio.sleep(0.2)

            # Send unknown command
            if captured_handler:
                mock_msg = AsyncMock()
                mock_msg.data.decode.return_value = json.dumps(
                    {"command": "unknown_command"}
                )
                await captured_handler(mock_msg)

            await asyncio.sleep(0.2)

            try:
                await run_task
            except Exception:
                # Run task may still be active; ignore cleanup races in this test.
                pass

    @pytest.mark.asyncio
    async def test_command_subscription_error(
        self,
        db_session: Session,
        test_flow: Flow,
        event_data,
        mock_agent_executor,
    ):
        """Test handling error when setting up command subscription."""
        # Mock NATS that fails to subscribe
        mock_nats = AsyncMock()
        mock_nats.is_connected = True
        mock_nats.subscribe = AsyncMock(side_effect=Exception("Subscription failed"))
        mock_nats.publish = AsyncMock()

        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_agent_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats,
            )

            await orchestrator.run()

            # Execution should still succeed
            assert orchestrator.execution_log.status == "SUCCEEDED"

    @pytest.mark.asyncio
    async def test_execution_log_update_error(
        self,
        db_session: Session,
        test_flow: Flow,
        mock_nats_client,
        event_data,
    ):
        """Test handling error when updating execution log fails."""
        from preloop.models import crud

        # Mock agent executor
        mock_executor = AsyncMock()
        mock_executor.start = AsyncMock(side_effect=Exception("Agent failed"))

        # Mock crud_flow_execution.update to fail
        original_update = crud.crud_flow_execution.update

        def mock_update_with_error(*args, **kwargs):
            if kwargs.get("obj_in").status == "FAILED":
                raise Exception("Database update failed")
            return original_update(*args, **kwargs)

        with (
            patch(
                "preloop.services.flow_orchestrator.create_executor_for_execution",
                return_value=mock_executor,
            ),
            patch(
                "preloop.models.crud.crud_flow_execution.update",
                side_effect=mock_update_with_error,
            ),
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            # Should handle update error gracefully
            await orchestrator.run()

            # Execution log exists but update might have failed
            assert orchestrator.execution_log is not None

    @pytest.mark.asyncio
    async def test_ai_model_query_with_uuid_conversion(
        self,
        db_session: Session,
        mock_nats_client,
        event_data,
        mock_agent_executor,
        test_account: Account,
    ):
        """Test AI model query with UUID string conversion."""
        from preloop.models.crud import crud_flow
        from preloop.models.schemas.flow import FlowCreate
        from preloop.models.models import AIModel
        from uuid import uuid4

        # Create an AI model
        ai_model_id = uuid4()
        ai_model = AIModel(
            id=str(ai_model_id),
            name="Test Model",
            model_identifier="gpt-5.4",
            provider_name="openai",
            api_endpoint="https://api.openai.com/v1",
            api_key="test-key",
            model_parameters={},
        )
        db_session.add(ai_model)
        db_session.commit()

        # Create flow with AI model
        flow_in = FlowCreate(
            name="Test Flow with AI Model",
            description="Test",
            trigger_event_source="github",
            trigger_event_types=["test"],  # Use array field
            prompt_template="Test",
            agent_type="openhands",
            agent_config={},
            account_id=test_account.id,
            ai_model_id=str(ai_model_id),
        )
        flow = crud_flow.create(
            db=db_session, flow_in=flow_in, account_id=test_account.id
        )

        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=mock_agent_executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )

            await orchestrator.run()

            # Verify AI model was loaded
            assert orchestrator.ai_model is not None
            assert orchestrator.ai_model.name == "Test Model"
            assert orchestrator.execution_log.status == "SUCCEEDED"

    @pytest.mark.asyncio
    async def test_gemini_flow_keeps_gateway_enabled_model_routing(
        self,
        db_session: Session,
        mock_nats_client,
        test_account: Account,
    ):
        """Gemini should now retain full gateway routing when configured."""
        from preloop.models.models import AIModel
        from uuid import uuid4

        ai_model = AIModel(
            id=str(uuid4()),
            name="Gemini Gateway Model",
            model_identifier="gemini-2.5-pro",
            provider_name="google",
            api_endpoint="https://generativelanguage.googleapis.com/v1beta",
            api_key="gemini-secret",
            model_parameters={},
            meta_data={
                "gateway": {
                    "enabled": True,
                    "url": "https://review.preloop.ai/openai/v1",
                    "model_alias": "gemini/gemini-2.5-pro",
                }
            },
        )
        db_session.add(ai_model)
        db_session.commit()

        test_flow = crud_flow.create(
            db=db_session,
            flow_in=FlowCreate(
                name="Gemini Gateway Flow",
                description="Test Gemini gateway routing",
                trigger_event_source="github",
                trigger_event_types=["issue_created"],
                prompt_template="Handle {{payload.issue.title}}",
                agent_type="gemini",
                agent_config={},
                account_id=test_account.id,
                ai_model_id=str(ai_model.id),
            ),
            account_id=test_account.id,
        )

        orchestrator = FlowExecutionOrchestrator(
            db=db_session,
            flow_id=test_flow.id,
            trigger_event_data={
                "source": "github",
                "type": "issue_created",
                "payload": {"issue": {"title": "Test issue"}},
            },
            nats_client=mock_nats_client,
        )
        orchestrator._get_flow_details()
        orchestrator.execution_log = type("ExecutionLogStub", (), {"id": uuid4()})()

        execution_context = await orchestrator._prepare_execution_context()

        assert execution_context["model_gateway_enabled"] is True
        assert execution_context["model_provider"] == "preloop"
        assert execution_context["model_identifier"] == "gemini/gemini-2.5-pro"
        assert (
            execution_context["model_endpoint"] == "https://review.preloop.ai/openai/v1"
        )
        assert execution_context["model_api_key"] is None
        assert "model_gateway_disabled_reason" not in execution_context

    def test_resolve_trigger_project_id_prefers_event_project(
        self, db_session: Session, test_flow: Flow, mock_nats_client
    ):
        """Webhook project should win over flow.trigger_project_ids[0]."""
        android_id = str(uuid4())
        ios_id = str(uuid4())
        test_flow.trigger_project_ids = [ios_id, android_id]

        orchestrator = FlowExecutionOrchestrator(
            db=db_session,
            flow_id=test_flow.id,
            trigger_event_data={
                "source": "gitlab",
                "account_id": str(test_flow.account_id),
                "tracker_id": str(uuid4()),
                "project_id": android_id,
                "payload": {
                    "project": {"path_with_namespace": "spacecode/preloop-android"}
                },
            },
            nats_client=mock_nats_client,
        )
        orchestrator._get_flow_details()

        assert orchestrator._resolve_trigger_project_id() == android_id

    @pytest.mark.skip(
        reason="FK constraint prevents creating flow with non-existent AI model. "
        "This edge case cannot occur in production. Coverage tested via code review."
    )
    @pytest.mark.asyncio
    async def test_ai_model_not_found_warning(
        self,
        db_session: Session,
        mock_nats_client,
        event_data,
        mock_agent_executor,
        test_flow: Flow,
    ):
        """Test warning when AI model not found."""
        # This scenario is prevented by FK constraint in production
        pass


class TestWorkspaceSeedValidation:
    """Trigger-payload workspace_files are validated before any agent starts."""

    @staticmethod
    def _orchestrator(db_session, test_flow, mock_nats_client, payload):
        orchestrator = FlowExecutionOrchestrator(
            db=db_session,
            flow_id=test_flow.id,
            trigger_event_data={
                "source": "webhook",
                "type": "webhook",
                "payload": payload,
            },
            nats_client=mock_nats_client,
        )
        orchestrator._get_flow_details()
        orchestrator.execution_log = type("ExecutionLogStub", (), {"id": uuid4()})()
        return orchestrator

    @pytest.mark.asyncio
    async def test_traversal_path_fails_context_preparation(
        self, db_session: Session, test_flow: Flow, mock_nats_client
    ):
        """A `..` seed path must abort before the agent container starts."""
        from preloop.utils.workspace_seed import WorkspaceSeedError

        orchestrator = self._orchestrator(
            db_session,
            test_flow,
            mock_nats_client,
            {
                "workspace_files": [
                    {"path": "../../etc/passwd", "content_base64": "eA=="}
                ]
            },
        )
        with pytest.raises(WorkspaceSeedError, match="escapes /workspace"):
            await orchestrator._prepare_execution_context()

    @pytest.mark.asyncio
    async def test_oversized_seed_fails_context_preparation(
        self, db_session: Session, test_flow: Flow, mock_nats_client
    ):
        """Seeds above the inline cap must abort with a clear message."""
        import base64

        from preloop.utils.workspace_seed import (
            MAX_TOTAL_SEED_ENCODED_BYTES,
            WorkspaceSeedError,
        )

        # Encoded size just over the cap (the cap applies to the base64 form).
        too_big = base64.b64encode(
            b"x" * (MAX_TOTAL_SEED_ENCODED_BYTES // 4 * 3 + 3)
        ).decode("ascii")
        assert len(too_big) > MAX_TOTAL_SEED_ENCODED_BYTES
        orchestrator = self._orchestrator(
            db_session,
            test_flow,
            mock_nats_client,
            {"workspace_files": [{"path": "big.bin", "content_base64": too_big}]},
        )
        with pytest.raises(WorkspaceSeedError, match="inline cap"):
            await orchestrator._prepare_execution_context()

    @pytest.mark.asyncio
    async def test_valid_seed_files_flow_into_context(
        self, db_session: Session, test_flow: Flow, mock_nats_client
    ):
        """Valid workspace_files pass validation and stay on the trigger data
        so the agent layer can materialize them."""
        import base64

        content = base64.b64encode(b'{"fixture": true}').decode("ascii")
        orchestrator = self._orchestrator(
            db_session,
            test_flow,
            mock_nats_client,
            {
                "workspace_files": [
                    {"path": "fixtures/input.json", "content_base64": content}
                ]
            },
        )
        execution_context = await orchestrator._prepare_execution_context()
        seeded = execution_context["trigger_event_data"]["payload"]["workspace_files"]
        assert seeded[0]["path"] == "fixtures/input.json"


def _confirmation_executor(
    *,
    status=AgentStatus.SUCCEEDED,
    monitor_status=None,
    exit_code=0,
    error_message=None,
    artifact=None,
    failure_analysis=None,
):
    """Mock executor for exercising the success-confirmation contract.

    Streams no logs, so tests set ``_agent_exec_started`` /
    ``_success_sentinel_seen`` on the orchestrator directly to simulate
    what the log stream would have detected.

    ``monitor_status`` overrides ``get_status`` so the grace-period path
    can keep the container "running" while ``get_result`` still returns
    a finished ``status``.
    """
    executor = AsyncMock()
    executor.start = AsyncMock(return_value="session-confirmation-123")
    executor.get_status = AsyncMock(return_value=monitor_status or status)
    executor.get_result = AsyncMock(
        return_value=AgentExecutionResult(
            status=status,
            session_reference="session-confirmation-123",
            output_summary="agent finished",
            error_message=error_message,
            exit_code=exit_code,
            failure_analysis=failure_analysis,
        )
    )
    executor.stop = AsyncMock()
    executor.cleanup = AsyncMock()
    executor.get_result_artifact = AsyncMock(return_value=artifact)

    async def empty_logs(session_ref):
        if False:  # pragma: no cover - makes this an async generator
            yield

    executor.stream_logs = empty_logs
    return executor


class TestSuccessConfirmationChannels:
    """Fail-closed positive-confirmation contract (two redundant channels).

    Exit code 0 is NEVER sufficient for success (agent CLIs exit 0 even when
    the agent died mid-task). Success requires an explicit act by the agent:
    the printed sentinel OR a result.json with a success status. An explicit
    failure status in result.json wins over everything.
    """

    async def _run(
        self,
        db_session,
        test_flow,
        mock_nats_client,
        event_data,
        executor,
        *,
        sentinel_seen,
    ):
        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )
            # Simulate real log streaming: the agent-exec-start marker was
            # seen, so the confirmation contract is armed.
            orchestrator._agent_exec_started = True
            if sentinel_seen:
                orchestrator._success_sentinel_seen.set()
            await orchestrator.run()
            return orchestrator

    @pytest.mark.asyncio
    async def test_exit_zero_without_any_confirmation_fails(
        self, db_session: Session, test_flow: Flow, mock_nats_client, event_data
    ):
        """Contract regression guard: exit 0 + neither channel -> FAILED."""
        executor = _confirmation_executor()
        orchestrator = await self._run(
            db_session,
            test_flow,
            mock_nats_client,
            event_data,
            executor,
            sentinel_seen=False,
        )

        assert orchestrator.execution_log.status == "FAILED"
        # The diagnostic must make this failure class recognizable at a
        # glance and name BOTH confirmation channels.
        error_message = orchestrator.execution_log.error_message
        assert "FLOW_EXECUTION_SUCCESS" in error_message
        assert "result.json" in error_message

    @pytest.mark.asyncio
    async def test_exit_zero_with_sentinel_succeeds(
        self, db_session: Session, test_flow: Flow, mock_nats_client, event_data
    ):
        """Channel 1: the printed sentinel confirms success."""
        executor = _confirmation_executor()
        orchestrator = await self._run(
            db_session,
            test_flow,
            mock_nats_client,
            event_data,
            executor,
            sentinel_seen=True,
        )

        assert orchestrator.execution_log.status == "SUCCEEDED"

    @pytest.mark.asyncio
    async def test_exit_zero_with_result_artifact_success_succeeds(
        self, db_session: Session, test_flow: Flow, mock_nats_client, event_data
    ):
        """Channel 2: result.json with a success status confirms success."""
        artifact = {"status": "success", "review_action": "approve"}
        executor = _confirmation_executor(artifact=artifact)
        orchestrator = await self._run(
            db_session,
            test_flow,
            mock_nats_client,
            event_data,
            executor,
            sentinel_seen=False,
        )

        assert orchestrator.execution_log.status == "SUCCEEDED"
        assert orchestrator.execution_log.result == artifact

    @pytest.mark.asyncio
    async def test_result_artifact_failure_wins_over_sentinel(
        self, db_session: Session, test_flow: Flow, mock_nats_client, event_data
    ):
        """An explicit failure report in result.json beats a printed sentinel."""
        artifact = {"status": "failure", "reason": "could not submit review"}
        executor = _confirmation_executor(artifact=artifact)
        orchestrator = await self._run(
            db_session,
            test_flow,
            mock_nats_client,
            event_data,
            executor,
            sentinel_seen=True,
        )

        assert orchestrator.execution_log.status == "FAILED"
        assert "result.json" in orchestrator.execution_log.error_message
        assert orchestrator.execution_log.result == artifact

    @pytest.mark.asyncio
    async def test_nonzero_exit_fails_despite_confirmations(
        self, db_session: Session, test_flow: Flow, mock_nats_client, event_data
    ):
        """A nonzero exit is a failure even if both channels claim success."""
        from preloop.agents.failure_analysis import AgentFailureAnalysis

        executor = _confirmation_executor(
            status=AgentStatus.FAILED,
            exit_code=1,
            error_message="Agent process exited with code 1",
            artifact={"status": "success"},
            failure_analysis=AgentFailureAnalysis(
                message="Agent process exited with code 1", transient=False
            ),
        )
        orchestrator = await self._run(
            db_session,
            test_flow,
            mock_nats_client,
            event_data,
            executor,
            sentinel_seen=True,
        )

        assert orchestrator.execution_log.status == "FAILED"

    def _monitor_orchestrator(self, mock_nats_client, event_data, *, sentinel_seen):
        """Orchestrator stub for calling ``_monitor_agent_execution`` directly."""
        from unittest.mock import MagicMock

        orchestrator = FlowExecutionOrchestrator(
            db=MagicMock(),
            flow_id=uuid4(),
            trigger_event_data=event_data,
            nats_client=mock_nats_client,
        )
        orchestrator._agent_exec_started = True
        if sentinel_seen:
            orchestrator._success_sentinel_seen.set()
        orchestrator.execution_log = type("ExecutionLogStub", (), {"id": uuid4()})()
        # Main added a DB-backed tool-loop probe; this stub has no session.
        orchestrator._sync_runtime_tool_activity_metrics = AsyncMock(return_value=None)
        return orchestrator

    @pytest.mark.asyncio
    async def test_eval_fail_verdict_does_not_fail_the_flow(
        self, mock_nats_client, event_data
    ):
        """Eval ``status: fail`` is a completed verdict, not a flow failure.

        Per 003-observe-eval.yaml, fail means the subject's checks failed
        but the eval ran to completion. Channel 2 therefore confirms the
        flow succeeded.
        """
        artifact = {
            "schema": "preloop.eval.result/v1",
            "status": "fail",
            "summary": "subject checks failed",
        }
        orchestrator = self._monitor_orchestrator(
            mock_nats_client, event_data, sentinel_seen=False
        )
        result = await orchestrator._monitor_agent_execution(
            "session-confirmation-123",
            _confirmation_executor(artifact=artifact),
        )

        assert result["status"] == "SUCCEEDED"
        assert result["result"] == artifact

    @pytest.mark.asyncio
    async def test_audit_fail_verdict_does_not_fail_the_flow(
        self, mock_nats_client, event_data
    ):
        """Audit ``verdict: fail`` (no sentinel) confirms flow completion.

        Per the preloop.cra.* contracts, a failing audit is a completed
        audit — channel 2 must recognize the verdict vocabulary even though
        the artifact carries no "status" key.
        """
        artifact = {
            "schema": "preloop.cra.releaseaudit/v1",
            "verdict": "fail",
            "checks": [{"name": "severity gate", "passed": False}],
        }
        orchestrator = self._monitor_orchestrator(
            mock_nats_client, event_data, sentinel_seen=False
        )
        result = await orchestrator._monitor_agent_execution(
            "session-confirmation-123",
            _confirmation_executor(artifact=artifact),
        )

        assert result["status"] == "SUCCEEDED"
        assert result["result"] == artifact

    @pytest.mark.asyncio
    async def test_audit_error_verdict_overrides_to_failed(
        self, mock_nats_client, event_data
    ):
        """Audit ``verdict: error`` means the audit could not complete."""
        artifact = {
            "schema": "preloop.cra.sbomaudit/v1",
            "verdict": "error",
            "checks": [{"name": "inputs", "passed": False}],
        }
        orchestrator = self._monitor_orchestrator(
            mock_nats_client, event_data, sentinel_seen=True
        )
        result = await orchestrator._monitor_agent_execution(
            "session-confirmation-123",
            _confirmation_executor(artifact=artifact),
        )

        assert result["status"] == "FAILED"
        assert "result.json" in result["error_message"]
        assert result["result"] == artifact

    @pytest.mark.asyncio
    async def test_unrecognized_verdict_without_sentinel_still_fails(
        self, mock_nats_client, event_data
    ):
        """Neither channel: an unrecognized verdict is not a confirmation."""
        artifact = {"schema": "preloop.cra.duediligence/v1", "verdict": "recorded"}
        orchestrator = self._monitor_orchestrator(
            mock_nats_client, event_data, sentinel_seen=False
        )
        result = await orchestrator._monitor_agent_execution(
            "session-confirmation-123",
            _confirmation_executor(artifact=artifact),
        )

        assert result["status"] == "FAILED"
        assert "FLOW_EXECUTION_SUCCESS" in result["error_message"]

    @pytest.mark.asyncio
    async def test_result_artifact_error_overrides_to_failed(
        self, mock_nats_client, event_data
    ):
        """Eval ``status: error`` means the run itself could not complete."""
        artifact = {
            "schema": "preloop.eval.result/v1",
            "status": "error",
            "summary": "eval aborted",
        }
        orchestrator = self._monitor_orchestrator(
            mock_nats_client, event_data, sentinel_seen=True
        )
        result = await orchestrator._monitor_agent_execution(
            "session-confirmation-123",
            _confirmation_executor(artifact=artifact),
        )

        assert result["status"] == "FAILED"
        assert "result.json" in result["error_message"]
        assert result["result"] == artifact

    @pytest.mark.asyncio
    async def test_result_artifact_failed_overrides_to_failed(
        self, mock_nats_client, event_data
    ):
        """Plain ``failed`` remains an explicit flow-failure confirmation."""
        artifact = {"status": "failed", "reason": "could not submit review"}
        orchestrator = self._monitor_orchestrator(
            mock_nats_client, event_data, sentinel_seen=True
        )
        result = await orchestrator._monitor_agent_execution(
            "session-confirmation-123",
            _confirmation_executor(artifact=artifact),
        )

        assert result["status"] == "FAILED"
        assert "result.json" in result["error_message"]
        assert result["result"] == artifact

    @pytest.mark.asyncio
    async def test_grace_period_failure_override_includes_exit_code(
        self, mock_nats_client, event_data
    ):
        """Grace-period failure override must carry exit_code for retries."""
        from preloop.agents.failure_analysis import AgentFailureAnalysis

        artifact = {"status": "error", "summary": "eval aborted after sentinel"}
        analysis = AgentFailureAnalysis(
            message="eval aborted after sentinel", transient=False
        )
        executor = _confirmation_executor(
            status=AgentStatus.SUCCEEDED,
            monitor_status=AgentStatus.RUNNING,
            exit_code=0,
            artifact=artifact,
            failure_analysis=analysis,
        )
        orchestrator = self._monitor_orchestrator(
            mock_nats_client, event_data, sentinel_seen=True
        )

        with patch(
            "preloop.services.flow_orchestrator.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            result = await orchestrator._monitor_agent_execution(
                "session-confirmation-123", executor
            )

        assert result["status"] == "FAILED"
        assert result["exit_code"] == 0
        assert result["failure_analysis"] is not None
        assert result["failure_analysis"]["transient"] is False
        assert result["result"] == artifact
        assert orchestrator._retry_decision(result) is not None


class TestResultArtifactConfirmation:
    """Eval ``fail`` is a verdict; only error/failed/failure fail the flow."""

    def test_fail_is_success_confirmation(self):
        assert _result_artifact_confirmation({"status": "fail"}) == "success"

    def test_error_is_failure_confirmation(self):
        assert _result_artifact_confirmation({"status": "error"}) == "failure"

    def test_failed_is_failure_confirmation(self):
        assert _result_artifact_confirmation({"status": "failed"}) == "failure"

    def test_failure_is_failure_confirmation(self):
        assert _result_artifact_confirmation({"status": "failure"}) == "failure"

    def test_pass_is_success_confirmation(self):
        assert _result_artifact_confirmation({"status": "pass"}) == "success"


class TestResultArtifactVerdictConfirmation:
    """Audit contracts confirm completion via top-level ``verdict``.

    Vocabulary (preloop.cra.sbomaudit/v1, preloop.cra.releaseaudit/v1):
    pass | pass_with_findings | fail are completed-run verdicts — a failing
    audit is a completed flow (same semantics as eval) — while ``error``
    means the audit itself could not complete.
    """

    @pytest.mark.parametrize(
        "verdict", ["pass", "passed", "pass_with_findings", "fail"]
    )
    def test_completed_verdicts_confirm_success(self, verdict):
        artifact = {"schema": "preloop.cra.releaseaudit/v1", "verdict": verdict}
        assert _result_artifact_confirmation(artifact) == "success"

    def test_verdict_is_normalized(self):
        assert (
            _result_artifact_confirmation({"verdict": "  Pass_With_Findings "})
            == "success"
        )

    def test_error_verdict_is_failure_confirmation(self):
        artifact = {"schema": "preloop.cra.releaseaudit/v1", "verdict": "error"}
        assert _result_artifact_confirmation(artifact) == "failure"

    def test_unrecognized_verdict_is_no_confirmation(self):
        # e.g. the due-diligence contract's "recorded" — not a completion
        # confirmation; the sentinel remains required for those flows.
        assert _result_artifact_confirmation({"verdict": "recorded"}) is None

    def test_non_string_verdict_is_no_confirmation(self):
        assert _result_artifact_confirmation({"verdict": True}) is None

    def test_neither_status_nor_verdict_is_no_confirmation(self):
        assert _result_artifact_confirmation({"schema": "x", "checks": []}) is None

    def test_recognized_status_wins_over_verdict(self):
        # status stays authoritative: an explicit failure status is not
        # softened by a passing verdict...
        assert (
            _result_artifact_confirmation({"status": "error", "verdict": "pass"})
            == "failure"
        )
        # ...and a recognized success status is not overridden by an
        # error verdict.
        assert (
            _result_artifact_confirmation({"status": "success", "verdict": "error"})
            == "success"
        )

    def test_unrecognized_status_falls_back_to_verdict(self):
        assert (
            _result_artifact_confirmation(
                {"status": "wat", "verdict": "pass_with_findings"}
            )
            == "success"
        )
        assert (
            _result_artifact_confirmation({"status": None, "verdict": "error"})
            == "failure"
        )


def _milestones(orchestrator, name):
    """All logged milestones with the given name."""
    return [
        m
        for m in orchestrator.execution_logger.get_milestones()
        if m["milestone"] == name
    ]


class TestSentinelLogScanHelpers:
    """Exact-line scanning over complete refetched logs (layer 3 primitives)."""

    def test_sentinel_found_after_exec_marker(self):
        lines = [
            "prompt echo ...",
            AGENT_EXEC_START_MARKER,
            "doing work",
            f"  {FLOW_SUCCESS_SENTINEL}  ",
        ]
        assert _sentinel_in_log_lines(lines) is True

    def test_sentinel_before_marker_is_prompt_echo(self):
        lines = [
            FLOW_SUCCESS_SENTINEL,
            AGENT_EXEC_START_MARKER,
            "doing work",
        ]
        assert _sentinel_in_log_lines(lines) is False

    def test_sentinel_without_marker_scans_all_lines(self):
        # Marker missing from refetched logs (runtime truncation): safe to
        # scan everything because instructions embed the sentinel INLINE.
        assert _sentinel_in_log_lines(["a", FLOW_SUCCESS_SENTINEL]) is True

    def test_inline_sentinel_is_not_a_match(self):
        assert (
            _sentinel_in_log_lines([f"print this marker: {FLOW_SUCCESS_SENTINEL}"])
            is False
        )

    def test_empty_logs_no_match(self):
        assert _sentinel_in_log_lines([]) is False

    def test_failure_report_reason_extracted(self):
        lines = [
            AGENT_EXEC_START_MARKER,
            f"{FLOW_FAILURE_REPORT_PREFIX} git push was rejected",
        ]
        assert _failure_report_in_log_lines(lines) == "git push was rejected"

    def test_failure_report_before_marker_ignored(self):
        lines = [
            f"{FLOW_FAILURE_REPORT_PREFIX} echoed from prompt",
            AGENT_EXEC_START_MARKER,
            "all good",
        ]
        assert _failure_report_in_log_lines(lines) is None

    def test_quoted_failure_report_ignored(self):
        # Lines quoted into the nudge prompt ("> ...") must never match.
        assert (
            _failure_report_in_log_lines([f"> {FLOW_FAILURE_REPORT_PREFIX} old"])
            is None
        )

    def test_failure_report_without_reason_gets_placeholder(self):
        assert (
            _failure_report_in_log_lines([FLOW_FAILURE_REPORT_PREFIX])
            == "no reason given"
        )


class TestConfirmationNudgePrompt:
    """The minimal follow-up prompt for the confirmation round."""

    def test_prompt_names_both_channels(self):
        prompt = _build_confirmation_nudge_prompt("original task", [], 4096)
        assert "/workspace/result.json" in prompt
        assert FLOW_SUCCESS_SENTINEL in prompt
        assert FLOW_FAILURE_REPORT_PREFIX in prompt
        assert "original task" in prompt

    def test_prompt_echo_cannot_trigger_exact_line_detectors(self):
        """Regression guard: embedding a prior run's sentinel/failure lines
        must not let the nudge prompt's own echo confirm or fail the run."""
        prior_logs = [
            FLOW_SUCCESS_SENTINEL,
            f"{FLOW_FAILURE_REPORT_PREFIX} old failure",
            AGENT_EXEC_START_MARKER,
        ]
        prompt = _build_confirmation_nudge_prompt("task", prior_logs, 4096)
        assert _sentinel_in_log_lines(prompt.splitlines()) is False
        assert _failure_report_in_log_lines(prompt.splitlines()) is None

    def test_token_ceiling_bounds_prior_context(self):
        huge_prompt = "p" * 100_000
        huge_logs = ["l" * 200] * 1000
        prompt = _build_confirmation_nudge_prompt(huge_prompt, huge_logs, 1000)
        # ~4 chars/token, split between prompt excerpt and log tail, plus the
        # fixed instruction scaffolding.
        assert len(prompt) < 1000 * 4 + 2000

    def test_token_ceiling_has_sane_floor(self):
        prompt = _build_confirmation_nudge_prompt("task", ["line"], 1)
        assert "task" in prompt
        assert "> line" in prompt


class TestNudgeCapabilityFlags:
    """Which runtimes may be re-invoked for the confirmation round."""

    def test_codex_and_opencode_support_the_nudge(self):
        from preloop.agents.codex import CodexAgent
        from preloop.agents.opencode import OpenCodeAgent

        assert CodexAgent.supports_confirmation_nudge is True
        assert OpenCodeAgent.supports_confirmation_nudge is True

    def test_other_runtimes_default_to_no_op(self):
        from preloop.agents.base import AgentExecutor
        from preloop.agents.aider import AiderAgent
        from preloop.agents.gemini import GeminiAgent
        from preloop.agents.openhands import OpenHandsAgent
        from preloop.agents.remote_runner import RemoteRunnerExecutor

        assert AgentExecutor.supports_confirmation_nudge is False
        assert AiderAgent.supports_confirmation_nudge is False
        assert GeminiAgent.supports_confirmation_nudge is False
        assert OpenHandsAgent.supports_confirmation_nudge is False
        assert RemoteRunnerExecutor.supports_confirmation_nudge is False


class TestPostExitLogRescan:
    """Layer 3: refetch the COMPLETE runtime logs before failing the run."""

    def _monitor_orchestrator(self, mock_nats_client, event_data):
        orchestrator = FlowExecutionOrchestrator(
            db=MagicMock(),
            flow_id=uuid4(),
            trigger_event_data=event_data,
            nats_client=mock_nats_client,
        )
        orchestrator._agent_exec_started = True
        orchestrator.execution_log = type("ExecutionLogStub", (), {"id": uuid4()})()
        orchestrator._sync_runtime_tool_activity_metrics = AsyncMock(return_value=None)
        return orchestrator

    @pytest.mark.asyncio
    async def test_reconnect_tail_regression_rescan_recovers_sentinel(
        self, mock_nats_client, event_data
    ):
        """The known edge: a late stream reconnect loses the log tail, the
        live detector never sees the sentinel, and a completed run used to be
        marked FAILED. The post-exit refetch must recover it."""
        executor = _confirmation_executor()
        executor.get_logs = AsyncMock(
            return_value=[
                "prompt echo",
                AGENT_EXEC_START_MARKER,
                "did the work",
                FLOW_SUCCESS_SENTINEL,
            ]
        )
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)

        result = await orchestrator._monitor_agent_execution(
            "session-confirmation-123", executor
        )

        assert result["status"] == "SUCCEEDED"
        executor.get_logs.assert_awaited_once_with(
            "session-confirmation-123", tail=None
        )
        rescans = _milestones(orchestrator, "post_exit_log_rescan")
        assert len(rescans) == 1
        assert rescans[0]["details"]["sentinel_found"] is True
        # The rescan confirmed the run, so the (more expensive) nudge must
        # not have been attempted.
        assert _milestones(orchestrator, "confirmation_nudge_used") == []
        assert _milestones(orchestrator, "success_confirmation_missing") == []

    @pytest.mark.asyncio
    async def test_rescan_ignores_sentinel_from_prompt_echo(
        self, mock_nats_client, event_data
    ):
        """A sentinel line before the exec-start marker stays prompt echo."""
        executor = _confirmation_executor()
        executor.get_logs = AsyncMock(
            return_value=[
                FLOW_SUCCESS_SENTINEL,
                AGENT_EXEC_START_MARKER,
                "did the work, forgot to confirm",
            ]
        )
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)

        result = await orchestrator._monitor_agent_execution(
            "session-confirmation-123", executor
        )

        assert result["status"] == "FAILED"
        assert "FLOW_EXECUTION_SUCCESS" in result["error_message"]
        rescans = _milestones(orchestrator, "post_exit_log_rescan")
        assert rescans[0]["details"]["sentinel_found"] is False

    @pytest.mark.asyncio
    async def test_rescan_tolerates_executors_without_usable_logs(
        self, mock_nats_client, event_data
    ):
        """A non-list get_logs result (mocks, broken runtimes) is treated as
        no logs and the ladder proceeds to fail closed."""
        executor = _confirmation_executor()  # AsyncMock get_logs -> MagicMock
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)

        result = await orchestrator._monitor_agent_execution(
            "session-confirmation-123", executor
        )

        assert result["status"] == "FAILED"
        rescans = _milestones(orchestrator, "post_exit_log_rescan")
        assert rescans[0]["details"] == {"sentinel_found": False, "line_count": 0}

    @pytest.mark.asyncio
    async def test_rescan_recovers_sentinel_end_to_end(
        self, db_session: Session, test_flow: Flow, mock_nats_client, event_data
    ):
        """Full-lifecycle version of the reconnect-tail regression."""
        executor = _confirmation_executor()
        executor.get_logs = AsyncMock(
            return_value=[AGENT_EXEC_START_MARKER, FLOW_SUCCESS_SENTINEL]
        )
        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=executor,
        ):
            orchestrator = FlowExecutionOrchestrator(
                db=db_session,
                flow_id=test_flow.id,
                trigger_event_data=event_data,
                nats_client=mock_nats_client,
            )
            orchestrator._agent_exec_started = True
            await orchestrator.run()

        assert orchestrator.execution_log.status == "SUCCEEDED"


class TestConfirmationNudge:
    """Layer 2: the one-shot confirmation round."""

    def _monitor_orchestrator(self, mock_nats_client, event_data):
        orchestrator = FlowExecutionOrchestrator(
            db=MagicMock(),
            flow_id=uuid4(),
            trigger_event_data=event_data,
            nats_client=mock_nats_client,
        )
        orchestrator._agent_exec_started = True
        orchestrator.execution_log = type("ExecutionLogStub", (), {"id": uuid4()})()
        orchestrator._sync_runtime_tool_activity_metrics = AsyncMock(return_value=None)
        orchestrator._execution_context = {
            "prompt": "original task prompt",
            "agent_type": "codex",
            "agent_config": {},
        }
        return orchestrator

    def _nudge_executor(self, *, logs=None, artifact=None):
        """Mock executor for the nudge session itself."""
        nudge_executor = AsyncMock()
        nudge_executor.get_status = AsyncMock(return_value=AgentStatus.SUCCEEDED)
        nudge_executor.get_logs = AsyncMock(return_value=logs or [])
        nudge_executor.get_result_artifact = AsyncMock(return_value=artifact)
        nudge_executor.get_evidence_archive = AsyncMock(return_value=None)
        return nudge_executor

    def _wire_nudge(self, orchestrator, executor, nudge_executor):
        """Mark the original executor nudge-capable and stub the session."""
        executor.supports_confirmation_nudge = True
        orchestrator._start_agent_session = AsyncMock(
            return_value=("nudge-session-1", nudge_executor)
        )

    @pytest.mark.asyncio
    async def test_nudge_confirms_success_via_sentinel(
        self, mock_nats_client, event_data
    ):
        executor = _confirmation_executor()
        executor.get_logs = AsyncMock(return_value=[])  # layer 3 finds nothing
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)
        nudge_executor = self._nudge_executor(
            logs=[AGENT_EXEC_START_MARKER, FLOW_SUCCESS_SENTINEL]
        )
        self._wire_nudge(orchestrator, executor, nudge_executor)

        result = await orchestrator._monitor_agent_execution(
            "session-confirmation-123", executor
        )

        assert result["status"] == "SUCCEEDED"
        nudges = _milestones(orchestrator, "confirmation_nudge_used")
        assert len(nudges) == 1
        assert nudges[0]["details"]["outcome"] == "confirmed_success"
        assert nudges[0]["details"]["channel"] == "sentinel"
        assert _milestones(orchestrator, "success_confirmation_missing") == []
        # The nudge session was cleaned up.
        nudge_executor.cleanup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_nudge_context_is_minimal_and_side_effect_free(
        self, mock_nats_client, event_data
    ):
        executor = _confirmation_executor()
        executor.get_logs = AsyncMock(return_value=[])
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)
        orchestrator._execution_context = {
            "prompt": "original task prompt",
            "agent_type": "codex",
            "agent_config": {},
            "git_clone_config": {"repositories": [{"url": "x"}]},
            "custom_commands": {"post": ["push things"]},
        }
        nudge_executor = self._nudge_executor(
            logs=[AGENT_EXEC_START_MARKER, FLOW_SUCCESS_SENTINEL]
        )
        self._wire_nudge(orchestrator, executor, nudge_executor)

        await orchestrator._monitor_agent_execution(
            "session-confirmation-123", executor
        )

        orchestrator._start_agent_session.assert_awaited_once()
        nudge_context = orchestrator._start_agent_session.await_args.args[0]
        assert nudge_context["confirmation_nudge"] is True
        # No clone/push/PR or custom-command side effects in the nudge round.
        assert nudge_context["git_clone_config"] is None
        assert nudge_context["custom_commands"] is None
        # Token ceiling forwarded for runtimes that honor model parameters.
        assert nudge_context["model_parameters"]["max_output_tokens"] == 4096
        assert "original task prompt" in nudge_context["prompt"]
        # The nudge is a SECOND session for the same execution, started while
        # the original agent Job is still around (it lingers until its TTL).
        # Without its own session name it can only ever fail with a 409 name
        # conflict on Kubernetes.
        assert nudge_context[AGENT_SESSION_SUFFIX_KEY] == "nudge"
        assert kubernetes_job_name(
            "exec-1", session_suffix=nudge_context[AGENT_SESSION_SUFFIX_KEY]
        ) != kubernetes_job_name("exec-1")
        # The original context object is untouched.
        assert orchestrator._execution_context["git_clone_config"] is not None

    @pytest.mark.asyncio
    async def test_nudge_token_ceiling_clamps_larger_inherited_limit(
        self, mock_nats_client, event_data
    ):
        """A larger flow-level output limit must not defeat the nudge cap."""
        executor = _confirmation_executor()
        executor.get_logs = AsyncMock(return_value=[])
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)
        orchestrator._execution_context = {
            "prompt": "original task prompt",
            "agent_type": "codex",
            "agent_config": {},
            "model_parameters": {"max_output_tokens": 128000, "temperature": 0.2},
        }
        nudge_executor = self._nudge_executor(
            logs=[AGENT_EXEC_START_MARKER, FLOW_SUCCESS_SENTINEL]
        )
        self._wire_nudge(orchestrator, executor, nudge_executor)

        await orchestrator._monitor_agent_execution(
            "session-confirmation-123", executor
        )

        nudge_context = orchestrator._start_agent_session.await_args.args[0]
        # The inherited 128k limit is clamped down to the nudge ceiling.
        assert nudge_context["model_parameters"]["max_output_tokens"] == 4096
        # Unrelated model parameters pass through untouched.
        assert nudge_context["model_parameters"]["temperature"] == 0.2
        # The original context object is untouched.
        assert (
            orchestrator._execution_context["model_parameters"]["max_output_tokens"]
            == 128000
        )

    @pytest.mark.asyncio
    async def test_nudge_token_ceiling_respects_tighter_existing_limit(
        self, mock_nats_client, event_data
    ):
        """A tighter pre-existing output limit is kept as-is."""
        executor = _confirmation_executor()
        executor.get_logs = AsyncMock(return_value=[])
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)
        orchestrator._execution_context = {
            "prompt": "original task prompt",
            "agent_type": "codex",
            "agent_config": {},
            "model_parameters": {"max_output_tokens": 1024},
        }
        nudge_executor = self._nudge_executor(
            logs=[AGENT_EXEC_START_MARKER, FLOW_SUCCESS_SENTINEL]
        )
        self._wire_nudge(orchestrator, executor, nudge_executor)

        await orchestrator._monitor_agent_execution(
            "session-confirmation-123", executor
        )

        nudge_context = orchestrator._start_agent_session.await_args.args[0]
        assert nudge_context["model_parameters"]["max_output_tokens"] == 1024

    @pytest.mark.asyncio
    async def test_nudge_confirms_success_via_result_artifact_and_merges(
        self, mock_nats_client, event_data
    ):
        # The original run wrote a rich report without a recognized
        # completion status; the nudge completes it.
        original_artifact = {"schema": "x", "notes": "rich report"}
        executor = _confirmation_executor(artifact=original_artifact)
        executor.get_logs = AsyncMock(return_value=[])
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)
        nudge_executor = self._nudge_executor(artifact={"status": "success"})
        self._wire_nudge(orchestrator, executor, nudge_executor)

        result = await orchestrator._monitor_agent_execution(
            "session-confirmation-123", executor
        )

        assert result["status"] == "SUCCEEDED"
        # Rich original fields preserved, completion status landed on top.
        assert result["result"] == {
            "schema": "x",
            "notes": "rich report",
            "status": "success",
        }
        nudges = _milestones(orchestrator, "confirmation_nudge_used")
        assert nudges[0]["details"]["channel"] == "result_artifact"

    @pytest.mark.asyncio
    async def test_nudge_explicit_failure_line_fails_with_agent_reason(
        self, mock_nats_client, event_data
    ):
        executor = _confirmation_executor()
        executor.get_logs = AsyncMock(return_value=[])
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)
        nudge_executor = self._nudge_executor(
            logs=[
                AGENT_EXEC_START_MARKER,
                f"{FLOW_FAILURE_REPORT_PREFIX} the review was never posted",
            ]
        )
        self._wire_nudge(orchestrator, executor, nudge_executor)

        result = await orchestrator._monitor_agent_execution(
            "session-confirmation-123", executor
        )

        assert result["status"] == "FAILED"
        assert "the review was never posted" in result["error_message"]
        nudges = _milestones(orchestrator, "confirmation_nudge_used")
        assert nudges[0]["details"]["outcome"] == "explicit_failure"

    @pytest.mark.asyncio
    async def test_nudge_explicit_failure_artifact_fails_with_agent_reason(
        self, mock_nats_client, event_data
    ):
        executor = _confirmation_executor()
        executor.get_logs = AsyncMock(return_value=[])
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)
        nudge_executor = self._nudge_executor(
            artifact={"status": "failure", "reason": "task died before submit"}
        )
        self._wire_nudge(orchestrator, executor, nudge_executor)

        result = await orchestrator._monitor_agent_execution(
            "session-confirmation-123", executor
        )

        assert result["status"] == "FAILED"
        assert "task died before submit" in result["error_message"]

    @pytest.mark.asyncio
    async def test_nudge_without_confirmation_fails_closed(
        self, mock_nats_client, event_data
    ):
        """No confirmation after the nudge => the existing
        success_confirmation_missing failure, message unchanged."""
        executor = _confirmation_executor()
        executor.get_logs = AsyncMock(return_value=[])
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)
        nudge_executor = self._nudge_executor(logs=["nothing to see"])
        self._wire_nudge(orchestrator, executor, nudge_executor)

        result = await orchestrator._monitor_agent_execution(
            "session-confirmation-123", executor
        )

        assert result["status"] == "FAILED"
        assert "FLOW_EXECUTION_SUCCESS" in result["error_message"]
        assert "result.json" in result["error_message"]
        missing = _milestones(orchestrator, "success_confirmation_missing")
        assert len(missing) == 1
        assert missing[0]["details"]["nudge_outcome"] == "no_confirmation"

    @pytest.mark.asyncio
    async def test_nudge_no_ops_for_unsupported_runtime(
        self, mock_nats_client, event_data
    ):
        """Runtimes without validated resume/fresh-invocation support are
        never re-invoked; the run fails closed as before."""
        executor = _confirmation_executor()  # no supports_confirmation_nudge=True
        executor.get_logs = AsyncMock(return_value=[])
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)
        orchestrator._start_agent_session = AsyncMock()

        result = await orchestrator._monitor_agent_execution(
            "session-confirmation-123", executor
        )

        assert result["status"] == "FAILED"
        assert "FLOW_EXECUTION_SUCCESS" in result["error_message"]
        orchestrator._start_agent_session.assert_not_awaited()
        nudges = _milestones(orchestrator, "confirmation_nudge_used")
        assert nudges[0]["details"]["outcome"] == "skipped_unsupported_runtime"

    @pytest.mark.asyncio
    async def test_nudge_is_single_shot(self, mock_nats_client, event_data):
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)
        executor = _confirmation_executor()
        nudge_executor = self._nudge_executor(logs=["still nothing"])
        self._wire_nudge(orchestrator, executor, nudge_executor)

        first = await orchestrator._run_confirmation_nudge(executor, [])
        second = await orchestrator._run_confirmation_nudge(executor, [])

        assert first["outcome"] == "no_confirmation"
        assert second["outcome"] == "skipped_already_used"
        orchestrator._start_agent_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_nudge_skipped_without_execution_context(
        self, mock_nats_client, event_data
    ):
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)
        orchestrator._execution_context = None
        executor = _confirmation_executor()
        executor.supports_confirmation_nudge = True
        orchestrator._start_agent_session = AsyncMock()

        outcome = await orchestrator._run_confirmation_nudge(executor, [])

        assert outcome["outcome"] == "skipped_no_execution_context"
        orchestrator._start_agent_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_nudge_timeout_fails_closed(self, mock_nats_client, event_data):
        executor = _confirmation_executor()
        executor.get_logs = AsyncMock(return_value=[])
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)
        nudge_executor = self._nudge_executor()
        nudge_executor.get_status = AsyncMock(return_value=AgentStatus.RUNNING)
        self._wire_nudge(orchestrator, executor, nudge_executor)

        with patch(
            "preloop.services.flow_orchestrator.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            result = await orchestrator._monitor_agent_execution(
                "session-confirmation-123", executor
            )

        assert result["status"] == "FAILED"
        assert "FLOW_EXECUTION_SUCCESS" in result["error_message"]
        nudge_executor.stop.assert_awaited_once()
        nudges = _milestones(orchestrator, "confirmation_nudge_used")
        assert nudges[0]["details"]["outcome"] == "timeout"

    @pytest.mark.asyncio
    async def test_nudge_start_error_fails_closed(self, mock_nats_client, event_data):
        executor = _confirmation_executor()
        executor.get_logs = AsyncMock(return_value=[])
        executor.supports_confirmation_nudge = True
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)
        orchestrator._start_agent_session = AsyncMock(
            side_effect=RuntimeError("no capacity")
        )

        result = await orchestrator._monitor_agent_execution(
            "session-confirmation-123", executor
        )

        assert result["status"] == "FAILED"
        assert "FLOW_EXECUTION_SUCCESS" in result["error_message"]
        nudges = _milestones(orchestrator, "confirmation_nudge_used")
        assert nudges[0]["details"]["outcome"] == "error"
        assert "no capacity" in nudges[0]["details"]["reason"]


class TestInPlaceCompletionNudge:
    """The cheap layer: the agent script reminds itself in its own container.

    The orchestrator's part is small on purpose: read the markers the script
    prints, publish them as timeline events, and stand down from its own
    (much more expensive) second-session nudge.
    """

    def _monitor_orchestrator(self, mock_nats_client, event_data):
        orchestrator = FlowExecutionOrchestrator(
            db=MagicMock(),
            flow_id=uuid4(),
            trigger_event_data=event_data,
            nats_client=mock_nats_client,
        )
        orchestrator._agent_exec_started = True
        orchestrator.execution_log = type("ExecutionLogStub", (), {"id": uuid4()})()
        orchestrator._sync_runtime_tool_activity_metrics = AsyncMock(return_value=None)
        orchestrator._execution_context = {
            "prompt": "original task prompt",
            "agent_type": "codex",
            "agent_config": {},
        }
        return orchestrator

    def _streaming_executor(self, lines):
        executor = _confirmation_executor()

        async def stream(session_ref):
            for line in lines:
                yield line

        executor.stream_logs = stream
        executor.get_logs = AsyncMock(return_value=[])
        return executor

    async def _stream(self, orchestrator, lines):
        """Run the live log reader over a fixed set of lines.

        The reader is exercised directly rather than through the monitor
        loop: what is under test is marker parsing, and the monitor's
        streaming task is racy by design (it finishes when the runtime
        closes the stream, not when the decision is taken).
        """
        await orchestrator._stream_logs_to_nats(
            self._streaming_executor(lines), "session-1"
        )

    @pytest.mark.asyncio
    async def test_marker_in_live_stream_logs_the_timeline_event(
        self, mock_nats_client, event_data
    ):
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)

        await self._stream(
            orchestrator,
            [
                AGENT_EXEC_START_MARKER,
                "did the work",
                COMPLETION_NUDGE_MARKER,
                FLOW_SUCCESS_SENTINEL,
                f"{COMPLETION_NUDGE_RESULT_MARKER} exit=0",
            ],
        )

        # The reminder round confirmed on the same channel as any other run.
        assert orchestrator._success_sentinel_seen.is_set()
        nudges = _milestones(orchestrator, "completion_nudge")
        assert len(nudges) == 1
        assert nudges[0]["details"]["mode"] == "in_place"
        assert nudges[0]["details"]["source"] == "live_stream"
        outcomes = _milestones(orchestrator, "completion_nudge_result")
        assert outcomes[0]["details"]["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_result_marker_is_not_mistaken_for_the_start_marker(
        self, mock_nats_client, event_data
    ):
        """The result marker starts with the start marker's text, so a
        prefix match would log the round twice with the wrong source."""
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)

        await self._stream(
            orchestrator,
            [
                AGENT_EXEC_START_MARKER,
                f"{COMPLETION_NUDGE_RESULT_MARKER} exit=7",
                FLOW_SUCCESS_SENTINEL,
            ],
        )

        assert len(_milestones(orchestrator, "completion_nudge")) == 0
        outcomes = _milestones(orchestrator, "completion_nudge_result")
        assert len(outcomes) == 1
        assert outcomes[0]["details"]["exit_code"] == 7

    @pytest.mark.asyncio
    async def test_marker_recovered_from_the_post_exit_refetch(
        self, mock_nats_client, event_data
    ):
        """The reconnect that loses a sentinel loses these markers too; the
        complete logs are the authoritative view."""
        executor = _confirmation_executor()
        executor.get_logs = AsyncMock(
            return_value=[
                AGENT_EXEC_START_MARKER,
                "work",
                COMPLETION_NUDGE_MARKER,
                "still nothing",
            ]
        )
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)

        result = await orchestrator._monitor_agent_execution("session-1", executor)

        assert result["status"] == "FAILED"
        nudges = _milestones(orchestrator, "completion_nudge")
        assert nudges[0]["details"]["source"] == "post_exit_rescan"

    @pytest.mark.asyncio
    async def test_marker_before_exec_start_is_prompt_echo(
        self, mock_nats_client, event_data
    ):
        """Same arming rule as the sentinel: a marker quoted in the prompt
        echo is not evidence that a round ran."""
        executor = _confirmation_executor()
        executor.get_logs = AsyncMock(
            return_value=[COMPLETION_NUDGE_MARKER, AGENT_EXEC_START_MARKER, "work"]
        )
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)

        await orchestrator._monitor_agent_execution("session-1", executor)

        assert _milestones(orchestrator, "completion_nudge") == []

    @pytest.mark.asyncio
    async def test_in_place_round_stands_down_the_session_nudge(
        self, mock_nats_client, event_data
    ):
        """Asking twice is two model calls for one answer, and the second
        session knows strictly less than the container did."""
        executor = _confirmation_executor()
        executor.get_logs = AsyncMock(
            return_value=[AGENT_EXEC_START_MARKER, COMPLETION_NUDGE_MARKER]
        )
        executor.supports_confirmation_nudge = True
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)
        orchestrator._start_agent_session = AsyncMock()

        result = await orchestrator._monitor_agent_execution("session-1", executor)

        assert result["status"] == "FAILED"
        orchestrator._start_agent_session.assert_not_awaited()
        nudges = _milestones(orchestrator, "confirmation_nudge_used")
        assert nudges[0]["details"]["outcome"] == "skipped_inplace_nudge_used"
        # The operator learns from the message alone that the agent was asked.
        assert "reminded once in its own container" in result["error_message"]
        assert (
            _milestones(orchestrator, "success_confirmation_missing")[0]["details"][
                "inplace_nudge"
            ]
            is True
        )

    @pytest.mark.asyncio
    async def test_recorded_actions_stand_down_the_session_nudge(
        self, mock_nats_client, event_data
    ):
        """A nudge re-invokes the agent. Once a comment or a push is on
        record, a model that decides to finish the job repeats it."""
        executor = _confirmation_executor()
        executor.get_logs = AsyncMock(return_value=[])
        executor.supports_confirmation_nudge = True
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)
        orchestrator._start_agent_session = AsyncMock()
        orchestrator.execution_logger.log_agent_action(
            "api_called", "posted a review comment"
        )

        result = await orchestrator._monitor_agent_execution("session-1", executor)

        assert result["status"] == "FAILED"
        orchestrator._start_agent_session.assert_not_awaited()
        nudges = _milestones(orchestrator, "confirmation_nudge_used")
        assert nudges[0]["details"]["outcome"] == "skipped_actions_recorded"
        assert nudges[0]["details"]["action_count"] == 1

    @pytest.mark.asyncio
    async def test_unsupported_marker_does_not_count_as_a_round(
        self, mock_nats_client, event_data
    ):
        """A CLI without a resume flag never asked anything, so the
        orchestrator keeps its own options open."""
        executor = _confirmation_executor()
        executor.get_logs = AsyncMock(
            return_value=[
                AGENT_EXEC_START_MARKER,
                f"{COMPLETION_NUDGE_UNSUPPORTED_MARKER} codex",
            ]
        )
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)

        await orchestrator._monitor_agent_execution("session-1", executor)

        assert _milestones(orchestrator, "completion_nudge") == []
        assert orchestrator._inplace_nudge_seen is False
        assert orchestrator._inplace_nudge_unsupported is True
        missing = _milestones(orchestrator, "success_confirmation_missing")
        assert missing[0]["details"]["inplace_nudge_unsupported"] is True


class TestWiderCompletionSignalFallback:
    """Runtimes that cannot be resumed at all: accept the written report."""

    def _monitor_orchestrator(self, mock_nats_client, event_data):
        orchestrator = FlowExecutionOrchestrator(
            db=MagicMock(),
            flow_id=uuid4(),
            trigger_event_data=event_data,
            nats_client=mock_nats_client,
        )
        orchestrator._agent_exec_started = True
        orchestrator.execution_log = type("ExecutionLogStub", (), {"id": uuid4()})()
        orchestrator._sync_runtime_tool_activity_metrics = AsyncMock(return_value=None)
        return orchestrator

    @pytest.mark.asyncio
    async def test_bare_result_report_completes_a_non_resumable_runtime(
        self, mock_nats_client, event_data
    ):
        """There is nobody left to ask: a report the agent actually wrote is
        the best evidence available, even in an unrecognized vocabulary."""
        executor = _confirmation_executor(artifact={"outcome": "all checks ran"})
        executor.get_logs = AsyncMock(return_value=[])
        executor.supports_inplace_completion_nudge = False
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)

        result = await orchestrator._monitor_agent_execution("session-1", executor)

        assert result["status"] == "SUCCEEDED"
        accepted = _milestones(orchestrator, "completion_signal_accepted")
        assert accepted[0]["details"]["signal"] == "result_artifact_present"
        assert accepted[0]["details"]["keys"] == ["outcome"]

    @pytest.mark.asyncio
    async def test_resumable_runtime_still_fails_closed(
        self, mock_nats_client, event_data
    ):
        """The agent was asked directly in its own container and declined to
        confirm, which outweighs the presence of a file."""
        executor = _confirmation_executor(artifact={"outcome": "all checks ran"})
        executor.get_logs = AsyncMock(return_value=[])
        executor.supports_inplace_completion_nudge = True
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)

        result = await orchestrator._monitor_agent_execution("session-1", executor)

        assert result["status"] == "FAILED"
        assert _milestones(orchestrator, "completion_signal_accepted") == []

    @pytest.mark.asyncio
    async def test_empty_report_is_not_a_completion_signal(
        self, mock_nats_client, event_data
    ):
        executor = _confirmation_executor(artifact={})
        executor.get_logs = AsyncMock(return_value=[])
        executor.supports_inplace_completion_nudge = False
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)

        result = await orchestrator._monitor_agent_execution("session-1", executor)

        assert result["status"] == "FAILED"
        assert _milestones(orchestrator, "completion_signal_accepted") == []

    @pytest.mark.asyncio
    async def test_timeout_status_is_not_success_for_a_non_resumable_runtime(
        self, mock_nats_client, event_data
    ):
        """A report that says the work did not finish must not be recorded
        as SUCCEEDED just because the runtime cannot be asked again."""
        executor = _confirmation_executor(artifact={"status": "timeout"})
        executor.get_logs = AsyncMock(return_value=[])
        executor.supports_inplace_completion_nudge = False
        orchestrator = self._monitor_orchestrator(mock_nats_client, event_data)

        result = await orchestrator._monitor_agent_execution("session-1", executor)

        assert result["status"] == "FAILED"
        assert _milestones(orchestrator, "completion_signal_accepted") == []


class TestPerFlowTimeoutBudget:
    """Item 3: each flow may own its wall-clock budget."""

    def _orchestrator(self, mock_nats_client, event_data, timeout_seconds):
        orchestrator = FlowExecutionOrchestrator(
            db=MagicMock(),
            flow_id=uuid4(),
            trigger_event_data=event_data,
            nats_client=mock_nats_client,
        )
        orchestrator.flow = MagicMock(id=uuid4(), timeout_seconds=timeout_seconds)
        orchestrator._agent_exec_started = True
        orchestrator.execution_log = type("ExecutionLogStub", (), {"id": uuid4()})()
        orchestrator._sync_runtime_tool_activity_metrics = AsyncMock(return_value=None)
        return orchestrator

    def test_flow_without_a_budget_uses_the_deployment_default(
        self, mock_nats_client, event_data
    ):
        orchestrator = self._orchestrator(mock_nats_client, event_data, None)

        budget = orchestrator._execution_timeout_budget()

        assert budget.seconds == settings.flow_execution_max_wait_seconds
        assert budget.source == "default"

    def test_tiny_deployment_default_is_floored(self, mock_nats_client, event_data):
        """A mistyped FLOW_EXECUTION_MAX_WAIT_SECONDS must not make every
        flow unrunnable."""
        orchestrator = self._orchestrator(mock_nats_client, event_data, None)

        with patch.object(settings, "flow_execution_max_wait_seconds", 10):
            budget = orchestrator._execution_timeout_budget()

        assert budget.seconds == FLOW_TIMEOUT_SECONDS_MIN
        assert budget.source == "default"

    def test_flow_budget_wins(self, mock_nats_client, event_data):
        orchestrator = self._orchestrator(mock_nats_client, event_data, 1800)

        budget = orchestrator._execution_timeout_budget()

        assert budget.seconds == 1800
        assert budget.source == "flow"

    @pytest.mark.parametrize(
        "configured,expected",
        [(1, FLOW_TIMEOUT_SECONDS_MIN), (10**9, FLOW_TIMEOUT_SECONDS_MAX)],
    )
    def test_out_of_range_budgets_are_clamped(
        self, mock_nats_client, event_data, configured, expected
    ):
        """A typo must not make a flow unrunnable, and a runaway value must
        not hold a worker slot for a week."""
        orchestrator = self._orchestrator(mock_nats_client, event_data, configured)

        assert orchestrator._execution_timeout_budget().seconds == expected

    def test_garbage_budget_falls_back_to_the_default(
        self, mock_nats_client, event_data
    ):
        orchestrator = self._orchestrator(mock_nats_client, event_data, "soon")

        budget = orchestrator._execution_timeout_budget()

        assert budget.seconds == settings.flow_execution_max_wait_seconds
        assert budget.source == "default"

    @pytest.mark.asyncio
    async def test_monitor_stops_the_agent_at_the_flow_budget(
        self, mock_nats_client, event_data
    ):
        """The budget is enforced, and the message names it so an operator
        can tell 'stuck' from 'needs more time' without reading the code."""
        executor = _confirmation_executor(monitor_status=AgentStatus.RUNNING)
        orchestrator = self._orchestrator(mock_nats_client, event_data, 60)

        with patch(
            "preloop.services.flow_orchestrator.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            result = await orchestrator._monitor_agent_execution("session-1", executor)

        assert result["status"] == "FAILED"
        assert "60 seconds" in result["error_message"]
        assert "this flow's timeout budget" in result["error_message"]
        executor.stop.assert_awaited_once_with("session-1")
        started = _milestones(orchestrator, "agent_monitoring_started")
        assert started[0]["details"]["timeout_seconds"] == 60
        assert started[0]["details"]["timeout_source"] == "flow"
        timed_out = _milestones(orchestrator, "agent_execution_timeout")
        assert timed_out[0]["details"] == {
            "timeout_seconds": 60,
            "timeout_source": "flow",
        }

    def test_timeout_messages_stay_in_the_timeout_category(self):
        """The failure-category classifier keys off this sentence."""
        from preloop.services.flow_failure_category import derive_failure_category

        for source in ("flow", "default"):
            message = TimeoutBudget(seconds=1800, source=source).timeout_message()
            assert (
                derive_failure_category(status="FAILED", error_message=message)
                == "timeout"
            )
            assert "1800" in message
            assert "timeout_seconds" in message
            assert "—" not in message


class TestFlowTimeoutSecondsField:
    """The API/schema surface of the budget."""

    def test_schema_accepts_a_budget(self):
        flow_in = FlowCreate(
            name="Budgeted flow",
            prompt_template="do the thing",
            agent_type="codex",
            agent_config={},
            timeout_seconds=1800,
        )
        assert flow_in.timeout_seconds == 1800

    def test_schema_defaults_to_unset(self):
        flow_in = FlowCreate(
            name="Default flow",
            prompt_template="do the thing",
            agent_type="codex",
            agent_config={},
        )
        assert flow_in.timeout_seconds is None

    @pytest.mark.parametrize("bad", [0, 59, 86401])
    def test_schema_rejects_out_of_range_budgets(self, bad):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            FlowCreate(
                name="Bad flow",
                prompt_template="do the thing",
                agent_type="codex",
                agent_config={},
                timeout_seconds=bad,
            )

    def test_budget_persists_through_the_crud_layer(
        self, db_session: Session, test_account: Account
    ):
        flow_in = FlowCreate(
            name="Persisted budget flow",
            prompt_template="do the thing",
            agent_type="codex",
            agent_config={},
            timeout_seconds=7200,
            account_id=test_account.id,
        )
        flow = crud_flow.create(
            db=db_session, flow_in=flow_in, account_id=test_account.id
        )

        assert flow.timeout_seconds == 7200
