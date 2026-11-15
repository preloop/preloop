"""Dry-run decisions share production matching without recording side effects."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

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
      description: Protect email addresses in tool arguments
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
    assert result.description == "Protect email addresses in tool arguments"


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
@pytest.mark.parametrize("expression", ["args.count > 0", "grant.active == true"])
async def test_stored_evaluation_uses_sync_firewall_without_recording(
    monkeypatch: pytest.MonkeyPatch,
    expression: str,
) -> None:
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
        condition_expression=expression,
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
        "get_for_server",
        lambda *args, **kwargs: [config],
    )
    monkeypatch.setattr(
        evaluator.crud_account,
        "get",
        lambda *args, **kwargs: models.Account(meta_data={}),
    )
    monkeypatch.setattr(
        evaluator.crud_tool_configuration, "get", lambda *args, **kwargs: config
    )
    monkeypatch.setattr(
        evaluator.crud_approval_workflow, "get_default", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        evaluator.crud_tool_access_rule,
        "get_multi_by_config",
        lambda *args, **kwargs: [rule],
    )
    with patch.object(evaluator, "_log_policy_decision_async") as audit:
        result = await simulate_policy(
            PolicyEvaluationRequest(
                name="read_file", stored=True, args={"count": 2}, grant={"active": True}
            ),
            db=MagicMock(),
            account_id=account_id,
            user_id=user_id,
        )
        audit.assert_not_called()
    assert result.decision == "deny"
    assert result.matched_rule == str(rule.id)


def test_scoped_simulation_reports_lower_priority_overlap() -> None:
    from preloop.services.policy_evaluator import _evaluate_rule_candidates

    rules = [
        {"id": "first", "action": "deny", "condition_expression": "args.count > 0"},
        {"id": "second", "action": "allow", "condition_expression": "args.count > 1"},
        {"id": "disabled", "action": "deny", "is_enabled": False},
        {"id": "broken", "action": "allow", "condition_expression": "args.count >"},
    ]
    with patch("preloop.services.policy_evaluator._log_policy_decision_async") as audit:
        decision = _evaluate_rule_candidates(
            rules=rules,
            tool_name="read_record",
            tool_args={"count": 2},
            context={},
            account_id=uuid4(),
            user_id=None,
            execution_id=None,
            record=False,
        )
    assert decision.action == "deny"
    assert decision.also_matched_rule_ids == ["second"]
    audit.assert_not_called()


def test_exact_server_configuration_lookup_filters_in_database(
    db_session: Session, test_user: models.User
) -> None:
    from preloop.models.crud import crud_tool_configuration

    account_id = test_user.account_id
    other_account = models.Account(
        organization_name="Synthetic other account", is_active=True
    )
    servers = [
        models.MCPServer(
            name=f"example-{i}",
            url="https://mcp.example.com/mcp",
            account_id=account_id,
            auth_type="none",
        )
        for i in range(2)
    ]
    db_session.add_all([other_account, *servers])
    db_session.flush()
    configs = [
        models.ToolConfiguration(
            account_id=account_id, tool_name="read_example", tool_source="builtin"
        ),
        models.ToolConfiguration(
            account_id=account_id,
            tool_name="read_example",
            tool_source="mcp",
            mcp_server_id=servers[0].id,
        ),
        models.ToolConfiguration(
            account_id=account_id,
            tool_name="read_example",
            tool_source="mcp",
            mcp_server_id=servers[1].id,
        ),
        models.ToolConfiguration(
            account_id=account_id,
            tool_name="different_example",
            tool_source="mcp",
            mcp_server_id=servers[0].id,
        ),
        models.ToolConfiguration(
            account_id=other_account.id, tool_name="read_example", tool_source="builtin"
        ),
    ]
    db_session.add_all(configs)
    db_session.flush()
    lookup = crud_tool_configuration.get_for_server
    assert lookup(db_session, account_id=str(account_id), tool_name="read_example") == [
        configs[0]
    ]
    assert lookup(
        db_session,
        account_id=str(account_id),
        tool_name="read_example",
        mcp_server_id=str(servers[0].id),
    ) == [configs[1]]
    assert lookup(
        db_session,
        account_id=str(account_id),
        tool_name="read_example",
        mcp_server_id=str(servers[1].id),
    ) == [configs[2]]


@pytest.mark.parametrize("action", ["allow", "deny", "require_approval"])
def test_authenticated_stored_simulation_with_sync_dependency(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """Exercise real authentication, sync CRUD and matching through HTTP."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from preloop.api.auth.jwt import create_access_token
    from preloop.api.endpoints.policies import router
    from preloop.models import crud, schemas
    from preloop.models.db import session as db_module

    server = crud.crud_mcp_server.create(
        db_session,
        obj_in={
            "name": "synthetic-crm",
            "url": "https://mcp.example.com/mcp",
            "account_id": test_user.account_id,
            "auth_type": "none",
        },
    )
    config = crud.crud_tool_configuration.create(
        db_session,
        config_in=schemas.ToolConfigurationCreate(
            account_id=str(test_user.account_id),
            tool_name="update_record",
            tool_source="mcp",
            mcp_server_id=str(server.id),
        ),
    )
    rule = crud.crud_tool_access_rule.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "tool_configuration_id": config.id,
            "action": action,
            "condition_expression": "args.count > 0",
            "condition_type": "simple",
            "priority": 0,
            "description": "Synthetic stored rule",
        },
    )
    # Keep the real get_db_session dependency, binding its factory to the test
    # transaction so request sessions cannot persist changes outside this test.
    monkeypatch.setattr(
        db_module,
        "get_session_factory",
        lambda: sessionmaker(
            bind=db_session.connection(), join_transaction_mode="create_savepoint"
        ),
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    token = create_access_token({"sub": str(test_user.id)})
    with (
        TestClient(app) as client,
        patch("preloop.services.policy_evaluator._log_policy_decision_async") as audit,
        patch(
            "preloop.services.sensitive_data.tool_policy.schedule_policy_notice"
        ) as notice,
    ):
        assert (
            client.post(
                "/api/v1/policies/evaluate",
                json={"name": "update_record", "stored": True},
            ).status_code
            == 401
        )
        response = client.post(
            "/api/v1/policies/evaluate",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "name": "update_record",
                "server": "synthetic-crm",
                "args": {"count": 2},
                "stored": True,
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["decision"] == action
        assert result["matched_rule"] == str(rule.id)
        assert result["description"] == "Synthetic stored rule"
        assert result["checked_rules"] == [
            {
                "id": str(rule.id),
                "expression": "args.count > 0",
                "matched": True,
                "error": None,
            }
        ]
        # A nonmatching rule and a different server both default to allow.
        # The latter must never fall back to a same-name tool on another server.
        for server_name, args in [
            ("synthetic-crm", {"count": 0}),
            ("unregistered-crm", {"count": 2}),
            ("builtin", {"count": 2}),
        ]:
            response = client.post(
                "/api/v1/policies/evaluate",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "name": "update_record",
                    "server": server_name,
                    "args": args,
                    "stored": True,
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["decision"] == "allow"
            assert response.json()["matched_rule"] is None
        audit.assert_not_called()
        notice.assert_not_called()
    assert not crud.crud_approval_request.get_multi(
        db_session, account_id=str(test_user.account_id)
    )


def _stored_rule(rule_id: str, priority: int, action: str, expression: str):
    return models.ToolAccessRule(
        id=rule_id,
        priority=priority,
        action=action,
        condition_expression=expression,
        condition_type="cel",
        description=None,
        is_enabled=True,
        approval_workflow_id=None,
    )


@pytest.mark.asyncio
async def test_draft_rule_is_tested_inside_the_tools_ordered_rule_list() -> None:
    """A live deny that comes first wins over the draft, as it would live.

    Production 2026-11-15: pay(amount=1500) showed "Require approval
    (draft-1)" while the live rule 1 (deny amount > 1000) decides it.
    """
    stored = [_stored_rule("live-1", 0, "deny", "args.amount > 1000")]
    config = models.ToolConfiguration(id=uuid4(), approval_workflow_id=None)
    request = PolicyEvaluationRequest(
        name="pay",
        args={"amount": 1500},
        draft_rule={
            "action": "require_approval",
            "condition_expression": "args.amount > 100",
            "condition_type": "cel",
        },
        draft_with_stored_rules=True,
    )
    with (
        patch(
            "preloop.services.policy_simulation.crud_tool_configuration.get_for_server",
            return_value=[config],
        ),
        patch(
            "preloop.services.policy_simulation.crud_tool_access_rule.get_multi_by_config",
            return_value=stored,
        ),
        patch("preloop.services.policy_evaluator._log_policy_decision_async") as audit,
    ):
        result = await simulate_policy(
            request, db=MagicMock(), account_id=uuid4(), user_id=uuid4()
        )
        audit.assert_not_called()
    assert result.decision == "deny"
    assert result.matched_rule == "live-1"
    assert result.also_matched_rule_ids == ["draft-1"]


@pytest.mark.asyncio
async def test_draft_rule_replaces_the_rule_it_edits_in_place() -> None:
    stored = [
        _stored_rule("live-1", 0, "deny", "args.amount > 1000"),
        _stored_rule("live-2", 1, "allow", "args.amount > 0"),
    ]
    config = models.ToolConfiguration(id=uuid4(), approval_workflow_id=None)
    request = PolicyEvaluationRequest(
        name="pay",
        args={"amount": 1500},
        draft_rule={
            "action": "require_approval",
            "condition_expression": "args.amount > 1000",
            "condition_type": "cel",
        },
        draft_rule_id="live-1",
        draft_with_stored_rules=True,
    )
    with (
        patch(
            "preloop.services.policy_simulation.crud_tool_configuration.get_for_server",
            return_value=[config],
        ),
        patch(
            "preloop.services.policy_simulation.crud_tool_access_rule.get_multi_by_config",
            return_value=stored,
        ),
        patch("preloop.services.policy_evaluator._log_policy_decision_async"),
    ):
        result = await simulate_policy(
            request, db=MagicMock(), account_id=uuid4(), user_id=uuid4()
        )
    assert result.decision == "require_approval"
    assert result.matched_rule == "draft-1"
    assert [r.id for r in result.checked_rules] == ["draft-1"]


@pytest.mark.asyncio
async def test_draft_enabling_a_disabled_rule_keeps_its_place() -> None:
    disabled = _stored_rule("live-1", 0, "deny", "args.amount > 1000")
    disabled.is_enabled = False
    stored = [disabled, _stored_rule("live-2", 1, "allow", "args.amount > 0")]
    config = models.ToolConfiguration(id=uuid4(), approval_workflow_id=None)
    request = PolicyEvaluationRequest(
        name="pay",
        args={"amount": 1500},
        draft_rule={
            "action": "require_approval",
            "condition_expression": "args.amount > 1000",
            "condition_type": "cel",
        },
        draft_rule_id="live-1",
        draft_with_stored_rules=True,
    )
    with (
        patch(
            "preloop.services.policy_simulation.crud_tool_configuration.get_for_server",
            return_value=[config],
        ),
        patch(
            "preloop.services.policy_simulation.crud_tool_access_rule.get_multi_by_config",
            return_value=stored,
        ),
        patch("preloop.services.policy_evaluator._log_policy_decision_async"),
    ):
        result = await simulate_policy(
            request, db=MagicMock(), account_id=uuid4(), user_id=uuid4()
        )
    assert result.decision == "require_approval"
    assert result.matched_rule == "draft-1"
