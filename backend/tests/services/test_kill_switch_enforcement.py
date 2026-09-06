"""Enforcement tests for the account kill switch (#157).

Covers the traffic classes the halt must block: MCP tool calls, new flow
executions (manual trigger, event/schedule dispatch, worker claim), and
the approval freeze while tools are halted.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from preloop.models.crud import crud_account_halt, crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.dynamic_fastmcp import DynamicFastMCP
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.services.kill_switch import (
    FlowHaltActiveError,
    invalidate_kill_switch_cache,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clear_kill_switch_cache():
    invalidate_kill_switch_cache()
    yield
    invalidate_kill_switch_cache()


@pytest.fixture
def halted_account(db_session: Session, test_user):
    """Halt every scope for the test account."""
    crud_account_halt.set_scopes(
        db_session,
        account_id=test_user.account_id,
        scopes=["gateway", "tools", "flows"],
        active=True,
        user_id=test_user.id,
        reason="incident drill",
    )
    invalidate_kill_switch_cache(test_user.account_id)
    return test_user.account_id


@pytest.fixture
def flow_row(db_session: Session, test_user):
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=f"kill-switch-test-{test_user.id}",
            prompt_template="hello",
            trigger_event_source="github",
            trigger_event_types=["test"],
            agent_type="openhands",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            is_enabled=True,
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )
    db_session.commit()
    db_session.refresh(flow)
    return flow


def _user_context(test_user) -> UserContext:
    return UserContext(
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        username=test_user.username,
        has_tracker=True,
        enabled_default_tools=[],
        enabled_proxied_tools=[],
    )


class TestToolCallHalt:
    """MCP tool calls are rejected while the tools scope is halted."""

    async def test_tool_call_denied_while_halted(
        self, db_session, test_user, halted_account
    ):
        mcp = DynamicFastMCP("test-mcp")
        mcp._user_context_provider = lambda: _user_context(test_user)

        with patch(
            "preloop.services.dynamic_fastmcp.get_db",
            side_effect=lambda: iter([db_session]),
        ):
            result = await mcp.call_tool("get_issue", {})

        assert len(result.content) == 1
        assert "kill switch" in result.content[0].text
        assert "Access denied" in result.content[0].text

    async def test_permission_prompt_denial_keeps_claude_code_schema(
        self, db_session, test_user, halted_account
    ):
        """Claude Code parses permission_prompt responses as JSON, not text."""
        mcp = DynamicFastMCP("test-mcp")
        mcp._user_context_provider = lambda: _user_context(test_user)

        with patch(
            "preloop.services.dynamic_fastmcp.get_db",
            side_effect=lambda: iter([db_session]),
        ):
            result = await mcp.call_tool("permission_prompt", {})

        payload = json.loads(result.content[0].text)
        assert payload["behavior"] == "deny"
        assert "kill switch" in payload["message"]

    async def test_tool_call_allowed_after_reenable(
        self, db_session, test_user, halted_account
    ):
        """Lifting the tools scope restores tool execution."""
        crud_account_halt.set_scopes(
            db_session,
            account_id=test_user.account_id,
            scopes=["tools"],
            active=False,
            user_id=test_user.id,
        )
        invalidate_kill_switch_cache(test_user.account_id)

        mcp = DynamicFastMCP("test-mcp")
        mcp._user_context_provider = lambda: _user_context(test_user)
        available = MagicMock()
        available.name = "get_issue"

        with (
            patch(
                "preloop.services.dynamic_fastmcp.get_db",
                side_effect=lambda: iter([db_session]),
            ),
            patch.object(mcp, "list_tools", return_value=[available]),
            patch(
                "preloop.services.policy_evaluator.evaluate_policy_async",
                new=AsyncMock(return_value=("allow", None, None)),
            ),
            patch.object(
                mcp.__class__.__bases__[0],
                "call_tool",
                new=AsyncMock(
                    return_value=MagicMock(content=[MagicMock(text="Result")])
                ),
                create=True,
            ) as mock_super,
        ):
            await mcp.call_tool("get_issue", {})

        mock_super.assert_called_once()


class TestFlowHalt:
    """New flow executions are blocked while the flows scope is halted."""

    async def test_manual_trigger_refused(
        self, db_session, test_user, halted_account, flow_row
    ):
        service = FlowTriggerService(db_session)
        with pytest.raises(FlowHaltActiveError):
            await service.trigger_flow(flow_id=flow_row.id, test_mode=True)

    async def test_matrix_trigger_refused(
        self, db_session, test_user, halted_account, flow_row
    ):
        service = FlowTriggerService(db_session)
        with pytest.raises(FlowHaltActiveError):
            await service.trigger_flow_matrix(
                flow_id=flow_row.id,
                matrix=[{}],
                test_mode=True,
            )

    async def test_no_execution_row_created(
        self, db_session, test_user, halted_account, flow_row
    ):
        from preloop.models.models.flow_execution import FlowExecution

        before = db_session.query(FlowExecution.id).count()
        service = FlowTriggerService(db_session)
        with pytest.raises(FlowHaltActiveError):
            await service.trigger_flow(flow_id=flow_row.id, test_mode=True)
        after = db_session.query(FlowExecution.id).count()
        assert after == before

    async def test_precreated_execution_stays_pending(
        self, db_session, halted_account, flow_row
    ):
        """A PENDING execution is not dispatched and not marked failed."""
        execution = crud_flow_execution.create(
            db_session,
            obj_in=FlowExecutionCreate(
                flow_id=flow_row.id,
                status="PENDING",
                trigger_event_details={"source": "test"},
            ),
        )
        db_session.commit()

        service = FlowTriggerService(db_session)
        result = await service._start_flow_execution(
            flow=flow_row,
            event_data={"source": "test"},
            nats_client=MagicMock(),
            precreated_execution=execution,
        )

        assert result.id == execution.id
        db_session.refresh(execution)
        assert execution.status == "PENDING"

    async def test_worker_leaves_claimed_execution_untouched(
        self, db_session, halted_account, flow_row
    ):
        """claim_and_run refuses to orchestrate while halted."""
        from preloop.services import flow_execution_runner as runner

        execution = crud_flow_execution.create(
            db_session,
            obj_in=FlowExecutionCreate(
                flow_id=flow_row.id,
                status="PENDING",
                trigger_event_details={"source": "test"},
            ),
        )
        db_session.commit()

        with (
            patch.object(runner, "get_db_session", return_value=iter([db_session])),
            # The runner closes its session in the finally block; keep the
            # test session usable for the assertions below.
            patch.object(db_session, "close"),
        ):
            result = await runner.claim_and_run_execution(str(execution.id))

        assert result["status"] == "skipped"
        db_session.refresh(execution)
        assert execution.status == "PENDING"
        # The claim was released, so a peer worker (or this one, after the
        # halt lifts) can pick the execution up again.
        assert execution.orchestrator_worker_id is None


async def test_halt_during_approval_blocks_proxied_dispatch(db_session, test_user):
    """A human approval after activation must not release the external call."""
    mcp = DynamicFastMCP("halt-after-approval")
    mcp._user_context_provider = lambda: _user_context(test_user)
    client = MagicMock(call_tool=AsyncMock())
    pool = MagicMock(get_client=AsyncMock(return_value=client))

    async def approve_after_halt(**kwargs):
        crud_account_halt.set_scopes(
            db_session,
            account_id=test_user.account_id,
            scopes=["tools"],
            active=True,
            user_id=test_user.id,
        )
        return True, ""

    with (
        patch(
            "preloop.services.approval_helper.require_approval",
            side_effect=approve_after_halt,
        ),
        patch(
            "preloop.services.dynamic_fastmcp.get_db",
            side_effect=lambda: iter([db_session]),
        ),
        patch.object(db_session, "close"),
        patch(
            "preloop.services.dynamic_fastmcp.crud_mcp_server.get",
            return_value=MagicMock(),
        ),
        patch(
            "preloop.services.dynamic_fastmcp.get_mcp_client_pool", return_value=pool
        ),
    ):
        wrapper = mcp._create_proxied_tool_wrapper(
            "external_write",
            "server",
            str(test_user.account_id),
            "write",
            {"type": "object"},
        )
        result = await wrapper()
    assert "kill switch" in result
    client.call_tool.assert_not_awaited()


async def test_async_approval_replay_blocks_halted_account(
    db_session, test_user, halted_account
):
    mcp = DynamicFastMCP("halt-replay")
    mcp._user_context_provider = lambda: _user_context(test_user)
    with (
        patch(
            "preloop.services.dynamic_fastmcp.get_db",
            side_effect=lambda: iter([db_session]),
        ),
        patch.object(db_session, "close"),
        patch.object(
            mcp.__class__.__bases__[0], "call_tool", new=AsyncMock()
        ) as dispatch,
    ):
        result = await mcp.call_registered_tool_without_policy(
            "get_issue", {}, account_id=str(test_user.account_id)
        )
    assert "kill switch" in result.content[0].text
    dispatch.assert_not_awaited()
