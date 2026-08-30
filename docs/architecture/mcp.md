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
estimate_compliance, improve_compliance) are only visible when the authenticated
account has one or more trackers configured
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

**Benefits:**
- Zero performance overhead for tool registration (happens once at startup)
- Dynamic filtering happens only during tool list requests
- Full compatibility with FastMCP's StreamableHTTP implementation
- Backward compatible with existing authentication infrastructure

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
