"""
MCP integration tests.

Validates that the MCP StreamableHTTP endpoint is reachable and responds
correctly to an MCP initialize handshake.  Uses the MCPTestClient helper
which wraps the Python MCP SDK's streamable HTTP transport.

Required env vars (set by CI):
    PRELOOP_TEST_URL      – base URL of the deployed instance
    PRELOOP_TEST_API_KEY  – valid API key
"""

import os

import pytest

from tests.integration.mcp_client import MCPTestClient

PRELOOP_URL = os.getenv("PRELOOP_TEST_URL", "").rstrip("/")
PRELOOP_API_KEY = os.getenv("PRELOOP_TEST_API_KEY", "")


def _skip_if_missing_env():
    if not PRELOOP_URL or not PRELOOP_API_KEY:
        pytest.skip("PRELOOP_TEST_URL and PRELOOP_TEST_API_KEY required")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_mcp_initialize():
    """MCP handshake succeeds and returns server info."""
    _skip_if_missing_env()

    async with MCPTestClient(PRELOOP_URL, PRELOOP_API_KEY) as client:
        # If we get here, initialize() succeeded (it raises on failure)
        assert client.session is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_mcp_list_tools():
    """MCP server exposes at least one tool after initialization."""
    _skip_if_missing_env()

    async with MCPTestClient(PRELOOP_URL, PRELOOP_API_KEY) as client:
        tools = await client.session.list_tools()
        tool_names = [t.name for t in tools.tools]

        print(f"Available MCP tools ({len(tool_names)}): {tool_names}")
        assert len(tool_names) > 0, "Expected at least one MCP tool"

        # Tools that are always present (no tracker required)
        expected_tools = {"ask_user", "request_approval"}
        present = expected_tools & set(tool_names)
        assert present, (
            f"None of the expected core tools {expected_tools} found in {tool_names}"
        )
