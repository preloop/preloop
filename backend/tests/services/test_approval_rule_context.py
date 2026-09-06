"""Tests for the matched-rule context carried onto approval requests.

The product claim under test: an approver can see WHICH rule demanded the
approval and read its expression, so a boundary case is distinguishable from
a mid-band one. The absence cases matter as much as the presence ones:
approvals raised without rule evaluation must carry nothing rather than a
fabricated explanation.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from preloop.services.approval_rule_context import (
    KNOWN_SOURCES,
    SOURCE_AGENT_PERMISSION_HOOK,
    SOURCE_MODEL_IO_RULE,
    SOURCE_RULE_EVALUATION_ERROR,
    SOURCE_TOOL_ACCESS_RULE,
    SOURCE_TOOL_DEFAULT_WORKFLOW,
    build_rule_context,
    referenced_args,
)
from preloop.services.policy_evaluator import PolicyDecision, evaluate_policy


# --------------------------------------------------------------------------
# build_rule_context
# --------------------------------------------------------------------------


def test_build_carries_rule_identity_and_expression():
    """The four facts an approver needs: which rule, what it says, how it ranks."""
    rule_id = uuid.uuid4()
    context = build_rule_context(
        source=SOURCE_TOOL_ACCESS_RULE,
        decision="require_approval",
        rule_id=rule_id,
        rule_name="High-value payments",
        expression="args.amount >= 1000",
        expression_type="cel",
        priority=10,
    )

    assert context["source"] == SOURCE_TOOL_ACCESS_RULE
    assert context["decision"] == "require_approval"
    assert context["rule_id"] == str(rule_id)
    assert context["rule_name"] == "High-value payments"
    assert context["expression"] == "args.amount >= 1000"
    assert context["expression_type"] == "cel"
    assert context["priority"] == 10


def test_build_falls_back_to_expression_then_generic_label():
    """An unnamed rule still gets a label; a bare one gets a generic label."""
    from_expression = build_rule_context(
        source=SOURCE_TOOL_ACCESS_RULE,
        decision="require_approval",
        expression="args.env == 'production'",
    )
    assert from_expression["rule_name"] == "args.env == 'production'"

    bare = build_rule_context(
        source=SOURCE_TOOL_ACCESS_RULE,
        decision="require_approval",
    )
    assert bare["rule_name"] == "Access rule"


def test_build_model_io_rule_includes_detector_summary():
    context = build_rule_context(
        source=SOURCE_MODEL_IO_RULE,
        decision="require_approval",
        rule_id="deny-pii",
        rule_name="PII on response",
        detector_summary={"pii.found": True, "pii.types_found": ["email"]},
    )
    assert context["source"] == SOURCE_MODEL_IO_RULE
    assert context["rule_id"] == "deny-pii"
    assert context["detector_summary"]["pii.found"] is True


def test_build_omits_absent_fields_entirely():
    """No None values reach JSONB; absence is expressed by a missing key."""
    context = build_rule_context(
        source=SOURCE_TOOL_DEFAULT_WORKFLOW,
        decision="require_approval",
        rule_name="Tool default policy",
    )
    assert "rule_id" not in context
    assert "expression" not in context
    assert "priority" not in context
    assert None not in context.values()


def test_build_omits_referenced_args_for_catchall_boolean_literals():
    """A catch-all ``true`` inspects no argument; do not render \"Checks true\"."""
    context = build_rule_context(
        source=SOURCE_TOOL_ACCESS_RULE,
        decision="require_approval",
        rule_name="Always ask",
        expression="true",
        expression_type="simple",
    )
    assert context["expression"] == "true"
    assert "referenced_args" not in context


def test_build_states_default_gating_plainly_without_a_rule():
    """No rule fired: say what actually gated the call, do not invent a rule."""
    context = build_rule_context(
        source=SOURCE_TOOL_DEFAULT_WORKFLOW,
        decision="require_approval",
    )
    assert "expression" not in context
    explanation = context["explanation"]
    assert "No access rule matched" in explanation
    assert "every call" in explanation
    # Honesty guard: the block explains WHY, it never claims an assessment.
    assert "risk" not in explanation.lower()


def test_agent_hook_explanation_does_not_claim_a_preloop_rule():
    """The agent's own hook escalated; no Preloop rule was consulted."""
    context = build_rule_context(
        source=SOURCE_AGENT_PERMISSION_HOOK,
        decision="require_approval",
    )
    assert "No Preloop access rule was evaluated" in context["explanation"]


def test_build_rejects_an_unknown_source():
    """A typo in a source must fail loudly, not persist a meaningless label."""
    with pytest.raises(ValueError, match="Unknown approval rule context source"):
        build_rule_context(source="made_up", decision="require_approval")


def test_all_known_sources_build():
    """Every declared source is constructible; no half-registered constant."""
    for source in KNOWN_SOURCES:
        context = build_rule_context(source=source, decision="require_approval")
        assert context["source"] == source
        assert context["rule_name"]


def test_also_matched_rule_ids_recorded_when_present():
    """Overlapping rules are surfaced so a mis-scoped one can be spotted."""
    other = uuid.uuid4()
    context = build_rule_context(
        source=SOURCE_TOOL_ACCESS_RULE,
        decision="require_approval",
        expression="args.amount > 0",
        also_matched_rule_ids=[other],
    )
    assert context["also_matched_rule_ids"] == [str(other)]


def test_no_also_matched_key_when_nothing_else_matched():
    """The common case stays clean: no empty list in the payload."""
    context = build_rule_context(
        source=SOURCE_TOOL_ACCESS_RULE,
        decision="require_approval",
        expression="args.amount > 0",
        also_matched_rule_ids=[],
    )
    assert "also_matched_rule_ids" not in context


# --------------------------------------------------------------------------
# referenced_args
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("args.amount >= 1000", ["amount"]),
        ("args.amount == 1000 && args.currency == 'EUR'", ["amount", "currency"]),
        ("args.amount > 1 || args.amount < 5", ["amount"]),  # deduped, ordered
        ("amount > 300", ["amount"]),  # bare shorthand form users write
        ("true", []),  # catch-all literal, not an argument named "true"
        ("false", []),
        ("TRUE", []),
        ("  False", []),
        ("", []),
        (None, []),
    ],
)
def test_referenced_args_extracts_the_arguments_the_rule_looks_at(expression, expected):
    """Highlighting hint: which arguments the expression text mentions."""
    assert referenced_args(expression) == expected


# --------------------------------------------------------------------------
# PolicyDecision back-compatibility
# --------------------------------------------------------------------------


def test_policy_decision_still_unpacks_as_the_historical_three_tuple():
    """Every existing caller unpacks 3 values; widening would break them."""
    decision = PolicyDecision("require_approval", "wf-1", "why", {"source": "x"})
    action, workflow_id, description = decision
    assert (action, workflow_id, description) == ("require_approval", "wf-1", "why")
    assert len(decision) == 3
    assert decision == ("require_approval", "wf-1", "why")


def test_policy_decision_exposes_rule_context_off_to_the_side():
    """New callers read the snapshot without disturbing the tuple shape."""
    decision = PolicyDecision("require_approval", None, "why", {"rule_name": "R"})
    assert decision.rule_context == {"rule_name": "R"}
    assert decision.action == "require_approval"
    assert decision.rule_description == "why"


def test_policy_decision_rule_context_defaults_to_none():
    """allow/deny decisions gate nothing, so they carry no rule context."""
    assert PolicyDecision("allow", None, "nothing matched").rule_context is None


# --------------------------------------------------------------------------
# evaluate_policy end to end (sync path, mocked CRUD)
# --------------------------------------------------------------------------


def _rule(*, action, expression, priority, description=None, condition_type="cel"):
    rule = MagicMock()
    rule.id = uuid.uuid4()
    rule.action = action
    rule.condition_expression = expression
    rule.condition_type = condition_type
    rule.priority = priority
    rule.description = description
    rule.is_enabled = True
    rule.approval_workflow_id = None
    return rule


@pytest.fixture
def policy_env(monkeypatch):
    """Patch the CRUD surface evaluate_policy() reads, returning a setter."""
    account = MagicMock()
    account.meta_data = {}
    monkeypatch.setattr(
        "preloop.services.policy_evaluator.crud_account.get",
        lambda db, id: account,
    )
    tool_config = MagicMock()
    tool_config.id = uuid.uuid4()
    tool_config.approval_workflow_id = None
    monkeypatch.setattr(
        "preloop.services.policy_evaluator.crud_tool_configuration.get_by_tool_name",
        lambda db, account_id, tool_name: tool_config,
    )
    monkeypatch.setattr(
        "preloop.services.policy_evaluator.crud_approval_workflow.get_default",
        lambda db, account_id: None,
    )
    monkeypatch.setattr(
        "preloop.services.policy_evaluator._log_policy_decision_async",
        lambda **kwargs: None,
    )

    def set_rules(rules):
        monkeypatch.setattr(
            "preloop.services.policy_evaluator.crud_tool_access_rule.get_multi_by_config",
            lambda db, config_id, account_id, enabled_only: rules,
        )

    set_rules([])
    return set_rules, tool_config


def test_matched_rule_is_persisted_verbatim_on_a_boundary_amount(policy_env):
    """The Dylan case: amount exactly on the threshold names the >= rule."""
    set_rules, tool_config = policy_env
    rule = _rule(
        action="require_approval",
        expression="args.amount >= 1000",
        priority=10,
        description="High-value payments",
    )
    set_rules([rule])

    decision = evaluate_policy(
        db=MagicMock(),
        tool_name="send_payment",
        tool_args={"amount": 1000},
        account_id=uuid.uuid4(),
    )

    assert decision.action == "require_approval"
    context = decision.rule_context
    assert context["source"] == SOURCE_TOOL_ACCESS_RULE
    assert context["rule_id"] == str(rule.id)
    assert context["rule_name"] == "High-value payments"
    assert context["expression"] == "args.amount >= 1000"
    assert context["priority"] == 10
    assert context["referenced_args"] == ["amount"]
    assert context["tool_configuration_id"] == str(tool_config.id)


def test_winning_rule_is_the_highest_priority_one_not_merely_the_last(policy_env):
    """Two rules match; the row must name the one that actually decided."""
    set_rules, _ = policy_env
    winner = _rule(
        action="require_approval",
        expression="args.amount >= 1000",
        priority=1,
        description="Winner",
    )
    loser = _rule(
        action="require_approval",
        expression="args.amount >= 100",
        priority=2,
        description="Loser",
    )
    set_rules([winner, loser])

    decision = evaluate_policy(
        db=MagicMock(),
        tool_name="send_payment",
        tool_args={"amount": 5000},
        account_id=uuid.uuid4(),
    )

    assert decision.rule_context["rule_id"] == str(winner.id)
    assert decision.rule_context["rule_name"] == "Winner"
    # The overlap is reported, so a mis-scoped rule is visible at review time.
    assert decision.rule_context["also_matched_rule_ids"] == [str(loser.id)]


def test_non_matching_lower_priority_rule_is_not_listed_as_also_matched(policy_env):
    """also_matched must mean 'would also have matched', not 'exists'."""
    set_rules, _ = policy_env
    winner = _rule(
        action="require_approval", expression="args.amount >= 1000", priority=1
    )
    unrelated = _rule(
        action="require_approval", expression="args.amount >= 90000", priority=2
    )
    set_rules([winner, unrelated])

    decision = evaluate_policy(
        db=MagicMock(),
        tool_name="send_payment",
        tool_args={"amount": 5000},
        account_id=uuid.uuid4(),
    )
    assert "also_matched_rule_ids" not in decision.rule_context


def test_allow_decision_carries_no_rule_context(policy_env):
    """Nothing was gated, so there is nothing to explain."""
    set_rules, _ = policy_env
    set_rules([_rule(action="allow", expression="args.amount < 10", priority=1)])

    decision = evaluate_policy(
        db=MagicMock(),
        tool_name="send_payment",
        tool_args={"amount": 5},
        account_id=uuid.uuid4(),
    )
    assert decision.action == "allow"
    assert decision.rule_context is None


def test_deny_decision_carries_no_rule_context(policy_env):
    """A denial never becomes an approval row, so it needs no snapshot."""
    set_rules, _ = policy_env
    set_rules([_rule(action="deny", expression="args.amount > 10", priority=1)])

    decision = evaluate_policy(
        db=MagicMock(),
        tool_name="send_payment",
        tool_args={"amount": 5000},
        account_id=uuid.uuid4(),
    )
    assert decision.action == "deny"
    assert decision.rule_context is None


def test_legacy_tool_workflow_with_no_rules_states_the_default_plainly(policy_env):
    """No rule exists; the approver is told that, not shown a phantom rule."""
    set_rules, tool_config = policy_env
    tool_config.approval_workflow_id = uuid.uuid4()
    set_rules([])

    decision = evaluate_policy(
        db=MagicMock(),
        tool_name="send_payment",
        tool_args={"amount": 5},
        account_id=uuid.uuid4(),
    )

    assert decision.action == "require_approval"
    context = decision.rule_context
    assert context["source"] == SOURCE_TOOL_DEFAULT_WORKFLOW
    assert "rule_id" not in context
    assert "expression" not in context
    assert "No access rule matched" in context["explanation"]


def test_fail_closed_on_a_broken_rule_says_so_rather_than_claiming_a_match(
    policy_env,
):
    """A malformed rule gates the call; the row must not imply it matched."""
    set_rules, _ = policy_env
    broken = _rule(
        action="require_approval",
        expression="args.amount >>> nonsense(",
        priority=1,
        description="Broken rule",
    )
    set_rules([broken])

    decision = evaluate_policy(
        db=MagicMock(),
        tool_name="send_payment",
        tool_args={"amount": 5},
        account_id=uuid.uuid4(),
    )

    assert decision.action == "require_approval"
    context = decision.rule_context
    assert context["source"] == SOURCE_RULE_EVALUATION_ERROR
    assert context["rule_id"] == str(broken.id)
    assert "failed closed" in context["explanation"]


@pytest.mark.asyncio
async def test_agent_permission_hook_records_that_no_rule_was_evaluated(
    monkeypatch,
):
    """Claude Code style escalation: state the hook, do not name a rule."""
    from preloop.services import agent_permission_service

    captured = {}

    class _FakeService:
        def __init__(self, db, base_url):
            pass

        async def create_and_notify(self, **kwargs):
            captured.update(kwargs)
            approval = MagicMock()
            approval.id = uuid.uuid4()
            approval.status = "approved"
            approval.approver_comment = None
            return approval

    monkeypatch.setattr(
        "preloop.services.approval_service.ApprovalService", _FakeService
    )
    monkeypatch.setattr(
        agent_permission_service,
        "native_tool_approvals_disabled",
        AsyncMock(return_value=False),
    )
    workflow = MagicMock()
    workflow.timeout_seconds = 60
    workflow.id = uuid.uuid4()
    monkeypatch.setattr(
        agent_permission_service, "resolve_workflow", AsyncMock(return_value=workflow)
    )
    config = MagicMock()
    config.id = uuid.uuid4()
    monkeypatch.setattr(
        agent_permission_service, "resolve_tool_config", AsyncMock(return_value=config)
    )
    monkeypatch.setattr(
        "preloop.models.crud.tool_configuration.get_tool_config_by_name_and_source_async",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        agent_permission_service,
        "apply_native_access_rules",
        AsyncMock(return_value=None),
    )

    class _FakeSession:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(
        agent_permission_service, "get_async_db_session", lambda: _FakeSession()
    )

    await agent_permission_service.request_agent_permission(
        base_url="http://localhost:8000",
        account_id=str(uuid.uuid4()),
        user_id=None,
        managed_agent_id=None,
        runtime_session_id=None,
        managed_agent_name="claude",
        source="claude_code",
        tool_name="Bash",
        tool_input={"command": "rm -rf /"},
        agent_reasoning=None,
        client_decision="ask",
    )

    context = captured["rule_context"]
    assert context["source"] == SOURCE_AGENT_PERMISSION_HOOK
    assert "expression" not in context
    assert "No Preloop access rule was evaluated" in context["explanation"]


# --------------------------------------------------------------------------
# Persistence through ApprovalService
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_approval_request_stores_the_snapshot_on_the_row():
    """The context must reach the ORM object, not just travel as a kwarg."""
    from preloop.services.approval_service import ApprovalService

    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.run_sync = AsyncMock(return_value=None)
    service = ApprovalService(db, "http://localhost:8000")
    service._broadcast_approval_update = AsyncMock()

    context = build_rule_context(
        source=SOURCE_TOOL_ACCESS_RULE,
        decision="require_approval",
        rule_name="High-value payments",
        expression="args.amount >= 1000",
    )

    request = await service.create_approval_request(
        account_id=str(uuid.uuid4()),
        tool_configuration_id=uuid.uuid4(),
        approval_workflow_id=uuid.uuid4(),
        tool_name="send_payment",
        tool_args={"amount": 1000},
        rule_context=context,
    )

    assert request.rule_context == context
    added = [call.args[0] for call in db.add.call_args_list]
    assert added[0] is request
    assert any(
        getattr(obj, "event_type", None) == "approval_requested" for obj in added
    )


@pytest.mark.asyncio
async def test_create_approval_request_defaults_to_no_snapshot():
    """Callers that evaluated no rule leave the column NULL, not empty-ish."""
    from preloop.services.approval_service import ApprovalService

    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.run_sync = AsyncMock(return_value=None)
    service = ApprovalService(db, "http://localhost:8000")
    service._broadcast_approval_update = AsyncMock()

    request = await service.create_approval_request(
        account_id=str(uuid.uuid4()),
        tool_configuration_id=uuid.uuid4(),
        approval_workflow_id=uuid.uuid4(),
        tool_name="request_approval",
        tool_args={"operation": "deploy"},
    )

    assert request.rule_context is None


# --------------------------------------------------------------------------
# Compact surface: push payload
# --------------------------------------------------------------------------


def test_push_payload_carries_the_rule_name_only():
    """A push has no room for an expression, and a clipped one would mislead."""
    from preloop.services.push_notifications.notification_payloads import (
        NotificationPayloadBuilder,
    )

    payload = NotificationPayloadBuilder.new_approval_request(
        request_id=str(uuid.uuid4()),
        tool_name="send_payment",
        rule_context=build_rule_context(
            source=SOURCE_TOOL_ACCESS_RULE,
            decision="require_approval",
            rule_name="High-value payments",
            expression="args.amount >= 1000",
        ),
    )

    assert payload["data"]["rule_name"] == "High-value payments"
    assert "expression" not in payload["data"]


def test_push_payload_omits_the_rule_name_when_there_is_none():
    """No snapshot, no key: clients branch on presence, not on empty strings."""
    from preloop.services.push_notifications.notification_payloads import (
        NotificationPayloadBuilder,
    )

    payload = NotificationPayloadBuilder.new_approval_request(
        request_id=str(uuid.uuid4()),
        tool_name="request_approval",
    )
    assert "rule_name" not in payload["data"]
