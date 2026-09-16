# MCP Implementation

The MCP server is implemented inside the FastAPI app via FastMCP. This chapter covers HTTP transport, dynamic tool filtering, and the MCP request path.

## MCP Implementation
The MCP server is implemented directly within the FastAPI application using a custom
extension of FastMCP. This provides several advantages:
- **HTTP Transport:** Natively supports HTTP-based MCP clients via StreamableHTTP,
enabling secure remote access.
- **Unified Authentication:** Leverages the same JWT authentication as the rest of the
API.
- **Code Reusability:** Directly calls internal services and CRUD operations, reducing
code duplication.
- **Scalability:** Benefits from the same deployment and scaling infrastructure as the
main API.
### Dynamic Tool Filtering
The MCP server implements per-user dynamic tool filtering using `DynamicFastMCP`, a
custom subclass of FastMCP:

**Implementation Details:**
- **`DynamicFastMCP`** (`preloop/services/dynamic_fastmcp.py`): Extends FastMCP and
overrides `_list_tools()` and `_mcp_call_tool()` methods
- **Tool Visibility:** Default tools (get_issue, create_issue, update_issue, search,
add_comment, estimate_compliance, improve_compliance) are only visible when the
authenticated account has one or more trackers configured. A tool whose definition
sets `default_enabled: false`, such as the two compliance tools, additionally needs
an explicit enable on the Tools page or a flow allow-list entry
- **User Context Propagation:** Uses Python's `ContextVar` for async-safe user context
storage across request boundaries
- **Authentication:** `PreloopBearerAuthBackend` validates JWT tokens and injects user
context into the request scope
- **Middleware:** `UserContextMiddleware` extracts authenticated user info and stores
it in a ContextVar for access during tool listing and execution
- **StreamableHTTP Transport:** Uses FastMCP's proven `http_app
(transport="streamable-http")` implementation for bidirectional streaming
- **Endpoint:** Mounted at `/mcp/v1` with full authentication and lifespan management

**Tool Registration:**
All built-in tools are registered in `preloop/services/initialize_mcp.py` using
FastMCP's `@mcp.tool()` decorator, then filtered at runtime based on user context.
Tools whose advertised schema must match the REST catalogue exactly take their
description and JSON schema from `preloop/tools/builtin_defs.py` and are added with
`FunctionTool.from_function`, so `preloop/api/endpoints/tools.py`,
`initialize_mcp.py` and `dynamic_mcp_server.py` cannot drift apart.

### Session search tool

`search_sessions` lets an agent query the runtime session corpus before
repeating work it already did. It is default-off, scoped to the calling agent's
own sessions unless an operator grants the account wide scope, and its answer is
size-capped so a search cannot flood a context window. The tool definition lives
in `preloop/tools/builtin_defs.py`, the scope and cap rules in
`preloop/services/agent_session_search.py`, and the guide is at
[docs/guide/agent-session-search.md](../guide/agent-session-search.md).

### Issue tools

`get_issue` and `update_issue` carry the issue triage surface. There are no separate
triage tools: triage is an option on the standard pair.

`get_issue(issue, include=None)` returns the synchronized issue. `include` accepts:

| Value | Added to the response |
| --- | --- |
| `label_catalog` | `label_catalog`, `complexity_scheme` |
| `revision` | `expected_revision`, `provider_issue` |

Any `include` entry also sets `triage_limitations` and `concurrency`, and makes the
call read the tracker live rather than only the local snapshot. An unknown entry is
a 422. Without `include`, `get_issue` performs no provider read and the triage fields
stay `None`.

`update_issue` keeps its metadata parameters and adds `expected_revision`,
`assessment` and `complexity_label`. A triage write needs both `expected_revision`
and `assessment`, returns the triage receipt instead of the plain update response,
and may not be combined with `description`, `status`, `priority`, `assignee`,
`labels`, `add_reaction` or `remove_reaction`; `title` is allowed. The triage write
requires the `edit_issues` permission and is GitHub and GitLab only.

Triage writes record a server-written receipt on the issue. Flow trigger suppression
keys on that receipt, not on which tools a flow selected, so any flow that produces a
matching write is suppressed and a lookalike event without a receipt is not.

**Benefits:**
- Zero performance overhead for tool registration (happens once at startup)
- Dynamic filtering happens only during tool list requests
- Full compatibility with FastMCP's StreamableHTTP implementation
- Backward compatible with existing authentication infrastructure

## Database session lifetime

Native MCP tool functions own a lazy database session per invocation through
`_with_tool_db`. The dependency generator remains alive until the tool finishes,
and the session closes on success, error, or cancellation. FastMCP invokes these
functions directly, so FastAPI does not manage their database dependencies.

Pull request and comment tools resolve tracker configuration and identifiers,
then close their read transaction before awaiting external tracker operations.
The tracker factory currently constructs clients without network I/O. Proxied
MCP tools likewise snapshot server configuration and release the database session
before connecting or calling a remote tool. Database writes after a provider
response can reopen the invocation's session and are still covered by final
cleanup. Concurrent provider waits therefore do not each reserve a database
connection on these paths.

## MCP Flow (Integrated HTTP)
1.  **MCP Client Request:** An MCP client (e.g., Claude Code) sends a tool request using streamable HTTP transport to the MCP server (e.g., `/mcp/v1`). The request includes the standard MCP payload and an `Authorization: Bearer <token>` header.
2.  **Preloop API Server:**
    *   Authenticates the request using the JWT token.
    *   Routes the request to the appropriate MCP tool endpoint.
    *   Validates the incoming MCP parameters against the Pydantic schema for that tool.
    *   Executes the tool logic, interacting with other Preloop services and `preloop.models` as needed.
    *   Formats the result into the standard MCP JSON response format.
3.  **MCP Client:** Receives the HTTP response containing the tool's output.

The `preloop tools list|describe|exec` CLI commands reuse this same `/mcp/v1` surface, so the backend remains the single source of truth for tool visibility and policy enforcement.
