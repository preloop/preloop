"""The matched-rule snapshot must survive a real round trip through Postgres.

Mock-based tests never exercise the JSONB column. This seeds approval rows on
a live database and reads them back, so a missing migration or a type mismatch
fails here rather than in production, where the approver would silently lose
the only explanation of why they are being asked.
"""

import uuid

from preloop.models import models
from preloop.models.schemas.approval_request import ApprovalRequestResponse
from preloop.services.approval_rule_context import (
    SOURCE_TOOL_ACCESS_RULE,
    build_rule_context,
)


def _workflow_and_config(db_session, test_user):
    """Minimal workflow + tool config an approval row can point at."""
    workflow = models.ApprovalWorkflow(
        account_id=test_user.account_id,
        name=f"rule-context-test-{uuid.uuid4().hex[:8]}",
        approval_type="manual",
        channel="email",
    )
    db_session.add(workflow)
    db_session.flush()

    config = models.ToolConfiguration(
        account_id=test_user.account_id,
        tool_name="send_payment",
        tool_source="mcp",
        approval_workflow_id=workflow.id,
        is_enabled=True,
        custom_config={},
    )
    db_session.add(config)
    db_session.flush()
    return workflow, config


def test_rule_context_round_trips_through_postgres(db_session, test_user):
    """Every field an approver reads survives the write and the read."""
    workflow, config = _workflow_and_config(db_session, test_user)
    rule_id = uuid.uuid4()
    context = build_rule_context(
        source=SOURCE_TOOL_ACCESS_RULE,
        decision="require_approval",
        rule_id=rule_id,
        rule_name="High-value payments",
        expression="args.amount >= 1000",
        expression_type="cel",
        priority=10,
        tool_configuration_id=config.id,
    )

    request = models.ApprovalRequest(
        account_id=test_user.account_id,
        tool_configuration_id=config.id,
        approval_workflow_id=workflow.id,
        tool_name="send_payment",
        tool_args={"amount": 1000},
        rule_context=context,
        status="pending",
    )
    db_session.add(request)
    db_session.flush()
    db_session.expire(request)

    stored = db_session.get(models.ApprovalRequest, request.id)
    assert stored.rule_context["rule_id"] == str(rule_id)
    assert stored.rule_context["expression"] == "args.amount >= 1000"
    assert stored.rule_context["priority"] == 10
    assert stored.rule_context["referenced_args"] == ["amount"]


def test_rule_context_is_nullable_for_approvals_with_no_rule(db_session, test_user):
    """request_approval and pre-existing rows legitimately have no context.

    If the column were NOT NULL, those paths would start failing to create
    approvals at all, which is a far worse outcome than a missing explanation.
    """
    workflow, config = _workflow_and_config(db_session, test_user)

    request = models.ApprovalRequest(
        account_id=test_user.account_id,
        tool_configuration_id=config.id,
        approval_workflow_id=workflow.id,
        tool_name="request_approval",
        tool_args={"operation": "deploy"},
        status="pending",
    )
    db_session.add(request)
    db_session.flush()
    db_session.expire(request)

    stored = db_session.get(models.ApprovalRequest, request.id)
    assert stored.rule_context is None

    # And the read schema serialises the absence rather than choking on it.
    payload = ApprovalRequestResponse.model_validate(stored)
    assert payload.rule_context is None


def test_read_schema_exposes_the_context_for_the_api(db_session, test_user):
    """The console reads this off the API, so it has to leave the schema."""
    workflow, config = _workflow_and_config(db_session, test_user)
    context = build_rule_context(
        source=SOURCE_TOOL_ACCESS_RULE,
        decision="require_approval",
        rule_name="Production deploys",
        expression="args.environment == 'production'",
        expression_type="cel",
        priority=0,
    )
    request = models.ApprovalRequest(
        account_id=test_user.account_id,
        tool_configuration_id=config.id,
        approval_workflow_id=workflow.id,
        tool_name="deploy",
        tool_args={"environment": "production"},
        rule_context=context,
        status="pending",
    )
    db_session.add(request)
    db_session.flush()

    payload = ApprovalRequestResponse.model_validate(request)
    assert payload.rule_context["rule_name"] == "Production deploys"
    assert payload.rule_context["expression"] == "args.environment == 'production'"
    assert payload.rule_context["referenced_args"] == ["environment"]
