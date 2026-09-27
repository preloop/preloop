"""Account hierarchy extension hooks H2 to H8, below the HTTP layer.

Each hook is registered with a fake and the test proves the open source call
site consults it. The login (H1), list endpoint (H4 ``resource:view``) and
gateway (H3) cases go through HTTP in
``tests/endpoints/test_account_hierarchy_hooks_api.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import create_access_token, get_current_user
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_ai_model,
    crud_api_usage,
    crud_flow,
    crud_flow_runner,
    crud_managed_agent,
    crud_mcp_server,
    crud_user,
)
from preloop.models.crud.billing import billing
from preloop.models.crud.budget import crud_budget_policy, crud_budget_spend
from preloop.models.crud.plan import (
    plan as crud_plan,
    subscription as crud_subscription,
)
from preloop.models.schemas.flow import FlowCreate
from preloop.plugins import account_hooks
from preloop.plugins.account_hooks import (
    ACTION_MODEL_INVOKE,
    ACTION_RUNNER_ACCEPT,
    ACTION_TOOL_CALL,
    VISIBLE_AI_MODEL,
    VISIBLE_FLOW,
    VISIBLE_MANAGED_AGENT,
    VISIBLE_MCP_SERVER,
    VISIBLE_RUNNER,
    BudgetExtension,
    Decision,
    HaltAncestry,
    SpendScope,
    VisibilityProvider,
)
from preloop.services import kill_switch
from preloop.services.model_gateway_auth import (
    ModelGatewayAuthContext,
    compute_authorized_model_ids,
)
from preloop.services.model_gateway_budget_enforcer import ModelGatewayBudgetEnforcer
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.policy_evaluator import evaluate_policy, evaluate_policy_async
from preloop.services.runner_service import hash_runner_token, lease_job


@pytest.fixture(autouse=True)
def _no_hooks():
    """Every test starts and ends with nothing registered."""
    account_hooks.reset_account_hooks()
    kill_switch.invalidate_kill_switch_cache()
    yield
    account_hooks.reset_account_hooks()
    kill_switch.invalidate_kill_switch_cache()


class _Visible(VisibilityProvider):
    """Visibility provider with a fixed id set per resource type."""

    def __init__(self, **ids: list[Any]) -> None:
        self.ids = ids
        self.calls: list[tuple[str, str]] = []

    def extra_visible_ids(self, db, account_id, resource_type):
        self.calls.append((str(account_id), resource_type))
        return self.ids.get(resource_type, [])


def _account(db: Session, name: str) -> models.Account:
    return crud_account.create(
        db, obj_in={"organization_name": f"{name} {uuid.uuid4().hex[:6]}"}
    )


def _user(db: Session, account: models.Account, name: str) -> models.User:
    unique = uuid.uuid4().hex[:8]
    return crud_user.create(
        db,
        obj_in={
            "account_id": account.id,
            "username": f"{name}{unique}",
            "email": f"{name}{unique}@example.com",
            "full_name": name,
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )


def _model(db: Session, account_id: Any, alias: str, secret: str) -> models.AIModel:
    return crud_ai_model.create_with_account(
        db=db,
        obj_in={
            "name": f"Model {uuid.uuid4().hex[:6]}",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": secret,
            "meta_data": {"gateway": {"enabled": True, "model_alias": alias}},
        },
        account_id=account_id,
    )


def _flow(db: Session, account_id: Any, name: str) -> models.Flow:
    return crud_flow.create(
        db=db,
        flow_in=FlowCreate(
            name=f"{name} {uuid.uuid4().hex[:6]}",
            prompt_template="Test",
            trigger_event_source="github",
            trigger_event_types=["push"],
            agent_type="codex",
            agent_config={},
            account_id=account_id,
        ),
        account_id=account_id,
    )


def _runner(db: Session, account_id: Any, name: str, *, online: bool = False):
    runner = models.FlowRunner(
        account_id=account_id,
        name=name,
        token_hash=hash_runner_token(f"token-{uuid.uuid4()}"),
        labels=["default"],
        status="online" if online else "offline",
        last_heartbeat=datetime.now(UTC) if online else None,
    )
    db.add(runner)
    db.flush()
    return runner


# ---------------------------------------------------------------------------
# H2: revoke fan-out
# ---------------------------------------------------------------------------


def test_h2_fanned_out_rows_tokens_are_invalid_on_their_next_request(
    db_session: Session,
) -> None:
    person_a = _user(db_session, _account(db_session, "A"), "rowa")
    person_b = _user(db_session, _account(db_session, "B"), "rowb")
    bystander = _user(db_session, _account(db_session, "C"), "rowc")
    token_b = create_access_token(
        data={"sub": str(person_b.id), "scopes": []}, auth_generation=0
    )
    token_c = create_access_token(
        data={"sub": str(bystander.id), "scopes": []}, auth_generation=0
    )
    assert get_current_user(token=token_b, db=db_session).id == person_b.id

    asked: list[Any] = []

    def fanout(db, user_id):
        asked.append(user_id)
        # The row itself may be named; it must still be bumped only once.
        return [person_b.id, user_id]

    account_hooks.register_revoke_fanout(fanout)
    new_generation = crud_user.bump_auth_generation(db_session, person_a.id)

    assert asked == [person_a.id]
    assert new_generation == 1
    db_session.expire_all()
    assert crud_user.get(db_session, id=person_b.id).auth_generation == 1
    with pytest.raises(HTTPException) as exc:
        get_current_user(token=token_b, db=db_session)
    assert exc.value.status_code == 401
    # A row nobody named keeps its session.
    assert get_current_user(token=token_c, db=db_session).id == bystander.id


def test_h2_unset_bumps_only_the_row_itself(db_session: Session) -> None:
    person_a = _user(db_session, _account(db_session, "A"), "rowa")
    person_b = _user(db_session, _account(db_session, "B"), "rowb")

    crud_user.bump_auth_generation(db_session, person_a.id)

    db_session.expire_all()
    assert crud_user.get(db_session, id=person_a.id).auth_generation == 1
    assert crud_user.get(db_session, id=person_b.id).auth_generation == 0


# ---------------------------------------------------------------------------
# H3: visibility provider, crud resolution points
# ---------------------------------------------------------------------------


def test_h3_ai_model_resolution_orders_own_then_shared_then_system(
    db_session: Session,
) -> None:
    mine = _account(db_session, "Mine")
    other = _account(db_session, "Other")
    foreign = _model(db_session, other.id, "shared/alias", "foreign-secret")
    hidden = _model(db_session, other.id, "hidden/alias", "hidden-secret")
    own = _model(db_session, mine.id, "own/alias", "own-secret")

    assert (
        crud_ai_model.get_for_account(db_session, id=foreign.id, account_id=mine.id)
        is None
    )
    before = [
        m.id for m in crud_ai_model.get_all_for_account(db_session, account_id=mine.id)
    ]
    assert foreign.id not in before

    provider = _Visible(**{VISIBLE_AI_MODEL: [foreign.id]})
    account_hooks.register_visibility_provider(provider)

    assert crud_ai_model.get_for_account(
        db_session, id=foreign.id, account_id=mine.id
    ).id == (foreign.id)
    assert (
        crud_ai_model.get_for_account(db_session, id=hidden.id, account_id=mine.id)
        is None
    )
    listed = crud_ai_model.get_all_for_account(db_session, account_id=mine.id)
    ids = [m.id for m in listed]
    assert ids.index(own.id) < ids.index(foreign.id)
    assert hidden.id not in ids
    system_positions = [i for i, m in enumerate(listed) if m.account_id is None]
    assert all(pos > ids.index(foreign.id) for pos in system_positions)
    assert (str(mine.id), VISIBLE_AI_MODEL) in provider.calls


def test_h3_shared_model_is_priced_with_its_owners_overrides(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.services.model_gateway_budget import ModelGatewayBudgetService
    from preloop.services.pricing_overrides import pricing_account_id

    other = _account(db_session, "Owner")
    foreign = _model(db_session, other.id, "shared/priced", "s1")
    own = _model(db_session, test_user.account_id, "own/priced", "s2")
    assert pricing_account_id(test_user.account_id, foreign) == test_user.account_id

    account_hooks.register_visibility_provider(
        _Visible(**{VISIBLE_AI_MODEL: [foreign.id]})
    )
    asked: list[Any] = []
    service = ModelGatewayBudgetService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )
    import preloop.services.pricing_overrides as pricing_overrides

    original = pricing_overrides.resolve_pricing_override

    def spy(db, *, account_id, ai_model, requested_alias):
        asked.append(account_id)
        return original(
            db,
            account_id=account_id,
            ai_model=ai_model,
            requested_alias=requested_alias,
        )

    monkeypatch.setattr(pricing_overrides, "resolve_pricing_override", spy)
    service._pricing_override_for_request(foreign, {"model": "shared/priced"})
    service._pricing_override_for_request(own, {"model": "own/priced"})

    assert [str(a) for a in asked] == [str(other.id), str(test_user.account_id)]


def test_h3_mcp_server_resolution_includes_shared_servers(db_session: Session) -> None:
    mine = _account(db_session, "Mine")
    other = _account(db_session, "Other")
    servers = {}
    for owner, name in ((mine, "own"), (other, "shared"), (other, "hidden")):
        server = models.MCPServer(
            name=f"{name}-{uuid.uuid4().hex[:6]}",
            url="http://localhost:8080/mcp",
            transport="http-streaming",
            auth_type="none",
            account_id=owner.id,
            status="active",
        )
        db_session.add(server)
        servers[name] = server
    db_session.flush()

    assert (
        crud_mcp_server.get_visible(
            db_session, id=servers["shared"].id, account_id=str(mine.id)
        )
        is None
    )
    assert [
        s.id
        for s in crud_mcp_server.get_active_visible_by_account(
            db_session, account_id=str(mine.id)
        )
    ] == [servers["own"].id]

    account_hooks.register_visibility_provider(
        _Visible(**{VISIBLE_MCP_SERVER: [servers["shared"].id]})
    )

    assert [
        s.id
        for s in crud_mcp_server.get_active_visible_by_account(
            db_session, account_id=str(mine.id)
        )
    ] == [servers["own"].id, servers["shared"].id]
    assert (
        crud_mcp_server.get_visible(
            db_session, id=servers["shared"].id, account_id=str(mine.id)
        ).id
        == servers["shared"].id
    )
    assert (
        crud_mcp_server.get_visible(
            db_session, id=servers["hidden"].id, account_id=str(mine.id)
        )
        is None
    )
    # Policy and configuration code keeps reading own servers only.
    assert [
        s.id
        for s in crud_mcp_server.get_active_by_account(
            db_session, account_id=str(mine.id)
        )
    ] == [servers["own"].id]


def test_h3_mcp_tool_discovery_uses_the_visible_servers(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.services import mcp_tool_discovery

    seen: list[str] = []

    def visible(db, account_id):
        seen.append(account_id)
        return []

    monkeypatch.setattr(crud_mcp_server, "get_active_visible_by_account", visible)
    account_id = str(uuid.uuid4())
    assert mcp_tool_discovery._get_proxied_tools_sync(account_id, db_session) == []
    assert seen == [account_id]


def test_h3_mcp_tool_call_resolves_the_server_through_get_visible() -> None:
    from pathlib import Path

    import preloop.services.dynamic_fastmcp as dynamic_fastmcp

    source = Path(dynamic_fastmcp.__file__).read_text()
    assert "crud_mcp_server.get_visible(" in source
    assert "crud_mcp_server.get(db, id=server_id, account_id=account_id)" not in source


def test_h3_flow_list_includes_shared_flows_only_when_asked(
    db_session: Session,
) -> None:
    mine = _account(db_session, "Mine")
    other = _account(db_session, "Other")
    own = _flow(db_session, mine.id, "own")
    shared = _flow(db_session, other.id, "shared")
    account_hooks.register_visibility_provider(_Visible(**{VISIBLE_FLOW: [shared.id]}))

    default = {f.id for f in crud_flow.get_multi(db_session, account_id=mine.id)}
    listed = {
        f.id
        for f in crud_flow.get_multi(
            db_session, account_id=mine.id, include_shared=True
        )
    }

    assert default == {own.id}
    assert listed == {own.id, shared.id}


def test_h3_runner_list_and_dispatch_query_include_shared_runners(
    db_session: Session,
) -> None:
    mine = _account(db_session, "Mine")
    other = _account(db_session, "Other")
    own = _runner(db_session, mine.id, "own", online=True)
    shared = _runner(db_session, other.id, "shared", online=True)
    _runner(db_session, other.id, "hidden", online=True)

    assert {
        r.id for r in crud_flow_runner.list_for_account(db_session, account_id=mine.id)
    } == {own.id}
    account_hooks.register_visibility_provider(
        _Visible(**{VISIBLE_RUNNER: [shared.id]})
    )

    assert {
        r.id for r in crud_flow_runner.list_for_account(db_session, account_id=mine.id)
    } == {own.id, shared.id}
    assert {
        r.id
        for r in crud_flow_runner.find_matching(
            db_session, account_id=mine.id, pool="default"
        )
    } == {own.id, shared.id}


def test_h3_managed_agent_list_includes_shared_agents_without_their_owner(
    db_session: Session,
) -> None:
    mine = _account(db_session, "Mine")
    other = _account(db_session, "Other")
    other_owner = _user(db_session, other, "owner")
    now = datetime.now(UTC).replace(tzinfo=None)
    agents = {}
    for owner_account, name, owner_id in (
        (mine, "own", None),
        (other, "shared", other_owner.id),
    ):
        agent = models.ManagedAgent(
            id=uuid.uuid4(),
            account_id=owner_account.id,
            owner_user_id=owner_id,
            agent_kind="codex",
            session_source_type="codex",
            session_source_id=f"{name}-{uuid.uuid4().hex[:6]}",
            display_name=name,
            enrolled_via="runtime_session_token",
            lifecycle_state="active",
            lifecycle_updated_at=now,
            last_seen_at=now,
            tags={},
        )
        db_session.add(agent)
        agents[name] = agent
    db_session.flush()

    before = crud_managed_agent.list_for_account(db_session, account_id=str(mine.id))
    assert {item["id"] for item in before["items"]} == {str(agents["own"].id)}

    account_hooks.register_visibility_provider(
        _Visible(**{VISIBLE_MANAGED_AGENT: [agents["shared"].id]})
    )
    after = crud_managed_agent.list_for_account(db_session, account_id=str(mine.id))

    by_id = {item["id"]: item for item in after["items"]}
    assert set(by_id) == {str(agents["own"].id), str(agents["shared"].id)}
    shared_item = by_id[str(agents["shared"].id)]
    assert shared_item["owner_user_id"] is None
    assert shared_item["owner_email"] is None
    assert shared_item["owner_username"] is None
    assert other_owner.email not in repr(after)


# ---------------------------------------------------------------------------
# H4: authorize, at each non-HTTP call site
# ---------------------------------------------------------------------------


class _Recorder:
    """Authorizer that records every question and denies a chosen set."""

    def __init__(self, deny=lambda action, resource: False) -> None:
        self.calls: list[tuple[Any, str, Any]] = []
        self.deny = deny

    def __call__(self, ctx, action, resource):
        self.calls.append((ctx, action, resource))
        if self.deny(action, resource):
            return Decision("deny", rule_ids=("rule-1",), reason="blocked by rule-1")
        return Decision("allow")


def test_h4_model_invoke_removes_denied_models(
    db_session: Session, test_user: models.User
) -> None:
    allowed = _model(db_session, test_user.account_id, "a/allowed", "s1")
    denied = _model(db_session, test_user.account_id, "a/denied", "s2")
    auth = ModelGatewayAuthContext(token="t", user=test_user)
    assert compute_authorized_model_ids(db_session, auth, [allowed, denied]) == (
        frozenset({str(allowed.id), str(denied.id)})
    )

    recorder = _Recorder(lambda action, resource: resource.id == denied.id)
    account_hooks.register_authorizer(recorder)

    result = compute_authorized_model_ids(db_session, auth, [allowed, denied])

    assert result == frozenset({str(allowed.id)})
    assert {call[1] for call in recorder.calls} == {ACTION_MODEL_INVOKE}
    assert recorder.calls[0][0].account_id == test_user.account_id


def test_h4_model_invoke_is_asked_once_per_gateway_request(
    db_session: Session, test_user: models.User
) -> None:
    from preloop.services.openai_gateway import OpenAIGatewayService

    model = _model(db_session, test_user.account_id, "a/memo", "s1")
    recorder = _Recorder()
    account_hooks.register_authorizer(recorder)
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )

    service._authorized_model_ids([model])
    service._authorized_model_ids([model])

    assert len(recorder.calls) == 1


def _quiet_policy_log(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    logged: list[dict] = []
    monkeypatch.setattr(
        "preloop.services.policy_evaluator._log_policy_decision_async",
        lambda **kwargs: logged.append(kwargs),
    )
    return logged


def test_h4_tool_call_deny_wins_in_evaluate_policy(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    logged = _quiet_policy_log(monkeypatch)
    allowed = evaluate_policy(
        db_session, "create_issue", {}, account_id=test_user.account_id
    )
    assert allowed.action != "deny"

    recorder = _Recorder(lambda action, resource: action == ACTION_TOOL_CALL)
    account_hooks.register_authorizer(recorder)
    decision = evaluate_policy(
        db_session, "create_issue", {"title": "x"}, account_id=test_user.account_id
    )

    assert decision.action == "deny"
    assert decision.source == "access_rule"
    assert decision[2] == "blocked by rule-1"
    assert recorder.calls[0][1] == ACTION_TOOL_CALL
    assert recorder.calls[0][2] == {"tool_name": "create_issue"}
    assert logged[-1]["extra_details"] == {"access_rule_ids": ["rule-1"]}


def test_h4_tool_call_deny_wins_in_evaluate_policy_async(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _quiet_policy_log(monkeypatch)
    recorder = _Recorder(lambda action, resource: action == ACTION_TOOL_CALL)
    account_hooks.register_authorizer(recorder)

    decision = asyncio.run(
        evaluate_policy_async(MagicMock(), "create_issue", {}, account_id=uuid.uuid4())
    )

    assert decision.action == "deny"
    assert recorder.calls[0][1] == ACTION_TOOL_CALL


def test_h4_runner_accept_skips_a_denied_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "preloop.models.crud.crud_flow_execution.admit_runtime_start",
        lambda *args, **kwargs: True,
    )
    denied = SimpleNamespace(id=uuid.uuid4(), status="online", free_slots=1)
    accepted = SimpleNamespace(id=uuid.uuid4(), status="online", free_slots=1)
    claimed: list[Any] = []
    monkeypatch.setattr(
        crud_flow_runner, "find_matching", lambda db, **kwargs: [denied, accepted]
    )

    def claim(db, *, runner_id):
        claimed.append(runner_id)
        return accepted if runner_id == accepted.id else denied

    monkeypatch.setattr(crud_flow_runner, "claim_free_slot", claim)
    monkeypatch.setattr(
        crud_flow_runner,
        "create_assignment",
        lambda db, **kwargs: SimpleNamespace(reported_status=None, **kwargs),
    )
    recorder = _Recorder(lambda action, resource: resource is denied)
    account_hooks.register_authorizer(recorder)

    execution_id = uuid.uuid4()
    result = lease_job(
        MagicMock(),
        account_id=uuid.uuid4(),
        pool="default",
        execution_id=execution_id,
        payload={"execution_id": str(execution_id)},
    )

    assert result is accepted
    assert claimed == [accepted.id]
    assert [call[1] for call in recorder.calls] == [ACTION_RUNNER_ACCEPT] * 2


# ---------------------------------------------------------------------------
# H5: budget extension
# ---------------------------------------------------------------------------


class _ParentBudget(BudgetExtension):
    def __init__(self, parent_id: Any, policies: list[Any]) -> None:
        self.parent_id = parent_id
        self.policies = policies
        self.policy_calls: list[Any] = []
        self.scope_calls: list[Any] = []

    def extra_policies(self, db, *, account_id, auth_context, ai_model, model_alias):
        self.policy_calls.append(account_id)
        return self.policies

    def extra_spend_scopes(self, db, *, account_id, subject_scopes, model_alias):
        self.scope_calls.append(account_id)
        return [SpendScope(account_id=self.parent_id, subject_type="account")]


def _priced_model() -> models.AIModel:
    return models.AIModel(
        id=uuid.uuid4(),
        provider_name="openai",
        model_identifier="synthetic-priced",
        meta_data={
            "gateway": {"enabled": True},
            "pricing": {"input_price_per_1k": 1, "output_price_per_1k": 1},
        },
    )


def test_h5_extra_policy_blocks_a_request(
    db_session: Session, test_user: models.User
) -> None:
    parent = _account(db_session, "Parent")
    parent_policy = crud_budget_policy.create(
        db_session,
        obj_in={
            "account_id": parent.id,
            "subject_type": "account",
            "period": models.BudgetPeriod.monthly,
            "hard_limit_usd": 1.0,
        },
    )
    crud_budget_spend.upsert_spend_batch(
        db_session,
        rows=[
            {
                "id": uuid.uuid4(),
                "account_id": parent.id,
                "subject_type": "account",
                "subject_id": None,
                "model_alias": "",
                "period": models.BudgetPeriod.monthly,
                "period_start": datetime.now(UTC).replace(
                    day=1, hour=0, minute=0, second=0, microsecond=0
                ),
                "spend_usd": 0.999,
            }
        ],
    )
    auth = ModelGatewayAuthContext(token="t", user=test_user)
    payload = {"model": "synthetic", "messages": [{"role": "user", "content": "x"}]}
    enforcer = ModelGatewayBudgetEnforcer()
    assert enforcer.enforce_or_raise(db_session, auth, _priced_model(), payload) is None

    extension = _ParentBudget(parent.id, [parent_policy])
    account_hooks.register_budget_extension(extension)
    with pytest.raises(ModelGatewayAPIError) as exc:
        enforcer.enforce_or_raise(db_session, auth, _priced_model(), payload)

    assert exc.value.status_code == 403
    assert exc.value.code == "budget_limit_exceeded"
    assert extension.policy_calls == [test_user.account_id]


def test_h5_extra_spend_lands_in_the_same_statement_and_transaction(
    db_session: Session, test_user: models.User
) -> None:
    parent = _account(db_session, "Parent")
    extension = _ParentBudget(parent.id, [])
    account_hooks.register_budget_extension(extension)
    inserts: list[str] = []

    def capture(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith("INSERT INTO BUDGET_SPEND"):
            inserts.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        crud_api_usage.log_gateway_request(
            db_session,
            endpoint="/openai/v1/chat/completions",
            method="POST",
            status_code=200,
            duration=0.1,
            user_id=str(test_user.id),
            account_id=str(test_user.account_id),
            model_alias="openai/gpt-5",
            provider_name="openai",
            total_tokens=10,
            estimated_cost=0.25,
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert extension.scope_calls == [test_user.account_id]
    # One upsert carries the account's buckets and the parent's, so the
    # usage fact's commit publishes both or neither.
    assert len(inserts) == 1
    spend_by_account = {
        str(row.account_id): row.spend_usd
        for row in db_session.query(models.BudgetSpendActivity).filter(
            models.BudgetSpendActivity.subject_type == "account",
            models.BudgetSpendActivity.model_alias == "",
            models.BudgetSpendActivity.period == models.BudgetPeriod.monthly,
            models.BudgetSpendActivity.account_id.in_(
                [test_user.account_id, parent.id]
            ),
        )
    }
    assert spend_by_account == {
        str(test_user.account_id): pytest.approx(0.25),
        str(parent.id): pytest.approx(0.25),
    }


# ---------------------------------------------------------------------------
# H6: halt ancestry
# ---------------------------------------------------------------------------


def test_h6_inherited_halt_applies_after_invalidation(
    db_session: Session, test_user: models.User
) -> None:
    class ParentHalted(HaltAncestry):
        def __init__(self) -> None:
            self.calls: list[Any] = []

        def extra_halted_scopes(self, db, account_id):
            self.calls.append(account_id)
            return {"gateway"}

    assert kill_switch.gateway_halted(db_session, test_user.account_id) is False
    ancestry = ParentHalted()
    account_hooks.register_halt_ancestry(ancestry)
    # Cached: the extension's toggle has to invalidate the affected accounts.
    assert kill_switch.gateway_halted(db_session, test_user.account_id) is False

    kill_switch.invalidate_kill_switch_cache_for_accounts([test_user.account_id])

    assert kill_switch.gateway_halted(db_session, test_user.account_id) is True
    assert kill_switch.tools_halted(db_session, test_user.account_id) is False
    assert ancestry.calls == [test_user.account_id]


# ---------------------------------------------------------------------------
# H7: billing account resolver
# ---------------------------------------------------------------------------


def test_h7_subscription_lookups_read_the_billing_account(
    db_session: Session,
) -> None:
    parent = _account(db_session, "Parent")
    child = _account(db_session, "Child")
    if crud_plan.get(db_session, id="teams") is None:
        crud_plan.create(
            db_session,
            obj_in={
                "id": "teams",
                "name": "Teams",
                "price_monthly": 29.0,
                "price_annually": 290.0,
                "features": {"max_users": 10},
                "is_active": True,
                "is_custom": False,
            },
        )
    subscription = crud_subscription.create(
        db_session,
        obj_in={
            "account_id": parent.id,
            "plan_id": "teams",
            "stripe_subscription_id": f"sub_{uuid.uuid4().hex[:10]}",
            "status": "active",
            "current_period_start": datetime.now(UTC) - timedelta(days=1),
            "current_period_end": datetime.now(UTC) + timedelta(days=29),
        },
    )
    lookups = (
        lambda: crud_subscription.get_active_for_account(
            db_session, account_id=str(child.id)
        ),
        lambda: crud_subscription.get_latest_for_account(
            db_session, account_id=str(child.id)
        ),
        lambda: billing.entitled_subscription(db_session, str(child.id)),
    )
    assert [lookup() for lookup in lookups] == [None, None, None]

    asked: list[Any] = []

    def resolver(db, account_id):
        asked.append(str(account_id))
        return parent.id if str(account_id) == str(child.id) else None

    account_hooks.register_billing_account_resolver(resolver)

    assert [lookup().id for lookup in lookups] == [subscription.id] * 3
    assert asked == [str(child.id)] * 3


# ---------------------------------------------------------------------------
# H8: multi-account usage summaries
# ---------------------------------------------------------------------------


def _usage(db: Session, account_id: Any, alias: str, cost: float, tokens: int):
    crud_api_usage.log_gateway_request(
        db,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        account_id=str(account_id),
        model_alias=alias,
        provider_name="openai",
        prompt_tokens=tokens,
        completion_tokens=tokens,
        total_tokens=2 * tokens,
        estimated_cost=cost,
    )


def test_h8_account_ids_equals_the_sum_of_single_account_results(
    db_session: Session,
) -> None:
    a = _account(db_session, "A")
    b = _account(db_session, "B")
    outsider = _account(db_session, "Outsider")
    _usage(db_session, a.id, "m/one", 0.5, 10)
    _usage(db_session, a.id, "m/two", 0.25, 5)
    _usage(db_session, b.id, "m/one", 1.0, 20)
    _usage(db_session, outsider.id, "m/one", 9.0, 90)
    start = datetime.now(UTC) - timedelta(days=1)
    end = datetime.now(UTC) + timedelta(days=1)
    both = [str(a.id), str(b.id)]

    def summary(**kw):
        return crud_api_usage.get_gateway_usage_summary(
            db_session, start_date=start, end_date=end, **kw
        )

    single = [summary(account_id=str(x)) for x in (a.id, b.id)]
    combined = summary(account_id=str(a.id), account_ids=both)
    for key in ("request_count", "total_tokens", "prompt_tokens", "completion_tokens"):
        assert combined[key] == single[0][key] + single[1][key], key
    assert combined["estimated_cost"] == pytest.approx(
        single[0]["estimated_cost"] + single[1]["estimated_cost"]
    )

    def by_model(**kw):
        rows = crud_api_usage.get_gateway_usage_by_model(
            db_session, start_date=start, end_date=end, **kw
        )
        return rows

    totals: dict[str, list[float]] = {}
    for x in (a.id, b.id):
        for row in by_model(account_id=str(x)):
            acc = totals.setdefault(row["model_alias"], [0, 0.0])
            acc[0] += row["request_count"]
            acc[1] += row["estimated_cost"]
    combined_models = {
        row["model_alias"]: [row["request_count"], row["estimated_cost"]]
        for row in by_model(account_id=str(a.id), account_ids=both)
    }
    assert combined_models.keys() == totals.keys()
    for alias, (count, cost) in totals.items():
        assert combined_models[alias][0] == count
        assert combined_models[alias][1] == pytest.approx(cost)

    def series(**kw):
        return crud_api_usage.get_gateway_usage_timeseries(
            db_session, start_date=start, end_date=end, **kw
        )

    single_series = [series(account_id=str(x)) for x in (a.id, b.id)]
    combined_series = series(account_id=str(a.id), account_ids=both)
    assert sum(p["request_count"] for p in combined_series) == sum(
        p["request_count"] for s in single_series for p in s
    )
    assert sum(p["estimated_cost"] for p in combined_series) == pytest.approx(
        sum(p["estimated_cost"] for s in single_series for p in s)
    )

    def spend(**kw):
        return crud_api_usage.get_gateway_spend(db_session, start=start, **kw)

    assert spend(account_id=str(a.id), account_ids=both) == pytest.approx(
        spend(account_id=str(a.id)) + spend(account_id=str(b.id))
    )
    # Without account_ids nothing changes: the account alone.
    assert summary(account_id=str(a.id))["request_count"] == 2
    assert spend(account_id=str(a.id), account_ids=[]) == 0.0
