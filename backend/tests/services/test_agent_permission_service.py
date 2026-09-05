"""Tests for agent permission workflow resolution."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from preloop.models import models
from preloop.services.agent_permission_service import (
    AGENT_TOOL_APPROVALS_WORKFLOW_NAME,
    _managed_agent_approval_workflow_pin,
    resolve_workflow,
    request_agent_permission,
)
from preloop.services.approval_workflow_service import DEFAULT_APPROVAL_TYPE


@pytest.mark.asyncio
async def test_resolve_workflow_creates_standard_agent_tool_workflow() -> None:
    """Auto-created agent workflows must use the canonical approval type."""
    account_id = str(uuid.uuid4())
    approver_id = uuid.uuid4()
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.rollback = AsyncMock()

    empty_result = MagicMock()
    empty_result.scalars.return_value.first.return_value = None
    db.execute = AsyncMock(return_value=empty_result)

    workflow = await resolve_workflow(db, account_id, approver_id)

    assert workflow.name == AGENT_TOOL_APPROVALS_WORKFLOW_NAME
    assert workflow.approval_type == DEFAULT_APPROVAL_TYPE
    assert workflow.approver_user_ids == [str(approver_id)]
    db.add.assert_called_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_workflow_recovers_from_duplicate_name_race() -> None:
    """Concurrent creators should observe the existing workflow after conflict."""
    account_id = str(uuid.uuid4())
    existing = models.ApprovalWorkflow(
        id=uuid.uuid4(),
        account_id=account_id,
        name=AGENT_TOOL_APPROVALS_WORKFLOW_NAME,
        approval_type=DEFAULT_APPROVAL_TYPE,
    )
    db = AsyncMock()
    db.commit = AsyncMock(side_effect=IntegrityError("insert", {}, Exception()))
    db.refresh = AsyncMock()
    db.rollback = AsyncMock()

    empty_result = MagicMock()
    empty_result.scalars.return_value.first.return_value = None
    existing_result = MagicMock()
    existing_result.scalars.return_value.first.return_value = existing
    # Lookups before the create attempt: account-defaults pin, account
    # default workflow, any workflow, the named agent-tool workflow; after
    # the IntegrityError one more fetch finds the concurrently created row.
    db.execute = AsyncMock(
        side_effect=[
            empty_result,
            empty_result,
            empty_result,
            empty_result,
            existing_result,
        ]
    )

    workflow = await resolve_workflow(db, account_id, uuid.uuid4())

    assert workflow is existing
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_workflow_prefers_agent_configured_workflow() -> None:
    """A workflow pinned on the managed agent via subject governance must win
    over the account default."""
    account_id = str(uuid.uuid4())
    managed_agent_id = uuid.uuid4()
    pinned = models.ApprovalWorkflow(
        id=uuid.uuid4(),
        account_id=account_id,
        name="Agent-specific workflow",
        approval_type=DEFAULT_APPROVAL_TYPE,
    )
    account = MagicMock()
    account.meta_data = {
        "subject_governance": {
            "managed_agents": {
                str(managed_agent_id): {"approval_workflow_id": str(pinned.id)}
            },
            "api_keys": {},
        }
    }

    account_and_workflow = MagicMock()
    account_and_workflow.first.return_value = (account, pinned)

    db = AsyncMock()
    db.execute = AsyncMock(return_value=account_and_workflow)

    workflow = await resolve_workflow(
        db, account_id, uuid.uuid4(), managed_agent_id=managed_agent_id
    )

    assert workflow is pinned
    # Account + pinned workflow are loaded in one outerjoin round-trip.
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_resolve_workflow_falls_back_when_agent_pin_missing() -> None:
    """When the pinned workflow no longer exists, resolution falls back to the
    account default instead of failing the permission check."""
    account_id = str(uuid.uuid4())
    managed_agent_id = uuid.uuid4()
    default_workflow = models.ApprovalWorkflow(
        id=uuid.uuid4(),
        account_id=account_id,
        name="Default Approval Workflow",
        approval_type=DEFAULT_APPROVAL_TYPE,
        is_default=True,
    )
    account = MagicMock()
    account.meta_data = {
        "subject_governance": {
            "managed_agents": {
                str(managed_agent_id): {"approval_workflow_id": str(uuid.uuid4())}
            },
            "api_keys": {},
        }
    }

    account_and_missing = MagicMock()
    account_and_missing.first.return_value = (account, None)
    default_result = MagicMock()
    default_result.scalars.return_value.first.return_value = default_workflow

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[account_and_missing, default_result])

    workflow = await resolve_workflow(
        db, account_id, uuid.uuid4(), managed_agent_id=managed_agent_id
    )

    assert workflow is default_workflow


def test_managed_agent_approval_workflow_pin_rejects_unsafe_segments() -> None:
    """Pin expression must coerce to UUID so free-form strings cannot smuggle
    arbitrary path segments; the UUID becomes one text argument of
    json_extract_path_text."""
    agent_id = uuid.uuid4()
    expr = _managed_agent_approval_workflow_pin(agent_id)
    compiled = str(expr.compile(compile_kwargs={"literal_binds": True}))
    assert "json_extract_path_text" in compiled
    assert str(agent_id) in compiled
    # String UUID form is accepted via coercion.
    expr_from_str = _managed_agent_approval_workflow_pin(str(agent_id))  # type: ignore[arg-type]
    assert str(agent_id) in str(
        expr_from_str.compile(compile_kwargs={"literal_binds": True})
    )
    try:
        _managed_agent_approval_workflow_pin("evil,injected}")  # type: ignore[arg-type]
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for non-UUID path segment")


def _governance_off_db(default_workflow: models.ApprovalWorkflow) -> AsyncMock:
    """An async DB mock whose governance lookup reports approvals switched off."""
    account = MagicMock()
    account.meta_data = {}

    # _native_tool_approvals_disabled reads (agent_setting, account_default)
    # as one row.
    setting_result = MagicMock()
    setting_result.first.return_value = ("off", None)
    account_and_workflow = MagicMock()
    account_and_workflow.first.return_value = (account, None)
    # Account-defaults workflow pin lookup (no pin configured).
    defaults_pin_result = MagicMock()
    defaults_pin_result.scalars.return_value.first.return_value = None
    default_result = MagicMock()
    default_result.scalars.return_value.first.return_value = default_workflow

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            setting_result,
            account_and_workflow,
            defaults_pin_result,
            default_result,
        ]
    )
    return db


@pytest.mark.asyncio
async def test_request_agent_permission_disabled_records_auto_approval() -> None:
    """native_tool_approvals="off" → allowed, but *recorded*, never silent.

    This is the core of the migration away from the old invisible
    short-circuit. The call must still produce an ApprovalRequest carrying
    ``auto_approved_reason='native_tool_approvals_off'`` so the audit trail
    shows what ran unsupervised; only the human gate and the notification are
    skipped.
    """
    account_id = str(uuid.uuid4())
    managed_agent_id = uuid.uuid4()
    default_workflow = models.ApprovalWorkflow(
        id=uuid.uuid4(),
        account_id=account_id,
        name="Default Approval Workflow",
        approval_type=DEFAULT_APPROVAL_TYPE,
        is_default=True,
        timeout_seconds=300,
    )
    db = _governance_off_db(default_workflow)

    tool_config = MagicMock()
    tool_config.id = uuid.uuid4()

    approval = MagicMock()
    approval.id = uuid.uuid4()
    approval.status = "approved"
    approval.approver_comment = "Auto-approved without review"

    with (
        patch(
            "preloop.services.agent_permission_service.get_async_db_session"
        ) as mock_get_session,
        patch("preloop.services.approval_service.ApprovalService") as mock_service_cls,
        patch(
            "preloop.models.crud.tool_configuration."
            "get_tool_config_by_name_and_source_async",
            new=AsyncMock(return_value=tool_config),
        ),
        patch(
            "preloop.services.agent_permission_service.apply_native_access_rules",
            new=AsyncMock(return_value=None),
        ),
    ):
        mock_get_session.return_value.__aenter__.return_value = db
        mock_service = AsyncMock()
        mock_service.create_and_notify = AsyncMock(return_value=approval)
        mock_service_cls.return_value = mock_service

        decision, _reason, request_id, _timed_out = await request_agent_permission(
            base_url="http://localhost",
            account_id=account_id,
            user_id=uuid.uuid4(),
            managed_agent_id=managed_agent_id,
            runtime_session_id=None,
            managed_agent_name="Claude Code",
            source="claude_code",
            tool_name="Bash",
            tool_input={"command": "ls"},
            agent_reasoning=None,
            client_decision=None,
        )

    # The agent is still allowed to proceed, exactly as before the migration.
    assert decision == "allow"
    # ...but now there is a request id, i.e. a record, where there was None.
    assert request_id == str(approval.id)

    mock_service.create_and_notify.assert_awaited_once()
    kwargs = mock_service.create_and_notify.await_args.kwargs
    assert (
        kwargs["standing_bypass_reason"]
        == models.AutoApprovedReason.NATIVE_TOOL_APPROVALS_OFF
    )


@pytest.mark.asyncio
async def test_request_agent_permission_disabled_allows_when_recording_fails() -> None:
    """A recording failure must not start gating an agent set to "off".

    The operator explicitly disabled approvals for this agent. Losing the audit
    row is bad, but silently blocking a tool call they said should not be
    blocked would be a regression, so this path fails *open* — and only this
    path: enforced agents still propagate the error.
    """
    account_id = str(uuid.uuid4())
    default_workflow = models.ApprovalWorkflow(
        id=uuid.uuid4(),
        account_id=account_id,
        name="Default Approval Workflow",
        approval_type=DEFAULT_APPROVAL_TYPE,
        is_default=True,
        timeout_seconds=300,
    )
    db = _governance_off_db(default_workflow)

    tool_config = MagicMock()
    tool_config.id = uuid.uuid4()

    with (
        patch(
            "preloop.services.agent_permission_service.get_async_db_session"
        ) as mock_get_session,
        patch("preloop.services.approval_service.ApprovalService") as mock_service_cls,
        patch(
            "preloop.models.crud.tool_configuration."
            "get_tool_config_by_name_and_source_async",
            new=AsyncMock(return_value=tool_config),
        ),
        patch(
            "preloop.services.agent_permission_service.apply_native_access_rules",
            new=AsyncMock(return_value=None),
        ),
    ):
        mock_get_session.return_value.__aenter__.return_value = db
        mock_service = AsyncMock()
        mock_service.create_and_notify = AsyncMock(
            side_effect=RuntimeError("database is on fire")
        )
        mock_service_cls.return_value = mock_service

        decision, reason, request_id, _timed_out = await request_agent_permission(
            base_url="http://localhost",
            account_id=account_id,
            user_id=uuid.uuid4(),
            managed_agent_id=uuid.uuid4(),
            runtime_session_id=None,
            managed_agent_name="Claude Code",
            source="claude_code",
            tool_name="Bash",
            tool_input={"command": "ls"},
            agent_reasoning=None,
            client_decision=None,
        )

    assert decision == "allow"
    assert reason == "Native tool approvals are disabled for this agent in Preloop"
    assert request_id is None


@pytest.mark.asyncio
async def test_request_agent_permission_enforced_propagates_recording_failure() -> None:
    """An enforced agent must fail closed when the approval cannot be created."""
    account_id = str(uuid.uuid4())
    default_workflow = models.ApprovalWorkflow(
        id=uuid.uuid4(),
        account_id=account_id,
        name="Default Approval Workflow",
        approval_type=DEFAULT_APPROVAL_TYPE,
        is_default=True,
        timeout_seconds=300,
    )
    account = MagicMock()
    account.meta_data = {}
    setting_result = MagicMock()
    setting_result.scalar.return_value = None
    account_and_workflow = MagicMock()
    account_and_workflow.first.return_value = (account, None)
    default_result = MagicMock()
    default_result.scalars.return_value.first.return_value = default_workflow
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[setting_result, account_and_workflow, default_result]
    )

    tool_config = MagicMock()
    tool_config.id = uuid.uuid4()

    with (
        patch(
            "preloop.services.agent_permission_service.get_async_db_session"
        ) as mock_get_session,
        patch("preloop.services.approval_service.ApprovalService") as mock_service_cls,
        patch(
            "preloop.models.crud.tool_configuration."
            "get_tool_config_by_name_and_source_async",
            new=AsyncMock(return_value=tool_config),
        ),
        patch(
            "preloop.services.agent_permission_service.apply_native_access_rules",
            new=AsyncMock(return_value=None),
        ),
        pytest.raises(RuntimeError, match="database is on fire"),
    ):
        mock_get_session.return_value.__aenter__.return_value = db
        mock_service = AsyncMock()
        mock_service.create_and_notify = AsyncMock(
            side_effect=RuntimeError("database is on fire")
        )
        mock_service_cls.return_value = mock_service

        await request_agent_permission(
            base_url="http://localhost",
            account_id=account_id,
            user_id=uuid.uuid4(),
            managed_agent_id=uuid.uuid4(),
            runtime_session_id=None,
            managed_agent_name="Claude Code",
            source="claude_code",
            tool_name="Bash",
            tool_input={"command": "ls"},
            agent_reasoning=None,
            client_decision=None,
        )


@pytest.mark.asyncio
async def test_request_agent_permission_absent_setting_runs_approval_path() -> None:
    """No native_tool_approvals setting → the normal approval path still runs."""
    account_id = str(uuid.uuid4())
    managed_agent_id = uuid.uuid4()
    default_workflow = models.ApprovalWorkflow(
        id=uuid.uuid4(),
        account_id=account_id,
        name="Default Approval Workflow",
        approval_type=DEFAULT_APPROVAL_TYPE,
        is_default=True,
        timeout_seconds=300,
    )
    account = MagicMock()
    account.meta_data = {}

    # 1) governance setting lookup (absent), 2) account+pin outerjoin,
    # 3) account-default workflow lookup.
    setting_result = MagicMock()
    setting_result.scalar.return_value = None
    account_and_workflow = MagicMock()
    account_and_workflow.first.return_value = (account, None)
    default_result = MagicMock()
    default_result.scalars.return_value.first.return_value = default_workflow

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[setting_result, account_and_workflow, default_result]
    )

    tool_config = MagicMock()
    tool_config.id = uuid.uuid4()

    approval = MagicMock()
    approval.id = uuid.uuid4()
    approval.status = "approved"
    approval.approver_comment = "Looks safe"

    with (
        patch(
            "preloop.services.agent_permission_service.get_async_db_session"
        ) as mock_get_session,
        patch("preloop.services.approval_service.ApprovalService") as mock_service_cls,
        patch(
            "preloop.models.crud.tool_configuration."
            "get_tool_config_by_name_and_source_async",
            new=AsyncMock(return_value=tool_config),
        ),
        patch(
            "preloop.services.agent_permission_service.apply_native_access_rules",
            new=AsyncMock(return_value=None),
        ),
    ):
        mock_get_session.return_value.__aenter__.return_value = db
        mock_service = AsyncMock()
        mock_service.create_and_notify = AsyncMock(return_value=approval)
        mock_service_cls.return_value = mock_service

        decision, reason, request_id, _timed_out = await request_agent_permission(
            base_url="http://localhost",
            account_id=account_id,
            user_id=uuid.uuid4(),
            managed_agent_id=managed_agent_id,
            runtime_session_id=None,
            managed_agent_name="Claude Code",
            source="claude_code",
            tool_name="Bash",
            tool_input={"command": "ls"},
            agent_reasoning=None,
            client_decision=None,
        )

    assert decision == "allow"
    assert reason == "Looks safe"
    assert request_id == str(approval.id)
    mock_service.create_and_notify.assert_awaited_once()


def _enforced_db(default_workflow: models.ApprovalWorkflow) -> AsyncMock:
    """Async DB mock whose governance lookup reports approvals enforced."""
    account = MagicMock()
    account.meta_data = {}
    setting_result = MagicMock()
    setting_result.first.return_value = (None, None)
    account_and_workflow = MagicMock()
    account_and_workflow.first.return_value = (account, None)
    defaults_pin_result = MagicMock()
    defaults_pin_result.scalars.return_value.first.return_value = None
    default_result = MagicMock()
    default_result.scalars.return_value.first.return_value = default_workflow
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            setting_result,
            account_and_workflow,
            defaults_pin_result,
            default_result,
        ]
    )
    return db


def _permission_kwargs(**overrides: object) -> dict:
    values: dict = dict(
        base_url="http://localhost",
        account_id=str(uuid.uuid4()),
        user_id=uuid.uuid4(),
        managed_agent_id=uuid.uuid4(),
        runtime_session_id=None,
        managed_agent_name="Claude Code",
        source="claude_code",
        tool_name="Bash",
        tool_input={"command": "git push --force origin main"},
        agent_reasoning=None,
        client_decision=None,
    )
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_request_agent_permission_deny_rule_short_circuits_before_approval() -> (
    None
):
    """A matching deny rule returns deny and never creates an approval."""
    tool_config = MagicMock()
    tool_config.id = uuid.uuid4()
    tool_config.is_enabled = True
    db = AsyncMock()

    with (
        patch(
            "preloop.services.agent_permission_service.get_async_db_session"
        ) as mock_get_session,
        patch("preloop.services.approval_service.ApprovalService") as mock_service_cls,
        patch(
            "preloop.models.crud.tool_configuration."
            "get_tool_config_by_name_and_source_async",
            new=AsyncMock(return_value=tool_config),
        ),
        patch(
            "preloop.services.agent_permission_service.apply_native_access_rules",
            new=AsyncMock(
                return_value=(
                    "deny",
                    "Block force-push",
                    None,
                    {"source": "tool_access_rule"},
                )
            ),
        ),
    ):
        mock_get_session.return_value.__aenter__.return_value = db
        decision, reason, request_id, timed_out = await request_agent_permission(
            **_permission_kwargs()
        )

    assert decision == "deny"
    assert reason == "Block force-push"
    assert request_id is None
    assert timed_out is False
    mock_service_cls.assert_not_called()


@pytest.mark.asyncio
async def test_request_agent_permission_allow_rule_returns_allow_without_approval() -> (
    None
):
    """A matching allow rule returns allow and never creates an approval."""
    tool_config = MagicMock()
    tool_config.id = uuid.uuid4()
    tool_config.is_enabled = True
    db = AsyncMock()

    with (
        patch(
            "preloop.services.agent_permission_service.get_async_db_session"
        ) as mock_get_session,
        patch("preloop.services.approval_service.ApprovalService") as mock_service_cls,
        patch(
            "preloop.models.crud.tool_configuration."
            "get_tool_config_by_name_and_source_async",
            new=AsyncMock(return_value=tool_config),
        ),
        patch(
            "preloop.services.agent_permission_service.apply_native_access_rules",
            new=AsyncMock(
                return_value=(
                    "allow",
                    "Safe read",
                    None,
                    {"source": "tool_access_rule"},
                )
            ),
        ),
    ):
        mock_get_session.return_value.__aenter__.return_value = db
        decision, reason, request_id, timed_out = await request_agent_permission(
            **_permission_kwargs(tool_input={"command": "ls"})
        )

    assert decision == "allow"
    assert reason == "Safe read"
    assert request_id is None
    assert timed_out is False
    mock_service_cls.assert_not_called()


@pytest.mark.asyncio
async def test_request_agent_permission_require_approval_rule_uses_rule_workflow_and_rule_context() -> (
    None
):
    """A require_approval rule uses its workflow and rule context."""
    account_id = str(uuid.uuid4())
    default_workflow = models.ApprovalWorkflow(
        id=uuid.uuid4(),
        account_id=account_id,
        name="Default Approval Workflow",
        approval_type=DEFAULT_APPROVAL_TYPE,
        is_default=True,
        timeout_seconds=300,
    )
    rule_workflow = models.ApprovalWorkflow(
        id=uuid.uuid4(),
        account_id=account_id,
        name="Native deny-to-human",
        approval_type=DEFAULT_APPROVAL_TYPE,
        timeout_seconds=120,
    )
    db = _enforced_db(default_workflow)
    rule_wf_result = MagicMock()
    rule_wf_result.scalars.return_value.first.return_value = rule_workflow
    db.execute = AsyncMock(
        side_effect=[
            *db.execute.side_effect,
            rule_wf_result,
        ]
    )

    tool_config = MagicMock()
    tool_config.id = uuid.uuid4()
    tool_config.is_enabled = True
    rule_context = {
        "source": "tool_access_rule",
        "decision": "require_approval",
        "rule_name": "Force-push needs a human",
    }
    approval = MagicMock()
    approval.id = uuid.uuid4()
    approval.status = "approved"
    approval.approver_comment = "Looks safe"

    with (
        patch(
            "preloop.services.agent_permission_service.get_async_db_session"
        ) as mock_get_session,
        patch("preloop.services.approval_service.ApprovalService") as mock_service_cls,
        patch(
            "preloop.models.crud.tool_configuration."
            "get_tool_config_by_name_and_source_async",
            new=AsyncMock(return_value=tool_config),
        ),
        patch(
            "preloop.services.agent_permission_service.apply_native_access_rules",
            new=AsyncMock(
                return_value=(
                    "require_approval",
                    "Force-push needs a human",
                    rule_workflow.id,
                    rule_context,
                )
            ),
        ),
    ):
        mock_get_session.return_value.__aenter__.return_value = db
        mock_service = AsyncMock()
        mock_service.create_and_notify = AsyncMock(return_value=approval)
        mock_service_cls.return_value = mock_service

        decision, reason, request_id, _timed_out = await request_agent_permission(
            **_permission_kwargs(account_id=account_id)
        )

    assert decision == "allow"
    assert reason == "Looks safe"
    assert request_id == str(approval.id)
    kwargs = mock_service.create_and_notify.await_args.kwargs
    assert kwargs["approval_workflow"] is rule_workflow
    assert kwargs["rule_context"] == rule_context


@pytest.mark.asyncio
async def test_request_agent_permission_blocked_config_denies() -> None:
    """is_enabled=false on the agent-source config denies without approval."""
    tool_config = MagicMock()
    tool_config.id = uuid.uuid4()
    tool_config.is_enabled = False
    db = AsyncMock()

    with (
        patch(
            "preloop.services.agent_permission_service.get_async_db_session"
        ) as mock_get_session,
        patch("preloop.services.approval_service.ApprovalService") as mock_service_cls,
        patch(
            "preloop.models.crud.tool_configuration."
            "get_tool_config_by_name_and_source_async",
            new=AsyncMock(return_value=tool_config),
        ),
    ):
        mock_get_session.return_value.__aenter__.return_value = db
        decision, reason, request_id, timed_out = await request_agent_permission(
            **_permission_kwargs(client_decision="allow")
        )

    assert decision == "deny"
    assert reason == "Tool blocked in Preloop"
    assert request_id is None
    assert timed_out is False
    mock_service_cls.assert_not_called()


@pytest.mark.asyncio
async def test_request_agent_permission_no_rule_match_keeps_legacy_approval_path() -> (
    None
):
    """No matching rule keeps the hook-escalation approval path."""
    account_id = str(uuid.uuid4())
    default_workflow = models.ApprovalWorkflow(
        id=uuid.uuid4(),
        account_id=account_id,
        name="Default Approval Workflow",
        approval_type=DEFAULT_APPROVAL_TYPE,
        is_default=True,
        timeout_seconds=300,
    )
    db = _enforced_db(default_workflow)
    tool_config = MagicMock()
    tool_config.id = uuid.uuid4()
    tool_config.is_enabled = True
    approval = MagicMock()
    approval.id = uuid.uuid4()
    approval.status = "approved"
    approval.approver_comment = "Looks safe"

    with (
        patch(
            "preloop.services.agent_permission_service.get_async_db_session"
        ) as mock_get_session,
        patch("preloop.services.approval_service.ApprovalService") as mock_service_cls,
        patch(
            "preloop.models.crud.tool_configuration."
            "get_tool_config_by_name_and_source_async",
            new=AsyncMock(return_value=tool_config),
        ),
        patch(
            "preloop.services.agent_permission_service.apply_native_access_rules",
            new=AsyncMock(return_value=None),
        ),
    ):
        mock_get_session.return_value.__aenter__.return_value = db
        mock_service = AsyncMock()
        mock_service.create_and_notify = AsyncMock(return_value=approval)
        mock_service_cls.return_value = mock_service

        decision, reason, request_id, _timed_out = await request_agent_permission(
            **_permission_kwargs(account_id=account_id, tool_input={"command": "ls"})
        )

    assert decision == "allow"
    assert request_id == str(approval.id)
    kwargs = mock_service.create_and_notify.await_args.kwargs
    assert kwargs["rule_context"]["source"] == "agent_permission_hook"
    assert kwargs["approval_workflow"] is default_workflow


@pytest.mark.asyncio
async def test_request_agent_permission_rule_overrides_client_allow() -> None:
    """A matching deny rule wins over the agent's own client_decision=allow."""
    tool_config = MagicMock()
    tool_config.id = uuid.uuid4()
    tool_config.is_enabled = True
    db = AsyncMock()

    with (
        patch(
            "preloop.services.agent_permission_service.get_async_db_session"
        ) as mock_get_session,
        patch("preloop.services.approval_service.ApprovalService") as mock_service_cls,
        patch(
            "preloop.models.crud.tool_configuration."
            "get_tool_config_by_name_and_source_async",
            new=AsyncMock(return_value=tool_config),
        ),
        patch(
            "preloop.services.agent_permission_service.apply_native_access_rules",
            new=AsyncMock(
                return_value=(
                    "deny",
                    "Block .github writes",
                    None,
                    {"source": "tool_access_rule"},
                )
            ),
        ),
    ):
        mock_get_session.return_value.__aenter__.return_value = db
        decision, reason, request_id, timed_out = await request_agent_permission(
            **_permission_kwargs(
                tool_name="Write",
                tool_input={"file_path": ".github/workflows/ci.yml", "content": "x"},
                client_decision="allow",
            )
        )

    assert decision == "deny"
    assert reason == "Block .github writes"
    assert request_id is None
    assert timed_out is False
    mock_service_cls.assert_not_called()


def test_native_tool_approvals_setting_reads_on_real_database(db_session, test_user):
    """The native_tool_approvals extraction executes on real Postgres.

    Same regression class as the workflow-pin test below: mock-based tests
    never execute the JSON path SQL, so run the exact select shape used by
    ``_native_tool_approvals_disabled`` against the actual database.
    """
    import uuid as uuid_module

    from sqlalchemy import select

    from preloop.models import models
    from preloop.models.crud import crud_account
    from preloop.services.agent_permission_service import (
        _managed_agent_governance_field,
    )

    agent_id = uuid_module.uuid4()
    account = crud_account.get(db_session, id=test_user.account_id)
    meta = dict(account.meta_data or {})
    meta["subject_governance"] = {
        "managed_agents": {str(agent_id): {"native_tool_approvals": "off"}}
    }
    crud_account.update(db_session, db_obj=account, obj_in={"meta_data": meta})

    value = db_session.execute(
        select(_managed_agent_governance_field(agent_id, "native_tool_approvals"))
        .select_from(models.Account)
        .where(models.Account.id == test_user.account_id)
        .limit(1)
    ).scalar()
    assert value == "off"

    # Agents without the setting read as NULL (approvals enforced).
    other_value = db_session.execute(
        select(
            _managed_agent_governance_field(
                uuid_module.uuid4(), "native_tool_approvals"
            )
        )
        .select_from(models.Account)
        .where(models.Account.id == test_user.account_id)
        .limit(1)
    ).scalar()
    assert other_value is None


def test_workflow_pin_join_executes_on_real_database(db_session, test_user):
    """Regression: the workflow-pin extraction must execute on real Postgres.

    The previous implementation bound the JSON path as VARCHAR
    (``meta_data #>> $1::VARCHAR``), which raises ``operator does not
    exist: json #>> character varying`` at runtime — invisible to the
    AsyncMock-based tests above. This test seeds a real pin and runs the
    same join shape through the actual database.
    """
    import uuid as uuid_module

    from sqlalchemy import String, and_, cast, select

    from preloop.models import models
    from preloop.models.crud import crud_account
    from preloop.services.agent_permission_service import (
        _managed_agent_approval_workflow_pin,
    )

    agent_id = uuid_module.uuid4()
    workflow = models.ApprovalWorkflow(
        account_id=test_user.account_id,
        name="Pinned workflow",
        approval_type="manual",
        channel="email",
    )
    db_session.add(workflow)
    db_session.flush()

    account = crud_account.get(db_session, id=test_user.account_id)
    meta = dict(account.meta_data or {})
    meta["subject_governance"] = {
        "managed_agents": {str(agent_id): {"approval_workflow_id": str(workflow.id)}}
    }
    crud_account.update(db_session, db_obj=account, obj_in={"meta_data": meta})

    pin = _managed_agent_approval_workflow_pin(agent_id)
    row = db_session.execute(
        select(models.Account, models.ApprovalWorkflow)
        .select_from(models.Account)
        .outerjoin(
            models.ApprovalWorkflow,
            and_(
                models.ApprovalWorkflow.account_id == models.Account.id,
                cast(models.ApprovalWorkflow.id, String) == pin,
            ),
        )
        .where(models.Account.id == test_user.account_id)
        .limit(1)
    ).first()

    assert row is not None
    assert row[1] is not None, "pinned workflow should join"
    assert row[1].id == workflow.id

    # Unpinned agent: query still executes, workflow side is NULL.
    other_pin = _managed_agent_approval_workflow_pin(uuid_module.uuid4())
    row = db_session.execute(
        select(models.Account, models.ApprovalWorkflow)
        .select_from(models.Account)
        .outerjoin(
            models.ApprovalWorkflow,
            and_(
                models.ApprovalWorkflow.account_id == models.Account.id,
                cast(models.ApprovalWorkflow.id, String) == other_pin,
            ),
        )
        .where(models.Account.id == test_user.account_id)
        .limit(1)
    ).first()
    assert row is not None
    assert row[1] is None
