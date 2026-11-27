"""Tests for agent permission workflow resolution."""

from __future__ import annotations

import uuid
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from preloop.models import models
from preloop.services.agent_permission_service import (
    AGENT_TOOL_APPROVALS_WORKFLOW_NAME,
    BLOCKED_TOOL_REASON,
    _managed_agent_approval_workflow_pin,
    apply_native_access_rules,
    resolve_workflow,
    request_agent_permission,
)
from preloop.services.approval_rule_context import SOURCE_SUBJECT_SCOPED_RULE
from preloop.services.approval_workflow_service import DEFAULT_APPROVAL_TYPE
from preloop.services.policy_evaluator import PolicyDecision


@pytest.fixture(autouse=True)
def mock_native_delivery_readiness() -> Any:
    """Keep permission unit tests independent of notification infrastructure."""
    with patch(
        "preloop.services.agent_permission_service.native_delivery_warning",
        return_value=None,
    ):
        yield


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


def _native_rule_kwargs(**overrides: object) -> dict:
    values: dict = dict(
        tool_name="Write",
        tool_input={"file_path": ".github/workflows/ci.yml", "content": "x"},
        account_id=str(uuid.uuid4()),
        user_id=uuid.uuid4(),
        managed_agent_id=uuid.uuid4(),
        runtime_session_id=None,
    )
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_apply_native_access_rules_deny_match() -> None:
    """A real-shaped deny PolicyDecision (rule_context None) is a match."""
    tool_config = MagicMock()
    tool_config.id = uuid.uuid4()
    tool_config.is_enabled = True
    call_kwargs = _native_rule_kwargs()
    evaluate = AsyncMock(
        return_value=PolicyDecision("deny", None, "Block .github writes", None)
    )

    with patch(
        "preloop.services.policy_evaluator.evaluate_policy_async",
        evaluate,
    ):
        result = await apply_native_access_rules(
            AsyncMock(), config=tool_config, **call_kwargs
        )

    assert result is not None
    assert result[0] == "deny"
    assert result[1] == "Block .github writes"
    kwargs = evaluate.await_args.kwargs
    assert kwargs["tool_configuration_id"] == tool_config.id
    assert kwargs["subject_context"]["tool_source"] == "agent"
    assert kwargs["subject_context"]["managed_agent_id"] == str(
        call_kwargs["managed_agent_id"]
    )


@pytest.mark.asyncio
async def test_apply_native_access_rules_allow_match() -> None:
    """A real-shaped allow PolicyDecision (rule_context None) is a match."""
    tool_config = MagicMock()
    tool_config.id = uuid.uuid4()
    tool_config.is_enabled = True
    evaluate = AsyncMock(return_value=PolicyDecision("allow", None, "Safe read", None))

    with patch(
        "preloop.services.policy_evaluator.evaluate_policy_async",
        evaluate,
    ):
        result = await apply_native_access_rules(
            AsyncMock(),
            config=tool_config,
            **_native_rule_kwargs(tool_input={"command": "ls"}),
        )

    assert result is not None
    assert result[0] == "allow"
    assert result[1] == "Safe read"


@pytest.mark.asyncio
async def test_apply_native_access_rules_require_approval_carries_workflow_and_context() -> (
    None
):
    """require_approval forwards the workflow id and rule_context snapshot."""
    tool_config = MagicMock()
    tool_config.id = uuid.uuid4()
    tool_config.is_enabled = True
    workflow_id = uuid.uuid4()
    rule_context = {
        "source": "tool_access_rule",
        "decision": "require_approval",
        "rule_name": "Force-push needs a human",
    }
    evaluate = AsyncMock(
        return_value=PolicyDecision(
            "require_approval",
            workflow_id,
            "Force-push needs a human",
            rule_context,
        )
    )

    with patch(
        "preloop.services.policy_evaluator.evaluate_policy_async",
        evaluate,
    ):
        result = await apply_native_access_rules(
            AsyncMock(), config=tool_config, **_native_rule_kwargs()
        )

    assert result == (
        "require_approval",
        "Force-push needs a human",
        workflow_id,
        rule_context,
    )


@pytest.mark.asyncio
async def test_apply_native_access_rules_no_match_sentinel_returns_none() -> None:
    """The evaluator's default-allow sentinel is not a matching rule."""
    tool_config = MagicMock()
    tool_config.id = uuid.uuid4()
    tool_config.is_enabled = True
    evaluate = AsyncMock(
        return_value=PolicyDecision(
            "allow", None, "No rules matched (default allow)", matched=False
        )
    )

    with patch(
        "preloop.services.policy_evaluator.evaluate_policy_async",
        evaluate,
    ):
        result = await apply_native_access_rules(
            AsyncMock(), config=tool_config, **_native_rule_kwargs()
        )

    assert result is None


@pytest.mark.asyncio
async def test_apply_native_access_rules_legacy_workflow_is_not_a_match() -> None:
    """approval_workflow_id with no rules is not a native match.

    Runs the real ``evaluate_policy_async`` so ``SOURCE_TOOL_DEFAULT_WORKFLOW``
    is produced the same way production does. Honouring that decision would
    defeat ``native_tool_approvals=off`` after the first auto-created config.
    """
    from preloop.services.approval_rule_context import SOURCE_TOOL_DEFAULT_WORKFLOW
    from preloop.services.policy_evaluator import evaluate_policy_async

    workflow_id = uuid.uuid4()
    tool_config = MagicMock()
    tool_config.id = uuid.uuid4()
    tool_config.is_enabled = True
    tool_config.approval_workflow_id = workflow_id
    account_id = uuid.uuid4()
    kwargs = _native_rule_kwargs(account_id=str(account_id))
    with (
        patch(
            "preloop.services.policy_evaluator.get_meta_data_async",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "preloop.services.policy_evaluator.is_tool_enabled_for_subject",
            return_value=True,
        ),
        patch(
            "preloop.services.policy_evaluator.get_scoped_tool_rules",
            return_value=[],
        ),
        patch(
            "preloop.services.policy_evaluator.get_tool_config_by_id_async",
            new=AsyncMock(return_value=tool_config),
        ),
        patch(
            "preloop.services.policy_evaluator.get_tool_config_by_tool_name_async",
            new=AsyncMock(return_value=tool_config),
        ),
        patch(
            "preloop.services.policy_evaluator.get_default_approval_workflow_async",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "preloop.services.policy_evaluator.get_multi_by_config_async",
            new=AsyncMock(return_value=[]),
        ),
        patch("preloop.services.policy_evaluator._log_policy_decision_async"),
    ):
        result = await apply_native_access_rules(
            AsyncMock(), config=tool_config, **kwargs
        )
        decision = await evaluate_policy_async(
            AsyncMock(),
            tool_name=kwargs["tool_name"],
            tool_args=kwargs["tool_input"],
            account_id=account_id,
        )

    assert result is None
    assert decision.action == "require_approval"
    assert decision.source == SOURCE_TOOL_DEFAULT_WORKFLOW


@pytest.mark.asyncio
async def test_apply_native_access_rules_blocked_config_short_circuits() -> None:
    """is_enabled=false denies without calling the evaluator."""
    tool_config = MagicMock()
    tool_config.id = uuid.uuid4()
    tool_config.is_enabled = False
    evaluate = AsyncMock()

    with patch(
        "preloop.services.policy_evaluator.evaluate_policy_async",
        evaluate,
    ):
        result = await apply_native_access_rules(
            AsyncMock(), config=tool_config, **_native_rule_kwargs()
        )

    assert result == ("deny", BLOCKED_TOOL_REASON, None, None)
    evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_native_access_rules_subject_disable_is_honoured() -> None:
    """A subject-level disable is a deny even with no rule_context."""
    tool_config = MagicMock()
    tool_config.id = uuid.uuid4()
    tool_config.is_enabled = True
    evaluate = AsyncMock(
        return_value=PolicyDecision(
            "deny", None, "Tool disabled by agent or API key configuration"
        )
    )

    with patch(
        "preloop.services.policy_evaluator.evaluate_policy_async",
        evaluate,
    ):
        result = await apply_native_access_rules(
            AsyncMock(), config=tool_config, **_native_rule_kwargs()
        )

    assert result is not None
    assert result[0] == "deny"
    assert result[1] == "Tool disabled by agent or API key configuration"


@pytest.mark.asyncio
async def test_apply_native_access_rules_ignores_scoped_mcp_source() -> None:
    """Scoped rules tagged source=mcp do not decide native calls."""
    tool_config = MagicMock()
    tool_config.id = uuid.uuid4()
    tool_config.is_enabled = True
    decision = PolicyDecision("deny", None, "MCP Write deny", None)
    decision.source = SOURCE_SUBJECT_SCOPED_RULE
    decision.stored_source = "mcp"
    evaluate = AsyncMock(return_value=decision)

    with patch(
        "preloop.services.policy_evaluator.evaluate_policy_async",
        evaluate,
    ):
        result = await apply_native_access_rules(
            AsyncMock(), config=tool_config, **_native_rule_kwargs()
        )

    assert result is None


@pytest.mark.asyncio
async def test_apply_native_access_rules_honours_scoped_agent_or_absent_source() -> (
    None
):
    """Scoped rules with source=agent or no source still decide native calls."""
    tool_config = MagicMock()
    tool_config.id = uuid.uuid4()
    tool_config.is_enabled = True
    agent_decision = PolicyDecision("deny", None, "Agent Write deny", None)
    agent_decision.source = SOURCE_SUBJECT_SCOPED_RULE
    agent_decision.stored_source = "agent"
    absent_decision = PolicyDecision("deny", None, "Legacy scoped deny", None)
    absent_decision.source = SOURCE_SUBJECT_SCOPED_RULE
    evaluate = AsyncMock(side_effect=[agent_decision, absent_decision])

    with patch(
        "preloop.services.policy_evaluator.evaluate_policy_async",
        evaluate,
    ):
        agent_result = await apply_native_access_rules(
            AsyncMock(), config=tool_config, **_native_rule_kwargs()
        )
        absent_result = await apply_native_access_rules(
            AsyncMock(), config=tool_config, **_native_rule_kwargs()
        )

    assert agent_result is not None and agent_result[0] == "deny"
    assert absent_result is not None and absent_result[0] == "deny"


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
    rule_wf_result.scalar_one_or_none.return_value = rule_workflow
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
async def test_request_agent_permission_keeps_repository_marker_in_tool_args() -> None:
    """The hook's repository marker survives into the approval tool_args.

    The endpoint stamps ``_preloop_repository`` into ``tool_input``; this pins
    that the service persists it on the approval row unchanged, next to the
    adapter marker, for approver surfaces and the session timeline to read.
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
    db = _enforced_db(default_workflow)
    tool_config = MagicMock()
    tool_config.id = uuid.uuid4()
    tool_config.is_enabled = True
    approval = MagicMock()
    approval.id = uuid.uuid4()
    approval.status = "approved"
    approval.approver_comment = "Looks safe"
    repository = {
        "remote": "github.com/example/repo",
        "toplevel": "/home/dev/repo",
        "relative_path": "pkg/sub",
        "source": "hook_cwd",
    }

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

        await request_agent_permission(
            **_permission_kwargs(
                account_id=account_id,
                tool_input={"command": "ls", "_preloop_repository": repository},
            )
        )

    kwargs = mock_service.create_and_notify.await_args.kwargs
    assert kwargs["tool_args"]["_preloop_repository"] == repository
    assert kwargs["tool_args"]["command"] == "ls"


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


@pytest.mark.asyncio
async def test_request_agent_permission_real_shaped_deny_overrides_client_allow() -> (
    None
):
    """Patch evaluate_policy_async only: a deny with no rule_context still wins."""
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
            "preloop.services.policy_evaluator.evaluate_policy_async",
            new=AsyncMock(
                return_value=PolicyDecision("deny", None, "Block .github writes", None)
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


@pytest.mark.asyncio
async def test_request_agent_permission_client_deny_is_honoured_before_rules() -> None:
    """A client deny returns before rules so a Preloop allow cannot widen it."""
    with patch(
        "preloop.services.agent_permission_service.apply_native_access_rules",
        new=AsyncMock(
            return_value=(
                "allow",
                "Safe read",
                None,
                {"source": "tool_access_rule"},
            )
        ),
    ) as mock_rules:
        decision, reason, request_id, timed_out = await request_agent_permission(
            **_permission_kwargs(client_decision="deny")
        )

    assert decision == "deny"
    assert reason == "Denied by client policy"
    assert request_id is None
    assert timed_out is False
    mock_rules.assert_not_called()


@pytest.mark.asyncio
async def test_request_agent_permission_require_approval_rule_recording_failure_raises() -> (
    None
):
    """approvals_off does not swallow a create failure after a require_approval rule."""
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
    tool_config.is_enabled = True

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
                    None,
                    {"source": "tool_access_rule"},
                )
            ),
        ),
    ):
        mock_get_session.return_value.__aenter__.return_value = db
        mock_service = AsyncMock()
        mock_service.create_and_notify = AsyncMock(
            side_effect=RuntimeError("database is on fire")
        )
        mock_service_cls.return_value = mock_service

        with pytest.raises(RuntimeError, match="database is on fire"):
            await request_agent_permission(**_permission_kwargs(account_id=account_id))


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


@pytest.mark.asyncio
async def test_client_allow_without_matching_rule_never_creates_human_approval() -> (
    None
):
    """Forwarding local allows must not introduce new human prompts."""
    with (
        patch(
            "preloop.services.agent_permission_service.get_async_db_session"
        ) as get_session,
        patch("preloop.services.approval_service.ApprovalService") as service_class,
        patch(
            "preloop.models.crud.tool_configuration."
            "get_tool_config_by_name_and_source_async",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "preloop.services.agent_permission_service.apply_native_access_rules",
            new=AsyncMock(return_value=None),
        ) as evaluate,
    ):
        get_session.return_value.__aenter__.return_value = AsyncMock()
        result = await request_agent_permission(
            **_permission_kwargs(client_decision="allow")
        )

    assert result == ("allow", "", None, False)
    evaluate.assert_awaited_once()
    service_class.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [None, "allow", "deny"])
async def test_pre_tool_use_evaluates_rules_without_claiming_host_allow(
    action: str | None,
) -> None:
    """An early central check never invents a native host decision or prompt."""
    db = AsyncMock()
    result = None if action is None else (action, "Native rule", None, None)
    with (
        patch(
            "preloop.services.agent_permission_service.get_async_db_session"
        ) as session,
        patch(
            "preloop.models.crud.tool_configuration."
            "get_tool_config_by_name_and_source_async",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "preloop.services.agent_permission_service.apply_native_access_rules",
            new=AsyncMock(return_value=result),
        ) as rules,
        patch("preloop.services.approval_service.ApprovalService") as approvals,
        patch("preloop.services.agent_permission_service.resolve_workflow") as workflow,
    ):
        session.return_value.__aenter__.return_value = db
        decision, _reason, request_id, timed_out = await request_agent_permission(
            **_permission_kwargs(source="codex_cli", client_decision=None),
            evaluation_phase="pre_tool_use",
        )
    assert decision == ("deny" if action == "deny" else "allow")
    assert request_id is None
    assert timed_out is False
    rules.assert_awaited_once()
    approvals.assert_not_called()
    workflow.assert_not_called()


@pytest.mark.asyncio
async def test_pre_tool_use_never_widens_client_deny() -> None:
    """The rule-only phase must still honor an explicit host deny."""
    with patch(
        "preloop.services.agent_permission_service.get_async_db_session"
    ) as session:
        result = await request_agent_permission(
            **_permission_kwargs(client_decision="deny"),
            evaluation_phase="pre_tool_use",
        )
    assert result == ("deny", "Denied by client policy", None, False)
    session.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["approved", "declined", "expired"])
async def test_pre_tool_use_require_approval_keeps_human_gate(status: str) -> None:
    """The rule-only phase bypasses only the no-rule automatic escalation."""
    account_id = str(uuid.uuid4())
    workflow = models.ApprovalWorkflow(
        id=uuid.uuid4(),
        account_id=account_id,
        name="Synthetic workflow",
        approval_type=DEFAULT_APPROVAL_TYPE,
        is_default=True,
        timeout_seconds=300,
    )
    db = _enforced_db(workflow)
    config = MagicMock()
    config.id = uuid.uuid4()
    approval = MagicMock()
    approval.id = uuid.uuid4()
    approval.status = status
    approval.approver_comment = "Synthetic decision"
    with (
        patch(
            "preloop.services.agent_permission_service.get_async_db_session"
        ) as session,
        patch(
            "preloop.models.crud.tool_configuration."
            "get_tool_config_by_name_and_source_async",
            new=AsyncMock(return_value=config),
        ),
        patch(
            "preloop.services.agent_permission_service.apply_native_access_rules",
            new=AsyncMock(return_value=("require_approval", "Native rule", None, None)),
        ),
        patch("preloop.services.approval_service.ApprovalService") as service_cls,
    ):
        session.return_value.__aenter__.return_value = db
        service = AsyncMock()
        service.create_and_notify.return_value = approval
        service_cls.return_value = service
        result = await request_agent_permission(
            **_permission_kwargs(account_id=account_id, source="codex_cli"),
            evaluation_phase="pre_tool_use",
        )
    assert result[0] == ("allow" if status == "approved" else "deny")
    assert result[2] == str(approval.id)
    assert result[3] is (status == "expired")
    service.create_and_notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_origin_snapshot_does_not_enter_native_policy_arguments() -> None:
    """Only tool arguments enter policy; origin remains on the approval copy."""
    config = MagicMock(id=uuid.uuid4(), is_enabled=True)
    call_kwargs = _native_rule_kwargs()
    tool_input = {
        "command": "ls",
        "_preloop_origin": {"session_id": "session-one", "model": "gpt-alpha"},
    }
    call_kwargs["tool_input"] = tool_input
    evaluate = AsyncMock(return_value=PolicyDecision("deny", None, "Denied", None))
    with patch("preloop.services.policy_evaluator.evaluate_policy_async", evaluate):
        await apply_native_access_rules(AsyncMock(), config=config, **call_kwargs)
    assert evaluate.await_args.args[2] == {"command": "ls"}
    assert tool_input["_preloop_origin"]["model"] == "gpt-alpha"


def _stored_policy_row(audit_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Map one evaluator audit payload to the row the audit service stores."""
    stored: dict[str, Any] = {}

    def _log_action(**kwargs: Any) -> MagicMock:
        stored.update(kwargs)
        row = MagicMock()
        row.id = uuid.uuid4()
        return row

    from preloop.services.audit.service import AuditService

    with (
        patch(
            "preloop.services.audit.service.crud_audit_log.log_action",
            _log_action,
        ),
        patch("preloop.services.audit.service._broadcast_audit_event"),
    ):
        AuditService().log_policy_decision(
            db=MagicMock(),
            account_id=audit_kwargs["account_id"],
            tool_name=audit_kwargs["tool_name"],
            action=audit_kwargs["action"],
            rule_description=audit_kwargs.get("rule_description"),
            condition_matched=audit_kwargs.get("condition_matched"),
            tool_args=audit_kwargs.get("tool_args"),
            user_id=audit_kwargs.get("user_id"),
            execution_id=audit_kwargs.get("execution_id"),
            correlation_id=audit_kwargs.get("correlation_id"),
            extra_details=audit_kwargs.get("extra_details"),
        )
    return stored


async def _run_native_hook(
    *,
    tool_config: Any,
    scoped_rules: list[Any] | None = None,
    access_rules: list[Any] | None = None,
    db: Any | None = None,
    approval_status: str | None = None,
    evaluation_phase: str = "pre_tool_use",
    client_decision: str | None = None,
    account_meta: dict[str, Any] | None = None,
    managed_agent_id: uuid.UUID | None = None,
    use_real_scoped_loader: bool = False,
) -> tuple[tuple[Any, ...], list[dict[str, Any]], AsyncMock]:
    """Run a native permission check through the real evaluator.

    Returns the hook answer, the policy rows the evaluator asked to store,
    and the approval service mock. ``use_real_scoped_loader`` leaves
    ``get_scoped_tool_rules`` in place so account metadata is filtered the
    way production does.
    """
    audit_calls: list[dict[str, Any]] = []

    def _record(**kwargs: Any) -> None:
        audit_calls.append(kwargs)

    account_id = str(uuid.uuid4())
    approval = MagicMock()
    approval.id = uuid.uuid4()
    approval.status = approval_status or "approved"
    approval.approver_comment = "Synthetic decision"
    session_db = db if db is not None else AsyncMock()
    scoped_patch = (
        nullcontext()
        if use_real_scoped_loader
        else patch(
            "preloop.services.policy_evaluator.get_scoped_tool_rules",
            return_value=list(scoped_rules or []),
        )
    )
    with (
        patch(
            "preloop.services.agent_permission_service.get_async_db_session"
        ) as session,
        patch(
            "preloop.models.crud.tool_configuration."
            "get_tool_config_by_name_and_source_async",
            new=AsyncMock(return_value=tool_config),
        ),
        patch("preloop.services.approval_service.ApprovalService") as service_cls,
        patch(
            "preloop.services.policy_evaluator.get_meta_data_async",
            new=AsyncMock(return_value=account_meta or {}),
        ),
        scoped_patch,
        patch(
            "preloop.services.policy_evaluator.get_tool_config_by_id_async",
            new=AsyncMock(return_value=tool_config),
        ),
        patch(
            "preloop.services.policy_evaluator.get_default_approval_workflow_async",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "preloop.services.policy_evaluator.get_multi_by_config_async",
            new=AsyncMock(return_value=list(access_rules or [])),
        ),
        patch(
            "preloop.services.policy_evaluator._log_policy_decision_async",
            _record,
        ),
    ):
        session.return_value.__aenter__.return_value = session_db
        service = AsyncMock()
        service.create_and_notify = AsyncMock(return_value=approval)
        service_cls.return_value = service
        result = await request_agent_permission(
            **_permission_kwargs(
                account_id=account_id,
                source="codex_cli",
                tool_name="Bash",
                tool_input={"command": "git status"},
                client_decision=client_decision,
                **(
                    {"managed_agent_id": managed_agent_id}
                    if managed_agent_id is not None
                    else {}
                ),
            ),
            evaluation_phase=evaluation_phase,
        )
    return result, audit_calls, service


def _scoped_shell_meta(agent_id: uuid.UUID, *, source: str) -> dict[str, Any]:
    """Account metadata with one Bash rule stored for ``source``."""
    return {
        "subject_governance": {
            "managed_agents": {
                str(agent_id): {
                    "tool_rules": {
                        "Bash": [
                            {
                                "action": "require_approval",
                                "condition_expression": "",
                                "condition_type": "simple",
                                "description": "Shell needs approval",
                                "source": source,
                                "is_enabled": True,
                                "priority": 1,
                            }
                        ]
                    }
                }
            }
        }
    }


def _bash_config(*, workflow_id: uuid.UUID | None) -> MagicMock:
    tool_config = MagicMock()
    tool_config.id = uuid.uuid4()
    tool_config.is_enabled = True
    tool_config.approval_workflow_id = workflow_id
    tool_config.tool_source = "agent"
    return tool_config


@pytest.mark.asyncio
async def test_native_hook_records_legacy_workflow_as_the_allow_that_took_effect() -> (
    None
):
    """A legacy tool workflow is not applied, and the audit row says allow.

    The evaluated require-approval result stays in details. No approval is
    created, and the hook allows the command.
    """
    result, calls, service = await _run_native_hook(
        tool_config=_bash_config(workflow_id=uuid.uuid4())
    )

    assert result[0] == "allow"
    assert result[2] is None
    service.create_and_notify.assert_not_awaited()
    assert len(calls) == 1
    row = _stored_policy_row(calls[0])
    assert row["action"] == "policy_allow"
    assert row["status"] == "allow"
    assert row["details"]["decision"] == "allow"
    assert row["details"]["evaluated_decision"] == "require_approval"
    assert (
        row["details"]["not_applied_reason"]
        == "legacy_tool_approval_not_applied_to_native_checks"
    )
    assert (
        row["details"]["rule_description"]
        == "Tool has approval workflow configured (legacy mode)"
    )


@pytest.mark.asyncio
async def test_native_hook_drops_foreign_scoped_rule_before_recording() -> None:
    """A subject-scoped rule for another tool source never reaches the log.

    The loader drops it before evaluation. The hook allows the command,
    creates no approval, and the row is the plain allow for no matching
    rule. It is not recorded as approval required.
    """
    agent_id = uuid.uuid4()
    result, calls, service = await _run_native_hook(
        tool_config=_bash_config(workflow_id=None),
        account_meta=_scoped_shell_meta(agent_id, source="mcp"),
        managed_agent_id=agent_id,
        use_real_scoped_loader=True,
    )

    assert result[0] == "allow"
    assert result[2] is None
    service.create_and_notify.assert_not_awaited()
    assert len(calls) == 1
    assert calls[0]["action"] == "allow"
    assert calls[0]["rule_description"] == "No access rules defined"
    extra = calls[0].get("extra_details") or {}
    assert "evaluated_decision" not in extra
    assert "not_applied_reason" not in extra
    row = _stored_policy_row(calls[0])
    assert row["action"] == "policy_allow"
    assert row["status"] == "allow"
    assert "evaluated_decision" not in row["details"]
    assert row["details"]["rule_description"] == "No access rules defined"


@pytest.mark.asyncio
async def test_native_hook_honours_agent_scoped_rule_through_the_loader() -> None:
    """A subject-scoped rule stored for agent tools still asks for approval."""
    account_id = str(uuid.uuid4())
    agent_id = uuid.uuid4()
    workflow = models.ApprovalWorkflow(
        id=uuid.uuid4(),
        account_id=account_id,
        name="Synthetic workflow",
        approval_type=DEFAULT_APPROVAL_TYPE,
        is_default=True,
        timeout_seconds=300,
    )
    result, calls, service = await _run_native_hook(
        tool_config=_bash_config(workflow_id=None),
        account_meta=_scoped_shell_meta(agent_id, source="agent"),
        managed_agent_id=agent_id,
        use_real_scoped_loader=True,
        db=_enforced_db(workflow),
        approval_status="approved",
    )

    assert result[0] == "allow"
    assert result[2] is not None
    service.create_and_notify.assert_awaited_once()
    assert len(calls) == 1
    assert calls[0]["action"] == "require_approval"
    assert calls[0]["rule_description"] == "Shell needs approval"
    row = _stored_policy_row(calls[0])
    assert row["action"] == "policy_require_approval"
    assert row["status"] == "require_approval"
    assert "evaluated_decision" not in row["details"]


@pytest.mark.asyncio
async def test_native_hook_honoured_require_approval_is_recorded_unchanged() -> None:
    """A matching access rule still records require_approval and asks."""
    account_id = str(uuid.uuid4())
    workflow = models.ApprovalWorkflow(
        id=uuid.uuid4(),
        account_id=account_id,
        name="Synthetic workflow",
        approval_type=DEFAULT_APPROVAL_TYPE,
        is_default=True,
        timeout_seconds=300,
    )
    rule = SimpleNamespace(
        id=uuid.uuid4(),
        priority=1,
        action="require_approval",
        condition_type="simple",
        condition_expression=None,
        description="Force-push needs a human",
        approval_workflow_id=None,
        is_enabled=True,
    )
    result, calls, service = await _run_native_hook(
        tool_config=_bash_config(workflow_id=None),
        access_rules=[rule],
        db=_enforced_db(workflow),
        approval_status="approved",
    )

    assert result[0] == "allow"
    assert result[2] is not None
    service.create_and_notify.assert_awaited_once()
    assert len(calls) == 1
    assert calls[0]["action"] == "require_approval"
    extra = calls[0].get("extra_details") or {}
    assert "evaluated_decision" not in extra
    assert "not_applied_reason" not in extra
    row = _stored_policy_row(calls[0])
    assert row["action"] == "policy_require_approval"
    assert row["status"] == "require_approval"
    assert row["details"]["decision"] == "require_approval"
    assert "evaluated_decision" not in row["details"]
    assert "not_applied_reason" not in row["details"]
    assert row["details"]["rule_description"] == "Force-push needs a human"


@pytest.mark.asyncio
async def test_native_hook_plain_allow_still_records_one_allow_row() -> None:
    """No matching rule still writes one allow row, as it does today."""
    result, calls, service = await _run_native_hook(
        tool_config=_bash_config(workflow_id=None)
    )

    assert result[0] == "allow"
    assert result[2] is None
    service.create_and_notify.assert_not_awaited()
    assert len(calls) == 1
    assert calls[0]["action"] == "allow"
    extra = calls[0].get("extra_details") or {}
    assert "evaluated_decision" not in extra
    assert "not_applied_reason" not in extra
    row = _stored_policy_row(calls[0])
    assert row["action"] == "policy_allow"
    assert row["status"] == "allow"


@pytest.mark.asyncio
async def test_permission_request_legacy_workflow_still_records_require_approval() -> (
    None
):
    """A permission request still creates an approval for a legacy workflow.

    The audit row stays require_approval. Allow is recorded only when the
    check lets the command run without an approval.
    """
    account_id = str(uuid.uuid4())
    workflow = models.ApprovalWorkflow(
        id=uuid.uuid4(),
        account_id=account_id,
        name="Synthetic workflow",
        approval_type=DEFAULT_APPROVAL_TYPE,
        is_default=True,
        timeout_seconds=300,
    )
    result, calls, service = await _run_native_hook(
        tool_config=_bash_config(workflow_id=workflow.id),
        db=_enforced_db(workflow),
        approval_status="approved",
        evaluation_phase="permission_request",
    )

    assert result[0] == "allow"
    assert result[2] is not None
    service.create_and_notify.assert_awaited_once()
    assert len(calls) == 1
    assert calls[0]["action"] == "require_approval"
    extra = calls[0].get("extra_details") or {}
    assert "evaluated_decision" not in extra
    assert "not_applied_reason" not in extra
    row = _stored_policy_row(calls[0])
    assert row["action"] == "policy_require_approval"
    assert row["status"] == "require_approval"
    assert row["details"]["decision"] == "require_approval"


@pytest.mark.asyncio
async def test_permission_request_client_allow_records_legacy_workflow_as_allow() -> (
    None
):
    """A host that already allowed the command does not create an approval.

    The legacy workflow was evaluated and not applied, so the row is the
    allow that took effect.
    """
    result, calls, service = await _run_native_hook(
        tool_config=_bash_config(workflow_id=uuid.uuid4()),
        evaluation_phase="permission_request",
        client_decision="allow",
    )

    assert result[0] == "allow"
    assert result[2] is None
    service.create_and_notify.assert_not_awaited()
    assert len(calls) == 1
    row = _stored_policy_row(calls[0])
    assert row["action"] == "policy_allow"
    assert row["status"] == "allow"
    assert row["details"]["evaluated_decision"] == "require_approval"
    assert (
        row["details"]["not_applied_reason"]
        == "legacy_tool_approval_not_applied_to_native_checks"
    )


@pytest.mark.asyncio
async def test_permission_prompt_legacy_workflow_still_records_require_approval() -> (
    None
):
    """A permission prompt creates an approval and keeps the evaluated row.

    The prompt does not record allow for a command that waits on approval.
    """
    from preloop.services.permission_prompt import evaluate_permission_prompt

    tool_config = _bash_config(workflow_id=uuid.uuid4())
    workflow = MagicMock()
    workflow.id = tool_config.approval_workflow_id
    approval = MagicMock()
    approval.id = uuid.uuid4()
    approval.status = "approved"
    approval.approver_comment = "Synthetic decision"
    calls: list[dict[str, Any]] = []

    def _record(**kwargs: Any) -> None:
        calls.append(kwargs)

    empty = MagicMock()
    empty.scalars.return_value.all.return_value = []
    db = AsyncMock()
    db.execute = AsyncMock(return_value=empty)
    with (
        patch("preloop.services.permission_prompt.get_async_db_session") as session,
        patch(
            "preloop.services.permission_prompt.resolve_workflow",
            new=AsyncMock(return_value=workflow),
        ),
        patch(
            "preloop.services.permission_prompt.resolve_tool_config",
            new=AsyncMock(return_value=tool_config),
        ),
        patch(
            "preloop.services.permission_prompt._mark_consumed",
            new=AsyncMock(),
        ),
        patch("preloop.services.approval_service.ApprovalService") as service_cls,
        patch(
            "preloop.services.policy_evaluator.get_meta_data_async",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "preloop.services.policy_evaluator.get_scoped_tool_rules",
            return_value=[],
        ),
        patch(
            "preloop.services.policy_evaluator.get_tool_config_by_id_async",
            new=AsyncMock(return_value=tool_config),
        ),
        patch(
            "preloop.services.policy_evaluator.get_default_approval_workflow_async",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "preloop.services.policy_evaluator.get_multi_by_config_async",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "preloop.services.policy_evaluator._log_policy_decision_async",
            _record,
        ),
    ):
        session.return_value.__aenter__.return_value = db
        service = AsyncMock()
        service.create_and_notify = AsyncMock(return_value=approval)
        service_cls.return_value = service
        behavior = await evaluate_permission_prompt(
            base_url="http://localhost",
            account_id=str(uuid.uuid4()),
            user_id=uuid.uuid4(),
            managed_agent_id=None,
            runtime_session_id=None,
            managed_agent_name="Claude Code",
            source="claude_code",
            tool_name="Bash",
            tool_input={"command": "git status"},
            wait_seconds=0,
        )

    assert behavior["behavior"] == "allow"
    service.create_and_notify.assert_awaited_once()
    assert len(calls) == 1
    assert calls[0]["action"] == "require_approval"
    extra = calls[0].get("extra_details") or {}
    assert "evaluated_decision" not in extra
    assert "not_applied_reason" not in extra
    row = _stored_policy_row(calls[0])
    assert row["action"] == "policy_require_approval"
    assert row["status"] == "require_approval"


@pytest.mark.asyncio
async def test_legacy_workflow_outside_native_checks_still_records_require_approval() -> (
    None
):
    """The MCP path still records a legacy workflow as require_approval."""
    from preloop.services.policy_evaluator import evaluate_policy_async

    workflow_id = uuid.uuid4()
    tool_config = _bash_config(workflow_id=workflow_id)
    account_id = uuid.uuid4()
    calls: list[dict[str, Any]] = []

    def _record(**kwargs: Any) -> None:
        calls.append(kwargs)

    with (
        patch(
            "preloop.services.policy_evaluator.get_meta_data_async",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "preloop.services.policy_evaluator.get_scoped_tool_rules",
            return_value=[],
        ),
        patch(
            "preloop.services.policy_evaluator.get_tool_config_by_id_async",
            new=AsyncMock(return_value=tool_config),
        ),
        patch(
            "preloop.services.policy_evaluator.get_default_approval_workflow_async",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "preloop.services.policy_evaluator.get_multi_by_config_async",
            new=AsyncMock(return_value=[]),
        ),
        patch("preloop.services.policy_evaluator._log_policy_decision_async", _record),
    ):
        decision = await evaluate_policy_async(
            AsyncMock(),
            tool_name="Bash",
            tool_args={"command": "git status"},
            account_id=account_id,
            tool_configuration_id=tool_config.id,
        )

    assert decision.action == "require_approval"
    assert len(calls) == 1
    assert calls[0]["action"] == "require_approval"
    assert calls[0].get("extra_details") in (None, {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,tool",
    [
        ("cursor", "Read"),
        ("cursor", "Grep"),
        ("cursor", "Glob"),
        ("cursor", "LS"),
        ("claude_code", "Read"),
        ("codex_cli", "read_file"),
        ("copilot_cli", "view"),
    ],
)
@pytest.mark.parametrize("action", [None, "deny", "require_approval"])
async def test_native_reads_default_allow_but_rules_win(
    source: str, tool: str, action: str | None
) -> None:
    """Native reads need no human by default; explicit rules still govern them."""
    outcome = None if action is None else (action, "Explicit rule", None, None)
    with (
        patch(
            "preloop.services.agent_permission_service.get_async_db_session"
        ) as session,
        patch(
            "preloop.models.crud.tool_configuration."
            "get_tool_config_by_name_and_source_async",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "preloop.services.agent_permission_service.apply_native_access_rules",
            new=AsyncMock(return_value=outcome),
        ),
        patch(
            "preloop.services.agent_permission_service.native_tool_approvals_disabled",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "preloop.services.agent_permission_service.resolve_workflow",
            new=AsyncMock(side_effect=RuntimeError("approval required")),
        ) as workflow,
    ):
        session.return_value.__aenter__.return_value = AsyncMock()
        kwargs = _permission_kwargs(
            source=source,
            tool_name=tool,
            client_decision=None if source == "claude_code" else "ask",
        )
        if action == "require_approval":
            with pytest.raises(RuntimeError, match="approval required"):
                await request_agent_permission(**kwargs)
            workflow.assert_awaited_once()
        else:
            result = await request_agent_permission(**kwargs)
            assert result[0] == ("deny" if action == "deny" else "allow")
            assert result[2] is None
            workflow.assert_not_called()


def test_native_read_classification_does_not_trust_shell_or_mcp_names() -> None:
    """Executable tools and external tools are never silently treated as reads."""
    from preloop.services.agent_permission_service import is_read_only_native_tool

    for tool in ("Shell", "Bash", "exec_command", "shell", "mcp__server__Read"):
        assert not is_read_only_native_tool("cursor", tool)
    assert not is_read_only_native_tool("unknown", "Read")
    assert not is_read_only_native_tool("cursor", "Read", "beforeMCPExecution")


@pytest.mark.asyncio
async def test_first_native_read_evaluates_scoped_rules() -> None:
    """A first-use read must not bypass a subject rule just because no row exists."""
    evaluate = AsyncMock(
        return_value=PolicyDecision(
            "deny", None, "Private files", None, source="scoped_rule"
        )
    )
    with patch("preloop.services.policy_evaluator.evaluate_policy_async", evaluate):
        result = await apply_native_access_rules(
            AsyncMock(), config=None, **_native_rule_kwargs(tool_name="Read")
        )
    assert result is not None and result[0] == "deny"
    evaluate.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("required_rule", [False, True])
async def test_pending_native_approval_without_interactive_destination_returns_guidance(
    required_rule: bool,
) -> None:
    """Persist the banner evidence and return promptly instead of silently polling."""
    workflow = models.ApprovalWorkflow(id=uuid.uuid4(), timeout_seconds=300)
    config = MagicMock(id=uuid.uuid4(), is_enabled=True)
    approval = models.ApprovalRequest(id=uuid.uuid4(), status="pending")
    warning = "Email only. Open Approvals or run `preloop agents pause`."
    with (
        patch(
            "preloop.services.agent_permission_service.get_async_db_session"
        ) as session,
        patch(
            "preloop.models.crud.tool_configuration."
            "get_tool_config_by_name_and_source_async",
            new=AsyncMock(return_value=config),
        ),
        patch(
            "preloop.services.agent_permission_service.apply_native_access_rules",
            new=AsyncMock(
                return_value=("require_approval", "Explicit rule", None, None)
                if required_rule
                else None
            ),
        ),
        patch(
            "preloop.services.agent_permission_service.native_tool_approvals_disabled",
            new=AsyncMock(return_value=required_rule),
        ),
        patch(
            "preloop.services.agent_permission_service.resolve_workflow",
            new=AsyncMock(return_value=workflow),
        ),
        patch(
            "preloop.services.agent_permission_service.native_delivery_warning",
            return_value=warning,
        ),
        patch("preloop.services.approval_service.ApprovalService") as service,
        patch(
            "preloop.services.agent_permission_service.asyncio.sleep", new=AsyncMock()
        ) as sleep,
    ):
        session.return_value.__aenter__.return_value = AsyncMock()
        service.return_value.create_and_notify = AsyncMock(return_value=approval)
        result = await request_agent_permission(**_permission_kwargs())
    assert result[0] == "deny" and result[2] == str(approval.id)
    assert "preloop agents pause" in result[1]
    assert "/console/approval/" in result[1]
    assert not result[3]
    sleep.assert_not_awaited()
    args = service.return_value.create_and_notify.await_args.kwargs["tool_args"]
    assert args["_preloop_native_delivery_warning"] == warning


@pytest.mark.asyncio
async def test_claude_explicit_local_ask_for_read_is_not_auto_allowed() -> None:
    """Claude's ordinary reads send allow; ask denotes a local Ask rule."""
    with (
        patch(
            "preloop.services.agent_permission_service.get_async_db_session"
        ) as session,
        patch(
            "preloop.models.crud.tool_configuration."
            "get_tool_config_by_name_and_source_async",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "preloop.services.agent_permission_service.apply_native_access_rules",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "preloop.services.agent_permission_service.native_tool_approvals_disabled",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "preloop.services.agent_permission_service.resolve_workflow",
            new=AsyncMock(side_effect=RuntimeError("local approval required")),
        ),
    ):
        session.return_value.__aenter__.return_value = AsyncMock()
        with pytest.raises(RuntimeError, match="local approval required"):
            await request_agent_permission(
                **_permission_kwargs(
                    source="claude_code", tool_name="Read", client_decision="ask"
                )
            )
