"""Tests for MCP server initialization and tool registration."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from preloop.services.dynamic_fastmcp import DynamicFastMCP
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
    "test_progress",
    "request_approval",
    "add_comment",
    "update_comment",
    "get_pull_request",
    "update_pull_request",
    "create_pull_request",
    "get_approval_status",
}


@pytest.fixture
def mcp_server():
    return initialize_mcp_with_tools()


class TestInitializeMcpWithTools:
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
        mock_router.assert_awaited_once_with("ABC-1")

    async def test_test_progress_without_context(self, mcp_server):
        fn = await self._fn(mcp_server, "test_progress")
        result = await fn(count=3, ctx=None)
        assert "No Context available" in result

    async def test_test_progress_reports_progress(self, mcp_server):
        fn = await self._fn(mcp_server, "test_progress")
        ctx = MagicMock()
        ctx.report_progress = AsyncMock()
        result = await fn(count=2, ctx=ctx)
        assert "Processed 2 items" in result
        assert ctx.report_progress.await_count == 2

    async def test_request_approval_no_user_context(self, mcp_server):
        fn = await self._fn(mcp_server, "request_approval")
        with patch(
            "preloop.services.dynamic_fastmcp_http.get_current_user_context",
            return_value=None,
        ):
            result = await fn(operation="deploy", context="prod", reasoning="needed")
        assert result == "Error: No user context available"
