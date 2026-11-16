"""Observe an unmatched MCP tool and share one approval across its calls."""

import os
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from preloop.models.crud import (
    crud_account,
    crud_flow,
    crud_flow_execution,
    tool_registry,
)
from preloop.models.crud.approval_request import get_approval_request_async
from preloop.models.crud.tool_configuration import (
    get_or_create_tool_configuration_async,
)
from preloop.models.schemas.tool_configuration import ToolConfigurationCreate
from preloop.services.approval_rule_context import (
    SOURCE_TOOL_REGISTRY,
    build_rule_context,
)
from preloop.services.approval_service import ApprovalService
from preloop.services.policy_evaluator import PolicyDecision


async def observe(
    db: Any,
    *,
    account_id: UUID,
    tool_name: str,
    tool_args: dict[str, Any],
    server_id: UUID | None,
    server_label: str | None,
    server_name: str | None,
    workflow: Any,
    user_id: UUID | None,
    subject_context: dict[str, Any] | None,
    execution_id: UUID | None,
    record: bool,
) -> PolicyDecision:
    """Gate a route; attach its first request while the entry lock is held.

    Simulation does not create observations or approvals. ApprovalService commits
    the entry and its request together before delivering notifications. A losing
    observer waits for that commit and adopts the attached request.
    """
    registry_name = (
        tool_name.split("__", 2)[2]
        if server_label and tool_name.startswith("mcp__")
        else tool_name
    )
    if not record:
        existing = await db.run_sync(
            lambda session: tool_registry.find(
                session,
                account_id=account_id,
                server_id=server_id,
                server_label=server_label,
                tool_name=registry_name,
            )
        )
        state = existing.state if existing else "pending"
        return PolicyDecision(
            "allow"
            if state == "approved"
            else "deny"
            if state == "denied"
            else "require_approval",
            workflow.id if workflow and state == "pending" else None,
            f"MCP tool registry: {state} (simulation)",
            matched=False,
            source="registry",
        )
    from preloop.models.crud import crud_account_halt

    await db.run_sync(
        lambda session: crud_account_halt.lock_account(session, account_id=account_id)
    )
    subject = subject_context or {}
    agent_id = subject.get("managed_agent_id")
    entry, _ = await db.run_sync(
        lambda session: tool_registry.get_or_create_seen(
            session,
            account_id=account_id,
            source="mcp",
            server_id=server_id,
            server_label=server_label,
            tool_name=registry_name,
            agent_id=UUID(str(agent_id)) if agent_id else None,
        )
    )
    if entry.state != "pending":
        await db.commit()
        return PolicyDecision(
            "allow" if entry.state == "approved" else "deny",
            None,
            f"MCP tool registry: {entry.state}",
            matched=False,
            source="registry",
        )
    if workflow is None:
        await db.commit()
        return PolicyDecision(
            "deny",
            None,
            "Registry approval workflow is unavailable",
            matched=False,
            source="registry",
        )
    request = None
    if entry.approval_request_id:
        request = await get_approval_request_async(
            db, request_id=entry.approval_request_id
        )
        if request and request.status in ("approved", "declined"):
            await db.run_sync(
                lambda session: tool_registry.resolve_approval(
                    session, request=request, user_id=None
                )
            )
            await db.commit()
            return PolicyDecision(
                "allow" if request.status == "approved" else "deny",
                None,
                f"MCP tool registry: {request.status}",
                matched=False,
                source="registry",
            )
        if request and (
            request.status != "pending"
            or (
                request.expires_at
                and request.expires_at.replace(tzinfo=timezone.utc)
                <= datetime.now(timezone.utc)
            )
        ):
            request = None
    if request is None:
        config = await get_or_create_tool_configuration_async(
            db,
            account_id=str(account_id),
            commit=False,
            obj_in=ToolConfigurationCreate(
                account_id=str(account_id),
                tool_name=tool_name,
                tool_source="mcp",
                mcp_server_id=str(server_id) if server_id else None,
                is_enabled=True,
                custom_config={},
            ),
        )
        execution = (
            await db.run_sync(
                lambda session: crud_flow_execution.get(
                    session, id=execution_id, account_id=str(account_id)
                )
            )
            if execution_id
            else None
        )
        flow = (
            await db.run_sync(
                lambda session: crud_flow.get(session, id=execution.flow_id)
            )
            if execution
            else None
        )
        account = await db.run_sync(
            lambda session: crud_account.get(session, id=account_id)
        )
        from preloop.services.approval_window import resolve_approval_window

        window = resolve_approval_window(flow=flow, workflow=workflow, account=account)
        request = await ApprovalService(
            db, os.getenv("PRELOOP_URL", "http://localhost:8000")
        ).create_and_notify(
            account_id=str(account_id),
            tool_configuration_id=config.id,
            approval_workflow=workflow,
            timeout_seconds=window.seconds,
            tool_name=tool_name,
            tool_args=tool_args,
            user_id=user_id,
            managed_agent_id=UUID(str(agent_id)) if agent_id else None,
            execution_id=str(execution_id) if execution_id else None,
            runtime_session_id=UUID(str(subject["runtime_session_id"]))
            if subject.get("runtime_session_id")
            else None,
            server_name=server_name or server_label,
            api_key_id=UUID(str(subject["api_key_id"]))
            if subject.get("api_key_id")
            else None,
            registry_entry_id=entry.id,
            rule_context={
                **build_rule_context(
                    source=SOURCE_TOOL_REGISTRY, decision="require_approval"
                ),
                "registry_entry_id": str(entry.id),
                "decision": "require_approval",
                "explanation": "Approve this MCP tool route once for the account.",
            },
        )
    if request.status in ("approved", "declined"):
        return PolicyDecision(
            "allow" if request.status == "approved" else "deny",
            None,
            f"MCP tool registry: {request.status}",
            matched=False,
            source="registry",
        )
    await db.commit()
    return PolicyDecision(
        "require_approval",
        workflow.id,
        "MCP tool registry: pending approval",
        matched=False,
        source="registry",
        rule_context={
            **build_rule_context(
                source=SOURCE_TOOL_REGISTRY, decision="require_approval"
            ),
            "registry_entry_id": str(entry.id),
            "registry_approval_request_id": str(request.id),
            "decision": "require_approval",
        },
    )


def observe_sync(db: Any, **kwargs: Any) -> PolicyDecision:
    """Bridge sync evaluators, including read-only callers on an event loop."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from preloop.models.db.session import SyncApprovalSession

    def run() -> PolicyDecision:
        """Use a separate loop; the sync session has only one active caller."""
        return asyncio.run(observe(SyncApprovalSession(db), **kwargs))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return run()
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(run).result()
