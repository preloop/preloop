"""Dry-run decisions share production matching without recording side effects."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from preloop.models import models
from preloop.services.policy_evaluator import _evaluate_loaded_access_rules
from preloop.services.policy_simulation import PolicyEvaluationRequest, simulate_policy


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["allow", "deny", "require_approval"])
async def test_draft_uses_production_core_without_audit(action: str) -> None:
    account_id, user_id = uuid4(), uuid4()
    request = PolicyEvaluationRequest(name="read_file", draft_rule={"action": action})
    with patch("preloop.services.policy_evaluator._log_policy_decision_async") as audit:
        result = await simulate_policy(
            request, db=MagicMock(), account_id=account_id, user_id=user_id
        )
        audit.assert_not_called()
        production = _evaluate_loaded_access_rules(
            rules=[
                models.ToolAccessRule(
                    id="draft-1",
                    priority=0,
                    action=action,
                    condition_expression=None,
                    condition_type="simple",
                    description=None,
                    approval_workflow_id=None,
                )
            ],
            tool_config=models.ToolConfiguration(id=uuid4(), approval_workflow_id=None),
            tool_name=request.name,
            tool_args={},
            context={},
            account_id=account_id,
            user_id=user_id,
            execution_id=None,
        )
        assert result.decision == production.action
        audit.assert_called_once()
    assert result.matched_rule == "draft-1"


@pytest.mark.asyncio
async def test_yaml_precedence_and_overlap() -> None:
    request = PolicyEvaluationRequest(
        name="read_file",
        draft_yaml="""version: "1.0"
metadata:
  name: Example policy
tools:
  - name: read_file
    conditions:
      - expression: args.count > 5
        action: deny
      - expression: args.count > 0
        action: allow
""",
        args={"count": 10},
    )
    with patch("preloop.services.policy_evaluator._log_policy_decision_async") as audit:
        result = await simulate_policy(
            request, db=MagicMock(), account_id=uuid4(), user_id=uuid4()
        )
        audit.assert_not_called()
    assert result.decision == "deny"
    assert result.matched_rule == "draft-1"
    assert result.also_matched_rule_ids == ["draft-2"]
    assert [r.id for r in result.checked_rules] == ["draft-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args,decision,error",
    [
        ({"path": "/etc/../etc"}, "deny", False),
        ({"path": "//etc"}, "allow", False),
        ({}, "require_approval", True),
        ({"path": 42}, "require_approval", True),
    ],
)
async def test_path_samples_and_condition_errors(
    args: dict, decision: str, error: bool
) -> None:
    request = PolicyEvaluationRequest(
        name="read_file",
        args=args,
        draft_rule={
            "action": "deny",
            "condition_expression": 'args.path.startsWith("/etc")',
            "condition_type": "cel",
        },
    )
    with (
        patch("preloop.services.policy_evaluator._log_policy_decision_async") as audit,
    ):
        result = await simulate_policy(
            request, db=MagicMock(), account_id=uuid4(), user_id=uuid4()
        )
        audit.assert_not_called()
    assert result.decision == decision
    assert bool(result.checked_rules[0].error) == error


@pytest.mark.asyncio
async def test_no_match_defaults_to_allow() -> None:
    result = await simulate_policy(
        PolicyEvaluationRequest(
            name="read_file",
            args={"count": 0},
            draft_rule={"action": "deny", "condition_expression": "args.count > 1"},
        ),
        db=MagicMock(),
        account_id=uuid4(),
        user_id=uuid4(),
    )
    assert result.decision == "allow"
    assert result.matched_rule is None


@pytest.mark.parametrize(
    "source",
    [
        {},
        {"stored": True, "draft_rule": {"action": "deny"}},
        {"draft_rule": {"action": "notify"}},
    ],
)
def test_source_and_tool_action_validation(source: dict) -> None:
    with pytest.raises(ValidationError):
        PolicyEvaluationRequest(name="read_file", **source)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["notify", "redact", "deny", "require_approval"])
async def test_sensitive_data_draft_without_notices_or_audit(action: str) -> None:
    request = PolicyEvaluationRequest(
        name="read_file",
        args={"text": "jane@example.com"},
        draft_yaml=f"""version: "1.0"
metadata:
  name: Example policy
sensitive_data:
  rules:
    - id: email-rule
      "on": [tool.args]
      types: [email]
      action: {action}
""",
    )
    with (
        patch("preloop.services.sensitive_data.tool_policy._audit") as audit,
        patch("preloop.services.sensitive_data.tool_policy._audit_rule") as rule_audit,
        patch(
            "preloop.services.sensitive_data.tool_policy.schedule_policy_notice"
        ) as notice,
    ):
        result = await simulate_policy(
            request, db=MagicMock(), account_id=uuid4(), user_id=uuid4()
        )
        audit.assert_not_called()
        rule_audit.assert_not_called()
        notice.assert_not_called()
    assert result.decision == action


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["notify", "redact"])
async def test_model_io_draft_without_recording(action: str) -> None:
    request = PolicyEvaluationRequest(
        name="read_file",
        model_text="jane@example.com",
        draft_yaml=f"""version: "1.0"
metadata:
  name: Example policy
model_io:
  - id: model-email
    target: model.request
    detectors:
      pii:
        enabled: true
        types: [email]
    conditions:
      - expression: pii.found == true
        action: {action}
""",
    )
    with (
        patch("preloop.services.model_content_policy._audit_decision") as audit,
        patch("preloop.services.model_content_policy._audit_redaction") as redact,
        patch("preloop.services.model_content_policy._emit_notices") as notice,
    ):
        result = await simulate_policy(
            request, db=MagicMock(), account_id=uuid4(), user_id=uuid4()
        )
        audit.assert_not_called()
        redact.assert_not_called()
        notice.assert_not_called()
    assert result.decision == action


def test_evaluate_endpoint_returns_draft_and_rejects_invalid_yaml() -> None:
    from fastapi import HTTPException
    from preloop.api.endpoints.policies import evaluate_policy_sample

    account, user = models.Account(id=uuid4()), models.User(id=uuid4())
    result = evaluate_policy_sample(
        PolicyEvaluationRequest(name="read_file", draft_rule={"action": "deny"}),
        account=account,
        current_user=user,
        db=MagicMock(),
    )
    assert result.decision == "deny"
    with pytest.raises(HTTPException) as exc:
        evaluate_policy_sample(
            PolicyEvaluationRequest(name="read_file", draft_yaml="["),
            account=account,
            current_user=user,
            db=MagicMock(),
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_stored_evaluation_uses_async_firewall_without_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock
    from preloop.services import policy_evaluator as evaluator
    from preloop.services import policy_simulation as simulation
    from preloop.services.policy.schema import SensitiveDataConfig

    account_id, user_id, config_id = uuid4(), uuid4(), uuid4()
    config = models.ToolConfiguration(
        id=config_id,
        tool_name="read_file",
        tool_source="builtin",
        approval_workflow_id=None,
    )
    rule = models.ToolAccessRule(
        id=uuid4(),
        priority=0,
        action="deny",
        condition_expression="args.count > 0",
        condition_type="simple",
        description=None,
        approval_workflow_id=None,
    )
    monkeypatch.setattr(
        simulation,
        "load_sensitive_data_config",
        lambda *args, **kwargs: SensitiveDataConfig(),
    )
    monkeypatch.setattr(
        simulation.crud_tool_configuration,
        "get_multi_by_account",
        lambda *args, **kwargs: [config],
    )
    monkeypatch.setattr(evaluator, "get_meta_data_async", AsyncMock(return_value={}))
    monkeypatch.setattr(
        evaluator, "get_tool_config_by_id_async", AsyncMock(return_value=config)
    )
    monkeypatch.setattr(
        evaluator, "get_default_approval_workflow_async", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        evaluator, "get_multi_by_config_async", AsyncMock(return_value=[rule])
    )
    with patch.object(evaluator, "_log_policy_decision_async") as audit:
        result = await simulate_policy(
            PolicyEvaluationRequest(name="read_file", stored=True, args={"count": 2}),
            db=MagicMock(),
            account_id=account_id,
            user_id=user_id,
        )
        audit.assert_not_called()
    assert result.decision == "deny"
    assert result.matched_rule == str(rule.id)
