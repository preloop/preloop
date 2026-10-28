"""Explicit historical export consent never becomes execution ownership."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.models import crud, models
from preloop.models.crud.base import CRUDBase
from preloop.models.crud import ci_result_export
from preloop.plugins.ci_authorization import register_ci_administrator
from preloop.services import ci_execution
from preloop.services.execution_tool_evidence import digest
from tests.api.test_ci_execution_http import client as make_client
from tests.api.test_ci_principal import ci_resources as make_resources, provision
from tests.services.test_ci_execution_admission import provider_payload


@pytest.fixture
def ci_resources(db_session: Session) -> tuple[Any, ...]:
    return make_resources.__wrapped__(db_session)


@pytest.fixture
def client(
    db_session: Session, ci_resources: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    client = make_client.__wrapped__(db_session, monkeypatch)
    client.app.dependency_overrides[get_current_active_user] = lambda: ci_resources[0]
    return client


@pytest.fixture
def scenario(
    db_session: Session, ci_resources: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    principal, key, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    remote = provider_payload(context)
    remote["base"]["sha"] = "b" * 40
    transport = AsyncMock(return_value=remote)
    monkeypatch.setattr(
        ci_execution,
        "_create_review_client",
        AsyncMock(return_value=SimpleNamespace(_request=transport)),
    )
    event = {
        "source": "github",
        "project_id": str(context.project_id),
        "payload": {
            "repository": remote["head"]["repo"],
            "pull_request": deepcopy(remote),
        },
    }
    execution = CRUDBase(models.FlowExecution).create(
        db_session,
        obj_in={
            "flow_id": context.flow_id,
            "status": "SUCCEEDED",
            "end_time": datetime.now(timezone.utc),
            "result": {"review": "Reviewed source", "secret": "synthetic-private"},
            "trigger_event_details": event,
        },
    )
    payload = {
        "principal_id": str(principal.id),
        "pr_number": 7,
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "result_sha256": digest({"review": "Reviewed source"}),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    return SimpleNamespace(
        principal=principal,
        key=key,
        token=token,
        context=context,
        execution=execution,
        payload=payload,
        remote=remote,
        transport=transport,
        path=f"/api/v1/flows/executions/{execution.id}",
    )


def consent(client: TestClient, scenario: SimpleNamespace) -> str:
    response = client.post(scenario.path + "/result-export", json=scenario.payload)
    assert response.status_code == 201, response.text
    return response.json()["grant_id"]


def export(
    client: TestClient,
    scenario: SimpleNamespace,
    grant_id: str,
    *,
    token: str | None = None,
) -> Any:
    return client.get(
        scenario.path + "/result-export/" + grant_id,
        headers={"Authorization": f"Bearer {token or scenario.token}"},
    )


def test_explicit_export_rotation_regrant_and_revocation_do_not_adopt(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    scenario: SimpleNamespace,
) -> None:
    grant_id = consent(client, scenario)
    response = export(client, scenario, grant_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"] == {"review": "Reviewed source"}
    assert body["receipt"]["result_sha256"] == digest(body["result"])
    assert body["receipt"]["base_sha"] == "b" * 40
    assert body["publication_authority"] == "separate_publisher_required"
    assert "synthetic-private" not in response.text
    again = export(client, scenario, consent(client, scenario))
    assert (
        again.json()["publication_idempotency_key"]
        == body["publication_idempotency_key"]
    )
    _, rotated = crud.crud_ci_principal.rotate(
        db_session,
        actor=ci_resources[0],
        principal_id=scenario.principal.id,
        key_id=scenario.key.id,
    )
    assert export(client, scenario, grant_id).status_code == 401
    assert (
        export(client, scenario, grant_id, token=rotated).json()["receipt"]
        == body["receipt"]
    )
    assert (
        client.get(
            scenario.path + "/result", headers={"Authorization": f"Bearer {rotated}"}
        ).status_code
        == 404
    )
    db_session.refresh(scenario.execution)
    assert scenario.execution.status == "SUCCEEDED"
    assert scenario.execution.ci_principal_id is None
    assert scenario.execution.ci_review_binding is None
    assert scenario.execution.initiating_ci_key_id is None
    assert (
        client.delete(scenario.path + "/result-export/" + grant_id).status_code == 204
    )
    assert export(client, scenario, grant_id, token=rotated).status_code == 403


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "STOPPED"),
        ("status", "FAILED"),
        ("end_time", None),
        ("result", None),
        ("result", {"review": "changed"}),
        ("result_schema_validation", {"valid": False}),
        ("trigger_event_details", None),
        ("trigger_event_details", {"payload": "malformed"}),
    ],
)
def test_invalid_source_denied_before_provider(
    db_session: Session,
    client: TestClient,
    scenario: SimpleNamespace,
    field: str,
    value: Any,
) -> None:
    CRUDBase(models.FlowExecution).update(
        db_session, db_obj=scenario.execution, obj_in={field: value}
    )
    response = client.post(scenario.path + "/result-export", json=scenario.payload)
    assert response.status_code == 403, response.text
    scenario.transport.assert_not_awaited()


@pytest.mark.parametrize(
    "field,value",
    [
        ("base_sha", "c" * 40),
        ("head_sha", "c" * 40),
        ("pr_number", 8),
        ("result_sha256", "0" * 64),
    ],
)
def test_reviewed_inputs_must_match_original_source(
    client: TestClient, scenario: SimpleNamespace, field: str, value: Any
) -> None:
    response = client.post(
        scenario.path + "/result-export", json={**scenario.payload, field: value}
    )
    assert response.status_code == 403, response.text
    scenario.transport.assert_not_awaited()


@pytest.mark.parametrize("mutation", ["head", "base", "closed", "fork", "review_id"])
def test_current_provider_change_denies_export(
    client: TestClient, scenario: SimpleNamespace, mutation: str
) -> None:
    grant_id = consent(client, scenario)
    if mutation in {"head", "base"}:
        scenario.remote[mutation]["sha"] = "c" * 40
    elif mutation == "closed":
        scenario.remote["state"] = "closed"
    elif mutation == "review_id":
        scenario.remote["id"] = 9009
    else:
        scenario.remote["head"]["repo"] = {"id": "other", "full_name": "other/fork"}
    response = export(client, scenario, grant_id)
    assert response.status_code == 403, response.text


def test_consent_requires_original_provider_review_id(
    client: TestClient, scenario: SimpleNamespace
) -> None:
    scenario.remote["id"] = 9009
    assert (
        client.post(scenario.path + "/result-export", json=scenario.payload).status_code
        == 403
    )


def test_unrelated_principal_and_tenant_denied(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    scenario: SimpleNamespace,
) -> None:
    grant_id = consent(client, scenario)
    _, _, other = provision(db_session, ci_resources)
    assert export(client, scenario, grant_id, token=other).status_code == 404
    foreign_resources = make_resources.__wrapped__(db_session)
    _, _, foreign = provision(db_session, foreign_resources)
    assert export(client, scenario, grant_id, token=foreign).status_code == 404
    assert export(client, scenario, str(uuid4())).status_code == 404
    client.app.dependency_overrides[get_current_active_user] = (
        lambda: foreign_resources[0]
    )
    assert (
        client.post(scenario.path + "/result-export", json=scenario.payload).status_code
        == 404
    )


def test_machine_cannot_self_grant_or_revoke(
    client: TestClient, scenario: SimpleNamespace
) -> None:
    grant_id = consent(client, scenario)
    headers = {"Authorization": f"Bearer {scenario.token}"}
    assert (
        client.post(
            scenario.path + "/result-export", json=scenario.payload, headers=headers
        ).status_code
        == 403
    )
    assert (
        client.delete(
            scenario.path + "/result-export/" + grant_id, headers=headers
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "result",
        "private_result",
        "source",
        "flow",
        "owner_inactive",
        "permission",
        "expiry",
    ],
)
def test_current_consent_and_source_are_rechecked(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    scenario: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    grant_id = consent(client, scenario)
    if mutation in {"result", "private_result"}:
        result = dict(scenario.execution.result)
        result["review" if mutation == "result" else "secret"] = "changed"
        CRUDBase(models.FlowExecution).update(
            db_session, db_obj=scenario.execution, obj_in={"result": result}
        )
    elif mutation == "source":
        event = deepcopy(scenario.execution.trigger_event_details)
        event["extra"] = True
        CRUDBase(models.FlowExecution).update(
            db_session,
            db_obj=scenario.execution,
            obj_in={"trigger_event_details": event},
        )
    elif mutation == "flow":
        other_flow = CRUDBase(models.Flow).create(
            db_session,
            obj_in={
                "account_id": ci_resources[0].account_id,
                "name": "Moved source",
                "agent_type": "openhands",
                "prompt_template": "Synthetic source",
                "agent_config": {},
            },
        )
        CRUDBase(models.FlowExecution).update(
            db_session, db_obj=scenario.execution, obj_in={"flow_id": other_flow.id}
        )
    elif mutation == "owner_inactive":
        crud.crud_user.update(
            db_session, db_obj=ci_resources[0], obj_in={"is_active": False}
        )
    elif mutation == "permission":
        register_ci_administrator(lambda *args: False)
    else:
        future = datetime.now(timezone.utc) + timedelta(days=1)
        monkeypatch.setattr(
            ci_result_export, "datetime", SimpleNamespace(now=lambda tz: future)
        )
    try:
        assert export(client, scenario, grant_id).status_code == 403
    finally:
        register_ci_administrator(None)


def test_post_provider_revocation_denies_export(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    scenario: SimpleNamespace,
) -> None:
    grant_id = consent(client, scenario)

    async def revoke_during_read(*args: Any) -> Any:
        ci_result_export.revoke_consent(
            db_session,
            actor=ci_resources[0],
            execution_id=scenario.execution.id,
            grant_id=UUID(grant_id),
        )
        return scenario.remote

    scenario.transport.side_effect = revoke_during_read
    assert export(client, scenario, grant_id).status_code == 403


def test_grant_identity_and_revocation_are_immutable(
    db_session: Session, client: TestClient, scenario: SimpleNamespace
) -> None:
    grant_id = UUID(consent(client, scenario))
    for changes in [
        {"result_sha256": "0" * 64},
        {"binding": {}},
        {"execution_id": uuid4()},
        {"expires_at": datetime.now(timezone.utc) + timedelta(days=1)},
        {"granted_by_user_id": uuid4()},
    ]:
        with db_session.connection().begin_nested() as savepoint:
            with pytest.raises(DBAPIError, match="consent is immutable"):
                db_session.execute(
                    update(models.ExecutionResultGrant)
                    .where(models.ExecutionResultGrant.id == grant_id)
                    .values(**changes)
                )
            savepoint.rollback()
    client.delete(scenario.path + "/result-export/" + str(grant_id))
    with pytest.raises(DBAPIError, match="consent is immutable"):
        db_session.execute(
            update(models.ExecutionResultGrant)
            .where(models.ExecutionResultGrant.id == grant_id)
            .values(revoked_at=None)
        )
    db_session.rollback()


def test_narrowing_removes_export_authority(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    scenario: SimpleNamespace,
) -> None:
    from preloop.schemas.ci_principal import CiAction

    grant_id = consent(client, scenario)
    grant = ci_resources[3].model_copy(update={"actions": (CiAction.READ_RESULT,)})
    crud.crud_ci_principal.change(
        db_session,
        actor=ci_resources[0],
        principal_id=scenario.principal.id,
        grant=grant,
    )
    assert export(client, scenario, grant_id).status_code == 403


def test_current_primary_owner_is_required_even_if_extension_allows(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    scenario: SimpleNamespace,
) -> None:
    owner = ci_resources[0]
    foreign_owner = make_resources.__wrapped__(db_session)[0]
    crud.crud_user.update(
        db_session, db_obj=foreign_owner, obj_in={"account_id": owner.account_id}
    )
    client.app.dependency_overrides[get_current_active_user] = lambda: foreign_owner
    register_ci_administrator(lambda *args: True)
    try:
        assert (
            client.post(
                scenario.path + "/result-export", json=scenario.payload
            ).status_code
            == 403
        )
    finally:
        register_ci_administrator(None)


@pytest.mark.parametrize("hours", [-1, 25])
def test_consent_expiry_is_bounded(
    client: TestClient, scenario: SimpleNamespace, hours: int
) -> None:
    payload = {
        **scenario.payload,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(),
    }
    assert (
        client.post(scenario.path + "/result-export", json=payload).status_code == 403
    )
    scenario.transport.assert_not_awaited()
