"""A CI identity learns compatibility only for the flow it can trigger."""

from typing import Any
from uuid import uuid4

import pytest

from preloop.models import crud
from tests.api.test_ci_principal import provision
from tests.api.test_ci_rest_boundary import (
    ci_http as setup_ci_http,
    ci_resources as make_ci_resources,
)
from tests.services.test_subscription_owner_flow_use import _agent, _subscription_model


@pytest.fixture
def ci_resources(db_session: Any) -> tuple[Any, ...]:
    """Create an isolated CI grant and bound flow."""
    return make_ci_resources.__wrapped__(db_session)


@pytest.fixture
def ci_http(monkeypatch: pytest.MonkeyPatch, db_session: Any) -> None:
    """Run CI middleware against the isolated test database."""
    setup_ci_http.__wrapped__(monkeypatch, db_session)


def test_ci_preflight_is_narrow_and_rechecks_authority(
    db_session: Any, client: Any, ci_resources: tuple[Any, ...], ci_http: None
) -> None:
    """No catalog, owner, configuration or credentials cross the CI boundary."""
    owner, _, flow, _ = ci_resources
    agent = _agent(db_session, owner.account_id, owner.id, "review-agent")
    model = _subscription_model(db_session, owner.account_id, agent)
    flow.ai_model_id = model.id
    flow.created_by_user_id = owner.id
    db_session.flush()
    _, key, token = provision(db_session, ci_resources)
    headers = {"Authorization": f"Bearer {token}"}
    path = f"/api/v1/flows/{flow.id}"
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    assert "etag" in response.headers
    body = response.json()
    assert set(body) == {"id", "executor_eligibility"}
    assert body["executor_eligibility"]["reason"] == "subscription_flow_opt_in_required"
    for secret in (owner.id, agent.id, model.id, model.credentials_secret_id):
        assert str(secret) not in response.text
    crud.crud_ai_model.set_owner_flow_use(db_session, db_obj=model, user_id=owner.id)
    assert client.get(path, headers=headers).json()["executor_eligibility"]["eligible"]
    assert client.get(f"/api/v1/flows/{uuid4()}", headers=headers).status_code == 403
    assert client.get("/api/v1/ai-models", headers=headers).status_code == 403
    assert (
        client.get(f"/api/v1/ai-models/{model.id}", headers=headers).status_code == 403
    )
    key.is_active = False
    db_session.flush()
    assert client.get(path, headers=headers).status_code == 401


def test_ci_preflight_requires_trigger_authority(
    db_session: Any, client: Any, ci_resources: tuple[Any, ...], ci_http: None
) -> None:
    """Execution-read authority alone cannot inspect a future run."""
    from preloop.schemas.ci_principal import CiAction

    owner, project, flow, grant = ci_resources
    grant = grant.model_copy(update={"actions": (CiAction.READ_EXECUTION,)})
    _, _, token = provision(db_session, (owner, project, flow, grant))
    response = client.get(
        f"/api/v1/flows/{flow.id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403
