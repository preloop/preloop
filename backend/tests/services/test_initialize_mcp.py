"""Tests for MCP server initialization and tool registration."""

import logging
import gc
import weakref
import json
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from preloop.services.dynamic_fastmcp import DynamicFastMCP
from preloop.models import models
from preloop.services.initialize_mcp import (
    CancelScopeErrorFilter,
    initialize_mcp_with_tools,
)


def make_record(levelname="ERROR", msg="session crashed", exc_info=None):
    record = logging.LogRecord(
        name="mcp.server.streamable_http_manager",
        level=getattr(logging, levelname),
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=exc_info,
    )
    return record


class FakeExceptionGroupError(Exception):
    """Version-independent stand-in for an ExceptionGroup."""

    def __init__(self, message, exceptions):
        super().__init__(message)
        self.exceptions = exceptions


class TestCancelScopeErrorFilter:
    def setup_method(self):
        self.filter = CancelScopeErrorFilter()

    def test_keeps_non_error_record(self):
        record = make_record(levelname="INFO", msg="something crashed")
        assert self.filter.filter(record) is True

    def test_keeps_error_without_crashed(self):
        record = make_record(msg="some other error")
        assert self.filter.filter(record) is True

    def test_keeps_crashed_error_without_exc_info(self):
        record = make_record(msg="session crashed")
        assert self.filter.filter(record) is True

    def test_suppresses_direct_cancel_scope_error(self):
        exc = RuntimeError(
            "Attempted to exit a cancel scope that isn't the current one"
        )
        record = make_record(exc_info=(RuntimeError, exc, None))
        assert self.filter.filter(record) is False

    def test_keeps_unrelated_exception(self):
        exc = ValueError("totally unrelated failure")
        record = make_record(exc_info=(ValueError, exc, None))
        assert self.filter.filter(record) is True

    def test_detects_within_exception_group(self):
        inner = RuntimeError("Attempted to exit a cancel scope foo")
        group = FakeExceptionGroupError("group", [ValueError("x"), inner])
        assert self.filter._contains_cancel_scope_error(group) is True

    def test_detects_via_cause(self):
        inner = RuntimeError("Attempted to exit a cancel scope")
        outer = RuntimeError("wrapper")
        outer.__cause__ = inner
        assert self.filter._contains_cancel_scope_error(outer) is True

    def test_detects_via_context(self):
        inner = RuntimeError("Attempted to exit a cancel scope")
        outer = RuntimeError("wrapper")
        outer.__context__ = inner
        assert self.filter._contains_cancel_scope_error(outer) is True

    def test_no_false_positive(self):
        assert self.filter._contains_cancel_scope_error(ValueError("normal")) is False


EXPECTED_TOOLS = {
    "get_issue",
    "create_issue",
    "update_issue",
    "search",
    "estimate_compliance",
    "improve_compliance",
    "request_approval",
    "permission_prompt",
    "add_comment",
    "update_comment",
    "get_pull_request",
    "update_pull_request",
    "create_pull_request",
    "get_approval_status",
    "resolve_sbom_upstreams",
    "send_note",
    "run_flow",
    "get_execution",
}


@pytest.fixture
def mcp_server():
    return initialize_mcp_with_tools()


class TestInitializeMcpWithTools:
    def test_unused_servers_can_be_collected(self) -> None:
        """Framework callable caches must not own a previous MCP server."""
        references = [weakref.ref(initialize_mcp_with_tools()) for _ in range(3)]

        gc.collect()

        assert all(reference() is None for reference in references)

    def test_returns_dynamic_fastmcp(self, mcp_server):
        assert isinstance(mcp_server, DynamicFastMCP)

    def test_installs_cancel_scope_filter(self):
        logger = logging.getLogger("mcp.server.streamable_http_manager")
        before = [f for f in logger.filters if isinstance(f, CancelScopeErrorFilter)]
        initialize_mcp_with_tools()
        after = [f for f in logger.filters if isinstance(f, CancelScopeErrorFilter)]
        assert len(after) > len(before)

    @pytest.mark.asyncio
    async def test_all_expected_tools_registered(self, mcp_server):
        for name in EXPECTED_TOOLS:
            tool = await mcp_server.get_tool(name)
            assert tool is not None
            assert tool.name == name

    @pytest.mark.asyncio
    async def test_unknown_tool_not_registered(self, mcp_server):
        assert await mcp_server.get_tool("definitely_not_a_tool") is None


@pytest.mark.asyncio
class TestRegisteredToolBehaviour:
    async def test_approval_replay_uses_the_owning_live_server(
        self, mcp_server: DynamicFastMCP
    ) -> None:
        """Creating another server must not redirect an earlier callback."""
        other_server = initialize_mcp_with_tools()
        tool = await mcp_server.get_tool("get_approval_status")
        request_id = uuid4()
        account_id = uuid4()
        approval = models.ApprovalRequest(
            id=request_id,
            account_id=account_id,
            status="approved",
            tool_name="get_issue",
            tool_args={"issue": "ABC-1"},
            responses=[],
        )
        approval_result = MagicMock()
        approval_result.scalar_one_or_none.return_value = approval
        events_result = MagicMock()
        events_result.scalars.return_value = []
        db = AsyncMock()
        db.execute.side_effect = [approval_result, events_result, approval_result]

        @asynccontextmanager
        async def session() -> AsyncIterator[AsyncMock]:
            yield db

        with (
            patch(
                "preloop.services.dynamic_fastmcp_http.get_current_user_context",
                return_value=SimpleNamespace(account_id=str(account_id)),
            ),
            patch("preloop.models.db.session.get_async_db_session", new=session),
            patch.object(
                mcp_server,
                "call_registered_tool_without_policy",
                new=AsyncMock(return_value="Replay completed"),
            ) as replay,
            patch.object(
                other_server,
                "call_registered_tool_without_policy",
                new=AsyncMock(),
            ) as other_replay,
            patch(
                "preloop.services.approval_service._log_approval_tool_executed_async"
            ),
        ):
            result = json.loads(await tool.fn(request_id=str(request_id)))

        assert result["tool_result"] == {"text": "Replay completed"}
        # get_issue is built in: replayed under its own name. The namespaced
        # name is refused as "not available" (no exception), so trying it
        # first never reached the built-in tool (live rehearsal 2026-10-09).
        replay.assert_awaited_once_with(
            "get_issue",
            {"issue": "ABC-1"},
            account_id=str(account_id),
        )
        other_replay.assert_not_awaited()

    async def test_approval_replay_uses_namespaced_name_for_proxied_tool(
        self, mcp_server: DynamicFastMCP
    ) -> None:
        """An external MCP tool is registered under the account namespace."""
        tool = await mcp_server.get_tool("get_approval_status")
        request_id = uuid4()
        account_id = uuid4()
        internal = f"account_{str(account_id).replace('-', '_')}_deploy"
        approval = models.ApprovalRequest(
            id=request_id,
            account_id=account_id,
            status="approved",
            tool_name="deploy",
            tool_args={"env": "dev"},
            responses=[],
        )
        approval_result = MagicMock()
        approval_result.scalar_one_or_none.return_value = approval
        events_result = MagicMock()
        events_result.scalars.return_value = []
        db = AsyncMock()
        db.execute.side_effect = [approval_result, events_result, approval_result]

        @asynccontextmanager
        async def session() -> AsyncIterator[AsyncMock]:
            yield db

        mcp_server._registered_proxied_tools.add(internal)
        try:
            with (
                patch(
                    "preloop.services.dynamic_fastmcp_http.get_current_user_context",
                    return_value=SimpleNamespace(account_id=str(account_id)),
                ),
                patch("preloop.models.db.session.get_async_db_session", new=session),
                patch.object(
                    mcp_server,
                    "call_registered_tool_without_policy",
                    new=AsyncMock(return_value="Deployed"),
                ) as replay,
                patch(
                    "preloop.services.approval_service._log_approval_tool_executed_async"
                ),
            ):
                json.loads(await tool.fn(request_id=str(request_id)))
        finally:
            mcp_server._registered_proxied_tools.discard(internal)

        replay.assert_awaited_once_with(
            internal, {"env": "dev"}, account_id=str(account_id)
        )

    async def test_approval_replay_refuses_tool_outside_flow_allow_list(
        self, mcp_server: DynamicFastMCP
    ) -> None:
        """get_approval_status is exposed to every allow-listed flow so a
        parked run can finish an approved call; it must not become a way to
        run a tool the flow was never allowed to call."""
        tool = await mcp_server.get_tool("get_approval_status")
        request_id = uuid4()
        account_id = uuid4()
        approval = models.ApprovalRequest(
            id=request_id,
            account_id=account_id,
            status="approved",
            tool_name="create_issue",
            tool_args={"title": "x"},
            responses=[],
        )
        approval_result = MagicMock()
        approval_result.scalar_one_or_none.return_value = approval
        events_result = MagicMock()
        events_result.scalars.return_value = []
        db = AsyncMock()
        db.execute.side_effect = [approval_result, events_result, approval_result]

        @asynccontextmanager
        async def session() -> AsyncIterator[AsyncMock]:
            yield db

        with (
            patch(
                "preloop.services.dynamic_fastmcp_http.get_current_user_context",
                return_value=SimpleNamespace(
                    account_id=str(account_id),
                    allowed_flow_tools=["get_pull_request", "update_pull_request"],
                ),
            ),
            patch("preloop.models.db.session.get_async_db_session", new=session),
            patch.object(
                mcp_server,
                "call_registered_tool_without_policy",
                new=AsyncMock(return_value="should not run"),
            ) as replay,
        ):
            result = json.loads(await tool.fn(request_id=str(request_id)))

        replay.assert_not_awaited()
        assert "not in this flow's allowed tools" in result["tool_execution_error"]
        assert approval.tool_result is None

    async def _fn(self, mcp_server, name):
        tool = await mcp_server.get_tool(name)
        return tool.fn

    async def test_no_user_context_returns_error(self, mcp_server):
        fn = await self._fn(mcp_server, "get_issue")
        with patch(
            "preloop.services.dynamic_fastmcp_http.get_current_user_context",
            return_value=None,
        ):
            result = await fn(issue="ABC-1")
        assert result == "Error: No user context available"

    async def test_approval_denied_returns_error(self, mcp_server):
        fn = await self._fn(mcp_server, "get_issue")
        user_ctx = SimpleNamespace(account_id=str(uuid4()), username="u")
        with (
            patch(
                "preloop.services.dynamic_fastmcp_http.get_current_user_context",
                return_value=user_ctx,
            ),
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(return_value=(False, "Approval denied by policy")),
            ),
        ):
            result = await fn(issue="ABC-1")
        assert result == "Approval denied by policy"

    async def test_approval_granted_calls_router(self, mcp_server):
        fn = await self._fn(mcp_server, "get_issue")
        user_ctx = SimpleNamespace(account_id=str(uuid4()), username="u")
        router_result = MagicMock()
        router_result.model_dump_json.return_value = '{"issue": "ok"}'
        with (
            patch(
                "preloop.services.dynamic_fastmcp_http.get_current_user_context",
                return_value=user_ctx,
            ),
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(return_value=(True, None)),
            ),
            patch(
                "preloop.api.endpoints.mcp.get_issue",
                new=AsyncMock(return_value=router_result),
            ) as mock_router,
        ):
            result = await fn(issue="ABC-1")
        assert result == '{"issue": "ok"}'
        mock_router.assert_awaited_once_with("ABC-1", include=None)

    async def test_resolve_sbom_upstreams_no_user_context(self, mcp_server):
        fn = await self._fn(mcp_server, "resolve_sbom_upstreams")
        with patch(
            "preloop.services.dynamic_fastmcp_http.get_current_user_context",
            return_value=None,
        ):
            result = await fn(components=[{"name": "JPEGDEC", "version": "1.2.8"}])
        assert result == "Error: No user context available"

    async def test_resolve_sbom_upstreams_calls_service(self, mcp_server):
        fn = await self._fn(mcp_server, "resolve_sbom_upstreams")
        user_ctx = SimpleNamespace(account_id=str(uuid4()), username="u")
        report = {
            "resolved": [],
            "unresolved": [],
            "stats": {
                "requested": 0,
                "resolved": 0,
                "unresolved": 0,
                "by_source": {},
            },
            "registry_status": {
                "arduino_index": "not queried",
                "platformio": "not queried",
            },
        }
        with (
            patch(
                "preloop.services.dynamic_fastmcp_http.get_current_user_context",
                return_value=user_ctx,
            ),
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(return_value=(True, None)),
            ),
            patch(
                "preloop.services.sbom_upstream_resolver.resolve_components",
                new=AsyncMock(return_value=report),
            ) as mock_resolve,
        ):
            result = await fn(components=[{"name": "JPEGDEC", "version": "1.2.8"}])
        import json

        assert json.loads(result) == report
        mock_resolve.assert_awaited_once_with([{"name": "JPEGDEC", "version": "1.2.8"}])

    async def test_resolve_sbom_upstreams_invalid_input_returns_error(self, mcp_server):
        """Malformed component entries surface as an error string, never
        an exception (the tool is called from unattended flow runs)."""
        fn = await self._fn(mcp_server, "resolve_sbom_upstreams")
        user_ctx = SimpleNamespace(account_id=str(uuid4()), username="u")
        with (
            patch(
                "preloop.services.dynamic_fastmcp_http.get_current_user_context",
                return_value=user_ctx,
            ),
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(return_value=(True, None)),
            ),
        ):
            result = await fn(components=[{"name": "JPEGDEC"}])
        assert result.startswith("Error:")
        assert "version" in result

    async def test_send_note_no_user_context(self, mcp_server):
        """No identity, no note: the tool cannot guess who is writing."""
        fn = await self._fn(mcp_server, "send_note")
        with patch(
            "preloop.services.dynamic_fastmcp_http.get_current_user_context",
            return_value=None,
        ):
            result = await fn(text="hi", agent_id=str(uuid4()))
        assert result == "Error: No user context available"

    async def test_send_note_signs_with_the_calling_identity(self, mcp_server):
        """The author is read off the call's own context, never an argument."""
        import json

        fn = await self._fn(mcp_server, "send_note")
        caller_agent_id = uuid4()
        target_agent_id = uuid4()
        user_ctx = SimpleNamespace(
            account_id=str(uuid4()),
            username="u",
            managed_agent_id=str(caller_agent_id),
            runtime_session_id=None,
            api_key_id=None,
            flow_execution_id=None,
            runtime_principal_name="Reviewer",
        )
        service_result = {"ok": True, "note": {"note_id": "abc123"}}
        with (
            patch(
                "preloop.services.dynamic_fastmcp_http.get_current_user_context",
                return_value=user_ctx,
            ),
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(return_value=(True, None)),
            ),
            patch(
                "preloop.models.db.session.get_db_session",
                return_value=iter([MagicMock()]),
            ),
            patch(
                "preloop.services.agent_send_note.send_note_from_agent",
                return_value=service_result,
            ) as mock_send,
        ):
            result = await fn(text="Rebase first.", agent_id=str(target_agent_id))

        assert json.loads(result) == service_result
        kwargs = mock_send.call_args.kwargs
        assert kwargs["account_id"] == user_ctx.account_id
        assert kwargs["author_agent_id"] == caller_agent_id
        assert kwargs["agent_id"] == str(target_agent_id)
        assert kwargs["text"] == "Rebase first."

    async def test_send_note_takes_its_lineage_from_the_context(self, mcp_server):
        """The run the note is scoped to is the one the platform recorded.

        An agent that could name its own execution could name any execution,
        and the note scope (#637) is keyed on exactly that id, so it comes
        off the authenticated context and never off an argument.
        """
        fn = await self._fn(mcp_server, "send_note")
        calling_execution = str(uuid4())
        user_ctx = SimpleNamespace(
            account_id=str(uuid4()),
            username="u",
            managed_agent_id=str(uuid4()),
            runtime_session_id=str(uuid4()),
            api_key_id=str(uuid4()),
            flow_execution_id=calling_execution,
            runtime_principal_type="managed_agent",
            runtime_principal_id=str(uuid4()),
            runtime_principal_name="Reviewer",
        )
        target_execution_id = str(uuid4())
        with (
            patch(
                "preloop.services.dynamic_fastmcp_http.get_current_user_context",
                return_value=user_ctx,
            ),
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(return_value=(True, None)),
            ),
            patch(
                "preloop.models.db.session.get_db_session",
                return_value=iter([MagicMock()]),
            ),
            patch(
                "preloop.services.agent_send_note.send_note_from_agent",
                return_value={"ok": True, "note": {"note_id": "abc123"}},
            ) as mock_send,
        ):
            await fn(text="Rebase first.", execution_id=target_execution_id)

        kwargs = mock_send.call_args.kwargs
        assert kwargs["author_execution_id"] == calling_execution
        # The execution the agent named is the target, not the author.
        assert kwargs["execution_id"] == target_execution_id
        assert kwargs["subject_context"]["api_key_id"] == user_ctx.api_key_id
        assert kwargs["subject_context"]["runtime_session_id"] == (
            user_ctx.runtime_session_id
        )
        assert kwargs["subject_context"]["managed_agent_id"] == (
            user_ctx.managed_agent_id
        )

    async def test_send_note_returns_a_refusal_as_json(self, mcp_server):
        """A refusal reaches the model as data it can act on, not a stack."""
        import json

        fn = await self._fn(mcp_server, "send_note")
        user_ctx = SimpleNamespace(
            account_id=str(uuid4()),
            username="u",
            managed_agent_id=str(uuid4()),
            runtime_session_id=None,
            api_key_id=None,
            flow_execution_id=None,
            runtime_principal_name="Reviewer",
        )
        refusal = {
            "ok": False,
            "error": {"code": "invalid_target", "message": "Name exactly one target"},
        }
        with (
            patch(
                "preloop.services.dynamic_fastmcp_http.get_current_user_context",
                return_value=user_ctx,
            ),
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(return_value=(True, None)),
            ),
            patch(
                "preloop.models.db.session.get_db_session",
                return_value=iter([MagicMock()]),
            ),
            patch(
                "preloop.services.agent_send_note.send_note_from_agent",
                return_value=refusal,
            ),
        ):
            result = await fn(text="hi")

        assert json.loads(result) == refusal

    async def test_send_note_schema_matches_the_shared_definition(self, mcp_server):
        """One schema for the REST catalogue and the MCP registration."""
        from preloop.tools.builtin_defs import SEND_NOTE_TOOL

        tool = await mcp_server.get_tool("send_note")
        assert tool.parameters == SEND_NOTE_TOOL["schema"]
        assert tool.description == SEND_NOTE_TOOL["description"]

    async def test_removed_test_progress_tool_not_registered(self, mcp_server):
        """The old test_progress debug tool must no longer ship to users."""
        assert await mcp_server.get_tool("test_progress") is None

    async def test_report_progress_through_dynamic_fastmcp(self):
        """ctx.report_progress works for tools registered on DynamicFastMCP.

        Replaces the coverage previously provided by the user-visible
        test_progress tool with a throwaway tool registered only here.
        """
        from typing import Optional

        from fastmcp import Context

        mcp = DynamicFastMCP("test-progress-coverage")

        @mcp.tool()
        async def _progress_probe(count: int = 2, ctx: Optional[Context] = None) -> str:
            if not ctx:
                return "no context"
            for i in range(count):
                await ctx.report_progress(
                    progress=i + 1, total=count, message=f"step {i + 1}"
                )
            return f"done {count}"

        tool = await mcp.get_tool("_progress_probe")
        assert tool is not None

        ctx = MagicMock()
        ctx.report_progress = AsyncMock()
        result = await tool.fn(count=3, ctx=ctx)

        assert result == "done 3"
        assert ctx.report_progress.await_count == 3

    async def test_request_approval_no_user_context(self, mcp_server):
        fn = await self._fn(mcp_server, "request_approval")
        with patch(
            "preloop.services.dynamic_fastmcp_http.get_current_user_context",
            return_value=None,
        ):
            result = await fn(operation="deploy", context="prod", reasoning="needed")
        assert result == "Error: No user context available"

    async def test_request_approval_schema_matches_catalog(self, mcp_server):
        import inspect

        from preloop.tools.builtin_defs import REQUEST_APPROVAL_TOOL

        tool = await mcp_server.get_tool("request_approval")
        catalog = REQUEST_APPROVAL_TOOL["schema"]
        runtime = {
            name for name in inspect.signature(tool.fn).parameters if name != "ctx"
        }
        assert runtime == set(catalog["properties"])
        assert catalog["required"] == ["operation", "context", "reasoning"]
        assert "publication_candidates" not in catalog["required"]
        items = catalog["properties"]["publication_candidates"]["items"]
        assert items["required"] == ["repository_url", "branch", "base", "head_sha"]
        assert tool.description == REQUEST_APPROVAL_TOOL["description"]

    async def test_invalid_publication_candidates_do_not_call_require_approval(
        self, mcp_server
    ):
        fn = await self._fn(mcp_server, "request_approval")
        user_context = MagicMock()
        user_context.account_id = str(uuid4())
        user_context.username = "tester"
        with (
            patch(
                "preloop.services.dynamic_fastmcp_http.get_current_user_context",
                return_value=user_context,
            ),
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(side_effect=AssertionError("must not create a row")),
            ),
        ):
            result = await fn(
                operation="publish",
                context="prod",
                reasoning="needed",
                publication_candidates=[
                    {
                        "repository_url": "https://github.com/example/firmware.git",
                        "branch": "preloop/change",
                        "base": "main",
                        "head_sha": "not-a-sha",
                    }
                ],
            )
        assert result.startswith("Error: publication_candidates")

    async def test_async_pending_payload_passes_through_untouched(self, mcp_server):
        fn = await self._fn(mcp_server, "request_approval")
        workflow = MagicMock()
        workflow.id = uuid4()
        pending = (
            '{"status": "pending_approval", "request_id": "abc", '
            '"approval_console_url": "https://p.example/console/approval/abc"}'
        )
        user_context = MagicMock()
        user_context.account_id = str(uuid4())
        user_context.username = "tester"
        with (
            patch(
                "preloop.services.dynamic_fastmcp_http.get_current_user_context",
                return_value=user_context,
            ),
            patch(
                "preloop.models.db.session.get_db_session",
                return_value=iter([MagicMock()]),
            ),
            patch(
                "preloop.models.crud.crud_approval_workflow.get_default",
                return_value=workflow,
            ),
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(return_value=(False, pending)),
            ),
        ):
            result = await fn(operation="deploy", context="prod", reasoning="needed")
        assert result == pending
