"""Determine whether a lean flow may need approval polling or replay."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.services.subject_governance import (
    get_scoped_tool_rules,
    is_tool_enabled_for_subject,
)
from preloop.tools.builtin_defs import TOOL_NAME_ALIASES

if TYPE_CHECKING:
    from preloop.services.dynamic_mcp_server import UserContext


def flow_may_use_approval_status(
    db: Session,
    context: UserContext,
    *,
    allowed: set[str],
    configs: Sequence[models.ToolConfiguration],
    account_meta: dict[str, Any],
    proxied_names: set[str],
) -> bool:
    """Keep replay available for every potentially applicable approval path.

    Discovery has no call arguments: conditional rules cannot be evaluated
    here. External resource-owner/grant policies can also change independently,
    so an allowed proxied tool with available workflows retains the helper.
    This is an availability optimization, never an authorization decision.
    Callers must retain the helper if metadata cannot be read completely.

    Args:
        db: Existing discovery session; reads use CRUD only.
        context: Authenticated flow identity.
        allowed: Normalized flow allow-list.
        configs: Tool configurations visible to this identity.
        account_meta: Account policy metadata.
        proxied_names: Client-visible external tool names.

    Returns:
        Whether an approval could require the status/replay helper.
    """
    if not allowed:
        return False
    if context.flow_execution_id and crud.crud_approval_request.get_multi_by_execution(
        db,
        execution_id=context.flow_execution_id,
        account_id=context.account_id,
        limit=1,
    ):
        # A completed approval can still hold a result that the resumed agent
        # has not read. Policy edits must not remove that replay path.
        return True
    if not crud.crud_approval_workflow.get_multi_by_account(
        db, account_id=context.account_id, limit=1
    ):
        return False

    subject = {
        "api_key_id": context.api_key_id,
        "flow_id": context.flow_id,
        "managed_agent_id": context.managed_agent_id,
    }
    allowed = {
        name
        for name in allowed
        if is_tool_enabled_for_subject(
            account_meta, tool_name=name, subject_context=subject
        )
    }
    if allowed & {
        "ask_user",
        "request_approval",
        "permission_prompt",
        "send_note",
    }:
        return True
    if allowed & proxied_names:
        return True

    default_workflow = crud.crud_approval_workflow.get_default(
        db, account_id=context.account_id
    )
    matching_config_ids: set[str] = set()
    for name in allowed:
        names = {name, TOOL_NAME_ALIASES.get(name, name)}
        matching = [
            config
            for config in configs
            if config.tool_name in names and config.tool_source == "builtin"
        ]
        if any(config.approval_workflow_id for config in matching):
            return True
        scoped = get_scoped_tool_rules(
            account_meta, tool_name=name, subject_context=subject
        )
        if not scoped and (alias := TOOL_NAME_ALIASES.get(name)):
            scoped = get_scoped_tool_rules(
                account_meta, tool_name=alias, subject_context=subject
            )
        for rule in scoped:
            if rule.get("is_enabled", True) and (
                rule.get("approval_workflow_id") or default_workflow
            ):
                # Even allow/deny conditions can fail evaluation and fall
                # back to approval in policy_evaluator.
                return True
        matching_config_ids.update(str(config.id) for config in matching)

    if crud.crud_tool_access_rule.has_approval_path(
        db,
        config_ids=sorted(matching_config_ids),
        account_id=context.account_id,
        has_default_workflow=default_workflow is not None,
    ):
        return True

    from preloop.services.sensitive_data.policy_store import (
        parse_sensitive_data_config,
    )
    from preloop.services.sensitive_data.tool_policy import applicable_rules

    sensitive = parse_sensitive_data_config(
        account_meta.get("sensitive_data"), strict=True
    )
    for name in allowed:
        for target in ("tool.args", "tool.result"):
            for sensitive_rule in applicable_rules(
                sensitive,
                target,
                tool_name=name,
                server_name="preloop-mcp",
                managed_agent_id=context.managed_agent_id,
            ):
                if sensitive_rule.action == "require_approval" and (
                    sensitive_rule.approval_workflow or default_workflow
                ):
                    return True
    return False
