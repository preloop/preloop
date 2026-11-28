"""Project-scoped current definitions and immutable, non-executable proposals."""

import hashlib
import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool
from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.api.auth.key_scopes import is_read_only_review_api_key, mcp_scope_allowed
from preloop.api.common import project_scope_denial
from preloop.api.flow_preconditions import if_match_condition, require_flow_revision
from preloop.api.loop_safety import run_db_off_loop
from preloop.models import models
from preloop.models.crud import (
    crud_api_key,
    crud_flow,
    crud_project,
    crud_user,
    flow_change_proposal,
)
from preloop.models.db.session import get_session_factory
from preloop.models.schemas.flow import FLOW_LIMIT_FIELDS, FlowUpdate
from preloop.models.schemas.flow_revision import flow_etag
from preloop.plugins.account_hooks import VISIBLE_FLOW, filter_viewable
from preloop.schemas.flow_change import (
    FLOW_DEFINITION_TOOLS,
    InspectFlows,
    ProposeFlowChange,
    ReadFlowProposal,
    ProposalDocument,
    ProposalReceipt,
    ProposalReview,
    SettingChange,
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
from preloop.services.model_gateway_events import redact_sensitive_text
from preloop.utils.permissions import ensure_permission_in_oss, require_permission
from preloop.utils.redaction import redact_agent_config
from preloop.utils.secret_scrubbing import scrub_secrets

Request = InspectFlows | ProposeFlowChange | ReadFlowProposal
PAGE_SIZE = 50


def _display(value: Any) -> Any:
    """Scrub arbitrary definition text, including schema property names."""
    if isinstance(value, str):
        return redact_sensitive_text(scrub_secrets(value) or "")[0]
    if isinstance(value, dict):
        return {_display(k): _display(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_display(item) for item in value]
    return value


def _run_settings(flow: models.Flow) -> dict[str, Any]:
    """Read the editable scalar settings without copying their enclosing configs."""
    limits = (flow.agent_config or {}).get("limits") or {}
    schedule = flow.schedule_config or {}
    return {
        "timeout_seconds": flow.timeout_seconds,
        **{field: limits.get(key) for field, key in FLOW_LIMIT_FIELDS.items()},
        "interval_every": schedule.get("every")
        if schedule.get("type") == "interval"
        else None,
    }


def _summary(flow: models.Flow) -> dict[str, Any]:
    """Return the bounded list projection."""
    return {
        "flow_id": str(flow.id),
        "name": _display(flow.name),
        "revision": flow.config_revision,
    }


def _detail(flow: models.Flow) -> dict[str, Any]:
    """Project definition fields without webhook, runner or schedule payloads."""
    schedule = flow.schedule_config or {}
    return _display(
        redact_agent_config(
            {
                **_summary(flow),
                "prompt_template": flow.prompt_template,
                "ai_model_id": str(flow.ai_model_id) if flow.ai_model_id else None,
                "result_schema": flow.result_schema,
                "validate_result": flow.validate_result,
                "trigger_event_source": flow.trigger_event_source,
                "trigger_event_types": flow.trigger_event_types,
                "schedule": {
                    k: v
                    for k, v in schedule.items()
                    if k in {"type", "expr", "at", "days", "every", "unit", "timezone"}
                },
                "run_settings": _run_settings(flow),
                "allowed_mcp_tools": [
                    {
                        k: v
                        for k, v in tool.items()
                        if k
                        in {"name", "tool_name", "source", "server_id", "server_name"}
                    }
                    if isinstance(tool, dict)
                    else tool
                    for tool in flow.allowed_mcp_tools or []
                ],
                "allowed_mcp_servers": flow.allowed_mcp_servers,
            }
        )
    )


def _digest(canonical: str) -> str:
    """Hash the exact stored review bytes, including resolved author and target."""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@require_permission("edit_flows")
def _propose(
    *,
    db: Session,
    current_user: models.User,
    flow: models.Flow,
    request: ProposeFlowChange,
    execution_id: UUID,
) -> dict[str, Any]:
    """Validate the complete candidate before committing only its review document."""
    from preloop.api.endpoints.flows import prepare_flow_update

    ensure_permission_in_oss(db, current_user, "edit_flows")
    require_flow_revision(if_match_condition(flow_etag(request.base_revision)), flow)
    before = _run_settings(flow)
    edits = request.changes.model_dump(exclude_unset=True)
    diff = [
        SettingChange(field=k, before=before[k], after=v)
        for k, v in sorted(edits.items())
        if before[k] != v
    ]
    if not diff:
        raise ToolError("Proposal does not change any setting")
    candidate = {
        k: getattr(flow, k) for k in FlowUpdate.model_fields if hasattr(flow, k)
    }
    # The runtime also accepts legacy name-only strings in stored tool grants.
    candidate["allowed_mcp_tools"] = [
        {"name": entry} if isinstance(entry, str) else entry
        for entry in flow.allowed_mcp_tools or []
    ]
    if "interval_every" in edits:
        schedule = flow.schedule_config or {}
        if (
            flow.trigger_event_source != "schedule"
            or schedule.get("type") != "interval"
        ):
            raise ToolError("An interval edit requires an existing interval trigger")
        candidate["schedule_config"] = {
            **schedule,
            "every": edits.pop("interval_every"),
        }
    try:
        complete = FlowUpdate.model_validate({**candidate, **edits})
        prepare_flow_update(
            db=db, flow=flow, flow_in=complete, current_user=current_user
        )
    except (ValidationError, HTTPException, ValueError):
        # Validator messages may quote raw stored settings or credentials.
        raise ToolError(
            "Resulting flow configuration failed validation or authorization"
        ) from None
    document = ProposalDocument(
        account_id=current_user.account_id,
        project_id=request.project_id,
        flow_id=flow.id,
        proposer_user_id=current_user.id,
        proposer_execution_id=execution_id,
        base_revision=request.base_revision,
        diff=diff,
        interval_unit=(flow.schedule_config or {}).get("unit")
        if "interval_every" in request.changes.model_fields_set
        else None,
        reason=request.reason,
        evidence=request.evidence,
        expected_outcome=request.expected_outcome,
        evaluation_window_days=request.evaluation_window_days,
    ).model_dump(mode="json")
    canonical = json.dumps(
        document, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    digest = _digest(canonical)
    if not flow_change_proposal.store_proposal(
        db,
        account_id=current_user.account_id,
        project_id=request.project_id,
        flow_id=flow.id,
        digest=digest,
        document=canonical,
    ):
        raise ToolError("Stored proposal integrity check failed")
    return {"digest": digest, "document": document}


@require_permission("view_flows")
def _scoped(
    *,
    db: Session,
    current_user: models.User,
    context: UserContext,
    request: Request,
    name: str,
) -> dict[str, Any]:
    """Bind project scope to the authenticated execution and current tracker policy."""
    ensure_permission_in_oss(db, current_user, "view_flows")
    account_id = current_user.account_id
    project_id = request.project_id
    key = crud_api_key.get(db, id=context.api_key_id, account_id=account_id)
    if (
        key is None
        or not key.is_valid()
        or key.user_id != current_user.id
        or key.requires_machine_authorization
        or is_read_only_review_api_key(key)
        or not mcp_scope_allowed(key.scopes, "mcp:write")
    ):
        raise ToolError("Flow credential unavailable")
    binding = key.context_data or {}
    if (
        binding.get("flow_execution_id") != context.flow_execution_id
        or binding.get("flow_id") != context.flow_id
        or not MCPConfigService.validate_tool_access(
            DEFAULT_TOOL_SERVER, name, binding.get("allowed_mcp_tools") or []
        )
    ):
        raise ToolError("Flow credential unavailable")
    caller = crud_flow.get_execution_project_flow(
        db,
        account_id=account_id,
        project_id=project_id,
        flow_id=UUID(context.flow_id),
        execution_id=UUID(context.flow_execution_id),
    )
    if not caller or not MCPConfigService.validate_tool_access(
        DEFAULT_TOOL_SERVER, name, caller.allowed_mcp_tools or []
    ):
        raise ToolError("Project unavailable to this execution")
    project = crud_project.get(db, id=project_id, account_id=account_id)
    if not project or not project.is_active or not project.organization.is_active:
        raise ToolError("Project unavailable to this execution")
    tracker = project.organization.tracker
    if (
        not tracker.is_active
        or tracker.is_deleted
        or project_scope_denial(db, tracker, project.organization, project)
    ):
        raise ToolError("Project unavailable to this execution")

    if isinstance(request, InspectFlows) and request.flow_id is None:
        visible = []
        after = request.after
        while len(visible) <= PAGE_SIZE:
            page = crud_flow.list_single_project(
                db,
                account_id=account_id,
                project_id=project_id,
                after=after,
                limit=PAGE_SIZE + 1,
            )
            visible.extend(filter_viewable(db, current_user, VISIBLE_FLOW, page))
            if len(page) <= PAGE_SIZE:
                break
            after = page[-1].id
        return {
            "project_id": str(project_id),
            "flows": [_summary(f) for f in visible[:PAGE_SIZE]],
            "next_after": str(visible[PAGE_SIZE - 1].id)
            if len(visible) > PAGE_SIZE
            else None,
        }
    flow = crud_flow.get_single_project(
        db,
        account_id=account_id,
        project_id=project_id,
        flow_id=request.flow_id,
        lock=isinstance(request, ProposeFlowChange),
    )
    if not flow or not filter_viewable(db, current_user, VISIBLE_FLOW, [flow]):
        raise ToolError("Flow unavailable in this project")
    if isinstance(request, InspectFlows):
        return {"project_id": str(project_id), **_detail(flow)}
    if isinstance(request, ProposeFlowChange):
        return _propose(
            db=db,
            current_user=current_user,
            flow=flow,
            request=request,
            execution_id=UUID(context.flow_execution_id),
        )
    row = flow_change_proposal.get_proposal(
        db,
        account_id=account_id,
        project_id=project_id,
        flow_id=flow.id,
        digest=request.digest,
    )
    if row is None or _digest(row.document) != request.digest:
        raise ToolError("Proposal unavailable in this scope")
    return {
        "digest": request.digest,
        "document": json.loads(row.document),
        "current_revision": flow.config_revision,
    }


async def _dispatch(name: str, request: Request, ctx: Context | None) -> dict[str, Any]:
    """Apply the MCP policy gate and open a fresh scoped CRUD session."""
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
        or name not in (context.allowed_flow_tools or [])
    ):
        raise ToolError("An explicitly authorized flow execution is required")
    approved, denial = await require_approval(
        tool_name=name,
        tool_source="builtin",
        account_id=context.account_id,
        arguments={"request": request.model_dump(mode="json", exclude_unset=True)},
        ctx=ctx,
        workflow_id=_rule_workflow_id_var.get(None),
        correlation_id=_correlation_id_var.get(None),
        justification=_justification_var.get(None),
    )
    if not approved:
        raise ToolError(denial or "Tool call denied")

    def run() -> dict[str, Any]:
        """Resolve the active account member inside the request transaction."""
        with get_session_factory()() as db:
            user = crud_user.get(db, id=context.user_id, account_id=context.account_id)
            if user is None or not user.is_active:
                raise ToolError("Flow principal unavailable")
            try:
                return _scoped(
                    db=db,
                    current_user=user,
                    context=context,
                    request=request,
                    name=name,
                )
            except ValidationError:
                raise ToolError("Invalid stored flow configuration") from None
            except HTTPException as exc:
                if exc.status_code == 412:
                    raise ToolError("Flow base revision has changed") from None
                raise ToolError("Flow access denied") from None

    return await run_db_off_loop(run)


async def inspect_flows(
    request: InspectFlows, ctx: Context | None = None
) -> dict[str, Any]:
    """List or inspect current definitions visible in the bound project."""
    return await _dispatch("inspect_flows", request, ctx)


async def propose_flow_change(
    request: ProposeFlowChange, ctx: Context | None = None
) -> ProposalReceipt:
    """Persist the canonical proposal for review."""
    return ProposalReceipt.model_validate(
        await _dispatch("propose_flow_change", request, ctx)
    )


async def get_flow_change_proposal(
    request: ReadFlowProposal, ctx: Context | None = None
) -> ProposalReview:
    """Read the immutable document with fresh authorization."""
    return ProposalReview.model_validate(
        await _dispatch("get_flow_change_proposal", request, ctx)
    )


def register_flow_definition_tools(mcp: DynamicFastMCP) -> None:
    """Register the exact tool inputs advertised by the catalog."""
    handlers: tuple[Callable[..., Any], ...] = (
        inspect_flows,
        propose_flow_change,
        get_flow_change_proposal,
    )
    for handler, definition in zip(handlers, FLOW_DEFINITION_TOOLS, strict=True):
        tool = FunctionTool.from_function(
            handler, description=definition["description"]
        )
        tool.parameters = definition["schema"]
        mcp.add_tool(tool)
