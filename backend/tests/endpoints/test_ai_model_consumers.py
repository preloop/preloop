"""What uses a model, and refusing to delete one that flows still select.

Shape under test: review flows with CI identities sharing a few models, some
with a rejected key. ``used-by`` lists every flow and CI identity that depends
on a model; a delete is refused while flows select it unless forced; rotating
a shared key reports every model and flow it reaches; each flow states
whether its model can serve a run.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints import ai_models
from preloop.api.endpoints.flows import read_flow_summaries
from preloop.api.middleware import ci_auth
from preloop.models import crud, models
from preloop.models.crud.base import CRUDBase
from preloop.models.db.session import get_db_session
from preloop.models.schemas.flow import FlowResponse
from tests.api import test_ci_principal as ci_fixtures
from tests.conftest import maybe_await

ci_resources = ci_fixtures.ci_resources
reset_ci_authority = ci_fixtures.reset_ci_authority
provision = ci_fixtures.provision


def _model(
    db: Session, account_id: Any, name: str, secret_id: Any = None
) -> models.AIModel:
    model = crud.crud_ai_model.create(
        db,
        obj_in=dict(
            account_id=account_id,
            name=name,
            provider_name="openrouter",
            model_identifier=f"example/{name.lower().replace(' ', '-')}",
        ),
    )
    if secret_id is not None:
        model.credentials_secret_id = secret_id
        db.flush()
    return model


def _secret(db: Session, account_id: Any) -> models.SecretReference:
    secret = models.SecretReference(
        account_id=account_id,
        name=f"key-{uuid4()}",
        secret_kind="ai_model_credentials",
        backend_type="encrypted_db",
        status="active",
    )
    db.add(secret)
    db.flush()
    return secret


def _flow(
    db: Session,
    account_id: Any,
    name: str,
    model: models.AIModel | None = None,
    agent_config: dict[str, Any] | None = None,
    enabled: bool = True,
) -> models.Flow:
    return CRUDBase(models.Flow).create(
        db,
        obj_in={
            "name": name,
            "prompt_template": "Review",
            "agent_type": "openhands",
            "agent_config": agent_config or {},
            "account_id": account_id,
            "is_enabled": enabled,
            "ai_model_id": model.id if model else None,
        },
    )


def _bind(db: Session, flow: models.Flow, model: models.AIModel) -> None:
    flow.ai_model_id = model.id
    db.flush()


def _used_by(db: Session, model: models.AIModel, user: models.User) -> Any:
    return maybe_await(
        ai_models.get_ai_model_used_by(model_id=model.id, db=db, current_user=user)
    )


def _delete(
    db: Session, model: models.AIModel, user: models.User, force: bool = False
) -> Any:
    return maybe_await(
        ai_models.delete_ai_model(
            model_id=model.id, force=force, db=db, current_user=user
        )
    )


def _audits(db: Session) -> list[models.AuditLog]:
    return (
        db.query(models.AuditLog)
        .filter(models.AuditLog.action == "config_changed")
        .order_by(models.AuditLog.timestamp.asc())
        .all()
    )


@pytest.mark.asyncio
async def test_used_by_lists_flows_runs_ci_identities_and_shared_models(
    db_session: Session, ci_resources: Any
) -> None:
    owner, _, review_flow, _ = ci_resources
    account_id = owner.account_id
    secret = _secret(db_session, account_id)
    model = _model(db_session, account_id, "Shared A", secret.id)
    sibling = _model(db_session, account_id, "Shared B", secret.id)
    _bind(db_session, review_flow, model)
    routed = _flow(
        db_session,
        account_id,
        "Routed flow",
        agent_config={"model_by_label": [{"ai_model_id": str(model.id)}]},
        enabled=False,
    )
    _flow(db_session, account_id, "Unrelated flow", sibling)
    principal, key, _ = provision(db_session, ci_resources)
    expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=9)
    key.expires_at = expiry
    started = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.add(
        models.FlowExecution(
            flow_id=review_flow.id,
            status="FAILED",
            start_time=started,
        )
    )
    db_session.flush()

    response = await _used_by(db_session, model, owner)

    by_name = {flow.name: flow for flow in response.flows}
    assert set(by_name) == {"Example review", "Routed flow"}
    assert by_name["Example review"].is_enabled is True
    assert by_name["Example review"].last_execution_status == "FAILED"
    assert by_name["Example review"].last_execution_at is not None
    assert by_name["Routed flow"].is_enabled is False
    assert by_name["Routed flow"].id == str(routed.id)
    assert by_name["Routed flow"].last_execution_at is None
    assert [ci.model_dump() for ci in response.ci_identities] == [
        {
            "id": str(principal.id),
            "name": "Synthetic CI",
            "flow_id": str(review_flow.id),
            "is_active": True,
            "retired": False,
            "active_key_count": 1,
            "key_expires_at": expiry,
        }
    ]
    assert [m.id for m in response.shared_credential_models] == [str(sibling.id)]
    serialized = response.model_dump_json()
    assert "key_hash" not in serialized and "ci_" not in serialized.replace(
        "ci_identities", ""
    )


@pytest.mark.asyncio
async def test_used_by_never_lists_another_accounts_flows(
    db_session: Session, ci_resources: Any, test_user: models.User
) -> None:
    owner, _, _, _ = ci_resources
    model = _model(db_session, owner.account_id, "Owned")
    _flow(db_session, owner.account_id, "Own flow", model)
    # A flow elsewhere that names this model id in its routing config.
    _flow(
        db_session,
        test_user.account_id,
        "Foreign flow",
        agent_config={"model_by_label": [{"ai_model_id": str(model.id)}]},
    )

    response = await _used_by(db_session, model, owner)

    assert [flow.name for flow in response.flows] == ["Own flow"]
    with pytest.raises(HTTPException) as exc:
        await _used_by(db_session, model, test_user)
    assert exc.value.status_code == 404


def test_restricted_ci_identity_cannot_read_used_by(
    db_session: Session, ci_resources: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, _, flow, _ = ci_resources
    model = _model(db_session, owner.account_id, "CI target")
    _bind(db_session, flow, model)
    _, _, token = provision(db_session, ci_resources)

    def sessions() -> Any:
        yield db_session

    app = FastAPI()
    app.include_router(ai_models.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = sessions
    # The human seam resolves to the owner, so only the middleware can refuse.
    app.dependency_overrides[get_current_active_user] = lambda: owner
    monkeypatch.setattr(ci_auth, "get_auth_db_session", sessions)
    app.add_middleware(ci_auth.RestrictedCiAuthMiddleware)
    client = TestClient(app)
    assert client.get(f"/api/v1/ai-models/{model.id}/used-by").status_code == 200

    response = client.get(
        f"/api/v1/ai-models/{model.id}/used-by",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code in {401, 403}, response.text
    assert "Example review" not in response.text


@pytest.mark.asyncio
async def test_delete_in_use_model_is_refused_with_the_flows(
    db_session: Session, ci_resources: Any
) -> None:
    owner, _, flow, _ = ci_resources
    model = _model(db_session, owner.account_id, "In use")
    _bind(db_session, flow, model)
    _flow(
        db_session,
        owner.account_id,
        "Retry flow",
        agent_config={
            "retry_on_no_progress": {"enabled": True, "ai_model_id": str(model.id)}
        },
    )

    with pytest.raises(HTTPException) as exc:
        await _delete(db_session, model, owner)

    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert detail["code"] == "model_in_use"
    assert detail["flow_count"] == 2
    assert {f["name"] for f in detail["flows"]} == {"Example review", "Retry flow"}
    assert crud.crud_ai_model.get(db_session, id=model.id) is not None
    db_session.refresh(flow)
    assert flow.ai_model_id == model.id


@pytest.mark.asyncio
async def test_forced_delete_records_orphaned_flows_and_marks_them(
    db_session: Session, ci_resources: Any
) -> None:
    owner, _, flow, _ = ci_resources
    model = _model(db_session, owner.account_id, "Retired")
    _bind(db_session, flow, model)

    with patch("preloop.utils.audit._get_audit_service") as service:
        response = await _delete(db_session, model, owner, force=True)

    assert response.status_code == 204
    assert crud.crud_ai_model.get(db_session, id=model.id) is None
    deleted = [
        call.kwargs
        for call in service.return_value.log_configuration_change.call_args_list
        if call.kwargs.get("action") == "deleted"
    ]
    assert len(deleted) == 1
    assert deleted[0]["old_value"]["orphaned_flows"] == [
        {"id": str(flow.id), "name": "Example review"}
    ]
    db_session.expire_all()
    row = crud.crud_flow.get(db_session, id=flow.id)
    assert row.ai_model_id is None
    assert row.agent_config["orphaned_model"] == {
        "id": str(model.id),
        "name": "Retired",
    }
    health = FlowResponse.model_validate(row).model_health
    assert health is not None
    assert (health.status, health.code, health.model_id) == (
        "missing",
        "model_deleted",
        str(model.id),
    )


@pytest.mark.asyncio
async def test_delete_unused_model_is_unchanged(
    db_session: Session, ci_resources: Any
) -> None:
    owner, _, _, _ = ci_resources
    model = _model(db_session, owner.account_id, "Unused")

    response = await _delete(db_session, model, owner)

    assert response.status_code == 204
    assert crud.crud_ai_model.get(db_session, id=model.id) is None


@pytest.mark.asyncio
async def test_rotating_a_shared_key_returns_both_models_and_their_flows(
    db_session: Session, ci_resources: Any
) -> None:
    owner, _, flow, _ = ci_resources
    secret = _secret(db_session, owner.account_id)
    first = _model(db_session, owner.account_id, "Rotate A", secret.id)
    second = _model(db_session, owner.account_id, "Rotate B", secret.id)
    _bind(db_session, flow, first)
    other = _flow(db_session, owner.account_id, "Second flow", second)
    secret.version = 2
    secret.rotated_at = datetime.now(timezone.utc)

    with patch.object(ai_models, "get_secret_service") as service:
        service.return_value.rotate_secret_reference.return_value = secret
        response = await maybe_await(
            ai_models.rotate_ai_model_credential(
                model_id=first.id,
                body=ai_models.AIModelCredentialRotateRequest(api_key="sk-new"),
                db=db_session,
                current_user=owner,
            )
        )

    assert response.shared_model_count == 2
    assert {m.id for m in response.affected_models} == {
        str(first.id),
        str(second.id),
    }
    assert {f.id for f in response.affected_flows} == {str(flow.id), str(other.id)}
    assert "sk-new" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_flow_summaries_mark_a_rejected_model(
    db_session: Session, ci_resources: Any
) -> None:
    owner, _, flow, _ = ci_resources
    secret = _secret(db_session, owner.account_id)
    model = _model(db_session, owner.account_id, "Rejected", secret.id)
    healthy = _model(db_session, owner.account_id, "Healthy")
    _bind(db_session, flow, model)
    fine = _flow(db_session, owner.account_id, "Fine flow", healthy)
    none = _flow(db_session, owner.account_id, "No model flow")
    secret.meta_data = {
        "upstream_auth_rejection": {
            "status_code": 401,
            "last_rejected_at": datetime.now(timezone.utc).isoformat(),
        }
    }
    db_session.flush()
    db_session.expire_all()

    rows = await maybe_await(
        read_flow_summaries(
            db=db_session,
            skip=0,
            limit=100,
            include_stats=False,
            stats_since=None,
            current_user=owner,
        )
    )

    health = {str(row.id): row.model_health for row in rows}
    assert health[str(flow.id)].status == "error"
    assert health[str(flow.id)].code == "upstream_auth_rejected"
    assert health[str(flow.id)].model_id == str(model.id)
    assert health[str(fine.id)].status == "ok"
    assert health[str(none.id)] is None
    assert str(uuid4()) not in health
