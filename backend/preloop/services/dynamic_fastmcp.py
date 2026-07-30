"""Dynamic FastMCP extension that provides per-user tool filtering.

This extends FastMCP to support dynamic tool lists based on authenticated user context
while keeping FastMCP's proven StreamableHTTP transport implementation.

Phase 1B: Added support for proxied tools from external MCP servers.
"""

import asyncio
import copy
import logging
import uuid
from contextvars import ContextVar
from typing import Callable, Dict, Optional

from fastmcp import FastMCP
from fastmcp.tools import Tool

from preloop.services.dynamic_mcp_server import (
    UserContext,
    has_tracker,
    get_tracker_types,
)
from preloop.services.mcp_client_pool import get_mcp_client_pool
from preloop.models.crud import crud_mcp_server, crud_tool_configuration
from preloop.models.db.session import get_db_session as get_db
from preloop.api.endpoints.tools import BUILTIN_TOOLS
from preloop.services.subject_governance import is_tool_enabled_for_subject

logger = logging.getLogger(__name__)

# Context variable to pass policy evaluation results from _call_tool() to
# individual tool wrappers (which call require_approval()).
# When set, require_approval() should use this workflow_id instead of looking
# it up from the tool configuration.
_rule_workflow_id_var: ContextVar[Optional[str]] = ContextVar(
    "_rule_workflow_id_var", default=None
)

# Context variable to pass a unique correlation_id from _call_tool() through
# to all audit-logging helpers (policy_evaluator, approval_helper, tool execution).
# Every audit log entry from the same tool invocation shares this ID so the
# frontend can group them into a single timeline entry.
_correlation_id_var: ContextVar[Optional[str]] = ContextVar(
    "_correlation_id_var", default=None
)

# Context variable to pass justification extracted from tool arguments
# through to require_approval(). The justification is injected into the tool
# schema by _list_tools() and popped from arguments in _call_tool() before
# the actual tool function is invoked.
_justification_var: ContextVar[Optional[str]] = ContextVar(
    "_justification_var", default=None
)

# Context variable to bypass approval checks during re-execution of an
# already-approved async tool call.  Set by get_approval_status() before
# replaying the tool so that require_approval() returns (True, "") immediately.
_bypass_approval_var: ContextVar[bool] = ContextVar(
    "_bypass_approval_var", default=False
)

# Context variable to ensure internal proxied tool names are only called via proxy translation
_is_proxy_translation_var: ContextVar[bool] = ContextVar(
    "_is_proxy_translation_var", default=False
)


def _strip_fields_from_json_text(text: str, dropped_fields: set[str]) -> Optional[str]:
    """Strip top-level fields from a JSON object or list-of-objects string.

    Args:
        text: Raw text that may contain a JSON document.
        dropped_fields: Top-level keys to remove from each result object.

    Returns:
        The re-serialized JSON with the requested keys removed, or ``None`` if
        the text is not JSON, is not a dict/list-of-dicts, or nothing changed.
    """
    import json

    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None

    changed = False

    def _strip_obj(obj: object) -> object:
        nonlocal changed
        if isinstance(obj, dict):
            for field in dropped_fields:
                if field in obj:
                    del obj[field]
                    changed = True
        return obj

    if isinstance(parsed, dict):
        _strip_obj(parsed)
    elif isinstance(parsed, list):
        for item in parsed:
            _strip_obj(item)
    else:
        # Scalars / strings: nothing to strip.
        return None

    if not changed:
        return None
    return json.dumps(parsed)


def apply_output_filters(
    result_items: list,
    *,
    account_id: str,
    tool_name: str,
    server_name: Optional[str] = None,
    managed_agent_id: Optional[str] = None,
) -> list:
    """Strip operator-configured fields from a tool result before the agent sees it.

    Looks up enabled :class:`ToolOutputFilter` rows that match the current
    call and removes the union of their ``dropped_fields`` from every
    JSON-parseable result content block (a dict, or a list of dicts). This
    trims wasted context tokens without changing the upstream tool.

    The function is fully defensive: if no filters match, content is not JSON,
    parsing fails, or anything raises, the original ``result_items`` is
    returned unchanged. It never raises into the proxy path.

    Args:
        result_items: The MCP content blocks returned by the upstream tool.
            Each item is expected to expose a ``.text`` attribute holding the
            block's text (e.g. ``TextContent``); other items pass through.
        account_id: Owning account id for the current call.
        tool_name: Name of the tool that produced the result.
        server_name: MCP server name for the current call, if known.
        managed_agent_id: Managed agent id for the current call, if known.

    Returns:
        The (possibly trimmed) list of content blocks.
    """
    try:
        if not result_items:
            return result_items

        from preloop.models.crud import crud_tool_output_filter
        from preloop.models.db.session import get_db_session as _get_db

        db = next(_get_db())
        try:
            filters = crud_tool_output_filter.list_active_for_tool(
                db,
                account_id=account_id,
                tool_name=tool_name,
                server_name=server_name,
                managed_agent_id=managed_agent_id,
            )
        finally:
            db.close()

        if not filters:
            return result_items

        dropped_fields: set[str] = set()
        for flt in filters:
            for field in flt.dropped_fields or []:
                if isinstance(field, str):
                    dropped_fields.add(field)

        if not dropped_fields:
            return result_items

        applied = False
        for item in result_items:
            text = getattr(item, "text", None)
            if not isinstance(text, str):
                continue
            stripped = _strip_fields_from_json_text(text, dropped_fields)
            if stripped is not None:
                item.text = stripped
                applied = True

        if applied:
            logger.info(
                "Applied output filter(s) to tool '%s' (server=%s, account=%s): "
                "stripped fields %s",
                tool_name,
                server_name,
                account_id,
                sorted(dropped_fields),
            )

        return result_items
    except Exception as exc:  # noqa: BLE001 - never break the proxy path
        logger.debug(
            "apply_output_filters skipped for tool '%s' due to error: %s",
            tool_name,
            exc,
        )
        return result_items


class DynamicFastMCP(FastMCP):
    """FastMCP extension with per-user dynamic tool filtering.

    This subclass overrides FastMCP's tool listing and execution to provide
    per-request filtering based on authenticated user context. It keeps all
    of FastMCP's StreamableHTTP transport functionality while adding dynamic
    tool visibility.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._user_context_provider: Optional[Callable[[], Optional[UserContext]]] = (
            None
        )
        # Track proxied tool -> server mapping for routing
        self._proxied_tool_servers: Dict[str, str] = {}
        self._proxied_tool_server_names: Dict[str, str] = {}
        # Track registered proxied tools to avoid re-registration
        self._registered_proxied_tools: set = set()
        logger.info("DynamicFastMCP initialized")

    def set_user_context_provider(self, provider: Callable[[], Optional[UserContext]]):
        """Set a function that provides current user context.

        This function will be called during tool listing and execution to get
        the current authenticated user's context.

        Args:
            provider: Function that returns UserContext or None
        """
        self._user_context_provider = provider
        logger.info("User context provider registered")

    async def list_tools(self, *, run_middleware: bool = True) -> list[Tool]:
        """Override FastMCP's list_tools to filter based on user context.

        This method is called by FastMCP's protocol handler to get the list
        of available tools. We filter the full tool list based on the current
        user's context.

        Phase 1B: Now includes proxied tools from external MCP servers.

        Args:
            run_middleware: Whether to run middleware (passed to super)

        Returns:
            List of tools available to the current user
        """
        logger.info("!!! list_tools called - ENTRY POINT !!!")
        # Get current user context
        user_context = self._get_current_user_context()
        logger.info(f"!!! Got user context: {user_context} !!!")

        if not user_context:
            logger.warning("No user context available, returning empty tool list")
            return []

        logger.info(
            f"Filtering tools for user {user_context.username}, has_tracker={user_context.has_tracker}"
        )

        # Start with empty list
        available_tools = []

        # Add built-in tools (filtered by tracker requirements metadata)
        default_tools = [
            t for t in await super().list_tools(run_middleware=run_middleware)
        ]
        # Filter out internal proxied tool names (they start with "account_")
        builtin_tools = [t for t in default_tools if not t.name.startswith("account_")]

        builtin_meta = {t["name"]: t for t in BUILTIN_TOOLS}

        filtered_tools = []
        for tool in builtin_tools:
            meta = builtin_meta.get(tool.name)
            if not meta:
                filtered_tools.append(tool)
                continue

            required_types = meta.get("required_tracker_types") or []
            requires_tracker = meta.get("requires_tracker", False)

            if requires_tracker and not user_context.has_tracker:
                logger.info(
                    f"Skipping tool '{tool.name}' (requires tracker but none configured)"
                )
                continue

            if required_types and not any(
                t in user_context.tracker_types for t in required_types
            ):
                logger.info(
                    f"Skipping tool '{tool.name}' (requires tracker types {required_types}, have {user_context.tracker_types})"
                )
                continue

            filtered_tools.append(tool)

        available_tools.extend(filtered_tools)
        logger.info(
            f"Added {len(filtered_tools)} default tools after tracker-type filtering "
            f"(filtered out {len(builtin_tools) - len(filtered_tools)} tracker-specific tools, "
            f"{len(default_tools) - len(builtin_tools)} internal names)"
        )

        if user_context.mcp_tools_cache is not None:
            logger.info("Returning cached tools from UserContext")
            return user_context.mcp_tools_cache

        # Add proxied tools from external MCP servers (Phase 1B)
        # Now with dynamic registration for streaming approval support
        proxied_tools_data = []
        justification_modes = {}
        builtin_enabled_map = {}
        account_meta = {}

        try:
            # One shared DB session for all list_tools metadata lookups so we
            # do not open three concurrent connections per tools/list call.
            def _fetch_list_tools_metadata():
                db = next(get_db())
                try:
                    from preloop.services.mcp_tool_discovery import (
                        _get_proxied_tools_sync,
                    )
                    from preloop.models.crud import crud_account

                    proxied = _get_proxied_tools_sync(user_context.account_id, db)
                    configs = crud_tool_configuration.get_multi_by_account(
                        db, account_id=str(user_context.account_id), limit=1000
                    )
                    modes = {
                        tc.tool_name: tc.justification_mode
                        for tc in configs
                        if tc.justification_mode in ("optional", "required")
                    }
                    enabled = {
                        tc.tool_name: tc.is_enabled
                        for tc in configs
                        if tc.tool_source == "builtin"
                    }
                    acc = crud_account.get(db, id=user_context.account_id)
                    meta = getattr(acc, "meta_data", {}) or {}
                    return proxied, modes, enabled, meta
                finally:
                    db.close()

            logger.info("Fetching DB metadata for list_tools...")
            loop = asyncio.get_event_loop()
            (
                proxied_tools_data,
                justification_modes,
                builtin_enabled_map,
                account_meta,
            ) = await asyncio.wait_for(
                loop.run_in_executor(None, _fetch_list_tools_metadata),
                timeout=30,
            )
            logger.info(f"Fetched {len(proxied_tools_data)} proxied tools")

            # Dynamically register wrapper functions for proxied tools
            proxied_tool_map = {}  # Track original_name -> internal_name mapping

            for mcp_server, mcp_tool in proxied_tools_data:
                # Create internal name with namespace (sanitize account_id)
                safe_account_id = user_context.account_id.replace("-", "_")
                internal_name = f"account_{safe_account_id}_{mcp_tool.name}"
                proxied_tool_map[mcp_tool.name] = (
                    internal_name,
                    mcp_tool,
                    mcp_server,
                )

                # Only register if not already registered
                if internal_name not in self._registered_proxied_tools:
                    logger.info(
                        f"Dynamically registering proxied tool: {mcp_tool.name} "
                        f"(internal: {internal_name})"
                    )

                    # Create wrapper function with approval and streaming
                    wrapper = self._create_proxied_tool_wrapper(
                        tool_name=mcp_tool.name,
                        server_id=str(mcp_server.id),
                        account_id=user_context.account_id,
                        description=mcp_tool.description or "",
                        input_schema=mcp_tool.input_schema,
                    )

                    # Register with FastMCP using @mcp.tool() decorator
                    self.tool()(wrapper)

                    # Track as registered
                    self._registered_proxied_tools.add(internal_name)

                # Always track the mapping for name translation
                self._proxied_tool_servers[mcp_tool.name] = str(mcp_server.id)
                self._proxied_tool_server_names[mcp_tool.name] = mcp_server.name

            # Now get all registered tools and map back to original names
            all_registered = await super().list_tools(run_middleware=run_middleware)
            logger.info(
                f"Total registered tools after dynamic registration: {len(all_registered)}"
            )

            for original_name, (
                internal_name,
                mcp_tool,
                mcp_server,
            ) in proxied_tool_map.items():
                # Check if this tool was registered
                if any(t.name == internal_name for t in all_registered):
                    # Add with original name for client visibility
                    tool = Tool(
                        name=original_name,  # Client sees original name
                        description=mcp_tool.description or "",
                        parameters=mcp_tool.input_schema,
                    )
                    available_tools.append(tool)
                    logger.info(
                        f"Exposing proxied tool: {original_name} (internal: {internal_name}) mcp_server: {mcp_server}"
                    )
                else:
                    logger.warning(
                        f"Proxied tool {internal_name} was not found in registered tools!"
                    )

            logger.info(
                f"Added {len(proxied_tool_map)} proxied tools to available list"
            )

        except Exception as e:
            logger.error(f"Error loading proxied tools: {e}", exc_info=True)
            # Continue with just default tools

        # ── Enforce builtin tool enable/disable configuration ────────────
        # A builtin tool is exposed only if:
        #   - an explicit ToolConfiguration row enables it, or
        #   - no config row exists AND its metadata does not default-disable it.
        # Flow executions are exempt: they carry an explicit allow-list
        # (allowed_flow_tools) that opts into exactly the tools the flow
        # needs, so account-level disables must not break preset flows.
        if user_context.allowed_flow_tools is None:
            before_count = len(available_tools)
            enabled_filtered = []
            for tool in available_tools:
                meta = builtin_meta.get(tool.name)
                if meta is None:
                    # Not a builtin tool (proxied); handled elsewhere
                    enabled_filtered.append(tool)
                    continue
                explicit = builtin_enabled_map.get(tool.name)
                if explicit is not None:
                    if explicit:
                        enabled_filtered.append(tool)
                    else:
                        logger.info(
                            f"Skipping builtin tool '{tool.name}' "
                            "(disabled by tool configuration)"
                        )
                elif meta.get("default_enabled", True):
                    enabled_filtered.append(tool)
                else:
                    logger.info(
                        f"Skipping builtin tool '{tool.name}' "
                        "(default-disabled, no explicit enable configured)"
                    )
            available_tools = enabled_filtered
            if before_count != len(available_tools):
                logger.info(
                    f"Builtin enable/disable filter removed "
                    f"{before_count - len(available_tools)} tools"
                )

        # SECURITY: Enforce flow-specific tool restrictions if present
        # This provides defense-in-depth: even if an agent is compromised,
        # it cannot call tools outside the flow's allowed list
        if user_context.allowed_flow_tools is not None:
            original_count = len(available_tools)
            available_tools = [
                tool
                for tool in available_tools
                if tool.name in user_context.allowed_flow_tools
            ]
            logger.info(
                f"Flow execution restriction: filtered {original_count} tools down to "
                f"{len(available_tools)} allowed tools for flow execution "
                f"{user_context.flow_execution_id}"
            )

        # ── Inject justification parameter based on ToolConfiguration ────
        # ── Inject justification parameter based on ToolConfiguration ────
        try:
            modified_tools = []
            for tool in available_tools:
                j_mode = justification_modes.get(tool.name)
                if j_mode:
                    modified_schema = copy.deepcopy(tool.parameters)
                    if "properties" not in modified_schema:
                        modified_schema["properties"] = {}
                    modified_schema["properties"]["justification"] = {
                        "type": "string",
                        "description": (
                            "Provide your reasoning and context for why "
                            "this tool is being called. This will be "
                            "reviewed by approvers and logged for audit "
                            "purposes."
                        ),
                    }
                    if j_mode == "required":
                        required = list(modified_schema.get("required", []))
                        if "justification" not in required:
                            required.append("justification")
                            modified_schema["required"] = required
                    tool = Tool(
                        name=tool.name,
                        description=tool.description or "",
                        parameters=modified_schema,
                    )
                    logger.info(
                        f"Injected justification parameter "
                        f"(mode={j_mode}) into "
                        f"tool '{tool.name}'"
                    )
                modified_tools.append(tool)

            available_tools = modified_tools
        except Exception as e:
            logger.error(
                f"Error injecting justification parameters: {e}",
                exc_info=True,
            )

        logger.info(
            f"Returning {len(available_tools)} total tools for user {user_context.username} before governance filter"
        )

        # ── Apply subject governance to filter out disabled tools ─────────
        try:
            subject_context = {
                "api_key_id": user_context.api_key_id,
                "managed_agent_id": getattr(user_context, "managed_agent_id", None),
            }

            final_tools = []
            for tool in available_tools:
                if is_tool_enabled_for_subject(
                    account_meta, tool_name=tool.name, subject_context=subject_context
                ):
                    final_tools.append(tool)

            available_tools = final_tools

        except Exception as e:
            logger.error(
                f"Error filtering tools through subject governance: {e}", exc_info=True
            )

        for tool in available_tools:
            logger.info(f"  - {tool.name}")

        user_context.mcp_tools_cache = available_tools
        return available_tools

    def _get_current_user_context(self) -> Optional[UserContext]:
        """Get the current user context for this request.

        This calls the user context provider function that was registered
        via set_user_context_provider().

        Returns:
            UserContext if available, None otherwise
        """
        if not self._user_context_provider:
            logger.warning("No user context provider registered")
            return None

        try:
            context = self._user_context_provider()
            if context:
                logger.debug(f"Got user context: {context.username}")
            else:
                logger.warning("User context provider returned None")
            return context
        except Exception as e:
            logger.error(f"Error getting user context: {e}", exc_info=True)
            return None

    def _create_proxied_tool_wrapper(
        self,
        tool_name: str,
        server_id: str,
        account_id: str,
        description: str,
        input_schema: dict,
    ):
        """Factory to create wrapper functions for proxied tools with approval and streaming.

        Creates a function with explicit parameters based on the input_schema.
        FastMCP doesn't support **kwargs, so we need to build the function dynamically.

        Args:
            tool_name: Name of the tool
            server_id: MCP server ID
            account_id: Owner account ID
            description: Tool description
            input_schema: Tool input schema (JSON Schema)

        Returns:
            Async wrapper function with Context support and explicit parameters
        """
        from fastmcp import Context

        # Create internal name with namespace to avoid collisions
        # Sanitize account_id for Python identifier (replace hyphens with underscores)
        safe_account_id = account_id.replace("-", "_")
        internal_name = f"account_{safe_account_id}_{tool_name}"

        # Extract parameters from input schema
        properties = input_schema.get("properties", {})
        required_params = set(input_schema.get("required", []))

        # Build parameter list dynamically
        req_params = []
        opt_params = []
        param_names = []

        for param_name, param_def in properties.items():
            param_names.append(param_name)
            param_type = param_def.get("type", "string")

            # Map JSON Schema types to Python type names
            if param_type == "string":
                type_str = "str"
            elif param_type == "integer":
                type_str = "int"
            elif param_type == "number":
                type_str = "float"
            elif param_type == "boolean":
                type_str = "bool"
            elif param_type == "array":
                type_str = "list"
            elif param_type == "object":
                type_str = "dict"
            else:
                type_str = "str"  # Default to string

            # Add Optional if not required
            if param_name not in required_params:
                opt_params.append(f"{param_name}: Optional[{type_str}] = None")
            else:
                req_params.append(f"{param_name}: {type_str}")

        # Add Context parameter at the end
        opt_params.append("ctx: Optional[Context] = None")

        # Create function signature string
        params = req_params + opt_params
        params_str = ", ".join(params)

        # Create the wrapper function using exec (yes, it's safe here - we control the input)
        wrapper_code = f"""
async def {internal_name}({params_str}) -> str:
    # DEBUG: Log Context availability
    logger.info(f"[WRAPPER] {{tool_name}} called with Context: {{ctx is not None}}")
    if ctx:
        logger.info(f"[WRAPPER] Context type: {{type(ctx)}}, has report_progress: {{hasattr(ctx, 'report_progress')}}")

    # SECURITY CHECK: Verify caller owns this tool
    user_context = self._get_current_user_context()
    if not user_context or user_context.account_id != account_id:
        logger.warning(
            f"Security violation: User {{user_context.account_id if user_context else 'None'}} "
            f"attempted to call tool '{{tool_name}}' owned by {{account_id}}"
        )
        return "Access denied: Tool not available"

    # Collect all arguments
    arguments = {{}}
    for param_name in param_names:
        value = locals().get(param_name)
        if value is not None:
            arguments[param_name] = value

    # Read justification from context var — _call_tool() already stripped it
    # from arguments and stored it in _justification_var.
    from preloop.services.dynamic_fastmcp import _justification_var
    justification = _justification_var.get(None)

    # Check approval with streaming (we have Context!)
    # The workflow_id may have been set by _call_tool() after evaluating access rules.
    from preloop.services.approval_helper import require_approval
    rule_workflow_id = _rule_workflow_id_var.get(None)
    corr_id = _correlation_id_var.get(None)

    approved, error = await require_approval(
        tool_name=tool_name,
        tool_source="mcp",
        account_id=account_id,
        arguments=arguments,
        ctx=ctx,
        workflow_id=rule_workflow_id,
        correlation_id=corr_id,
        justification=justification,
    )

    if not approved:
        return error

    # Call external MCP server
    try:
        db = next(get_db())
        try:
            # Use CRUD layer to get MCP server
            mcp_server = crud_mcp_server.get(db, id=server_id, account_id=account_id)

            if not mcp_server:
                return f"Error: MCP server {{server_id}} not found"

            # Get client from pool
            client_pool = get_mcp_client_pool()
            client = await client_pool.get_client(
                server_id=server_id,
                url=mcp_server.url,
                auth_type=mcp_server.auth_type,
                auth_config=mcp_server.auth_config,
                transport=mcp_server.transport,
            )

            # Call tool on external server
            result = await client.call_tool(tool_name, arguments)
            logger.info(
                f"Tool {{tool_name}} executed successfully on external server"
            )

            # Apply operator-configured output filters BEFORE the result
            # reaches the agent, stripping unused fields to save context tokens.
            if isinstance(result, list):
                result = apply_output_filters(
                    result,
                    account_id=account_id,
                    tool_name=tool_name,
                    server_name=getattr(mcp_server, "name", None),
                    managed_agent_id=getattr(
                        user_context, "managed_agent_id", None
                    ),
                )

            # Convert result to string
            if isinstance(result, list):
                return "\\n".join(
                    item.text if hasattr(item, "text") else str(item)
                    for item in result
                )
            return str(result)

        finally:
            db.close()

    except Exception as e:
        from preloop.services.mcp_client_pool import (
            _unwrap_exception_group,
            is_mcp_unavailable_error,
        )

        cause = _unwrap_exception_group(e)
        server_obj = locals().get("mcp_server")
        server_label = getattr(server_obj, "name", None) or "the MCP server"
        logger.error(
            f"Error executing proxied tool {{tool_name}} via {{server_label}}: {{cause}}",
            exc_info=True,
        )
        if is_mcp_unavailable_error(cause):
            return (
                f"The '{{server_label}}' MCP server is temporarily unavailable, so the "
                f"'{{tool_name}}' tool could not run. Please retry in a moment; if it "
                f"keeps happening the server may be down."
            )
        return f"Error executing tool '{{tool_name}}': {{cause}}"
"""

        # Create local namespace with required variables
        namespace = {
            "self": self,
            "account_id": account_id,
            "tool_name": tool_name,
            "server_id": server_id,
            "param_names": param_names,
            "logger": logger,
            "get_db": get_db,
            "crud_mcp_server": crud_mcp_server,
            "get_mcp_client_pool": get_mcp_client_pool,
            "apply_output_filters": apply_output_filters,
            "Optional": Optional,
            "Context": Context,
            "_rule_workflow_id_var": _rule_workflow_id_var,
            "_correlation_id_var": _correlation_id_var,
        }

        # Execute the code to create the function
        exec(wrapper_code, namespace)
        wrapper = namespace[internal_name]

        # Set function metadata
        wrapper.__doc__ = description
        # Store original name and owner for reference
        wrapper._display_name = tool_name  # type: ignore
        wrapper._account_id = account_id  # type: ignore

        logger.info(
            f"Created wrapper function for {tool_name} with parameters: {param_names}"
        )

        return wrapper

    async def call_tool(
        self,
        name: str,
        arguments: dict | None = None,
        *,
        version=None,
        run_middleware: bool = True,
        task_meta=None,
    ):
        """Override tool execution for access validation and name translation.

        This is called by FastMCP's protocol handler before executing a tool.
        We check if the user has access to the requested tool, then translate
        the tool name if it's a proxied tool (client name -> internal name).

        Approval is now handled at the function level (in tool implementations)
        for both builtin and proxied tools, allowing streaming progress updates.

        Args:
            name: Name of the tool
            arguments: Arguments derived from FastMCP
            version: Optional Tool Version
            run_middleware: Whether to run middleware
            task_meta: Optional Background Task Metadata

        Returns:
            ToolResult from tool execution
        """
        from fastmcp.tools.tool import ToolResult
        from mcp.types import TextContent

        arguments = arguments or {}

        # Extract justification from arguments before it reaches the tool function.
        # Justification is injected into the schema by list_tools() but isn't part
        # of the actual tool's function signature.
        justification = arguments.pop("justification", None)
        _justification_var.set(justification)

        logger.info(f"!!! call_tool called for tool: {name} !!!")

        # ── Internal proxied tool name re-entry ─────────────────────────
        # When we translate a client-facing proxied tool name to its internal
        # `account_<id>_<tool>` form below and call ``super().call_tool``,
        # FastMCP's tool dispatch re-enters this override with the internal
        # name. The access checks, justification/policy evaluation, and audit
        # logging here are scoped to the user-visible tool name and were
        # already performed on the first entry, so we shortcut directly to
        # the FastMCP base implementation. The dynamically-registered wrapper
        # (see ``_create_proxied_tool_wrapper``) still enforces account-level
        # ownership before dispatching to the upstream MCP server.
        if name in self._registered_proxied_tools:
            if not _is_proxy_translation_var.get(False):
                logger.warning(
                    f"Blocked direct invocation of internal proxied tool name: {name}"
                )
                from fastmcp.tools.tool import ToolResult
                from mcp.types import TextContent

                return ToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=f"Access denied: Cannot invoke internal tool name '{name}' directly",
                        )
                    ]
                )

            logger.info(
                f"Internal proxied tool re-entry: {name} (skipping duplicate checks)"
            )
            return await super().call_tool(
                name,
                arguments,
                version=version,
                run_middleware=run_middleware,
                task_meta=task_meta,
            )

        # Get current user context
        user_context = self._get_current_user_context()

        # ── Server-side justification enforcement ─────────────────────────
        # Schema injection alone isn't sufficient — clients can skip
        # validation. Verify server-side that required justifications are
        # actually provided.
        # Skip during async re-execution (_bypass_approval_var=True) because
        # justification was already validated on the original call and is not
        # persisted in tool_args.
        if user_context and not _bypass_approval_var.get(False):
            try:

                def _check_tool_config():
                    db = next(get_db())
                    try:
                        configs = crud_tool_configuration.get_multi_by_account(
                            db,
                            account_id=str(user_context.account_id),
                            limit=1000,
                        )
                        requires_just = False
                        builtin_enabled = None
                        for tc in configs:
                            if tc.tool_name != name:
                                continue
                            if tc.justification_mode == "required":
                                requires_just = True
                            if tc.tool_source == "builtin":
                                builtin_enabled = tc.is_enabled
                        return requires_just, builtin_enabled
                    finally:
                        db.close()

                (
                    requires_justification,
                    builtin_explicit_enabled,
                ) = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(None, _check_tool_config),
                    timeout=30,
                )

                # ── Enforce builtin tool enable/disable at call time ──────
                # Mirrors the list_tools filter: a disabled builtin tool must
                # not be invocable by name either. Flow executions are exempt
                # because their explicit allow-list (allowed_flow_tools) is
                # enforced separately via the list_tools access check below.
                builtin_call_meta = next(
                    (t for t in BUILTIN_TOOLS if t["name"] == name), None
                )
                if (
                    builtin_call_meta is not None
                    and user_context.allowed_flow_tools is None
                ):
                    is_disabled = builtin_explicit_enabled is False or (
                        builtin_explicit_enabled is None
                        and not builtin_call_meta.get("default_enabled", True)
                    )
                    if is_disabled:
                        logger.warning(f"Blocked call to disabled builtin tool: {name}")
                        return ToolResult(
                            content=[
                                TextContent(
                                    type="text",
                                    text=(
                                        f"Access denied: Tool '{name}' is disabled "
                                        "for this account. Enable it on the Tools "
                                        "page to use it."
                                    ),
                                )
                            ]
                        )
                if requires_justification and not justification:
                    from fastmcp.tools.tool import ToolResult
                    from mcp.types import TextContent

                    return ToolResult(
                        content=[
                            TextContent(
                                type="text",
                                text=(
                                    f"Justification required: Tool '{name}' requires a "
                                    f"'justification' parameter explaining why this tool "
                                    f"is being called."
                                ),
                            )
                        ]
                    )
            except Exception as e:
                # SECURITY: Fail closed — if we cannot verify whether
                # justification is required, block the call rather than
                # allowing potentially unjustified tool executions.
                logger.error(
                    f"Justification enforcement check failed for '{name}': {e}. "
                    f"Blocking tool call (fail closed)."
                )
                from fastmcp.tools.tool import ToolResult
                from mcp.types import TextContent

                return ToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=(
                                f"Error: Unable to verify justification requirements "
                                f"for tool '{name}'. Please try again."
                            ),
                        )
                    ]
                )

        if not user_context:
            logger.warning("No user context available for tool call")
            return ToolResult(
                content=[
                    TextContent(type="text", text="Error: No user context available")
                ]
            )

        # Check if user has access to this tool
        available_tools = await self.list_tools(run_middleware=run_middleware)
        if not any(tool.name == name for tool in available_tools):
            logger.warning(
                f"User {user_context.username} attempted to call "
                f"unauthorized tool: {name}"
            )
            return ToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=f"Access denied: Tool '{name}' is not available",
                    )
                ]
            )

        # ── Generate correlation_id for audit grouping ──────────────────
        correlation_id = str(uuid.uuid4())
        _correlation_id_var.set(correlation_id)

        # ── Evaluate access rules (ToolAccessRule) ──────────────────────
        # This is the central enforcement point for all tool calls.
        # evaluate_policy_async() checks rules in priority order and returns:
        #   "deny"             -> block the call, return denial reason
        #   "require_approval" -> let the per-tool require_approval() handle it
        #   "allow"            -> proceed to execution
        try:
            from preloop.models.db.session import get_async_db_session
            from preloop.services.policy_evaluator import evaluate_policy_async

            async with get_async_db_session() as db:
                action, approval_workflow_id, reason = await evaluate_policy_async(
                    db=db,
                    tool_name=name,
                    tool_args=arguments,
                    account_id=uuid.UUID(user_context.account_id),
                    user_id=uuid.UUID(user_context.user_id),
                    subject_context={
                        "api_key_id": user_context.api_key_id,
                        "managed_agent_id": getattr(
                            user_context, "managed_agent_id", None
                        ),
                        "runtime_session_id": user_context.runtime_session_id,
                        "runtime_principal_type": user_context.runtime_principal_type,
                        "runtime_principal_id": user_context.runtime_principal_id,
                        "runtime_principal_name": user_context.runtime_principal_name,
                    },
                    correlation_id=correlation_id,
                    extra_details={
                        "runtime_session_id": user_context.runtime_session_id,
                        "runtime_principal_type": user_context.runtime_principal_type,
                        "runtime_principal_id": user_context.runtime_principal_id,
                        "runtime_principal_name": user_context.runtime_principal_name,
                        "api_key_id": user_context.api_key_id,
                        "api_key_name": user_context.api_key_name,
                    },
                )

            logger.info(
                f"Policy evaluation for '{name}': action={action}, "
                f"workflow_id={approval_workflow_id}, reason={reason}"
            )

            if action == "deny":
                denial_msg = reason or "Tool call denied by access rule"
                return ToolResult(
                    content=[
                        TextContent(type="text", text=f"Access denied: {denial_msg}")
                    ]
                )

            if action == "require_approval":
                if approval_workflow_id:
                    # Store the workflow_id so require_approval() in the tool
                    # wrapper picks it up instead of relying on the legacy
                    # config-level policy.
                    _rule_workflow_id_var.set(str(approval_workflow_id))
                else:
                    # SECURITY: a rule asked for approval but no workflow could
                    # be resolved (no rule/config workflow and no account-level
                    # default). Fail closed instead of silently allowing the
                    # tool through, otherwise an explicit ``require_approval``
                    # rule would behave like ``allow``.
                    _rule_workflow_id_var.set(None)
                    logger.error(
                        f"Tool '{name}' matched require_approval rule but no "
                        "approval workflow is configured (rule, tool config, "
                        "and account default are all unset). Blocking the call."
                    )
                    return ToolResult(
                        content=[
                            TextContent(
                                type="text",
                                text=(
                                    f"Tool '{name}' requires approval but no "
                                    "approval workflow is configured for this "
                                    "account. Configure an approval workflow "
                                    "(or mark one as default) and retry."
                                ),
                            )
                        ]
                    )
            else:
                _rule_workflow_id_var.set(None)

        except Exception as e:
            logger.error(
                f"Error evaluating access rules for '{name}': {e}", exc_info=True
            )
            # SECURITY: fail CLOSED. If the central policy evaluation itself
            # raises (DB outage, malformed principal id, etc.) we must not fall
            # through to execution — otherwise any explicit ``deny`` /
            # ``require_approval`` rule (including subject-governance
            # tool-disabled denials) could be bypassed simply by inducing an
            # infrastructure error. Block the call and surface a ret600able
            # error to the agent.
            _rule_workflow_id_var.set(None)
            return ToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=(
                            f"Access denied: policy evaluation for '{name}' "
                            "failed and the request was blocked as a safety "
                            "measure. Please retry; if this persists, contact "
                            "your Preloop administrator."
                        ),
                    )
                ]
            )

        # ── Translate and execute ───────────────────────────────────────
        # Translate tool name for proxied tools
        # Client calls "calculate_fibonacci", we translate to "account_123_calculate_fibonacci"
        translation_token = None
        if name in self._proxied_tool_servers:
            safe_account_id = user_context.account_id.replace("-", "_")
            internal_name = f"account_{safe_account_id}_{name}"
            logger.info(f"Translating proxied tool name: {name} -> {internal_name}")
            # Modify target name for the FastMCP router
            name = internal_name
            translation_token = _is_proxy_translation_var.set(True)
        else:
            # Builtin tool - call with original name
            logger.info(f"Calling builtin tool: {name}")

        # Call parent
        import time

        start_time = time.monotonic()
        exec_status = "executed"
        exec_error: Optional[str] = None
        try:
            result = await super().call_tool(
                name,
                arguments,
                version=version,
                run_middleware=run_middleware,
                task_meta=task_meta,
            )
        except Exception as e:
            exec_status = "failed"
            exec_error = str(e)
            raise
        finally:
            if translation_token is not None:
                _is_proxy_translation_var.reset(translation_token)
            elapsed_ms = int((time.monotonic() - start_time) * 1000)

            # Clean up context vars after execution
            _rule_workflow_id_var.set(None)
            _correlation_id_var.set(None)

            # ── Audit: log tool execution ───────────────────────────────
            try:
                from preloop.plugins.base import get_plugin_manager
                from preloop.utils.redaction import redact_dict

                plugin_manager = get_plugin_manager()
                audit_service = plugin_manager.get_service("audit_service")
                if audit_service:
                    audit_service.log_tool_call_async(
                        db_factory=lambda: next(get_db()),
                        account_id=uuid.UUID(user_context.account_id),
                        user_id=uuid.UUID(user_context.user_id),
                        tool_name=name,
                        tool_args=redact_dict(arguments),
                        result=exec_status,
                        duration_ms=elapsed_ms,
                        policy_decision=None,
                        rule_matched=None,
                        correlation_id=correlation_id,
                        runtime_session_id=user_context.runtime_session_id,
                        runtime_principal_type=user_context.runtime_principal_type,
                        runtime_principal_id=user_context.runtime_principal_id,
                        runtime_principal_name=user_context.runtime_principal_name,
                        api_key_id=user_context.api_key_id,
                        api_key_name=user_context.api_key_name,
                    )
            except Exception as audit_err:
                logger.debug(f"Failed to audit tool execution: {audit_err}")

            # ── Runtime session activity persistence ──────────────────────
            try:
                if user_context.runtime_session_id:
                    from preloop.models.crud import crud_runtime_session_activity
                    from preloop.services.account_realtime import (
                        ACCOUNT_TOPIC_AUDIT,
                        ACCOUNT_TOPIC_GATEWAY_ACTIVITY,
                        ACCOUNT_TOPIC_MANAGED_AGENTS,
                        ACCOUNT_TOPIC_RUNTIME_SESSIONS,
                        build_account_event,
                        emit_account_event,
                    )
                    from preloop.utils.redaction import redact_dict

                    activity_status = "failed" if exec_status == "failed" else "success"
                    server_name = self._proxied_tool_server_names.get(
                        name, "preloop-mcp"
                    )
                    db = next(get_db())
                    try:
                        activity = crud_runtime_session_activity.log_tool_call(
                            db,
                            account_id=user_context.account_id,
                            runtime_session_id=user_context.runtime_session_id,
                            flow_execution_id=user_context.flow_execution_id,
                            api_key_id=user_context.api_key_id,
                            server_name=server_name,
                            tool_name=name,
                            status=activity_status,
                            summary=exec_error,
                            metadata={
                                "correlation_id": correlation_id,
                                "arguments": redact_dict(arguments),
                            },
                        )
                        activity_timestamp = (
                            activity.timestamp.isoformat()
                            if activity.timestamp
                            else None
                        )
                        managed_agent = None
                        if (
                            user_context.runtime_principal_type
                            and user_context.runtime_principal_id
                        ):
                            from preloop.models.crud import crud_managed_agent

                            managed_agent = crud_managed_agent.get_by_source(
                                db,
                                account_id=user_context.account_id,
                                session_source_type=user_context.runtime_principal_type,
                                session_source_id=user_context.runtime_principal_id,
                            )
                        emit_account_event(
                            build_account_event(
                                account_id=user_context.account_id,
                                topic=ACCOUNT_TOPIC_RUNTIME_SESSIONS,
                                event_type="runtime_session_updated",
                                payload={
                                    "runtime_session_id": str(
                                        user_context.runtime_session_id
                                    ),
                                    "session_source_type": user_context.runtime_principal_type,
                                    "session_source_id": user_context.runtime_principal_id,
                                    "session_reference": user_context.runtime_principal_name,
                                    "runtime_principal_type": user_context.runtime_principal_type,
                                    "runtime_principal_id": user_context.runtime_principal_id,
                                    "runtime_principal_name": user_context.runtime_principal_name,
                                    "last_activity_at": activity_timestamp,
                                    "tool_name": name,
                                    "server_name": server_name,
                                    "status": activity_status,
                                },
                                runtime_session_id=user_context.runtime_session_id,
                                execution_id=user_context.flow_execution_id,
                            )
                        )
                        emit_account_event(
                            build_account_event(
                                account_id=user_context.account_id,
                                topic=ACCOUNT_TOPIC_GATEWAY_ACTIVITY,
                                event_type="mcp_call",
                                payload={
                                    "runtime_session_id": str(
                                        user_context.runtime_session_id
                                    ),
                                    "runtime_principal_type": user_context.runtime_principal_type,
                                    "runtime_principal_id": user_context.runtime_principal_id,
                                    "runtime_principal_name": user_context.runtime_principal_name,
                                    "managed_agent_id": str(managed_agent.id)
                                    if managed_agent is not None
                                    else user_context.managed_agent_id,
                                    "api_key_id": user_context.api_key_id,
                                    "api_key_name": user_context.api_key_name,
                                    "server_name": server_name,
                                    "tool_name": name,
                                    "status": activity_status,
                                    "summary": exec_error,
                                    "correlation_id": correlation_id,
                                    "timestamp": activity_timestamp,
                                },
                                runtime_session_id=user_context.runtime_session_id,
                                execution_id=user_context.flow_execution_id,
                            )
                        )
                        emit_account_event(
                            build_account_event(
                                account_id=user_context.account_id,
                                topic=ACCOUNT_TOPIC_AUDIT,
                                event_type="audit_event",
                                payload={
                                    "action": "tool_call",
                                    "runtime_session_id": str(
                                        user_context.runtime_session_id
                                    ),
                                    "runtime_principal_type": user_context.runtime_principal_type,
                                    "runtime_principal_id": user_context.runtime_principal_id,
                                    "runtime_principal_name": user_context.runtime_principal_name,
                                    "tool_name": name,
                                    "server_name": server_name,
                                    "status": activity_status,
                                    "correlation_id": correlation_id,
                                },
                                runtime_session_id=user_context.runtime_session_id,
                                execution_id=user_context.flow_execution_id,
                            )
                        )
                        if managed_agent is not None:
                            emit_account_event(
                                build_account_event(
                                    account_id=user_context.account_id,
                                    topic=ACCOUNT_TOPIC_MANAGED_AGENTS,
                                    event_type="managed_agent_updated",
                                    payload={
                                        "agent_id": str(managed_agent.id),
                                        "runtime_session_id": str(
                                            user_context.runtime_session_id
                                        ),
                                        "display_name": user_context.runtime_principal_name,
                                        "session_source_type": user_context.runtime_principal_type,
                                        "session_source_id": user_context.runtime_principal_id,
                                        "last_seen_at": activity_timestamp,
                                        "tool_name": name,
                                        "server_name": server_name,
                                        "status": activity_status,
                                    },
                                    runtime_session_id=user_context.runtime_session_id,
                                    execution_id=user_context.flow_execution_id,
                                )
                            )
                    finally:
                        db.close()
            except Exception as activity_err:
                logger.debug(
                    f"Failed to persist runtime session activity: {activity_err}"
                )

        return result

    async def call_registered_tool_without_policy(
        self,
        name: str,
        arguments: dict | None = None,
    ):
        """Execute an already-registered tool without re-running policy checks.

        Async approval polling calls this only after the original tool call has
        been approved and claimed for idempotent re-execution.
        """
        translation_token = None
        if name in self._registered_proxied_tools:
            translation_token = _is_proxy_translation_var.set(True)
        try:
            return await super().call_tool(name, arguments or {})
        finally:
            if translation_token is not None:
                _is_proxy_translation_var.reset(translation_token)


def create_dynamic_mcp_server() -> DynamicFastMCP:
    """Create a DynamicFastMCP server instance.

    Returns:
        Configured DynamicFastMCP instance
    """
    mcp = DynamicFastMCP("preloop-mcp")
    logger.info("Created DynamicFastMCP server")
    return mcp


def create_user_context_from_scope(scope: dict) -> Optional[UserContext]:
    """Extract user context from ASGI scope.

    This is called by middleware to build UserContext from the authenticated
    user information stored in the ASGI scope.

    Args:
        scope: ASGI scope dict with user authentication info

    Returns:
        UserContext if user is authenticated, None otherwise
    """
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

    auth_user = scope.get("user")

    if not isinstance(auth_user, AuthenticatedUser):
        logger.warning("No authenticated user in scope")
        return None

    user = getattr(auth_user.access_token, "user", None)

    if not user:
        logger.warning("No user cached in access token")
        return None

    # Extract API key if available (for flow execution context)
    api_key = getattr(auth_user.access_token, "api_key", None)

    # Check tracker status
    db = next(get_db())
    try:
        # Get user's account for tracker check
        # Handle detached instance by catching the error and querying directly
        from sqlalchemy.orm.exc import DetachedInstanceError
        from preloop.models.crud import crud_account

        try:
            account = user.account if hasattr(user, "account") else None
        except DetachedInstanceError:
            account = None

        if not account:
            # Fallback: query account if relationship not loaded or detached
            account = crud_account.get(db, id=user.account_id)

        user_has_tracker = has_tracker(account, db) if account else False
        user_tracker_types = get_tracker_types(account, db) if account else []

        # Extract flow execution context from API key if present
        flow_execution_id = None
        runtime_session_id = None
        allowed_flow_tools = None
        runtime_principal_type = None
        runtime_principal_id = None
        runtime_principal_name = None
        managed_agent_id = None
        api_key_id = str(api_key.id) if api_key else None
        api_key_name = api_key.name if api_key else None
        if api_key and api_key.context_data:
            flow_execution_id = api_key.context_data.get("flow_execution_id")
            runtime_session_id = api_key.context_data.get("runtime_session_id")
            managed_agent_id = api_key.context_data.get("managed_agent_id")
            runtime_principal = api_key.context_data.get("runtime_principal") or {}
            runtime_principal_type = runtime_principal.get("type")
            runtime_principal_id = runtime_principal.get("id")
            runtime_principal_name = runtime_principal.get("name")
            # Combine allowed_mcp_tools with tool names from allowed_mcp_servers
            allowed_mcp_tools = api_key.context_data.get("allowed_mcp_tools")
            if allowed_mcp_tools is not None:
                # Extract tool names from the allowed_mcp_tools list
                # This could be a list of strings or a list of dicts with "name" or "tool_name" key
                allowed_flow_tools = []
                for tool in allowed_mcp_tools:
                    if isinstance(tool, str):
                        allowed_flow_tools.append(tool)
                    elif isinstance(tool, dict):
                        # Support both "tool_name" (from DB schema) and "name" (legacy)
                        tool_name = tool.get("tool_name") or tool.get("name")
                        if tool_name:
                            allowed_flow_tools.append(tool_name)

                logger.info(
                    f"Flow execution context: execution_id={flow_execution_id}, "
                    f"allowed_tools={allowed_flow_tools}"
                )

        user_context = UserContext(
            user_id=str(user.id),
            account_id=str(user.account_id),
            username=user.username,
            has_tracker=user_has_tracker,
            enabled_default_tools=[],  # Empty = all tools
            enabled_proxied_tools=[],
            tracker_types=user_tracker_types,
            flow_execution_id=flow_execution_id,
            runtime_session_id=runtime_session_id,
            allowed_flow_tools=allowed_flow_tools,
            runtime_principal_type=runtime_principal_type,
            runtime_principal_id=runtime_principal_id,
            runtime_principal_name=runtime_principal_name,
            api_key_id=api_key_id,
            api_key_name=api_key_name,
            managed_agent_id=(
                str(managed_agent_id) if managed_agent_id is not None else None
            ),
        )

        logger.info(
            f"Created user context for {user.username} "
            f"(account: {user.account_id}), has_tracker={user_has_tracker}, "
            f"tracker_types={user_tracker_types}, "
            f"flow_execution_id={flow_execution_id}, "
            f"runtime_session_id={runtime_session_id}, "
            f"runtime_principal={runtime_principal_type}:{runtime_principal_id}"
        )

        return user_context
    finally:
        db.close()
