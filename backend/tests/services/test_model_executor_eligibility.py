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
