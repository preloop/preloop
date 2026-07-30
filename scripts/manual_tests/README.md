# Manual MCP Testing Scripts

This directory contains manual testing scripts for verifying MCP functionality.

## AI Control Plane Review Guides

- `ai-control-plane-realistic-data.md`: Docker-host recipe for generating realistic control-plane data with managed/containerized flows, the example MCP server, and optional runtime-session token onboarding.
- `claude-code-anthropic-gateway-smoke-test.md`: Manual host-based smoke test for Claude Code style traffic through the Anthropic-compatible gateway.
- `ai-control-plane-ux-integration-review.md`: Current prioritized UX and integration findings for the delivered AI workforce/control-plane surfaces.

## Progress Reporting Tests

### Basic Progress Test (FastMCP Docs Example)

Tests progress reporting with a minimal FastMCP server to verify the feature works independently.

**Start the test server:**
```bash
python scripts/manual_tests/test_basic_progress_server.py
```

**Run the client test:**
```bash
python scripts/manual_tests/test_basic_progress_client.py
```

Expected output: Progress handler should be called for each table backup (5 times total).

### Preloop Progress Test

Progress reporting against a live Preloop server is covered by the unit test
`backend/tests/services/test_initialize_mcp.py::TestRegisteredToolBehaviour::test_report_progress_through_dynamic_fastmcp`
(the old user-visible `test_progress` debug tool was removed). For manual
verification of streaming progress, use `test_approval_streaming.py` or
`test_proxied_approval_streaming.py`.

**Note:** If progress updates are not received with `stateless_http=True`, you may need to investigate whether FastMCP's stateless mode supports SSE streaming for progress notifications. The `json_response=None` parameter should enable this, but it may require verification.

## Troubleshooting

If progress updates don't work with `stateless_http=True`:

1. Check FastMCP's documentation for stateless HTTP limitations
2. Verify that SSE (Server-Sent Events) streaming is enabled
3. Consider whether session state is required for progress notifications
4. Look for any FastMCP configuration options related to progress in stateless mode

If the issue persists, you may need to:
- Review FastMCP's implementation of stateless HTTP + progress
- Check if there's a way to maintain session state only for progress notifications
- Consider alternative approaches (e.g., polling, webhooks)
