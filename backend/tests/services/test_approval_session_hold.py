"""Regression tests: approval waits must not hold a DB session.

A deployment observed SQLAlchemy QueuePool exhaustion under concurrent
pending approvals: the approval paths opened a session before creating the
approval request and kept it checked out across the whole human-decision
polling loop (up to the workflow timeout). These tests instrument
``get_async_db_session`` and assert that no session is held at any point
where the code sleeps waiting for a decision.
"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from preloop.services.approval_wrapper import with_approval

pytestmark = pytest.mark.asyncio

REAL_SLEEP = asyncio.sleep


class SessionLedger:
    """Track how many instrumented DB sessions are currently held.

    A session counts as held from context-manager entry until it is
    released, either by exiting the context manager or by an explicit
    ``close()`` (both return the pooled connection in production).
    """

    def __init__(self) -> None:
        self.held = 0
        self.opened = 0
        self.at_sleep: list[int] = []
        self.halted = False

    def make_session_factory(self):
        ledger = self

        @asynccontextmanager
        async def _fake_session():
            ledger.opened += 1
            ledger.held += 1
            released = False

            def _release() -> None:
                nonlocal released
                if not released:
                    ledger.held -= 1
                    released = True

            session = MagicMock()
            session.execute = AsyncMock()
            session.run_sync = AsyncMock(
                side_effect=lambda callback: {"tools"} if ledger.halted else set()
            )
            session.commit = AsyncMock()
            session.rollback = AsyncMock()
            session.refresh = AsyncMock()
            session.add = MagicMock()

            async def _close() -> None:
                _release()

            session.close = _close
            try:
                yield session
            finally:
                _release()

        return _fake_session

    def make_sleep(self, on_sleep=None):
        """Fake asyncio.sleep that records held sessions and yields control."""
        ledger = self

        async def _fake_sleep(_seconds) -> None:
            ledger.at_sleep.append(ledger.held)
            if on_sleep is not None:
                on_sleep()
            await REAL_SLEEP(0)

        return _fake_sleep


def make_request(status: str, comment=None):
    request = MagicMock()
    request.id = uuid4()
    request.status = status
    request.approver_comment = comment
    request.resolved_at = None
    request.responses = []
    request.expires_at = None
    return request


def make_workflow(timeout_seconds: int = 300):
    workflow = MagicMock()
    workflow.id = uuid4()
    workflow.approval_type = "slack"
    workflow.channel = "approvals"
    workflow.user = None
    workflow.name = "test-workflow"
    workflow.timeout_seconds = timeout_seconds
    workflow.escalation_user_ids = None
    workflow.escalation_team_ids = None
    workflow.async_approval_enabled = False
    workflow.approvals_required = 1
    workflow.approver_user_ids = None
    return workflow


@pytest.fixture
def mock_user_context():
    ctx = MagicMock()
    ctx.account_id = uuid4()
    ctx.user_id = uuid4()
    ctx.execution_id = None
    return ctx


def wrapper_patches(ledger, poll_state, workflow, mock_user_context, fake_sleep):
    """Common patch set for driving with_approval into its polling loop."""
    config = MagicMock()
    config.id = uuid4()
    config.approval_workflow_id = workflow.id

    approval_service = AsyncMock()
    approval_service.create_and_notify = AsyncMock(return_value=poll_state["pending"])
    approval_service.update_approval_request = AsyncMock()

    async def _get_request(_db, request_id=None):
        return poll_state["current"]

    return (
        patch(
            "preloop.services.dynamic_fastmcp_http.get_current_user_context",
            return_value=mock_user_context,
        ),
        patch(
            "preloop.models.db.session.get_async_db_session",
            new=ledger.make_session_factory(),
        ),
        patch(
            "preloop.models.crud.tool_configuration."
            "get_tool_config_by_name_and_source_async",
            new_callable=AsyncMock,
            return_value=config,
        ),
        patch(
            "preloop.services.policy_evaluator.evaluate_policy_async",
            new_callable=AsyncMock,
            return_value=("require_approval", workflow.id, "gated by test rule"),
        ),
        patch(
            "preloop.models.crud.approval_workflow.get_approval_workflow_async",
            new_callable=AsyncMock,
            return_value=workflow,
        ),
        patch(
            "preloop.services.approval_service.ApprovalService",
            return_value=approval_service,
        ),
        patch(
            "preloop.models.crud.approval_request.get_approval_request_async",
            new=AsyncMock(side_effect=_get_request),
        ),
        patch("preloop.services.approval_wrapper.asyncio.sleep", new=fake_sleep),
    )


async def run_with_approval(
    ledger, poll_state, workflow, user_context, fake_sleep, concurrency=1
):
    """Run one or more wrapped calls under a single shared patch stack."""

    async def tool_func(**kwargs):
        return "tool_result"

    wrapped = with_approval(tool_func)
    patches = wrapper_patches(ledger, poll_state, workflow, user_context, fake_sleep)
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        results = await asyncio.gather(
            *(wrapped(arg1="value") for _ in range(concurrency))
        )
    return results if concurrency > 1 else results[0]


class TestWithApprovalSessionRelease:
    """The outer session must be returned before any approval-wait sleep."""

    async def test_outer_session_released_before_first_poll_sleep(
        self, mock_user_context
    ):
        ledger = SessionLedger()
        pending = make_request("pending")
        approved = make_request("approved")
        approved.id = pending.id
        poll_state = {"pending": pending, "current": pending}

        def resolve():
            poll_state["current"] = approved

        fake_sleep = ledger.make_sleep(on_sleep=resolve)
        result = await run_with_approval(
            ledger, poll_state, make_workflow(), mock_user_context, fake_sleep
        )

        assert ledger.at_sleep, "polling loop never slept"
        assert ledger.at_sleep[0] == 0, (
            "a DB session was still checked out when the approval wait began; "
            f"held sessions at first poll sleep: {ledger.at_sleep[0]}"
        )
        # Approved requests must execute the wrapped tool.
        assert result == "tool_result"

    async def test_concurrent_pending_approvals_hold_no_sessions(
        self, mock_user_context
    ):
        n_tasks = 5
        ledger = SessionLedger()
        pending = make_request("pending")
        approved = make_request("approved")
        approved.id = pending.id
        poll_state = {"pending": pending, "current": pending}

        def resolve_after_all_waiting():
            # Let every concurrent call reach its polling wait at least once
            # before the approval resolves.
            if len(ledger.at_sleep) >= n_tasks:
                poll_state["current"] = approved

        fake_sleep = ledger.make_sleep(on_sleep=resolve_after_all_waiting)
        results = await run_with_approval(
            ledger,
            poll_state,
            make_workflow(),
            mock_user_context,
            fake_sleep,
            concurrency=n_tasks,
        )

        assert len(ledger.at_sleep) >= n_tasks
        assert max(ledger.at_sleep) == 0, (
            "concurrent pending approvals accumulated checked-out sessions "
            f"during their waits: peak {max(ledger.at_sleep)}"
        )
        assert all(result == "tool_result" for result in results)


class TestWithApprovalDecisionPaths:
    """Approved, declined, and expired paths keep their caller behavior."""

    async def test_declined_returns_decline_message(self, mock_user_context):
        ledger = SessionLedger()
        pending = make_request("pending")
        declined = make_request("declined", comment="not allowed")
        declined.id = pending.id
        poll_state = {"pending": pending, "current": pending}

        def resolve():
            poll_state["current"] = declined

        fake_sleep = ledger.make_sleep(on_sleep=resolve)
        result = await run_with_approval(
            ledger, poll_state, make_workflow(), mock_user_context, fake_sleep
        )

        assert result == "Tool execution declined: not allowed"
        assert max(ledger.at_sleep) == 0

    async def test_cancelled_returns_cancel_message(self, mock_user_context):
        ledger = SessionLedger()
        pending = make_request("pending")
        cancelled = make_request("cancelled")
        cancelled.id = pending.id
        poll_state = {"pending": pending, "current": pending}

        def resolve():
            poll_state["current"] = cancelled

        fake_sleep = ledger.make_sleep(on_sleep=resolve)
        result = await run_with_approval(
            ledger, poll_state, make_workflow(), mock_user_context, fake_sleep
        )

        assert result == "Tool execution cancelled"

    async def test_expired_returns_timeout_message(self, mock_user_context):
        ledger = SessionLedger()
        pending = make_request("pending")
        poll_state = {"pending": pending, "current": pending}

        fake_sleep = ledger.make_sleep()
        result = await run_with_approval(
            ledger,
            poll_state,
            make_workflow(timeout_seconds=4),
            mock_user_context,
            fake_sleep,
        )

        assert result.startswith("Approval timeout:")
        assert "expired" in result
        assert max(ledger.at_sleep) == 0


class TestRequireApprovalSessionRelease:
    """approval_helper.require_approval must not hold a session while polling."""

    @pytest.mark.parametrize("halt_during_wait", [False, True])
    async def test_session_released_before_first_poll_sleep(self, halt_during_wait):
        from preloop.services.approval_helper import require_approval

        ledger = SessionLedger()
        workflow = make_workflow()
        pending = make_request("pending")
        approved = make_request("approved")
        approved.id = pending.id
        poll_state = {"current": pending}

        def resolve():
            ledger.halted = halt_during_wait
            poll_state["current"] = approved

        async def _get_request(_db, request_id=None):
            return poll_state["current"]

        config = MagicMock()
        config.id = uuid4()
        config.approval_workflow_id = workflow.id

        approval_service = AsyncMock()
        approval_service.create_and_notify = AsyncMock(return_value=pending)

        with (
            patch(
                "preloop.models.db.session.get_async_db_session",
                new=ledger.make_session_factory(),
            ),
            patch(
                "preloop.models.crud.tool_configuration."
                "get_tool_config_by_name_and_source_async",
                new_callable=AsyncMock,
                return_value=config,
            ),
            patch(
                "preloop.models.crud.approval_workflow.get_approval_workflow_async",
                new_callable=AsyncMock,
                return_value=workflow,
            ),
            patch(
                "preloop.services.approval_service.ApprovalService",
                return_value=approval_service,
            ),
            patch(
                "preloop.models.crud.approval_request.get_approval_request_async",
                new=AsyncMock(side_effect=_get_request),
            ),
            patch(
                "preloop.services.approval_helper.asyncio.sleep",
                new=ledger.make_sleep(on_sleep=resolve),
            ),
        ):
            approved_result, error = await require_approval(
                tool_name="test_tool",
                tool_source="mcp",
                account_id=str(uuid4()),
                arguments={"arg": "value"},
                ctx=None,
                workflow_id=str(workflow.id),
            )

        assert approved_result is not halt_during_wait
        assert ("kill switch" in error) if halt_during_wait else error == ""
        assert ledger.at_sleep, "polling loop never slept"
        assert max(ledger.at_sleep) == 0, (
            "require_approval held a DB session during the approval wait: "
            f"{max(ledger.at_sleep)} session(s) checked out at sleep"
        )


class TestWaitForApprovalSessionRelease:
    """ApprovalService.wait_for_approval must poll with fresh sessions."""

    async def test_polls_with_fresh_sessions_and_none_held_at_sleep(self):
        from preloop.services.approval_service import ApprovalService

        ledger = SessionLedger()
        pending = make_request("pending")
        approved = make_request("approved")
        approved.id = pending.id
        poll_state = {"current": pending}

        def resolve():
            poll_state["current"] = approved

        async def _get_request(_db, request_id=None):
            return poll_state["current"]

        # The service is constructed with a session that is closed before the
        # wait begins; polling must not rely on it.
        service = ApprovalService(MagicMock(), "http://test")

        with (
            patch(
                "preloop.models.db.session.get_async_db_session",
                new=ledger.make_session_factory(),
            ),
            # approval_service binds the CRUD function at module import time,
            # so patch that binding rather than the crud module.
            patch(
                "preloop.services.approval_service.get_approval_request_for_update_async",
                new=AsyncMock(side_effect=_get_request),
            ),
            patch(
                "preloop.services.approval_service.asyncio.sleep",
                new=ledger.make_sleep(on_sleep=resolve),
            ),
        ):
            final_request = await service.wait_for_approval(
                pending.id, poll_interval=2.0
            )

        assert final_request.status == "approved"
        assert ledger.opened >= 1, (
            "wait_for_approval never opened a fresh polling session; it is "
            "still polling on the caller's long-lived session"
        )
        assert ledger.at_sleep, "polling loop never slept"
        assert max(ledger.at_sleep) == 0, (
            "wait_for_approval held a DB session during the approval wait"
        )


class TestDynamicMcpServerSessionRelease:
    """_request_and_wait_for_approval must close its session before waiting."""

    async def test_no_session_held_while_waiting(self):
        from preloop.services.dynamic_mcp_server import DynamicMCPServer

        ledger = SessionLedger()
        workflow = make_workflow()
        pending = make_request("pending")
        approved = make_request("approved")
        approved.id = pending.id

        config = MagicMock()
        config.id = uuid4()
        config.approval_workflow_id = workflow.id

        held_during_wait: list[int] = []

        async def _wait(request_id, poll_interval=1.0):
            held_during_wait.append(ledger.held)
            return approved

        approval_service = AsyncMock()
        approval_service.create_and_notify = AsyncMock(return_value=pending)
        approval_service.wait_for_approval = AsyncMock(side_effect=_wait)

        user_context = MagicMock()
        user_context.account_id = uuid4()

        with (
            patch(
                "preloop.models.db.session.get_async_db_session",
                new=ledger.make_session_factory(),
            ),
            patch(
                "preloop.models.crud.tool_configuration."
                "get_tool_config_by_name_and_source_async",
                new_callable=AsyncMock,
                return_value=config,
            ),
            patch(
                "preloop.models.crud.approval_workflow.get_approval_workflow_async",
                new_callable=AsyncMock,
                return_value=workflow,
            ),
            patch(
                "preloop.services.approval_service.ApprovalService",
                return_value=approval_service,
            ),
        ):
            await DynamicMCPServer._request_and_wait_for_approval(
                MagicMock(),
                user_context,
                "test_tool",
                {"arg": "value"},
            )

        assert held_during_wait == [0], (
            "a DB session was still checked out while waiting for the "
            f"approval decision: {held_during_wait}"
        )
