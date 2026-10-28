"""Owner opt-in: a subscription owner's own hosted flows may use the subscription.

Pins the rule in ``preloop.services.subscription_flow_use`` at the three
places that enforce it: trigger admission (``prepare_execution_routing``),
gateway authorization (``compute_authorized_model_ids`` via the gateway
service) and usage attribution. Other agents, other users' flows and flows
with the opt-in off keep the existing refusal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from preloop.models import models
from preloop.models.crud import (
    crud_ai_model,
    crud_api_key,
    crud_managed_agent_ai_model_binding,
    crud_user,
)
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.model_routing import (
    FlowModelPrincipalError,
    prepare_execution_routing,
    validate_execution_model_principal,
)
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.secret_service import OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE
from preloop.services.subscription_flow_use import (
    OWNER_FLOW_SUBSCRIPTION_USE,
    flow_may_use_subscription,
    owner_flow_use_enabled,
    subscription_owner_user_id,
)

pytestmark = pytest.mark.usefixtures("isolate_system_ai_models")

ALIAS = "openai/gpt-example-sub"


def _second_user(db_session, test_user, name: str) -> models.User:
    user = crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "email": f"{name}@example.com",
            "username": name,
            "full_name": name.title(),
            "is_active": True,
            "email_verified": True,
            "hashed_password": "unused",
            "user_source": "local",
        },
    )
    db_session.flush()
    return user


def _agent(db_session, account_id, owner_id, source_id: str) -> models.ManagedAgent:
    now = datetime.now(UTC).replace(tzinfo=None)
    agent = models.ManagedAgent(
        id=uuid4(),
        account_id=account_id,
        owner_user_id=owner_id,
        agent_kind="codex",
        session_source_type="codex",
        session_source_id=source_id,
        display_name=f"Codex {source_id}",
        enrolled_via="runtime_session_token",
        lifecycle_state="active",
        lifecycle_updated_at=now,
        last_seen_at=now,
    )
    db_session.add(agent)
    db_session.commit()
    return agent


def _subscription_model(db_session, account_id, agent) -> models.AIModel:
    model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"Codex subscription {uuid4()}",
            "provider_name": "openai-codex",
            "model_identifier": "gpt-example-sub",
            "credential_type": OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE,
            "credential_payload": {"access": "placeholder", "refresh": "placeholder"},
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": ALIAS,
                    "provider_adapter": "preloop",
                }
            },
        },
        account_id=account_id,
    )
    crud_managed_agent_ai_model_binding.replace_for_agent(
        db_session,
        account_id=str(account_id),
        agent_id=str(agent.id),
        bindings=[
            {"config_key": "primary", "gateway_alias": ALIAS, "ai_model_id": model.id}
        ],
    )
    assert model.is_principal_bound_oauth
    return model


def _flow(db_session, account_id, owner_id, model) -> models.Flow:
    flow = models.Flow(
        account_id=account_id,
        name=f"Review {uuid4()}",
        prompt_template="Review the change.",
        agent_type="codex",
        agent_config={"execution_path": "ephemeral"},
        ai_model_id=model.id,
        runner_pool="server",
        created_by_user_id=owner_id,
    )
    db_session.add(flow)
    db_session.flush()
    return flow


def _execution(db_session, flow) -> models.FlowExecution:
    execution = models.FlowExecution(flow_id=flow.id, status="RUNNING")
    db_session.add(execution)
    db_session.flush()
    return execution


def _flow_context(db_session, test_user, flow, execution) -> ModelGatewayAuthContext:
    """The context ``create_flow_runtime_token`` mints for one execution."""
    api_key, token = crud_api_key.create_runtime_key(
        db_session,
        name=f"Flow Execution {execution.id}",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={
            "flow_execution_id": str(execution.id),
            "flow_id": str(flow.id),
            "runtime_principal": {
                "type": "flow_execution",
                "id": str(execution.id),
                "name": flow.name,
                "user_id": str(test_user.id),
            },
        },
    )
    return ModelGatewayAuthContext(token=token, user=test_user, api_key=api_key)


def _agent_context(db_session, test_user, agent) -> ModelGatewayAuthContext:
    api_key, token = crud_api_key.create_runtime_key(
        db_session,
        name=f"Managed Agent Credential {uuid4()}",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={
            "managed_agent_id": str(agent.id),
            "runtime_principal": {
                "type": agent.session_source_type,
                "id": agent.session_source_id,
            },
        },
    )
    return ModelGatewayAuthContext(token=token, user=test_user, api_key=api_key)


@pytest.fixture
def team(db_session, test_user) -> dict[str, Any]:
    """Owner (test_user) onboarded Codex; an admin and an employee also exist."""
    employee = _second_user(db_session, test_user, "employee")
    agent = _agent(db_session, test_user.account_id, test_user.id, "owner-laptop")
    model = _subscription_model(db_session, test_user.account_id, agent)
    return {"owner": test_user, "employee": employee, "agent": agent, "model": model}


def _opt_in(db_session, team) -> None:
    crud_ai_model.set_owner_flow_use(
        db_session, db_obj=team["model"], user_id=team["owner"].id
    )


# --- ownership and opt-in rule -------------------------------------------


def test_owner_is_the_bound_agents_owner(db_session, team) -> None:
    assert subscription_owner_user_id(db_session, team["model"]) == str(
        team["owner"].id
    )
    assert not owner_flow_use_enabled(db_session, team["model"])
    _opt_in(db_session, team)
    assert owner_flow_use_enabled(db_session, team["model"])


def test_two_owners_on_one_lineage_means_no_owner(db_session, team) -> None:
    other = _agent(
        db_session, team["owner"].account_id, team["employee"].id, "employee-laptop"
    )
    crud_managed_agent_ai_model_binding.replace_for_agent(
        db_session,
        account_id=str(team["owner"].account_id),
        agent_id=str(other.id),
        bindings=[
            {
                "config_key": "primary",
                "gateway_alias": ALIAS,
                "ai_model_id": team["model"].id,
            }
        ],
    )
    _opt_in(db_session, team)
    assert subscription_owner_user_id(db_session, team["model"]) is None
    assert not owner_flow_use_enabled(db_session, team["model"])


def test_opt_in_lapses_when_the_agent_changes_owner(db_session, team) -> None:
    _opt_in(db_session, team)
    flow = _flow(db_session, team["owner"].account_id, team["owner"].id, team["model"])
    assert flow_may_use_subscription(db_session, flow, team["model"])
    team["agent"].owner_user_id = team["employee"].id
    db_session.flush()
    assert not flow_may_use_subscription(db_session, flow, team["model"])


# --- admission -------------------------------------------------------------


def test_admission_accepts_owner_flow_with_opt_in(db_session, team) -> None:
    _opt_in(db_session, team)
    flow = _flow(db_session, team["owner"].account_id, team["owner"].id, team["model"])
    details = prepare_execution_routing(db_session, flow, {})
    assert details["_model_routing"]["ai_model_id"] == str(team["model"].id)
    # Orchestrator startup re-checks the same predicate.
    validate_execution_model_principal(db_session, flow, details)


def test_admission_refuses_owner_flow_without_opt_in(db_session, team) -> None:
    flow = _flow(db_session, team["owner"].account_id, team["owner"].id, team["model"])
    with pytest.raises(FlowModelPrincipalError, match="Use from my flows"):
        prepare_execution_routing(db_session, flow, {})


def test_admission_refuses_another_users_flow(db_session, team) -> None:
    """Team account: someone else's flow never spends the owner's subscription."""
    _opt_in(db_session, team)
    flow = _flow(
        db_session, team["owner"].account_id, team["employee"].id, team["model"]
    )
    with pytest.raises(FlowModelPrincipalError):
        prepare_execution_routing(db_session, flow, {})


def test_admission_refuses_flow_without_recorded_owner(db_session, team) -> None:
    _opt_in(db_session, team)
    flow = _flow(db_session, team["owner"].account_id, None, team["model"])
    with pytest.raises(FlowModelPrincipalError):
        prepare_execution_routing(db_session, flow, {})


# --- gateway authorization -------------------------------------------------


def test_gateway_serves_owner_flow_with_opt_in(db_session, team) -> None:
    _opt_in(db_session, team)
    flow = _flow(db_session, team["owner"].account_id, team["owner"].id, team["model"])
    execution = _execution(db_session, flow)
    service = OpenAIGatewayService(
        db_session, _flow_context(db_session, team["owner"], flow, execution)
    )
    listed = [item["id"] for item in service.list_models()["data"]]
    assert ALIAS in listed
    resolved = service._resolve_requested_model(ALIAS, provider="openai")
    assert resolved.id == team["model"].id
    # No binding was created or changed for the flow principal.
    bindings = crud_managed_agent_ai_model_binding.list_for_agent(
        db_session,
        account_id=str(team["owner"].account_id),
        agent_id=str(team["agent"].id),
    )
    assert [str(b.ai_model_id) for b in bindings] == [str(team["model"].id)]


def test_gateway_refuses_owner_flow_without_opt_in(db_session, team) -> None:
    flow = _flow(db_session, team["owner"].account_id, team["owner"].id, team["model"])
    execution = _execution(db_session, flow)
    service = OpenAIGatewayService(
        db_session, _flow_context(db_session, team["owner"], flow, execution)
    )
    assert ALIAS not in [item["id"] for item in service.list_models()["data"]]
    with pytest.raises(ModelGatewayAPIError) as excinfo:
        service._resolve_requested_model(ALIAS, provider="openai")
    assert excinfo.value.code == "model_not_authorized"
    assert "Use from my flows" in excinfo.value.message


def test_gateway_refuses_other_users_flow_in_team_account(db_session, team) -> None:
    _opt_in(db_session, team)
    flow = _flow(
        db_session, team["owner"].account_id, team["employee"].id, team["model"]
    )
    execution = _execution(db_session, flow)
    service = OpenAIGatewayService(
        db_session, _flow_context(db_session, team["owner"], flow, execution)
    )
    with pytest.raises(ModelGatewayAPIError) as excinfo:
        service._resolve_requested_model(ALIAS, provider="openai")
    assert excinfo.value.code == "model_not_authorized"


def test_gateway_refuses_execution_of_another_flow(db_session, team) -> None:
    """A key naming the owner's flow but another flow's execution is refused."""
    _opt_in(db_session, team)
    owner_flow = _flow(
        db_session, team["owner"].account_id, team["owner"].id, team["model"]
    )
    other_flow = _flow(
        db_session, team["owner"].account_id, team["employee"].id, team["model"]
    )
    execution = _execution(db_session, other_flow)
    service = OpenAIGatewayService(
        db_session, _flow_context(db_session, team["owner"], owner_flow, execution)
    )
    with pytest.raises(ModelGatewayAPIError):
        service._resolve_requested_model(ALIAS, provider="openai")


def test_opt_in_does_not_grant_another_agent(db_session, team) -> None:
    _opt_in(db_session, team)
    other = _agent(
        db_session, team["owner"].account_id, team["owner"].id, "owner-other-host"
    )
    service = OpenAIGatewayService(
        db_session, _agent_context(db_session, team["owner"], other)
    )
    with pytest.raises(ModelGatewayAPIError) as excinfo:
        service._resolve_requested_model(ALIAS, provider="openai")
    assert "bound to another agent" in excinfo.value.message


def test_onboarded_agent_keeps_its_subscription(db_session, team) -> None:
    _opt_in(db_session, team)
    service = OpenAIGatewayService(
        db_session, _agent_context(db_session, team["owner"], team["agent"])
    )
    assert (
        service._resolve_requested_model(ALIAS, provider="openai").id
        == team["model"].id
    )


# --- usage attribution -----------------------------------------------------


def _record(service: OpenAIGatewayService, model: models.AIModel) -> None:
    service._last_upstream_credential_type = "oauth"
    service._record_gateway_request(
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.2,
        ai_model=model,
        requested_model=ALIAS,
        response_payload={
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
            }
        },
        upstream_response={
            "id": "resp_1",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
            },
        },
        endpoint_kind="responses",
        request_payload={"model": ALIAS},
    )


def _latest_usage(db_session, account_id) -> models.ApiUsage:
    return (
        db_session.query(models.ApiUsage)
        .filter(models.ApiUsage.account_id == account_id)
        .order_by(models.ApiUsage.timestamp.desc())
        .first()
    )


def test_owner_flow_usage_is_attributed_and_tagged(db_session, team) -> None:
    from preloop.models.crud import crud_api_usage

    _opt_in(db_session, team)
    flow = _flow(db_session, team["owner"].account_id, team["owner"].id, team["model"])
    execution = _execution(db_session, flow)
    service = OpenAIGatewayService(
        db_session, _flow_context(db_session, team["owner"], flow, execution)
    )
    _record(service, team["model"])

    usage = _latest_usage(db_session, team["owner"].account_id)
    assert str(usage.flow_execution_id) == str(execution.id)
    assert usage.cost_source == "subscription"
    assert usage.estimated_cost == 0.0
    assert usage.meta_data["subscription_use"] == OWNER_FLOW_SUBSCRIPTION_USE
    assert "api_equivalent_cost" in usage.meta_data

    end = datetime.now(UTC)
    summary = crud_api_usage.owner_flow_subscription_usage(
        db_session,
        account_id=team["owner"].account_id,
        ai_model_id=team["model"].id,
        start=end.replace(year=end.year - 1),
        end=end.replace(year=end.year + 1),
    )
    assert summary["request_count"] == 1
    assert summary["execution_count"] == 1


def test_agent_usage_is_not_tagged_as_flow_use(db_session, team) -> None:
    service = OpenAIGatewayService(
        db_session, _agent_context(db_session, team["owner"], team["agent"])
    )
    _record(service, team["model"])
    usage = _latest_usage(db_session, team["owner"].account_id)
    assert usage.meta_data.get("subscription_use") is None


# --- console API -----------------------------------------------------------


def _act_as(app, db_session, user) -> None:
    from preloop.api.auth.jwt import get_current_active_user

    user_id = user.id
    app.dependency_overrides[get_current_active_user] = lambda: crud_user.get(
        db_session, id=user_id
    )


def test_owner_toggles_and_sees_state(db_session, team, client) -> None:
    url = f"/api/v1/ai-models/{team['model'].id}/owner-flow-use"
    state = client.get(url).json()
    assert state["applicable"] and state["can_manage"] and not state["enabled"]

    state = client.put(url, json={"enabled": True}).json()
    assert state["enabled"] is True
    assert state["usage"]["request_count"] == 0
    db_session.refresh(team["model"])
    assert str(team["model"].owner_flow_use_user_id) == str(team["owner"].id)

    assert client.put(url, json={"enabled": False}).json()["enabled"] is False


def test_admin_cannot_set_it_for_the_owner(db_session, team, app, client) -> None:
    from preloop.models.crud import crud_role, crud_user_role

    admin = team["employee"]
    role = crud_role.get_by_name(db_session, name="owner")
    crud_user_role.create(db_session, obj_in={"user_id": admin.id, "role_id": role.id})
    db_session.flush()
    _act_as(app, db_session, admin)
    url = f"/api/v1/ai-models/{team['model'].id}/owner-flow-use"

    state = client.get(url).json()
    assert state["can_manage"] is False and state["usage"] is None
    response = client.put(url, json={"enabled": True})
    assert response.status_code == 403
    db_session.refresh(team["model"])
    assert team["model"].owner_flow_use_user_id is None


def test_api_key_model_has_no_setting(db_session, test_user, client) -> None:
    model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "BYOK",
            "provider_name": "openai",
            "model_identifier": "gpt-example",
            "api_key": "sk-placeholder",
        },
        account_id=test_user.account_id,
    )
    url = f"/api/v1/ai-models/{model.id}/owner-flow-use"
    assert client.get(url).json() == {
        "applicable": False,
        "enabled": False,
        "can_manage": False,
        "usage": None,
    }
    assert client.put(url, json={"enabled": True}).status_code == 400


def test_crud_create_records_flow_owner(db_session, test_user) -> None:
    from preloop.models import schemas
    from preloop.models.crud import crud_flow

    flow = crud_flow.create(
        db_session,
        flow_in=schemas.FlowCreate(
            name=f"Owned {uuid4()}",
            prompt_template="Example",
            agent_type="codex",
            agent_config={},
        ),
        account_id=test_user.account_id,
        created_by_user_id=test_user.id,
    )
    assert flow.created_by_user_id == test_user.id


def test_preset_clone_records_flow_owner(db_session, test_user) -> None:
    from unittest.mock import MagicMock

    from preloop.services.flow_presets_service import clone_preset_for_account

    preset = models.Flow(
        id=uuid4(),
        name="Preset",
        prompt_template="Example",
        agent_type="codex",
        agent_config={},
        is_preset=True,
        account_id=None,
        created_by_user_id=uuid4(),
    )
    crud = MagicMock()
    crud.get_by_name_and_account.return_value = None
    clone_preset_for_account(
        db_session,
        preset,
        test_user.account_id,
        bound_ai_model_id=uuid4(),
        flow_crud=crud,
        created_by_user_id=test_user.id,
    )
    kwargs = crud.create.call_args.kwargs
    assert kwargs["created_by_user_id"] == test_user.id
