"""Connection probes expose useful causes without leaking endpoint credentials."""

from unittest.mock import AsyncMock, patch

import httpx2 as httpx
import pytest

from preloop.services.mcp_client_pool import MCPClient, describe_mcp_connection_error


@pytest.mark.asyncio
async def test_connect_unwraps_nested_task_group_failure() -> None:
    """The initial connect probe must preserve the actual connection failure."""
    leaf = httpx.ConnectError("connection refused")
    streams = AsyncMock()
    streams.__aenter__.side_effect = ExceptionGroup(
        "unhandled errors in a TaskGroup", [ExceptionGroup("nested", [leaf])]
    )
    client = MCPClient(url="https://example.com/mcp")
    with patch(
        "preloop.services.mcp_client_pool.streamable_http_client", return_value=streams
    ):
        with pytest.raises(httpx.ConnectError) as raised:
            await client.connect()
    assert raised.value is leaf
    assert not client.is_connected()


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "authentication"),
        (403, "authentication"),
        (404, "endpoint not found"),
        (503, "availability"),
    ],
)
def test_http_diagnostics_omit_credentials(status: int, expected: str) -> None:
    """HTTP status explains remediation while the secret-bearing URL is omitted."""
    request = httpx.Request(
        "GET",
        "https://user:secret@example.com/mcp?token=private",
        headers={"Authorization": "Bearer private"},
    )
    response = httpx.Response(status, request=request)
    error = httpx.HTTPStatusError(
        "request token=private failed", request=request, response=response
    )
    diagnostic = describe_mcp_connection_error(ExceptionGroup("opaque", [error]))
    assert expected in diagnostic.lower()
    assert str(status) in diagnostic
    assert "private" not in diagnostic
    assert "secret" not in diagnostic
    assert "example.com" not in diagnostic


@pytest.mark.parametrize(
    "error,expected",
    [
        (httpx.ConnectError("token=private"), "reachability from Preloop"),
        (httpx.ReadTimeout("token=private"), "timed out"),
        (ValueError("token=private"), "MCP protocol"),
    ],
)
def test_non_http_diagnostics_omit_raw_exception(
    error: Exception, expected: str
) -> None:
    diagnostic = describe_mcp_connection_error(ExceptionGroup("opaque", [error]))
    assert expected in diagnostic
    assert "private" not in diagnostic
    assert "TaskGroup" not in diagnostic


@pytest.mark.parametrize("code", [-32601, -32020, -32022, -32000])
def test_rpc_diagnostic_preserves_code_without_untrusted_message(code: int) -> None:
    from mcp import MCPError

    error = MCPError(code=code, message="token=private", data={"secret": "private"})
    diagnostic = describe_mcp_connection_error(error)
    assert str(code) in diagnostic
    assert "JSON-RPC" in diagnostic
    assert "private" not in diagnostic
    assert "secret" not in diagnostic


def test_validation_diagnostic_omits_inputs_and_server_controlled_paths() -> None:
    from pydantic import ValidationError

    error = ValidationError.from_exception_data(
        "private-model",
        [{"type": "int_parsing", "loc": ("private-field",), "input": "private-value"}],
    )
    diagnostic = describe_mcp_connection_error(error)
    assert "invalid MCP response" in diagnostic
    assert "1 validation error" in diagnostic
    assert "private" not in diagnostic
