"""Dynamic MCP server with per-request tool filtering based on user context.

This module implements Option B from the POC: using the low-level mcp.server.Server
class to build a custom MCP server that provides different tool lists based on
authenticated user context.

For Phase 1A: Only default tools, conditional on tracker presence.
For Phase 1B: Add support for proxied tools from external MCP servers.
"""

import logging
import time
from typing import Dict, List, Optional, Any
from uuid import UUID

from mcp.server import Server
from mcp import types
from sqlalchemy.orm import Session

from preloop.models.models.account import Account

logger = logging.getLogger(__name__)


def _get_audit_service():
    """Get the audit service instance (lazy import to avoid circular deps)."""
    try:
        from plugins.audit.service import get_audit_service

        return get_audit_service()
    except ImportError:
        logger.debug("Audit service not available")
        return None


def _get_db_factory():
    """Get a database session factory for async audit logging."""
    try:
        from preloop.models.db.session import get_session_factory

        def _create_session():
            """Create a session that the caller is responsible for closing."""
            factory = get_session_factory()
            return factory()

        return _create_session
    except ImportError:
        return None


def _log_tool_execution_async(
    account_id: str,
    user_id: Optional[str],
    tool_name: str,
    tool_args: Dict[str, Any],
    status: str = "executed",
    result: Optional[str] = None,
    execution_time_ms: Optional[int] = None,
    execution_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    runtime_session_id: Optional[str] = None,
    runtime_principal_type: Optional[str] = None,
    runtime_principal_id: Optional[str] = None,
    runtime_principal_name: Optional[str] = None,
    api_key_id: Optional[str] = None,
    api_key_name: Optional[str] = None,
) -> None:
    """Log a tool execution asynchronously (fire-and-forget)."""
    try:
        from preloop.utils.redaction import redact_dict

        audit_service = _get_audit_service()
        if not audit_service:
            return

        db_factory = _get_db_factory()
        if not db_factory:
            return

        # Redact sensitive fields before audit
        tool_args = redact_dict(tool_args)

        # Convert string IDs to UUIDs where needed
        user_uuid = None
        execution_uuid = None
        if user_id:
            try:
                user_uuid = UUID(user_id)
            except (ValueError, TypeError):
                # user_id may be a non-UUID principal id; omit it from audit metadata.
                pass
        if execution_id:
            try:
                execution_uuid = UUID(execution_id)
            except (ValueError, TypeError):
                # execution_id may be a non-UUID flow id; omit it from audit metadata.
                pass

        audit_service.log_tool_call_async(
            db_factory=db_factory,
            account_id=account_id,
            user_id=user_uuid,
            tool_name=tool_name,
            tool_args=tool_args,
            result=status,
            duration_ms=execution_time_ms,
            execution_id=execution_uuid,
            correlation_id=correlation_id,
            runtime_session_id=runtime_session_id,
            runtime_principal_type=runtime_principal_type,
            runtime_principal_id=runtime_principal_id,
            runtime_principal_name=runtime_principal_name,
            api_key_id=api_key_id,
            api_key_name=api_key_name,
        )
    except Exception as e:
        logger.debug(f"Failed to log tool execution to audit: {e}")


class UserContext:
    """User context extracted from request authentication."""

    def __init__(
        self,
        user_id: str,
        account_id: str,
        username: str,
        has_tracker: bool = False,
        enabled_default_tools: Optional[List[str]] = None,
        enabled_proxied_tools: Optional[List[str]] = None,
        tracker_types: Optional[List[str]] = None,
        flow_execution_id: Optional[str] = None,
        runtime_session_id: Optional[str] = None,
        allowed_flow_tools: Optional[List[str]] = None,
        runtime_principal_type: Optional[str] = None,
        runtime_principal_id: Optional[str] = None,
        runtime_principal_name: Optional[str] = None,
        api_key_id: Optional[str] = None,
        api_key_name: Optional[str] = None,
        managed_agent_id: Optional[str] = None,
        mcp_tools_cache: Optional[List[Any]] = None,
    ):
        self.user_id = user_id
        self.account_id = account_id
        self.username = username
        self.has_tracker = has_tracker
        self.enabled_default_tools = enabled_default_tools or []
        self.enabled_proxied_tools = enabled_proxied_tools or []
        self.tracker_types = tracker_types or []
        # Flow execution context for tool restrictions
        self.flow_execution_id = flow_execution_id
        self.runtime_session_id = runtime_session_id
        self.allowed_flow_tools = allowed_flow_tools
        self.runtime_principal_type = runtime_principal_type
        self.runtime_principal_id = runtime_principal_id
        self.runtime_principal_name = runtime_principal_name
        self.api_key_id = api_key_id
        self.api_key_name = api_key_name
        self.managed_agent_id = managed_agent_id
        self.mcp_tools_cache = mcp_tools_cache


class DynamicMCPServer:
    """Dynamic MCP server with per-request tool filtering.

    This server uses the low-level mcp.server.Server class to provide
    full control over tool registration and execution. Different authenticated
    users will see different tools based on their account configuration.

    Phase 1A Features:
    - Default tools only (6 tools from current implementation)
    - Conditional visibility: tools only visible if user has tracker(s)
    - Per-request dynamic tool lists

    Future Phases:
    - Phase 1B: Proxied tools from external MCP servers
    - Phase 2: Approval workflow interceptors
    """

    def __init__(self):
        """Initialize the dynamic MCP server."""
        self.server = Server("preloop-mcp")
        self._default_tools_registry: Dict[str, types.Tool] = {}
        self._proxied_tools_registry: Dict[str, types.Tool] = {}
        self._tool_handlers: Dict[str, Any] = {}
        self._setup_handlers()

        logger.info("DynamicMCPServer initialized")

    def _setup_handlers(self):
        """Register MCP protocol handlers for list_tools and call_tool."""

        @self.server.list_tools()
        async def handle_list_tools() -> list[types.Tool]:
            """Return tools based on current request context.

            This handler is called for every MCP tools/list request.
            It extracts user context from server.request_context (as per POC findings)
            and returns a filtered tool list based on user configuration.

            Returns:
                List of tools available to the authenticated user.
            """
            logger.info("handle_list_tools called")
            try:
                # Access request context (as per OPTION_B_FINDINGS.md)
                user_context = self._extract_user_context(self.server.request_context)

                if not user_context:
                    logger.warning("No user context available in request")
                    return []

                logger.info(
                    f"Extracted user context for {user_context.username}, has_tracker={user_context.has_tracker}"
                )

                # Build dynamic tool list based on user
                tools = self._get_tools_for_user(user_context)
                logger.info(
                    f"Returning {len(tools)} tools for user {user_context.username}"
                )

                for tool in tools:
                    logger.info(f"  - Tool: {tool.name}")

                return tools

            except LookupError:
                # No request context available
                logger.warning("No request context available (LookupError)")
                return []
            except Exception as e:
                logger.error(f"Error in handle_list_tools: {e}", exc_info=True)
                return []

        @self.server.call_tool()
        async def handle_call_tool(
            name: str, arguments: dict | None
        ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
            """Call a tool if user has access.

            This handler is called for every MCP tools/call request.
            It verifies the user has access to the requested tool and
            executes it with the provided arguments.

            Args:
                name: Name of the tool to call
                arguments: Arguments to pass to the tool

            Returns:
                List of content items representing the tool execution result
            """
            logger.info(f"handle_call_tool called for tool: {name}")
            try:
                # Access request context (as per OPTION_B_FINDINGS.md)
                user_context = self._extract_user_context(self.server.request_context)

                if not user_context:
                    logger.warning("No user context available for tool call")
                    return [
                        types.TextContent(
                            type="text", text="Error: No user context available"
                        )
                    ]

                # Check if user has access to this tool
                available_tools = self._get_tools_for_user(user_context)
                if not any(tool.name == name for tool in available_tools):
                    logger.warning(
                        f"User {user_context.username} attempted to call "
                        f"unauthorized tool: {name}"
                    )
                    return [
                        types.TextContent(
                            type="text",
                            text=f"Access denied: Tool '{name}' is not available",
                        )
                    ]

                # Evaluate policy rules to determine action (allow/deny/require_approval)
                policy_decision = await self._evaluate_policy(
                    user_context, name, arguments or {}
                )
                (
                    action,
                    approval_workflow_id,
                    rule_description,
                ) = policy_decision

                logger.info(
                    f"Policy evaluation for tool '{name}': "
                    f"action={action}, rule='{rule_description}'"
                )

                # Handle deny action - block execution
                if action == "deny":
                    logger.warning(
                        f"Tool {name} execution DENIED by policy rule: {rule_description}"
                    )
                    return [
                        types.TextContent(
                            type="text",
                            text=f"Tool execution denied: {rule_description}",
                        )
                    ]

                # Handle require_approval action
                if action == "require_approval":
                    logger.info(
                        f"Tool {name} requires approval - initiating approval flow"
                    )
                    try:
                        # Wait for approval
                        await self._request_and_wait_for_approval(
                            user_context,
                            name,
                            arguments or {},
                            approval_workflow_id,
                            # getattr, not attribute access: tests patch
                            # _evaluate_policy with a plain tuple and a missing
                            # snapshot must read as "not recorded".
                            rule_context=getattr(policy_decision, "rule_context", None),
                        )
                        logger.info(f"Tool {name} approved - proceeding with execution")
                    except TimeoutError as e:
                        logger.warning(f"Approval timeout for tool {name}: {e}")
                        return [
                            types.TextContent(
                                type="text",
                                text=f"Approval timeout: {str(e)}",
                            )
                        ]
                    except PermissionError as e:
                        logger.warning(f"Approval declined for tool {name}: {e}")
                        return [
                            types.TextContent(
                                type="text",
                                text=f"Approval declined: {str(e)}",
                            )
                        ]
                    except Exception as e:
                        logger.error(
                            f"Approval flow error for tool {name}: {e}", exc_info=True
                        )
                        return [
                            types.TextContent(
                                type="text",
                                text=f"Approval error: {str(e)}",
                            )
                        ]

                # Execute the tool
                handler = self._tool_handlers.get(name)
                if not handler:
                    logger.error(f"No handler found for tool: {name}")
                    return [
                        types.TextContent(
                            type="text",
                            text=f"Error: Handler not found for tool '{name}'",
                        )
                    ]

                from preloop.utils.redaction import redact_for_log

                logger.info(
                    f"Executing tool {name} for user {user_context.username} "
                    f"with args: {redact_for_log(arguments or {})}"
                )

                # Track execution time
                start_time = time.time()

                # Call the handler (existing MCP router functions)
                result = await handler(**(arguments or {}))

                # Calculate execution time
                execution_time_ms = int((time.time() - start_time) * 1000)

                # Convert result to MCP TextContent
                # The handlers return Pydantic models, we need to serialize them
                result_text = (
                    result.model_dump_json()
                    if hasattr(result, "model_dump_json")
                    else str(result)
                )

                # Log successful tool execution to audit (fire-and-forget)
                _log_tool_execution_async(
                    account_id=user_context.account_id,
                    user_id=user_context.user_id,
                    tool_name=name,
                    tool_args=arguments or {},
                    status="executed",
                    result=result_text[:500] if result_text else None,
                    execution_time_ms=execution_time_ms,
                    execution_id=user_context.flow_execution_id,
                    runtime_session_id=user_context.runtime_session_id,
                    runtime_principal_type=user_context.runtime_principal_type,
                    runtime_principal_id=user_context.runtime_principal_id,
                    runtime_principal_name=user_context.runtime_principal_name,
                    api_key_id=user_context.api_key_id,
                    api_key_name=user_context.api_key_name,
                )

                return [types.TextContent(type="text", text=result_text)]

            except Exception as e:
                logger.error(f"Error executing tool {name}: {e}", exc_info=True)
                # Log failed tool execution to audit
                _log_tool_execution_async(
                    account_id=user_context.account_id if user_context else "unknown",
                    user_id=user_context.user_id if user_context else None,
                    tool_name=name,
                    tool_args=arguments or {},
                    status="failed",
                    result=str(e)[:500],
                    runtime_session_id=(
                        user_context.runtime_session_id if user_context else None
                    ),
                    runtime_principal_type=(
                        user_context.runtime_principal_type if user_context else None
                    ),
                    runtime_principal_id=(
                        user_context.runtime_principal_id if user_context else None
                    ),
                    runtime_principal_name=(
                        user_context.runtime_principal_name if user_context else None
                    ),
                    api_key_id=(user_context.api_key_id if user_context else None),
                    api_key_name=(user_context.api_key_name if user_context else None),
                )
                return [
                    types.TextContent(
                        type="text", text=f"Error executing tool: {str(e)}"
                    )
                ]

    def _extract_user_context(self, request_context) -> Optional[UserContext]:
        """Extract user context from MCP request metadata.

        The middleware should inject user data into request_context.session.meta
        which is accessible here.

        Args:
            request_context: MCP request context from server.request_context

        Returns:
            UserContext if available, None otherwise
        """
        try:
            # Try to get user context from session metadata
            if hasattr(request_context, "session") and hasattr(
                request_context.session, "meta"
            ):
                user_data = request_context.session.meta
                logger.info(f"Found user context in session.meta: {user_data}")
            elif hasattr(request_context, "meta"):
                user_data = request_context.meta
                logger.info(f"Found user context in meta: {user_data}")
            else:
                logger.warning(
                    f"Request context has no session.meta or meta. Attributes: {dir(request_context)}"
                )
                return None

            if not user_data:
                return None

            return UserContext(
                user_id=user_data.get("user_id", "unknown"),
                account_id=user_data.get("account_id", "unknown"),
                username=user_data.get("username", "unknown"),
                has_tracker=user_data.get("has_tracker", False),
                enabled_default_tools=user_data.get("enabled_default_tools", []),
                enabled_proxied_tools=user_data.get("enabled_proxied_tools", []),
                runtime_session_id=user_data.get("runtime_session_id"),
                runtime_principal_type=user_data.get("runtime_principal_type"),
                runtime_principal_id=user_data.get("runtime_principal_id"),
                runtime_principal_name=user_data.get("runtime_principal_name"),
                api_key_id=user_data.get("api_key_id"),
                api_key_name=user_data.get("api_key_name"),
            )
        except Exception as e:
            logger.error(f"Error extracting user context: {e}", exc_info=True)
            return None

    async def _evaluate_policy(
        self, user_context: UserContext, tool_name: str, tool_args: dict
    ) -> tuple:
        """Evaluate policy rules to determine tool access action.

        Uses the policy evaluator to check ToolAccessRule entries and determine
        whether to allow, deny, or require approval for the tool call.

        Args:
            user_context: User context
            tool_name: Name of the tool
            tool_args: Tool arguments for condition evaluation

        Returns:
            A ``PolicyDecision``, which unpacks as the 3-tuple
            (action, approval_workflow_id, rule_description) and carries
            ``.rule_context`` describing the rule that gated the call.
            - action: 'allow', 'deny', or 'require_approval'
            - approval_workflow_id: UUID of approval workflow (if require_approval)
            - rule_description: Description of the matched rule
        """
        # Imported outside the try: the fail-closed branch below builds a
        # PolicyDecision, and it must not itself blow up with a NameError.
        from preloop.services.approval_rule_context import (
            SOURCE_RULE_EVALUATION_ERROR,
            build_rule_context,
        )
        from preloop.services.policy_evaluator import (
            PolicyDecision,
            evaluate_policy_async,
        )

        try:
            from preloop.models.db.session import get_async_db_session
            from preloop.models.crud.tool_configuration import (
                get_tool_config_by_name_and_source_async,
            )

            logger.info(
                f"Evaluating policy for tool '{tool_name}' "
                f"(account_id={user_context.account_id})"
            )

            # Get database session
            async with get_async_db_session() as db:
                # Check if there's a tool configuration for this tool
                # First check default tools using CRUD
                config = await get_tool_config_by_name_and_source_async(
                    db,
                    account_id=user_context.account_id,
                    tool_name=tool_name,
                    tool_source="builtin",
                )

                # If not found in default tools, check proxied tools
                if not config:
                    config = await get_tool_config_by_name_and_source_async(
                        db,
                        account_id=user_context.account_id,
                        tool_name=tool_name,
                        tool_source="mcp",
                    )

                # Evaluate policy rules
                decision = await evaluate_policy_async(
                    db=db,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    account_id=user_context.account_id,
                    tool_configuration_id=config.id if config else None,
                    user_id=getattr(user_context, "user_id", None),
                    execution_id=getattr(user_context, "execution_id", None),
                    subject_context={
                        "api_key_id": getattr(user_context, "api_key_id", None),
                        "managed_agent_id": getattr(
                            user_context, "managed_agent_id", None
                        ),
                        "runtime_session_id": getattr(
                            user_context, "runtime_session_id", None
                        ),
                        "runtime_principal_type": getattr(
                            user_context, "runtime_principal_type", None
                        ),
                        "runtime_principal_id": getattr(
                            user_context, "runtime_principal_id", None
                        ),
                        "runtime_principal_name": getattr(
                            user_context, "runtime_principal_name", None
                        ),
                    },
                    extra_details={
                        "runtime_session_id": getattr(
                            user_context, "runtime_session_id", None
                        ),
                        "runtime_principal_type": getattr(
                            user_context, "runtime_principal_type", None
                        ),
                        "runtime_principal_id": getattr(
                            user_context, "runtime_principal_id", None
                        ),
                        "runtime_principal_name": getattr(
                            user_context, "runtime_principal_name", None
                        ),
                        "api_key_id": getattr(user_context, "api_key_id", None),
                        "api_key_name": getattr(user_context, "api_key_name", None),
                    },
                )

                action, approval_workflow_id, rule_description = decision

                logger.info(
                    f"Policy evaluation result for '{tool_name}': "
                    f"action={action}, approval_workflow_id={approval_workflow_id}, "
                    f"rule='{rule_description}'"
                )

                return decision

        except Exception as e:
            # SECURITY: Fail closed on errors
            logger.error(f"Error evaluating policy for {tool_name}: {e}", exc_info=True)
            return PolicyDecision(
                "require_approval",
                None,
                f"Policy evaluation error: {e}",
                build_rule_context(
                    source=SOURCE_RULE_EVALUATION_ERROR,
                    decision="require_approval",
                    explanation=(
                        "Preloop could not evaluate this tool call's access "
                        "rules, so it failed closed and asked for approval "
                        "instead of deciding on its own."
                    ),
                ),
            )

    async def _request_and_wait_for_approval(
        self,
        user_context: UserContext,
        tool_name: str,
        tool_args: dict,
        approval_workflow_id: Optional[str] = None,
        rule_context: Optional[dict] = None,
    ):
        """Request approval and wait for user response.

        Args:
            user_context: User context
            tool_name: Name of the tool
            tool_args: Tool arguments
            approval_workflow_id: Workflow to raise the approval against
            rule_context: Snapshot of the rule that gated the call, persisted
                on the request so the approver can see why it was raised

        Raises:
            TimeoutError: If approval request times out
            PermissionError: If approval is declined
            Exception: If approval flow fails
        """
        try:
            from preloop.models.db.session import get_async_db_session
            from preloop.models.crud.tool_configuration import (
                get_tool_config_by_name_and_source_async,
            )
            from preloop.models.crud.approval_workflow import (
                get_approval_workflow_async,
            )
            from preloop.services.approval_service import ApprovalService
            import os

            # Get base URL from environment or default
            base_url = os.getenv("PRELOOP_URL", "http://localhost:8000")

            async with get_async_db_session() as db:
                # Get tool configuration using CRUD
                # Try builtin first, then mcp
                config = await get_tool_config_by_name_and_source_async(
                    db,
                    account_id=user_context.account_id,
                    tool_name=tool_name,
                    tool_source="builtin",
                )
                if not config:
                    config = await get_tool_config_by_name_and_source_async(
                        db,
                        account_id=user_context.account_id,
                        tool_name=tool_name,
                        tool_source="mcp",
                    )

                final_workflow_id = approval_workflow_id or (
                    config.approval_workflow_id if config else None
                )
                if not final_workflow_id:
                    raise Exception("No approval workflow configured for this tool")

                # Get approval workflow using CRUD
                policy = await get_approval_workflow_async(
                    db, workflow_id=final_workflow_id
                )

                if not policy:
                    raise Exception("Approval workflow not found")

                # Debug: Log policy escalation configuration
                logger.info(
                    f"Approval workflow loaded: id={policy.id}, "
                    f"timeout_seconds={policy.timeout_seconds}, "
                    f"escalation_user_ids={policy.escalation_user_ids}, "
                    f"escalation_team_ids={policy.escalation_team_ids}, "
                    f"approvals_required={policy.approvals_required}"
                )

                # Create approval service
                approval_service = ApprovalService(db, base_url)

                # Create approval request and send notification
                approval_request = await approval_service.create_and_notify(
                    account_id=user_context.account_id,
                    tool_configuration_id=config.id if config else None,
                    approval_workflow=policy,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    agent_reasoning=None,  # Could be extracted from context if available
                    execution_id=None,  # Could be extracted from context if available
                    rule_context=rule_context,
                )

                logger.info(
                    f"Approval request created: {approval_request.id}, waiting for response..."
                )

                # Wait for approval with polling
                final_request = await approval_service.wait_for_approval(
                    approval_request.id, poll_interval=2.0
                )

                # Check final status
                if final_request.status == "declined":
                    raise PermissionError(
                        "Tool execution declined"
                        + (
                            f": {final_request.approver_comment}"
                            if final_request.approver_comment
                            else ""
                        )
                    )
                elif final_request.status == "cancelled":
                    raise PermissionError("Tool execution cancelled")
                elif final_request.status != "approved":
                    raise Exception(
                        f"Unexpected approval status: {final_request.status}"
                    )

                # Approval granted!
                logger.info(f"Tool {tool_name} approved by user")

        except Exception as e:
            logger.error(f"Error in approval flow: {e}", exc_info=True)
            raise

    def _get_tools_for_user(self, user_context: UserContext) -> List[types.Tool]:
        """Build user-specific tool list.

        Phase 1A: Only return default tools if user has tracker(s).
        Phase 1B: Also include proxied tools.

        Args:
            user_context: User context with configuration

        Returns:
            List of tools available to this user
        """
        tools = []

        # Add default tools if user has tracker
        if user_context.has_tracker:
            # If enabled_default_tools is empty, return all default tools
            # (this is the default behavior in Phase 1A)
            if not user_context.enabled_default_tools:
                tools.extend(self._default_tools_registry.values())
            else:
                # Only return explicitly enabled tools
                for tool_name in user_context.enabled_default_tools:
                    if tool_name in self._default_tools_registry:
                        tools.append(self._default_tools_registry[tool_name])

        # Phase 1B: Add proxied tools
        for tool_name in user_context.enabled_proxied_tools:
            if tool_name in self._proxied_tools_registry:
                tools.append(self._proxied_tools_registry[tool_name])

        return tools

    def register_default_tool(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, Any],
        handler: Any,
    ):
        """Register a default (built-in) tool with handler.

        Args:
            name: Tool name
            description: Tool description
            input_schema: JSON schema for tool parameters
            handler: Async function to handle tool execution
        """
        tool = types.Tool(name=name, description=description, inputSchema=input_schema)
        self._default_tools_registry[name] = tool
        self._tool_handlers[name] = handler

        logger.info(f"Registered default tool: {name}")

    def register_proxied_tool(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, Any],
        handler: Any,
    ):
        """Register a proxied tool from external MCP server.

        Phase 1B: This will be used to register tools from external servers.

        Args:
            name: Tool name
            description: Tool description
            input_schema: JSON schema for tool parameters
            handler: Async function to proxy to external server
        """
        tool = types.Tool(name=name, description=description, inputSchema=input_schema)
        self._proxied_tools_registry[name] = tool
        self._tool_handlers[name] = handler

        logger.info(f"Registered proxied tool: {name}")

    def get_registered_tool_names(self) -> Dict[str, List[str]]:
        """Get names of all registered tools for debugging.

        Returns:
            Dict with 'default' and 'proxied' tool lists
        """
        return {
            "default": list(self._default_tools_registry.keys()),
            "proxied": list(self._proxied_tools_registry.keys()),
        }


def get_dynamic_mcp_server() -> DynamicMCPServer:
    """Get the singleton DynamicMCPServer instance.

    Returns:
        The dynamic MCP server instance
    """
    global _dynamic_mcp_server
    if "_dynamic_mcp_server" not in globals():
        _dynamic_mcp_server = DynamicMCPServer()
    return _dynamic_mcp_server


def has_tracker(account: Account, db: Session) -> bool:
    """Check if an account has at least one configured tracker.

    Args:
        account: Account model instance
        db: Database session

    Returns:
        True if account has trackers, False otherwise
    """
    # Use CRUD method to check for trackers
    from preloop.models.crud import crud_tracker

    return crud_tracker.has_tracker(db, account_id=account.id)


def get_tracker_types(account: Account, db: Session) -> List[str]:
    """Get list of tracker types configured for an account.

    Args:
        account: Account model instance
        db: Database session

    Returns:
        List of tracker type strings (e.g., ['github', 'gitlab'])
    """
    from preloop.models.crud import crud_tracker

    trackers = crud_tracker.get_for_account(db, account_id=account.id)
    # Return unique tracker types
    return list(set(tracker.tracker_type for tracker in trackers))


def register_default_tools(server: DynamicMCPServer):
    """Register all default tools with the DynamicMCPServer.

    This function registers the 6 default tools from the current MCP implementation:
    1. get_issue
    2. create_issue
    3. update_issue
    4. search
    5. estimate_compliance
    6. improve_compliance

    Args:
        server: The DynamicMCPServer instance to register tools with
    """
    # Import the MCP router functions
    from preloop.api.endpoints import mcp as mcp_router

    # Tool 1: get_issue
    server.register_default_tool(
        name="get_issue",
        description="Get detailed information about an issue by its identifier (URL, key, or ID)",
        input_schema={
            "type": "object",
            "properties": {
                "issue": {
                    "type": "string",
                    "description": "Issue identifier (URL, key like 'PROJECT#123', or UUID)",
                }
            },
            "required": ["issue"],
        },
        handler=mcp_router.get_issue,
    )

    # Tool 2: create_issue
    server.register_default_tool(
        name="create_issue",
        description="Create a new issue in a project",
        input_schema={
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "Project identifier or slug",
                },
                "title": {
                    "type": "string",
                    "description": "Issue title",
                },
                "description": {
                    "type": "string",
                    "description": "Issue description",
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of labels to apply",
                },
                "assignee": {
                    "type": "string",
                    "description": "Username of the assignee",
                },
                "priority": {
                    "type": "string",
                    "description": "Priority level",
                },
                "status": {
                    "type": "string",
                    "description": "Initial status",
                },
            },
            "required": ["project", "title", "description"],
        },
        handler=mcp_router.create_issue,
    )

    # Tool 3: update_issue
    server.register_default_tool(
        name="update_issue",
        description="Update an existing issue",
        input_schema={
            "type": "object",
            "properties": {
                "issue": {
                    "type": "string",
                    "description": "Issue identifier (URL, key, or UUID)",
                },
                "title": {
                    "type": "string",
                    "description": "New title",
                },
                "description": {
                    "type": "string",
                    "description": "New description",
                },
                "status": {
                    "type": "string",
                    "description": "New status",
                },
                "priority": {
                    "type": "string",
                    "description": "New priority",
                },
                "assignee": {
                    "type": "string",
                    "description": "New assignee username",
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "New labels list",
                },
            },
            "required": ["issue"],
        },
        handler=mcp_router.update_issue,
    )

    # Tool 4: search
    server.register_default_tool(
        name="search",
        description="Search for issues and comments using similarity or fulltext search",
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
                "project": {
                    "type": "string",
                    "description": "Project identifier to limit search scope",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
        handler=mcp_router.search,
    )

    # Tool 5: estimate_compliance
    server.register_default_tool(
        name="estimate_compliance",
        description="Estimate compliance for a list of issues provided as URLs or issue keys (slug or identifier).",
        input_schema={
            "type": "object",
            "properties": {
                "issues": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of issue identifiers (URLs, keys, or UUIDs)",
                },
                "compliance_metric": {
                    "type": "string",
                    "description": "Name of compliance metric to use",
                    "default": "DoR",
                },
            },
            "required": ["issues"],
        },
        handler=mcp_router.estimate_compliance,
    )

    # Tool 6: improve_compliance
    server.register_default_tool(
        name="improve_compliance",
        description="Get suggestions to improve compliance for a list of issues provided as URLs or issue keys (slug or identifier). Use update_issue tool to apply each suggestion.",
        input_schema={
            "type": "object",
            "properties": {
                "issues": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of issue identifiers (URLs, keys, or UUIDs)",
                },
                "compliance_metric": {
                    "type": "string",
                    "description": "Name of compliance metric to use",
                    "default": "DoR",
                },
            },
            "required": ["issues"],
        },
        handler=mcp_router.improve_compliance,
    )

    logger.info("All 6 default tools registered successfully")


def initialize_dynamic_mcp_server() -> DynamicMCPServer:
    """Initialize and configure the DynamicMCPServer with all default tools.

    This is the main entry point for setting up the MCP server.

    Returns:
        Configured DynamicMCPServer instance
    """
    server = DynamicMCPServer()
    register_default_tools(server)

    # Log registered tools for verification
    registered = server.get_registered_tool_names()
    logger.info(
        f"DynamicMCPServer initialized with {len(registered['default'])} default tools:"
    )
    for tool_name in registered["default"]:
        logger.info(f"  - {tool_name}")

    return server
