"""Admitted subject rules must not become no-rule allows for native tools."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from preloop.schemas.subject_governance import SubjectGovernanceConfig
from preloop.services.agent_permission_service import (
    apply_native_access_rules,
    request_agent_permission,
)
from preloop.services.policy_evaluator import PolicyDecision, _evaluate_rule_candidates
from preloop.services.subject_governance import sanitize_subject_governance_config


def evaluated_subject_rule(action: str, source: str) -> PolicyDecision:
    """Use the same schema, sanitizer and evaluator as stored scoped rules."""
    payload = SubjectGovernanceConfig(
        tool_rules={
            "Bash": [
                {
                    "action": action,
                    "source": source,
                    "condition_expression": "true",
                    "condition_type": "cel",
                    "description": "Synthetic central restriction",
                }
            ]
        }
    )
    config = sanitize_subject_governance_config(payload.model_dump())
    with patch("preloop.services.policy_evaluator._log_policy_decision_async"):
        outcome = _evaluate_rule_candidates(
            rules=config["tool_rules"]["Bash"],
            tool_name="Bash",
            tool_args={},
            context={},
            account_id=uuid4(),
            user_id=None,
            execution_id=None,
        )
    assert outcome is not None
    assert outcome.action == action
    return outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["require_justification", "unexpected_action"])
@pytest.mark.parametrize("phase", ["pre_tool_use", "permission_request"])
async def test_unsupported_matching_native_action_is_policy_deny(
    action: str,
    phase: str,
) -> None:
    outcome = evaluated_subject_rule(action, "agent")
    existing = MagicMock(is_enabled=True, id=uuid4())
    with (
        patch(
            "preloop.services.agent_permission_service.get_async_db_session"
        ) as session,
        patch(
            "preloop.models.crud.tool_configuration.get_tool_config_by_name_and_source_async",
            new=AsyncMock(return_value=existing),
        ),
        patch(
            "preloop.services.policy_evaluator.evaluate_policy_async",
            new=AsyncMock(return_value=outcome),
        ),
    ):
        session.return_value.__aenter__.return_value = AsyncMock()
        result = await request_agent_permission(
            base_url="https://example.com",
            account_id=str(uuid4()),
            user_id=None,
            managed_agent_id=None,
            runtime_session_id=None,
            managed_agent_name=None,
            source="codex_cli",
            tool_name="Bash",
            tool_input={},
            agent_reasoning=None,
            client_decision="allow",
            evaluation_phase=phase,
        )
    assert result[0] == "deny"
    assert "Unsupported native access rule action" in result[1]
    assert result[2] is None
    assert result[3] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    ["allow", "deny", "require_approval", "require_justification", "unexpected_action"],
)
@pytest.mark.parametrize("source", ["agent", "mcp"])
async def test_native_action_validation_preserves_source_filter(
    action: str,
    source: str,
) -> None:
    outcome = evaluated_subject_rule(action, source)
    with patch(
        "preloop.services.policy_evaluator.evaluate_policy_async",
        new=AsyncMock(return_value=outcome),
    ):
        result = await apply_native_access_rules(
            AsyncMock(),
            config=MagicMock(is_enabled=True, id=uuid4()),
            tool_name="Bash",
            tool_input={},
            account_id=str(uuid4()),
            user_id=None,
            managed_agent_id=None,
            runtime_session_id=None,
        )
    if source == "mcp":
        assert result is None
    else:
        assert result is not None
        assert result[0] == (
            action if action in ("allow", "deny", "require_approval") else "deny"
        )
