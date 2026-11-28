"""MCP tools that let a flow run read and write its flow's persistent state.

The flow is never an argument: it comes from the flow-execution credential,
so a run can only reach the state of the flow it belongs to. The binding
check (:func:`bound_execution`) is shared with other flow-scoped tools such as
``ci_health_report``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool
from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.api.loop_safety import run_db_off_loop
from preloop.models import models
from preloop.models.crud import crud_api_key, crud_flow, crud_flow_execution, crud_user
from preloop.models.crud import flow_state as crud_flow_state
from preloop.models.db.session import get_session_factory
from preloop.schemas.flow_state import (
    FLOW_STATE_TOOLS,
    FlowStateEntry,
    FlowStateList,
    GetFlowState,
    PutFlowState,
)
from preloop.services.approval_helper import require_approval
from preloop.services.dynamic_fastmcp import (
    DynamicFastMCP,
    _correlation_id_var,
    _justification_var,
    _rule_workflow_id_var,
)
from preloop.services.dynamic_fastmcp_http import get_current_user_context
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.mcp_config_service import DEFAULT_TOOL_SERVER, MCPConfigService

UNAVAILABLE = "A flow execution credential that allows this tool is required"


@dataclass(frozen=True)
class BoundExecution:
    """The flow run a tool call is bound to, re-read from the database."""

    user: models.User
    flow: models.Flow
    execution: models.FlowExecution

    @property
    def account_id(self) -> UUID:
        """The owning account."""
        return self.flow.account_id


def caller_context(name: str) -> UserContext:
    """Return the request's flow-execution context or refuse the call.

    Args:
        name: Tool name, which the flow's allow-list must contain.

    Returns:
        The user context.

    Raises:
        ToolError: without a flow-execution credential that allows ``name``.
    """
    context = get_current_user_context()
    if (
        not context
        or not context.account_id
        or not context.api_key_id
        or not context.user_id
        or not context.flow_id
        or not context.flow_execution_id
        or context.runtime_principal_type != "flow_execution"
        or context.runtime_principal_id != context.flow_execution_id
        or context.allowed_flow_tools is None
        or name not in context.allowed_flow_tools
    ):
        raise ToolError(UNAVAILABLE)
    return context


def bound_execution(db: Session, context: UserContext, name: str) -> BoundExecution:
    """Re-check the credential binding against the database.

    The key must be valid, belong to the user, be bound to this execution
    and flow, and both the key binding and the flow's current allow-list must
    still grant ``name``.

    Args:
        db: Database session.
        context: Context from :func:`caller_context`.
        name: Tool name.

    Returns:
        The bound user, flow and execution.

    Raises:
        ToolError: when any part of the binding does not hold.
    """
    account_id = context.account_id
    user = crud_user.get(db, id=context.user_id, account_id=account_id)
    key = crud_api_key.get(db, id=context.api_key_id, account_id=account_id)
    if user is None or not user.is_active or key is None or not key.is_valid():
        raise ToolError(UNAVAILABLE)
    binding = key.context_data or {}
    if (
        key.user_id != user.id
        or binding.get("flow_execution_id") != context.flow_execution_id
        or binding.get("flow_id") != context.flow_id
        or not MCPConfigService.validate_tool_access(
            DEFAULT_TOOL_SERVER, name, binding.get("allowed_mcp_tools") or []
        )
    ):
        raise ToolError(UNAVAILABLE)
    flow = crud_flow.get(db, id=context.flow_id, account_id=account_id)
    execution = crud_flow_execution.get(
        db, id=context.flow_execution_id, account_id=account_id
    )
    if (
        flow is None
        or execution is None
        or execution.flow_id != flow.id
        or not MCPConfigService.validate_tool_access(
            DEFAULT_TOOL_SERVER, name, flow.allowed_mcp_tools or []
        )
    ):
        raise ToolError(UNAVAILABLE)
    return BoundExecution(user=user, flow=flow, execution=execution)


def entry(row: models.FlowState, *, with_value: bool = True) -> FlowStateEntry:
    """Project a stored row.

    Args:
        row: The row.
        with_value: Include the value (False for listings).

    Returns:
        The public entry.
    """
    return FlowStateEntry(
        key=row.key,
        value=row.value if with_value else None,
        version=row.version,
        size_bytes=row.size_bytes,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
        updated_by_execution_id=(
            str(row.updated_by_execution_id) if row.updated_by_execution_id else None
        ),
        reserved=crud_flow_state.is_reserved(row.key),
    )


async def _approved(name: str, context: UserContext, arguments: dict, ctx: Any) -> None:
    """Apply the account's tool policy to the call.

    Raises:
        ToolError: when the policy denies the call.
    """
    approved, denial = await require_approval(
        tool_name=name,
        tool_source="builtin",
        account_id=context.account_id,
        arguments=arguments,
        ctx=ctx,
        workflow_id=_rule_workflow_id_var.get(None),
        correlation_id=_correlation_id_var.get(None),
        justification=_justification_var.get(None),
    )
    if not approved:
        raise ToolError(denial or "Tool call denied")


async def get_flow_state(
    key: Optional[str] = None, ctx: Optional[Context] = None
) -> dict[str, Any]:
    """Read one key of the caller's flow, or list its keys.

    Args:
        key: Key to read; None lists keys.
        ctx: MCP request context.

    Returns:
        A :class:`FlowStateEntry` or :class:`FlowStateList` as a dict.
    """
    name = "get_flow_state"
    context = caller_context(name)
    try:
        request = GetFlowState(key=key)
    except ValidationError as exc:
        raise ToolError(f"flow_state_key_invalid: {exc.errors()[0]['msg']}") from None
    await _approved(name, context, request.model_dump(), ctx)

    def run() -> dict[str, Any]:
        with get_session_factory()() as db:
            bound = bound_execution(db, context, name)
            if request.key is None:
                rows = crud_flow_state.list_states(
                    db, account_id=bound.account_id, flow_id=bound.flow.id
                )
                return FlowStateList(
                    keys=[entry(row, with_value=False) for row in rows]
                ).model_dump()
            row = crud_flow_state.get_state(
                db, account_id=bound.account_id, flow_id=bound.flow.id, key=request.key
            )
            if row is None:
                return FlowStateEntry(
                    key=request.key,
                    version=0,
                    size_bytes=0,
                    reserved=crud_flow_state.is_reserved(request.key),
                ).model_dump()
            return entry(row).model_dump()

    return await run_db_off_loop(run)


async def put_flow_state(
    key: str,
    value: Any,
    expected_version: Optional[int] = None,
    ctx: Optional[Context] = None,
) -> dict[str, Any]:
    """Create or replace one key of the caller's flow.

    Args:
        key: State key; keys under the reserved prefix are refused.
        value: JSON value.
        expected_version: Compare-and-set guard (see the tool schema).
        ctx: MCP request context.

    Returns:
        The stored entry without its value.
    """
    name = "put_flow_state"
    context = caller_context(name)
    try:
        request = PutFlowState(key=key, value=value, expected_version=expected_version)
    except ValidationError as exc:
        raise ToolError(
            f"flow_state_request_invalid: {exc.errors()[0]['msg']}"
        ) from None
    if crud_flow_state.is_reserved(request.key):
        raise ToolError(
            f"flow_state_key_reserved: keys under '{crud_flow_state.RESERVED_PREFIX}'"
            " are written by the platform"
        )
    await _approved(
        name,
        context,
        {"key": request.key, "expected_version": request.expected_version},
        ctx,
    )

    def run() -> dict[str, Any]:
        with get_session_factory()() as db:
            bound = bound_execution(db, context, name)
            try:
                row = crud_flow_state.put_state(
                    db,
                    account_id=bound.account_id,
                    flow_id=bound.flow.id,
                    key=request.key,
                    value=request.value,
                    expected_version=request.expected_version,
                    execution_id=bound.execution.id,
                )
            except crud_flow_state.FlowStateError as exc:
                db.rollback()
                raise ToolError(f"{exc.code}: {exc}") from None
            return entry(row, with_value=False).model_dump()

    return await run_db_off_loop(run)


def register_flow_state_tools(mcp: DynamicFastMCP) -> None:
    """Register both tools with the catalog's schemas.

    Args:
        mcp: The server.
    """
    handlers: tuple[Callable[..., Any], ...] = (get_flow_state, put_flow_state)
    for handler, definition in zip(handlers, FLOW_STATE_TOOLS, strict=True):
        tool = FunctionTool.from_function(
            handler, description=definition["description"], output_schema=None
        )
        tool.parameters = definition["schema"]
        mcp.add_tool(tool)
