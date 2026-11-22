# Proxied Tool Integration Tests

## test_proxied_tool_consecutive_calls.py

Regression test for Issue #3: Proxied tool calls failing on second invocation.

### What it tests

1. **Consecutive calls test**: Calls a proxied tool 3 times in a row to verify that async context cleanup doesn't break subsequent calls
2. **Concurrent calls test**: Launches 5 concurrent calls to verify that AsyncExitStack cleanup doesn't interfere with parallel operations

### Prerequisites

1. Preloop server running on `http://localhost:8001`
2. External MCP server with a test tool registered and connected to Preloop
3. Environment variables set:
   - `PRELOOP_TOKEN`: Your API key/token for authentication
   - `TEST_MCP_TOOL`: Name of the proxied tool to test (default: `get_random_number`)
   - `RUN_INTEGRATION_TESTS=1`: Enable integration tests

### Running the tests

**With pytest:**
```bash
export PRELOOP_TOKEN="your-api-key"
export TEST_MCP_TOOL="get_random_number"  # Optional, defaults to this
export RUN_INTEGRATION_TESTS=1

pytest tests/integration/test_proxied_tool_consecutive_calls.py -v
```

**Standalone (for debugging):**
```bash
export PRELOOP_TOKEN="your-api-key"
export TEST_MCP_TOOL="get_random_number"
export RUN_INTEGRATION_TESTS=1

python tests/integration/test_proxied_tool_consecutive_calls.py
```

### Expected behavior

**Before fix (with manual cleanup):**
- First call succeeds
- Second call sometimes/always fails with errors related to session cleanup
- Error messages about closed streams or invalid sessions

**After fix (with AsyncExitStack):**
- All 3 consecutive calls succeed
- All 5 concurrent calls succeed
- No cleanup-related errors

### Test setup example

If you need to set up a test MCP server for this:

```python
# test_mcp_server.py
from fastmcp import FastMCP

mcp = FastMCP("test-server")


@mcp.tool()
async def get_random_number() -> str:
    import random

    return str(random.randint(1, 100))


if __name__ == "__main__":
    mcp.run(transport="streamable-http", port=8002)
```

Then configure Preloop to connect to this server and run the tests.

### Why this test is important

This test prevents regressions of the async context cleanup issue. Without proper cleanup order:
- Streams might be closed before sessions
- Context managers might not exit cleanly
- Resources might leak or become invalid

The AsyncExitStack ensures proper LIFO (Last In, First Out) cleanup order, which is critical for nested async contexts.
