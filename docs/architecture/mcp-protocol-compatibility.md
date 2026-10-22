# MCP protocol compatibility

Preloop's preferred MCP URL is `/mcp`. Existing `/mcp/v1` configurations use
exactly the same authenticated transport. The app internally rewrites `/mcp`
to `/mcp/v1` before routing; it does not redirect the HTTP request. Neither the
protocol revision nor the Python SDK major version introduces a new URL.

## Tested matrix

The migration uses released `mcp==2.3.0` and `fastmcp==4.1.3`. All three
application dependency locks include those versions and the SDK's `httpx2`
transport. The table distinguishes wire tests from tests using a separate
installation of the previous SDK.

| Protocol revision | Inbound `/mcp` and `/mcp/v1` | Outbound Streamable HTTP |
| --- | --- | --- |
| `2026-07-28` | `server/discover`, direct requests with per-request metadata; no initialization or protocol session | Discovery, version validation, per-request metadata and mirrored headers |
| `2025-11-25` | `initialize`, initialized notification, listing and calls | Discovery rejection followed by initialization; server-issued session IDs, GET stream and DELETE cleanup |
| `2025-06-18` | Same legacy path, with the requested revision retained | Same legacy path with the selected revision retained |
| `2025-03-26` | Same legacy path; absent version header remains compatible | Same legacy path with the selected revision retained |
| `2024-11-05` | SDK compatibility on the Streamable HTTP endpoint | SDK compatibility on Streamable HTTP |

The last row does **not** add the deprecated, separate HTTP+SSE transport from
2024. Preloop already used Streamable HTTP for these SDK compatibility
connections. Neither inbound nor outbound URLs automatically fall back to an
HTTP+SSE endpoint.

Wire tests exercise every row. Separate cross-major process tests use
`mcp==1.30.0` / `fastmcp==3.4.11`: the new outbound pool connects to that
released legacy server, and the legacy client lists and calls tools on both
Preloop URLs. These are synthetic local peers, not claims of compatibility
with every hosted provider.

Preloop retains its existing **stateless legacy HTTP configuration**. An
inbound legacy initialization does not mint a session ID or require sticky
routing; a standalone GET stream is unavailable. Outbound connections still
honor upstream servers that assign legacy session IDs. Each pool operation
owns and closes its transport in the same async task.

## Validation and execution boundary

For modern HTTP requests the SDK validates protocol, method, name, and annotated
parameter headers against the body. Missing or mismatched required headers
return HTTP 400 / JSON-RPC `-32020`. Unsupported versions return HTTP 400 /
`-32022` with `requested` and `supported`; unknown modern methods return HTTP
404 / `-32601`. Modern GET and DELETE return 405; session and resumption
headers do not establish a modern session. Origin protection is explicitly
enabled, including for deployments where FastMCP's default would disable it.

SDK 2.3 routes eras by the HTTP version header. Preloop adds a bounded ASGI
guard so modern body metadata with a missing, duplicated, or legacy header
cannot enter the legacy handler. Authentication remains outside this guard.
The SDK owns the remaining schema validation and request dispatch.

Outbound negotiation uses `ClientSession.discover()` before any operation.
Only an HTTP 400 discovery rejection **without a recognized modern error**
permits fallback to `initialize`. Authentication failures, redirects, outages,
and modern errors do not cause a downgrade. Supported modern version selection
and its bounded discovery retry are provided by the SDK. Legacy era selection
is cached on the pool entry; changing connection configuration invalidates that
entry. Modern connections discover on each short-lived session.

The pool lists modern tool schemas before dispatch so the SDK can emit
`Mcp-Param-*` headers and exclude invalid `x-mcp-header` definitions. It uses the
lower-level session API, which does not automatically replay a tool call after
a header mismatch. HTTP redirects are rejected before the SDK's own redirect
handler can resend a POST. A lost response, auth error, or disconnect never
replays a dispatched tool. OAuth lifecycle refresh, when present, remains
outside this adapter and completes before dispatch.

Modern and legacy calls enter the same `DynamicFastMCP.call_tool` policy and
approval path. Tests assert that policy denial and approval refusal prevent
upstream invocation, and allowed calls dispatch once. SSE progress remains
request-scoped; disconnecting a modern response stream cancels its handler.
The existing OAuth discovery, consent, authorization and approval regression
suites run with the new packages.

## Python API migration

- `streamablehttp_client` is replaced by
  `streamable_http_client(url, http_client=...)`, which yields two streams.
  Preloop owns the `httpx2.AsyncClient` lifetime, timeouts, auth and response
  policy. Non-MCP HTTP services continue to use `httpx`.
- Protocol models use snake_case Python attributes, including `input_schema`,
  `structured_content`, `is_error`, and `mime_type`. JSON keeps its specified
  camelCase names; use `model_dump(by_alias=True)` when building wire blocks.
  SDK v2 ignores unknown annotation fields; the defined tool hints are retained.
- FastMCP's `ToolResult` comes from `fastmcp.tools`. Removed `task_meta`
  arguments are no longer forwarded. Approval progress reads FastMCP's raw
  metadata dictionary. No background task extension is advertised by this change.
- The older low-level adapter uses SDK `add_request_handler` with explicit
  result models. It obtains identity from the authenticated middleware context,
  never from client-supplied `_meta`.
- SDK-native fixture servers use `MCPServer`, with HTTP host/port supplied when
  running the transport. Resource URIs are strings. Python representations use
  snake_case; JSON content blocks retain the protocol spelling.

## Reproducing the cross-major checks

Create a separate environment; do not downgrade the application's environment:

```sh
export PRELOOP_DISABLE_TELEMETRY=true
uv venv /tmp/preloop-mcp-legacy
uv pip install --python /tmp/preloop-mcp-legacy/bin/python \
  'mcp==1.30.0' 'fastmcp==3.4.11'
MCP_LEGACY_PYTHON=/tmp/preloop-mcp-legacy/bin/python \
  .venv/bin/pytest backend/tests/services/test_mcp_released_interop.py
.venv/bin/pytest backend/tests/services/test_mcp_protocol.py \
  backend/tests/services/test_mcp_outbound_protocol.py
```

The process tests listen only on disposable loopback ports. Broader database
regressions require the migrated local PostgreSQL setup described in
[native development](../native-dev.md).

## Normative and released API sources

Audited on 2026-10-22:

- [Protocol revision registry](https://modelcontextprotocol.io/specification/versioning)
- [2026-07-28 versioning](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)
- [2026-07-28 Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)
- [Python SDK migration guide](https://py.sdk.modelcontextprotocol.io/migration/)
- [MCP 2.3.0](https://pypi.org/project/mcp/2.3.0/) and
  [FastMCP 4.1.3](https://pypi.org/project/fastmcp/4.1.3/) package metadata and
  installed source signatures
