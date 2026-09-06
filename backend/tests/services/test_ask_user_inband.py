"""Tests for in-session ask_user delivery and deep links (issue #130).

Covers:
- Token-free console/mobile deep links for a pending question.
- The in-session notice formatting (question, options, agent warning).
- Best-effort Agent Control delivery (local WS, NATS fan-out, offline).
- The pending_approval passthrough on ask_user (async workflows).
- REGRESSION: approve->execute handoff — an approved ask_user replayed via
  get_approval_status must return the human's answer as the tool result
  instead of losing it (the ``_approved_comment_var`` carry-over).
"""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.services import ask_user_inband
from preloop.services.ask_user_inband import (
    QUESTION_NOTICE_KIND,
    approval_console_url,
    approval_mobile_deep_link,
    deliver_question_to_session,
    format_question_notice,
)


@pytest.fixture(autouse=True)
def halt_session():
    """Keep replay unit tests independent of the async database connection pool."""
    session = AsyncMock()
    session.run_sync.return_value = set()

    @asynccontextmanager
    async def factory():
        yield session

    with patch("preloop.models.db.session.get_async_db_session", new=factory):
        yield session


class TestDeepLinks:
    def test_console_url_is_token_free(self):
        request_id = uuid.uuid4()
        url = approval_console_url("https://preloop.example/", request_id)
        assert url == f"https://preloop.example/console/approval/{request_id}"
        assert "token" not in url

    def test_mobile_deep_link_matches_registered_scheme(self):
        request_id = uuid.uuid4()
        assert approval_mobile_deep_link(request_id) == (
            f"preloop://approve/{request_id}"
        )


class TestFormatQuestionNotice:
    def test_ask_user_notice_includes_question_options_and_links(self):
        request_id = uuid.uuid4()
        text = format_question_notice(
            tool_name="ask_user",
            arguments={
                "is_question": True,
                "question": "Ship v2 today?",
                "options": ["yes", "no"],
                "context": "Release window closes at 5pm",
            },
            console_url=f"https://x.example/console/approval/{request_id}",
            mobile_link=f"preloop://approve/{request_id}",
            request_id=request_id,
        )
        assert "Ship v2 today?" in text
        assert "yes | no" in text
        assert "Release window closes at 5pm" in text
        assert f"https://x.example/console/approval/{request_id}" in text
        assert f"preloop://approve/{request_id}" in text
        # The agent must be told not to answer on the human's behalf.
        assert "do NOT answer this yourself" in text

    def test_request_approval_notice_uses_operation(self):
        request_id = uuid.uuid4()
        text = format_question_notice(
            tool_name="request_approval",
            arguments={"operation": "delete prod database"},
            console_url="https://x.example/console/approval/abc",
            mobile_link="preloop://approve/abc",
            request_id=request_id,
        )
        assert "Approval needed" in text
        assert "delete prod database" in text


@pytest.mark.asyncio
class TestDeliverQuestionToSession:
    async def test_returns_false_without_session_or_agent(self):
        result = await deliver_question_to_session(
            account_id=str(uuid.uuid4()),
            approval_request_id=uuid.uuid4(),
            tool_name="ask_user",
            arguments={"question": "?"},
            console_url="https://x/approval/1",
            mobile_link="preloop://approve/1",
            runtime_session_id=None,
            managed_agent_id=None,
        )
        assert result is False

    async def test_never_raises_on_internal_failure(self):
        with patch.object(
            ask_user_inband, "_deliver", new=AsyncMock(side_effect=RuntimeError("boom"))
        ):
            result = await deliver_question_to_session(
                account_id=str(uuid.uuid4()),
                approval_request_id=uuid.uuid4(),
                tool_name="ask_user",
                arguments={"question": "?"},
                console_url="https://x/approval/1",
                mobile_link="preloop://approve/1",
                runtime_session_id=str(uuid.uuid4()),
                managed_agent_id=str(uuid.uuid4()),
            )
        assert result is False

    def _agent(self, account_id):
        return SimpleNamespace(
            id=uuid.uuid4(),
            account_id=account_id,
            runtime_session_id=uuid.uuid4(),
            session_source_type="openclaw",
            session_source_id="openclaw-live",
        )

    def _patched_env(self, agent, *, send_result, publish_result=None):
        manager = MagicMock()
        manager.send_to_agent = AsyncMock(return_value=send_result)
        db = MagicMock()
        return (
            manager,
            db,
            [
                patch.object(
                    ask_user_inband,
                    "get_db_session",
                    return_value=iter([db]),
                ),
                patch.object(
                    ask_user_inband.crud_managed_agent,
                    "get_for_account",
                    return_value=agent,
                ),
                patch.object(
                    ask_user_inband, "_agent_control_manager", return_value=manager
                ),
                patch.object(
                    ask_user_inband,
                    "_publish_command_to_peers",
                    new=AsyncMock(return_value=publish_result),
                ),
                patch.object(
                    ask_user_inband.crud_agent_control_command, "create_command"
                ),
                patch.object(
                    ask_user_inband.crud_agent_control_command, "mark_delivered"
                ),
                patch.object(
                    ask_user_inband.crud_runtime_session_activity,
                    "log_agent_control_message",
                ),
            ],
        )

    async def _run(self, agent, patches, runtime_session_id=None):
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4] as mock_create,
            patches[5] as mock_delivered,
            patches[6] as mock_activity,
        ):
            result = await deliver_question_to_session(
                account_id=str(agent.account_id),
                approval_request_id=uuid.uuid4(),
                tool_name="ask_user",
                arguments={"question": "Ship it?", "is_question": True},
                console_url="https://x/approval/1",
                mobile_link="preloop://approve/1",
                runtime_session_id=runtime_session_id,
                managed_agent_id=str(agent.id),
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        return result, mock_create, mock_delivered, mock_activity

    async def test_local_delivery_marks_delivered_and_logs_turn(self):
        account_id = uuid.uuid4()
        agent = self._agent(account_id)
        manager, db, patches = self._patched_env(agent, send_result=True)
        result, mock_create, mock_delivered, mock_activity = await self._run(
            agent, patches, runtime_session_id=str(agent.runtime_session_id)
        )
        assert result is True
        mock_create.assert_called_once()
        create_kwargs = mock_create.call_args.kwargs
        assert create_kwargs["source"] == "preloop_system"
        envelope = create_kwargs["envelope"]
        assert envelope["name"] == "send_message"
        assert envelope["payload"]["metadata"]["kind"] == QUESTION_NOTICE_KIND
        assert "Ship it?" in envelope["payload"]["text"]
        mock_delivered.assert_called_once()
        mock_activity.assert_called_once()
        assert mock_activity.call_args.kwargs["status"] == "delivered"

    async def test_falls_back_to_nats_when_not_locally_connected(self):
        account_id = uuid.uuid4()
        agent = self._agent(account_id)
        manager, db, patches = self._patched_env(
            agent, send_result=False, publish_result="agent-control.commands.x"
        )
        result, mock_create, mock_delivered, mock_activity = await self._run(
            agent, patches
        )
        assert result is True
        mock_create.assert_called_once()
        mock_delivered.assert_not_called()
        assert mock_activity.call_args.kwargs["status"] == "queued"

    async def test_returns_false_when_agent_offline(self):
        account_id = uuid.uuid4()
        agent = self._agent(account_id)
        agent.runtime_session_id = None
        manager, db, patches = self._patched_env(agent, send_result=False)
        result, mock_create, _, _ = await self._run(agent, patches)
        assert result is False
        mock_create.assert_not_called()


class TestPendingResponseDeepLinks:
    """The async pending_approval payload must carry actionable deep links."""

    def test_async_response_shape(self):
        # Mirrors the payload construction in approval_helper (kept in sync
        # by test_ask_user_pending_passthrough below exercising the parser).
        request_id = uuid.uuid4()
        console_url = approval_console_url("https://p.example", request_id)
        mobile_link = approval_mobile_deep_link(request_id)
        assert console_url.endswith(f"/console/approval/{request_id}")
        assert mobile_link == f"preloop://approve/{request_id}"


def _user_ctx(**overrides):
    base = {
        "account_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "username": "u",
        "runtime_session_id": None,
        "managed_agent_id": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
class TestAskUserToolBehaviour:
    async def _ask_user_fn(self):
        from preloop.services.initialize_mcp import initialize_mcp_with_tools

        mcp = initialize_mcp_with_tools()
        tool = await mcp.get_tool("ask_user")
        return tool.fn

    def _workflow_patches(self):
        workflow = SimpleNamespace(id=uuid.uuid4())
        return [
            patch(
                "preloop.services.dynamic_fastmcp_http.get_current_user_context",
                return_value=_user_ctx(),
            ),
            patch(
                "preloop.models.db.session.get_db_session",
                return_value=iter([MagicMock()]),
            ),
            patch(
                "preloop.models.crud.crud_approval_workflow.get_default",
                return_value=workflow,
            ),
        ]

    async def test_pending_approval_payload_passes_through_untouched(self):
        """Async workflows: the agent must receive the deep-link payload."""
        fn = await self._ask_user_fn()
        pending = (
            '{"status": "pending_approval", "request_id": "abc", '
            '"approval_console_url": "https://p.example/console/approval/abc", '
            '"approval_mobile_link": "preloop://approve/abc"}'
        )
        p1, p2, p3 = self._workflow_patches()
        with (
            p1,
            p2,
            p3,
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(return_value=(False, pending)),
            ),
        ):
            result = await fn(question="Ship it?")
        assert result == pending

    async def test_declined_still_reports_no_answer(self):
        fn = await self._ask_user_fn()
        p1, p2, p3 = self._workflow_patches()
        with (
            p1,
            p2,
            p3,
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(return_value=(False, "Tool execution declined")),
            ),
        ):
            result = await fn(question="Ship it?")
        assert result == "No answer provided: Tool execution declined"

    async def test_answer_is_returned(self):
        fn = await self._ask_user_fn()
        p1, p2, p3 = self._workflow_patches()
        with (
            p1,
            p2,
            p3,
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(return_value=(True, "blue")),
            ),
        ):
            result = await fn(question="Favourite colour?")
        assert result == "User answered: blue"


@pytest.mark.asyncio
class TestAskUserApprovalAuditTrailer:
    """The answered/declined returns carry the platform approval id (and
    approver identity/timestamp when known) so agents transcribing human
    decisions — e.g. interactive waiver collection — can reference the
    governed approval record instead of asserting one."""

    async def _ask_user_fn(self):
        from preloop.services.initialize_mcp import initialize_mcp_with_tools

        mcp = initialize_mcp_with_tools()
        tool = await mcp.get_tool("ask_user")
        return tool.fn

    def _workflow_patches(self):
        workflow = SimpleNamespace(id=uuid.uuid4())
        return [
            patch(
                "preloop.services.dynamic_fastmcp_http.get_current_user_context",
                return_value=_user_ctx(),
            ),
            patch(
                "preloop.models.db.session.get_db_session",
                return_value=iter([MagicMock()]),
            ),
            patch(
                "preloop.models.crud.crud_approval_workflow.get_default",
                return_value=workflow,
            ),
        ]

    def _set_meta(self, **overrides):
        from preloop.services import approval_helper

        meta = {
            "request_id": "11111111-2222-3333-4444-555555555555",
            "status": "approved",
            "resolved_at": "2026-08-24T12:00:00",
            "responded_by": "a-user-id",
        }
        meta.update(overrides)
        approval_helper._last_approval_meta_var.set(meta)

    async def test_answer_carries_approval_trailer(self):
        fn = await self._ask_user_fn()
        p1, p2, p3 = self._workflow_patches()
        with (
            p1,
            p2,
            p3,
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(return_value=(True, "accept: CVE-2023-38545")),
            ),
        ):
            self._set_meta()
            result = await fn(question="Waive which findings?")
        assert result.startswith("User answered: accept: CVE-2023-38545\n")
        assert "approval_id: 11111111-2222-3333-4444-555555555555" in result
        assert "answered_by: a-user-id" in result
        assert "answered_at: 2026-08-24T12:00:00" in result
        assert "status: approved" in result

    async def test_timeout_still_reports_the_approval_id(self):
        """Fail-closed transcription: even a non-answer references the
        approval record that expired."""
        fn = await self._ask_user_fn()
        p1, p2, p3 = self._workflow_patches()
        with (
            p1,
            p2,
            p3,
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(
                    return_value=(
                        False,
                        "Approval timeout: request expired after 300s",
                    )
                ),
            ),
        ):
            self._set_meta(status="expired", resolved_at=None, responded_by=None)
            result = await fn(question="Waive which findings?")
        assert result.startswith(
            "No answer provided: Approval timeout: request expired after 300s"
        )
        assert "approval_id: 11111111-2222-3333-4444-555555555555" in result
        assert "status: expired" in result
        assert "answered_by" not in result

    async def test_meta_is_consumed_not_reused(self):
        fn = await self._ask_user_fn()
        p1, _, p3 = self._workflow_patches()
        with (
            p1,
            # Fresh session iterator per call: this test calls ask_user twice.
            patch(
                "preloop.models.db.session.get_db_session",
                side_effect=lambda: iter([MagicMock()]),
            ),
            p3,
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(return_value=(True, "blue")),
            ),
        ):
            self._set_meta()
            first = await fn(question="Favourite colour?")
            second = await fn(question="Favourite colour?")
        assert "approval_id" in first
        assert second == "User answered: blue"

    async def test_no_meta_keeps_legacy_format(self):
        fn = await self._ask_user_fn()
        p1, p2, p3 = self._workflow_patches()
        with (
            p1,
            p2,
            p3,
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(return_value=(True, "blue")),
            ),
        ):
            result = await fn(question="Favourite colour?")
        assert result == "User answered: blue"

    async def test_stale_meta_from_prior_approval_is_not_reused(self):
        """REGRESSION (review): `require_approval` gates every approval-required
        tool but only `ask_user` consumed the shared ContextVar, and several
        approval paths return without setting fresh metadata. The metadata is
        now scoped to the current call — cleared on entry — so an early-
        returning `require_approval` (here: the bypass path) can never leave a
        previous approval's id behind to be misattributed to a later answer."""
        from preloop.services import approval_helper
        from preloop.services.dynamic_fastmcp import _bypass_approval_var

        self._set_meta()
        bypass_token = _bypass_approval_var.set(True)
        try:
            approved, _ = await approval_helper.require_approval(
                tool_name="ask_user",
                tool_source="builtin",
                account_id=str(uuid.uuid4()),
                arguments={"question": "Favourite colour?"},
            )
        finally:
            _bypass_approval_var.reset(bypass_token)
        assert approved is True
        assert approval_helper.consume_last_approval_meta() is None


@pytest.mark.asyncio
class TestApprovedAskUserReplayRegression:
    """REGRESSION (approve->execute handoff): replaying an approved ask_user
    must return the approver's comment (the human's answer) as the tool
    result instead of dropping it.

    get_approval_status() sets ``_bypass_approval_var`` and
    ``_approved_comment_var`` before re-executing the tool; require_approval
    short-circuits and must surface the stored answer to ask_user.
    """

    async def test_bypass_returns_stored_answer_for_ask_user(self):
        from preloop.services.approval_helper import require_approval
        from preloop.services.dynamic_fastmcp import (
            _approved_comment_var,
            _bypass_approval_var,
        )

        bypass_token = _bypass_approval_var.set(True)
        comment_token = _approved_comment_var.set("42, obviously")
        try:
            approved, answer = await require_approval(
                tool_name="ask_user",
                tool_source="builtin",
                account_id=str(uuid.uuid4()),
                arguments={"question": "?"},
                return_comment_on_approve=True,
            )
        finally:
            _bypass_approval_var.reset(bypass_token)
            _approved_comment_var.reset(comment_token)
        assert approved is True
        assert answer == "42, obviously"

    async def test_bypass_keeps_empty_comment_for_gating_tools(self):
        """Tool-gating callers must keep getting (True, "") during replay."""
        from preloop.services.approval_helper import require_approval
        from preloop.services.dynamic_fastmcp import (
            _approved_comment_var,
            _bypass_approval_var,
        )

        bypass_token = _bypass_approval_var.set(True)
        comment_token = _approved_comment_var.set("side-channel comment")
        try:
            approved, error = await require_approval(
                tool_name="create_issue",
                tool_source="builtin",
                account_id=str(uuid.uuid4()),
                arguments={},
            )
        finally:
            _bypass_approval_var.reset(bypass_token)
            _approved_comment_var.reset(comment_token)
        assert approved is True
        assert error == ""

    async def test_replayed_ask_user_returns_answer_as_tool_result(self):
        """End-to-end over the tool fn: the replay path (bypass + stored
        comment, as set by get_approval_status) yields the user's answer."""
        from preloop.services.dynamic_fastmcp import (
            _approved_comment_var,
            _bypass_approval_var,
        )
        from preloop.services.initialize_mcp import initialize_mcp_with_tools

        mcp = initialize_mcp_with_tools()
        tool = await mcp.get_tool("ask_user")
        workflow = SimpleNamespace(id=uuid.uuid4())

        bypass_token = _bypass_approval_var.set(True)
        comment_token = _approved_comment_var.set("Option B")
        try:
            with (
                patch(
                    "preloop.services.dynamic_fastmcp_http.get_current_user_context",
                    return_value=_user_ctx(),
                ),
                patch(
                    "preloop.models.db.session.get_db_session",
                    return_value=iter([MagicMock()]),
                ),
                patch(
                    "preloop.models.crud.crud_approval_workflow.get_default",
                    return_value=workflow,
                ),
            ):
                result = await tool.fn(question="A or B?", options=["A", "B"])
        finally:
            _bypass_approval_var.reset(bypass_token)
            _approved_comment_var.reset(comment_token)
        assert result == "User answered: Option B"


@pytest.mark.asyncio
async def test_replayed_answer_stays_blocked_during_tools_halt(halt_session):
    from preloop.services.approval_helper import require_approval
    from preloop.services.dynamic_fastmcp import (
        _approved_comment_var,
        _bypass_approval_var,
    )

    halt_session.run_sync.return_value = {"tools"}
    bypass_token = _bypass_approval_var.set(True)
    comment_token = _approved_comment_var.set("Option B")
    try:
        approved, answer = await require_approval(
            tool_name="ask_user",
            tool_source="builtin",
            account_id=str(uuid.uuid4()),
            arguments={"question": "?"},
            return_comment_on_approve=True,
        )
    finally:
        _bypass_approval_var.reset(bypass_token)
        _approved_comment_var.reset(comment_token)
    assert approved is False
    assert "kill switch" in answer
    assert "Option B" not in answer
