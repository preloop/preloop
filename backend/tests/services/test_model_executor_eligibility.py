"""The read, trigger and gateway must agree about execution credentials."""

from typing import Any

import pytest

from preloop.models import models
from preloop.services.model_executor_eligibility import model_executor_eligibility
from preloop.services.model_gateway_auth import compute_authorized_model_ids
from preloop.services.model_routing import (
    FlowModelPrincipalError,
    validate_execution_model_principal,
)
from tests.services.test_subscription_owner_flow_use import (
    _agent_context,
    _execution,
    _flow,
    _flow_context,
    _opt_in,
    team as subscription_team,
)


@pytest.fixture
def team(db_session: Any, test_user: models.User) -> dict[str, Any]:
    """Reuse synthetic subscription ownership and binding fixtures."""
    return subscription_team.__wrapped__(db_session, test_user)


@pytest.mark.parametrize("opt_in", [False, True])
@pytest.mark.parametrize("caller", ["owner", "employee"])
def test_subscription_read_trigger_gateway_agree(
    db_session: Any, team: dict[str, Any], opt_in: bool, caller: str
) -> None:
    """Opt-in only admits the owner's flow, regardless of who triggers it."""
    if opt_in:
        _opt_in(db_session, team)
    user, model = team[caller], team["model"]
    flow = _flow(db_session, user.account_id, user.id, model)
    read = model_executor_eligibility(db_session, model, user)
    expected = opt_in and caller == "owner"
    assert read["hosted"].eligible is expected
    if not opt_in and caller == "owner":
        assert read["hosted"].reason == "subscription_flow_opt_in_required"
    context = _flow_context(db_session, user, flow, _execution(db_session, flow))
    assert (
        str(model.id) in compute_authorized_model_ids(db_session, context, [model])
    ) is expected
    if expected:
        validate_execution_model_principal(db_session, flow, {})
    else:
        with pytest.raises(FlowModelPrincipalError):
            validate_execution_model_principal(db_session, flow, {})
    serialized = str({key: value.model_dump() for key, value in read.items()})
    assert str(team["owner"].id) not in serialized
    assert str(team["agent"].id) not in serialized
    assert "placeholder" not in serialized


def test_private_host_uses_owners_bound_login(
    db_session: Any, team: dict[str, Any]
) -> None:
    """Host profiles retain their agent identity without hosted opt-in."""
    user, model = team["owner"], team["model"]
    read = model_executor_eligibility(db_session, model, user)
    assert read["private_runner_host"].eligible
    assert read["interactive_agent"].eligible
    flow = _flow(db_session, user.account_id, user.id, model)
    flow.runner_pool = "private"
    flow.agent_config = {"host_exec_profile": "codex-review"}
    validate_execution_model_principal(db_session, flow, {})
    context = _agent_context(db_session, user, team["agent"])
    assert str(model.id) in compute_authorized_model_ids(db_session, context, [model])


@pytest.mark.parametrize("kind", ["api_key", "built_in", "own_server"])
def test_unbound_registration_kinds(
    db_session: Any, test_user: models.User, kind: str
) -> None:
    """Ordinary registrations do not inherit subscription restrictions."""
    from tests.services.test_model_routing import _usable_model

    model = _usable_model(db_session, test_user.account_id)
    if kind == "built_in":
        model.account_id = None
    elif kind == "own_server":
        model.provider_name = "openai-compatible"
        model.api_endpoint = "https://models.example.com/v1"
    read = model_executor_eligibility(db_session, model, test_user)
    assert all(item.eligible for item in read.values())
    flow = _flow(db_session, test_user.account_id, test_user.id, model)
    validate_execution_model_principal(db_session, flow, {})
    context = _flow_context(db_session, test_user, flow, _execution(db_session, flow))
    assert str(model.id) in compute_authorized_model_ids(db_session, context, [model])


@pytest.mark.parametrize("caller", ["owner", "employee"])
def test_model_rest_reads_and_flow_warning(
    db_session: Any, team: dict[str, Any], client: Any, app: Any, caller: str
) -> None:
    """Both catalog surfaces reflect the caller; flow reads use the creator."""
    from preloop.api.auth import get_current_active_user

    user, model = team[caller], team["model"]
    app.dependency_overrides[get_current_active_user] = lambda: user
    detail = client.get(f"/api/v1/ai-models/{model.id}")
    assert detail.status_code == 200, detail.text
    listing = client.get("/api/v1/ai-models").json()
    listed = next(row for row in listing if row["id"] == str(model.id))
    eligibility = detail.json()["executor_eligibility"]
    assert eligibility == listed["executor_eligibility"]
    assert not eligibility["hosted"]["eligible"]
    assert str(team["owner"].id) not in str(eligibility)
    assert str(model.credentials_secret_id) not in str(eligibility)
    flow = _flow(db_session, user.account_id, team["owner"].id, model)
    before = client.get(f"/api/v1/flows/{flow.id}")
    assert before.status_code == 200, before.text
    assert (
        before.json()["executor_eligibility"]["reason"]
        == "subscription_flow_opt_in_required"
    )
    assert [w["code"] for w in before.json()["config_warnings"]] == [
        "subscription_flow_opt_in_required"
    ]
    _opt_in(db_session, team)
    after = client.get(f"/api/v1/flows/{flow.id}").json()
    assert after["executor_eligibility"]["eligible"]
    assert not after["config_warnings"]
    detail = client.get(f"/api/v1/ai-models/{model.id}").json()
    assert detail["executor_eligibility"]["hosted"]["eligible"] is (caller == "owner")


@pytest.mark.parametrize("owner_state", ["inactive", "other_account"])
def test_owner_remediation_when_opt_in_already_enabled(
    db_session: Any, team: dict[str, Any], owner_state: str
) -> None:
    """An unavailable owner cannot be repaired by enabling an existing opt-in."""
    from preloop.models.crud import crud_account
    from preloop.services.model_executor_eligibility import hosted_model_eligibility
    from preloop.services.subscription_flow_use import flow_may_use_subscription

    _opt_in(db_session, team)
    owner, model = team["owner"], team["model"]
    flow = _flow(db_session, owner.account_id, owner.id, model)
    if owner_state == "inactive":
        owner.is_active = False
    else:
        account = crud_account.create(
            db_session, obj_in={"organization_name": "Another organization"}
        )
        owner.account_id = account.id
    db_session.flush()
    decision = hosted_model_eligibility(db_session, model, flow)
    assert not decision.eligible
    assert decision.reason == "subscription_flow_owner_inactive"
    assert "active member" in decision.message
    assert not flow_may_use_subscription(db_session, flow, model)


def test_catalog_eligibility_query_count_is_bounded(
    db_session: Any, team: dict[str, Any]
) -> None:
    """Additional models, credential lineages and agent slots add no queries."""
    from sqlalchemy import event

    from preloop.models.crud import crud_ai_model, crud_managed_agent_ai_model_binding
    from preloop.services.model_executor_eligibility import model_executor_eligibilities
    from tests.services.test_subscription_owner_flow_use import _agent

    owner = team["owner"]
    catalog = [team["model"]]
    for index in range(6):
        agent = _agent(db_session, owner.account_id, owner.id, f"catalog-{index}")
        model = crud_ai_model.create_with_account(
            db_session,
            account_id=owner.account_id,
            obj_in={
                "name": f"Subscription {index}",
                "provider_name": "openai-codex",
                "model_identifier": f"model-{index}",
                "credential_type": "oauth_openai_codex",
                "credential_payload": {
                    "access": "placeholder",
                    "refresh": "placeholder",
                },
            },
        )
        catalog.append(model)
        crud_managed_agent_ai_model_binding.replace_for_agent(
            db_session,
            account_id=str(owner.account_id),
            agent_id=str(agent.id),
            bindings=[
                {
                    "config_key": slot,
                    "gateway_alias": f"alias-{index}-{slot}",
                    "ai_model_id": str(model.id),
                }
                for slot in ("primary", "second-slot")
            ],
        )
    # Match the endpoint's eager-loaded catalog before counting admission reads.
    catalog = crud_ai_model.get_selectable_for_account(
        db_session, account_id=owner.account_id
    )
    _ = owner.id, owner.account_id, owner.is_active
    queries: list[str] = []

    def count_query(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        queries.append(statement)

    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", count_query)
    try:
        model_executor_eligibilities(db_session, catalog[:1], owner)
        single_count = len(queries)
        queries.clear()
        decisions = model_executor_eligibilities(db_session, catalog, owner)
        assert len(queries) == single_count <= 2
        assert all(row["interactive_agent"].eligible for row in decisions.values())
        assert all(
            row["hosted"].reason == "subscription_flow_opt_in_required"
            for row in decisions.values()
        )
    finally:
        event.remove(connection, "before_cursor_execute", count_query)


@pytest.mark.parametrize(
    "other_binding", ["none", "other_owner", "ownerless", "inactive", "retired"]
)
def test_batch_ownership_matches_gateway_across_credential_siblings(
    db_session: Any, team: dict[str, Any], other_binding: str
) -> None:
    """Hidden sibling owners affect hosted admission, but not exact agent bindings."""
    from preloop.models.crud import crud_ai_model, crud_managed_agent_ai_model_binding
    from preloop.services.model_executor_eligibility import (
        hosted_model_eligibility,
        model_executor_eligibilities,
    )
    from tests.services.test_subscription_owner_flow_use import _agent

    owner, model = team["owner"], team["model"]
    _opt_in(db_session, team)
    sibling = crud_ai_model.create_with_account(
        db_session,
        account_id=owner.account_id,
        obj_in={
            "name": "Sibling subscription",
            "provider_name": "openai-codex",
            "model_identifier": "sibling-model",
            "credentials_secret_id": model.credentials_secret_id,
            "meta_data": {"gateway": {"enabled": True, "model_alias": "sibling-model"}},
        },
    )
    crud_ai_model.set_owner_flow_use(db_session, db_obj=sibling, user_id=owner.id)
    if other_binding != "none":
        other = _agent(db_session, owner.account_id, team["employee"].id, "other-agent")
        if other_binding == "ownerless":
            other.owner_user_id = None
        if other_binding == "retired":
            other.lifecycle_state = "retired"
        crud_managed_agent_ai_model_binding.replace_for_agent(
            db_session,
            account_id=str(owner.account_id),
            agent_id=str(other.id),
            bindings=[
                {
                    "config_key": "primary",
                    "gateway_alias": "sibling-model",
                    "ai_model_id": sibling.id,
                    "status": "inactive"
                    if other_binding == "inactive"
                    else "gateway_ready",
                }
            ],
        )
    flow = _flow(db_session, owner.account_id, owner.id, model)
    # Only the original model is visible; its credential's other owners still count.
    read = model_executor_eligibilities(db_session, [model], owner)[str(model.id)]
    expected = other_binding in {"none", "inactive", "retired"}
    assert read["hosted"].eligible is expected
    assert read["hosted"] == hosted_model_eligibility(db_session, model, flow)
    context = _flow_context(db_session, owner, flow, _execution(db_session, flow))
    assert (
        str(model.id) in compute_authorized_model_ids(db_session, context, [model])
    ) is expected
    assert read["interactive_agent"].eligible
    assert not model_executor_eligibility(db_session, sibling, owner)[
        "interactive_agent"
    ].eligible
