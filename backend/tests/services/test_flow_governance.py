"""Per-flow governance overrides (subject type ``flows``).

A flow execution's credential carries ``flow_id``; its traffic is governed by
the flow override first, then the managed agent it runs as (employee flows),
then the account defaults.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from preloop.models.crud import crud_account, crud_ai_model, crud_api_key
from preloop.services.agent_permission_service import (
    native_tool_approvals_disabled,
    resolve_workflow,
)
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_budget import ModelGatewayBudgetService
from preloop.services.policy_evaluator import evaluate_policy
from preloop.services.subject_governance import (
    SUBJECT_TYPE_FLOWS,
    SUBJECT_TYPE_MANAGED_AGENTS,
    build_subject_context_from_api_key,
    get_scoped_tool_rules,
    is_tool_enabled_for_subject,
    normalize_subject_governance_store,
    set_subject_governance,
    subject_scope_chain,
)

FLOW_ID = "11111111-1111-4111-8111-111111111111"
AGENT_ID = "22222222-2222-4222-8222-222222222222"


def _flow_key(flow_id: str = FLOW_ID, **extra):
    context = {"flow_execution_id": str(uuid.uuid4()), "flow_id": flow_id}
    context.update(extra)
    return SimpleNamespace(id=uuid.uuid4(), context_data=context)


def test_flows_bucket_survives_normalize():
    meta = set_subject_governance(
        {},
        subject_type=SUBJECT_TYPE_FLOWS,
        subject_id=FLOW_ID,
        config={"allowed_models": ["openai/gpt-5"]},
    )
    store = normalize_subject_governance_store(meta)
    assert store[SUBJECT_TYPE_FLOWS][FLOW_ID]["allowed_models"] == ["openai/gpt-5"]


def test_flow_execution_key_puts_flow_in_scope_chain_before_agent():
    context = build_subject_context_from_api_key(_flow_key(managed_agent_id=AGENT_ID))
    chain = subject_scope_chain(context)
    assert [scope[0] for scope in chain] == [
        "api_keys",
        SUBJECT_TYPE_FLOWS,
        SUBJECT_TYPE_MANAGED_AGENTS,
    ]
    assert chain[1][1] == FLOW_ID


def test_key_without_execution_does_not_claim_a_flow():
    key = SimpleNamespace(id=uuid.uuid4(), context_data={"flow_id": FLOW_ID})
    assert build_subject_context_from_api_key(key)["flow_id"] is None


def test_flow_tool_rule_override_wins_over_agent():
    meta = set_subject_governance(
        {},
        subject_type=SUBJECT_TYPE_MANAGED_AGENTS,
        subject_id=AGENT_ID,
        config={"tool_rules": {"search_issues": [{"action": "allow"}]}},
    )
    meta = set_subject_governance(
        meta,
        subject_type=SUBJECT_TYPE_FLOWS,
        subject_id=FLOW_ID,
        config={"tool_rules": {"search_issues": [{"action": "deny"}]}},
    )
    rules = get_scoped_tool_rules(
        meta,
        tool_name="search_issues",
        subject_context={"flow_id": FLOW_ID, "managed_agent_id": AGENT_ID},
    )
    assert rules == [{"action": "deny"}]


def test_flow_enabled_override_disables_tool_only_for_that_flow():
    meta = set_subject_governance(
        {},
        subject_type=SUBJECT_TYPE_FLOWS,
        subject_id=FLOW_ID,
        config={"tool_enabled_overrides": {"create_issue": False}},
    )
    assert not is_tool_enabled_for_subject(
        meta, tool_name="create_issue", subject_context={"flow_id": FLOW_ID}
    )
    assert is_tool_enabled_for_subject(
        meta,
        tool_name="create_issue",
        subject_context={"flow_id": str(uuid.uuid4())},
    )


def _write_meta(db_session, test_user, meta):
    account = crud_account.get(db_session, id=test_user.account_id)
    crud_account.update(db_session, db_obj=account, obj_in={"meta_data": meta})


def test_policy_evaluator_denies_tool_by_flow_rule(db_session, test_user):
    _write_meta(
        db_session,
        test_user,
        set_subject_governance(
            {},
            subject_type=SUBJECT_TYPE_FLOWS,
            subject_id=FLOW_ID,
            config={"tool_rules": {"search_issues": [{"action": "deny"}]}},
        ),
    )
    flow_decision = evaluate_policy(
        db_session,
        "search_issues",
        {},
        test_user.account_id,
        subject_context={"flow_id": FLOW_ID},
    )
    other_decision = evaluate_policy(
        db_session,
        "search_issues",
        {},
        test_user.account_id,
        subject_context={"flow_id": str(uuid.uuid4())},
    )
    assert flow_decision[0] == "deny"
    assert other_decision[0] == "allow"


def _gateway_model(db_session, test_user, name, identifier):
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": name,
            "provider_name": "openai",
            "model_identifier": identifier,
            "meta_data": {
                "gateway": {"enabled": True},
                "pricing": {"input_price_per_1k": 0.01, "output_price_per_1k": 0.02},
            },
        },
        account_id=test_user.account_id,
    )


def _flow_runtime_key(db_session, test_user, flow_id):
    api_key, _ = crud_api_key.create_runtime_key(
        db_session,
        name="Flow Execution test",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={"flow_execution_id": str(uuid.uuid4()), "flow_id": flow_id},
    )
    return api_key


def test_gateway_denies_model_blocked_by_flow_override(db_session, test_user):
    """The account allows every model; the flow override narrows it."""
    allowed = _gateway_model(db_session, test_user, "Allowed", "gpt-5-mini")
    denied = _gateway_model(db_session, test_user, "Denied", "gpt-5")
    _write_meta(
        db_session,
        test_user,
        set_subject_governance(
            {},
            subject_type=SUBJECT_TYPE_FLOWS,
            subject_id=FLOW_ID,
            config={"allowed_models": ["openai/gpt-5-mini"]},
        ),
    )
    flow_key = _flow_runtime_key(db_session, test_user, FLOW_ID)
    other_key = _flow_runtime_key(db_session, test_user, str(uuid.uuid4()))

    def check(api_key, model, wire):
        service = ModelGatewayBudgetService(
            db_session,
            ModelGatewayAuthContext(token="t", user=test_user, api_key=api_key),
        )
        return service.preflight_check(model, {"model": wire, "input": "hi"})

    blocked = check(flow_key, denied, "openai/gpt-5")
    assert blocked.hard_limit_exceeded is True
    assert blocked.enforcement_reason == "subject_model_not_allowed"
    assert check(flow_key, allowed, "openai/gpt-5-mini").hard_limit_exceeded is False
    # Another flow inherits the (unrestricted) account policy.
    assert check(other_key, denied, "openai/gpt-5").hard_limit_exceeded is False


class _AsyncDBShim:
    def __init__(self, db_session):
        self._db = db_session

    async def execute(self, statement):
        return self._db.execute(statement)


@pytest.mark.asyncio
async def test_native_approvals_chain_flow_then_agent_then_account(
    db_session, test_user
):
    meta = {
        "subject_governance": {
            "flows": {FLOW_ID: {"native_tool_approvals": "enforce"}},
            "managed_agents": {AGENT_ID: {"native_tool_approvals": "off"}},
            "account_defaults": {"native_tool_approvals": "off"},
        }
    }
    _write_meta(db_session, test_user, meta)
    db = _AsyncDBShim(db_session)
    account_id = str(test_user.account_id)
    flow = uuid.UUID(FLOW_ID)
    agent = uuid.UUID(AGENT_ID)

    # Flow "enforce" shields the execution from agent and account "off".
    assert (
        await native_tool_approvals_disabled(db, account_id, agent, flow_id=flow)
        is False
    )
    assert (
        await native_tool_approvals_disabled(db, account_id, None, flow_id=flow)
        is False
    )
    # A flow without an override inherits the account default.
    assert (
        await native_tool_approvals_disabled(db, account_id, None, flow_id=uuid.uuid4())
        is True
    )


@pytest.mark.asyncio
async def test_resolve_workflow_prefers_flow_pin(db_session, test_user):
    from preloop.models.crud import crud_approval_workflow

    pinned = crud_approval_workflow.create(
        db_session,
        obj_in={"name": "Flow approvals", "approval_type": "standard"},
        account_id=test_user.account_id,
    )
    _write_meta(
        db_session,
        test_user,
        set_subject_governance(
            {},
            subject_type=SUBJECT_TYPE_FLOWS,
            subject_id=FLOW_ID,
            config={"approval_workflow_id": str(pinned.id)},
        ),
    )
    workflow = await resolve_workflow(
        _AsyncDBShim(db_session),
        str(test_user.account_id),
        test_user.id,
        flow_id=uuid.UUID(FLOW_ID),
    )
    assert workflow.id == pinned.id


def test_permission_identity_reads_flow_id_only_from_execution_tokens():
    from preloop.api.endpoints.agent_permission import _flow_id_from_context

    assert _flow_id_from_context(
        {"flow_execution_id": "exec", "flow_id": FLOW_ID}
    ) == uuid.UUID(FLOW_ID)
    assert _flow_id_from_context({"flow_id": FLOW_ID}) is None
    assert _flow_id_from_context({"flow_execution_id": "x", "flow_id": "bad"}) is None


@pytest.mark.asyncio
async def test_native_rules_pass_flow_to_policy_evaluation(monkeypatch):
    """A flow tool rule reaches the native permission path."""
    from preloop.services import agent_permission_service as service
    from preloop.services import policy_evaluator

    seen = {}

    async def fake_evaluate(db, tool_name, tool_args, account_id, **kwargs):
        seen.update(kwargs["subject_context"])
        return policy_evaluator.PolicyDecision("deny", None, "flow rule")

    monkeypatch.setattr(policy_evaluator, "evaluate_policy_async", fake_evaluate)
    outcome = await service.apply_native_access_rules(
        None,
        config=SimpleNamespace(id=uuid.uuid4(), is_enabled=True),
        tool_name="Bash",
        tool_input={},
        account_id="acct",
        user_id=None,
        managed_agent_id=None,
        runtime_session_id=None,
        flow_id=uuid.UUID(FLOW_ID),
    )
    assert seen["flow_id"] == FLOW_ID
    assert outcome[0] == "deny"
