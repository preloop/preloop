"""Regression coverage for execution readiness and reused model credentials."""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.models.crud.base import CRUDBase
from preloop.schemas.ci_principal import CiAction
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_runtime_resolver import effective_gateway_alias
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.review_completion_evidence import timings_projection
from tests.api.test_ci_execution_http import client as make_client, owned_run
from tests.api.test_ci_principal import ci_resources as make_resources


@pytest.fixture
def ci_resources(db_session: Session) -> tuple[Any, ...]:
    """Build isolated account-local CI resources."""
    return make_resources.__wrapped__(db_session)


@pytest.fixture
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Use the HTTP boundary with real local CRUD operations."""
    return make_client.__wrapped__(db_session, monkeypatch)


@pytest.mark.parametrize("captured", [False, True])
def test_result_waits_for_controller_finalization(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    captured: bool,
) -> None:
    """Early terminal writes cannot expose a result before finalization settles."""
    _, _, token, _, execution = owned_run(db_session, ci_resources)
    crud.crud_flow_execution.record_completion_evidence(
        db_session,
        execution_id=execution.id,
        facts={
            "capture_state": "captured" if captured else "absent",
            "finalization_state": "pending",
        },
        result={"review": "Done"} if captured else None,
    )
    CRUDBase(models.FlowExecution).update(
        db_session, db_obj=execution, obj_in={"status": "FAILED"}
    )
    headers = {"Authorization": f"Bearer {token}"}
    url = f"/api/v1/flows/executions/{execution.id}/result"
    response = client.get(url, headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "result_pending"
    crud.crud_flow_execution.settle_result_capture(
        db_session, execution_id=execution.id, captured=captured, source="file"
    )
    db_session.commit()
    assert client.get(url, headers=headers).status_code == (200 if captured else 404)


def test_authorized_ci_request_records_key_usage(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
) -> None:
    """Successful CI reads persist activity; denied actions do not refresh it."""
    _, _, token, context, execution = owned_run(db_session, ci_resources)
    key = crud.crud_api_key.get(db_session, id=context.key_id)
    assert key.last_used_at is None
    headers = {"Authorization": f"Bearer {token}"}
    assert (
        client.get(
            f"/api/v1/flows/executions/{execution.id}", headers=headers
        ).status_code
        == 200
    )
    db_session.refresh(key)
    assert key.last_used_at is not None
    recorded = key.last_used_at
    assert (
        client.get(
            f"/api/v1/flows/executions/{execution.id}", headers=headers
        ).status_code
        == 200
    )
    db_session.refresh(key)
    assert key.last_used_at == recorded
    older = recorded - timedelta(minutes=2)
    CRUDBase(models.ApiKey).update(
        db_session, db_obj=key, obj_in={"last_used_at": older}
    )
    assert (
        client.get(
            f"/api/v1/flows/executions/{execution.id}", headers=headers
        ).status_code
        == 200
    )
    db_session.refresh(key)
    assert key.last_used_at > older
    CRUDBase(models.ApiKey).update(
        db_session, db_obj=key, obj_in={"ci_actions": [], "last_used_at": older}
    )
    assert (
        client.get(
            f"/api/v1/flows/executions/{execution.id}", headers=headers
        ).status_code
        == 401
    )
    with pytest.raises(PermissionError):
        crud.crud_ci_principal.authorize(
            db_session, context=context, action=CiAction.READ_EXECUTION
        )
    db_session.refresh(key)
    assert key.last_used_at == older


def test_reused_secret_inherits_provider_routing_and_gets_own_alias(
    db_session: Session,
    test_user: models.User,
) -> None:
    """The secret's endpoint and routing settings survive reuse by a new model."""
    source = crud.crud_ai_model.create_with_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={
            "name": "Source",
            "provider_name": "openai-compatible",
            "model_identifier": "model-a",
            "api_key": "synthetic-key",
            "api_endpoint": "https://models.example.com/v1",
            "meta_data": {
                "provider_runtime": {"responses_api_mode": "native"},
                "gateway": {
                    "enabled": True,
                    "model_alias": "example/source",
                    "url": "https://gateway.example.com/openai/v1",
                },
                "pricing": {"input": 123},
            },
        },
    )
    reused = crud.crud_ai_model.create_with_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={
            "name": "Reused",
            "provider_name": source.provider_name,
            "model_identifier": "model-b",
            "credentials_secret_id": source.credentials_secret_id,
        },
    )
    assert reused.api_endpoint == source.api_endpoint
    assert reused.meta_data["provider_runtime"] == source.meta_data["provider_runtime"]
    assert reused.meta_data["gateway"]["url"] == source.meta_data["gateway"]["url"]
    assert effective_gateway_alias(reused) == "openai-compatible/model-b"
    assert "pricing" not in reused.meta_data
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="synthetic", user=test_user)
    )
    assert (
        service._resolve_requested_model(
            effective_gateway_alias(reused), provider="openai"
        ).id
        == reused.id
    )


def test_first_model_response_uses_observed_gateway_boundary() -> None:
    """Request completion duration must not replace the first response event."""
    from types import SimpleNamespace

    execution = SimpleNamespace(
        startup_receipt={"agent_started_at": "2030-01-01T00:00:10Z"}
    )
    facts = [
        {"first_response_at": "2030-01-01T00:00:18Z", "duration": 30},
        {"first_response_at": "2030-01-01T00:00:14Z", "duration": 50},
    ]
    timings = timings_projection(execution, facts)
    assert timings["first_model_response_seconds"] == 4
    assert timings["first_model_response_unavailable_reason"] is None


def test_gateway_observes_first_chunk_and_resets_per_request() -> None:
    """The first chunk observation stays stable while the stream continues."""
    service = OpenAIGatewayService(MagicMock(), MagicMock())
    service._begin_request_accounting()
    before = datetime.now(timezone.utc)
    stream = service._prefetch_upstream_stream(
        iter([{"id": "one"}, {"id": "two"}]), provider="openai"
    )
    observed = service._first_model_response_at
    assert before <= datetime.fromisoformat(observed) <= datetime.now(timezone.utc)
    assert len(list(stream)) == 2
    assert service._first_model_response_at == observed
    service._begin_request_accounting()
    service._prefetch_upstream_stream(iter([]), provider="openai")
    assert service._first_model_response_at is None


@pytest.mark.parametrize("mode", ["native", "transcode"])
@pytest.mark.parametrize("stream", [False, True])
def test_gateway_records_observation_for_responses_transports(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    stream: bool,
) -> None:
    """Both Responses routes observe an upstream body before accounting."""
    from tests.services.test_openai_compatible_reasoning_wire import codex, rig, stub

    recorded: list[str | None] = []
    server, _ = stub(200)
    try:
        http = rig(
            monkeypatch,
            "openai-compatible",
            f"http://127.0.0.1:{server.server_port}/v1",
            mode,
        )

        def record(service: OpenAIGatewayService, **kwargs: Any) -> None:
            recorded.append(service._first_model_response_at)

        monkeypatch.setattr(OpenAIGatewayService, "_record_gateway_request", record)
        monkeypatch.setattr(OpenAIGatewayService, "_defer_stream_record", record)
        payload = codex()
        payload["stream"] = stream
        assert http.post("/openai/v1/responses", json=payload).status_code == 200
        assert recorded and all(recorded)
    finally:
        server.shutdown()
        server.server_close()


def test_repointed_model_inherits_new_key_routing(
    db_session: Session,
    test_user: models.User,
) -> None:
    """Repointing credentials also switches their endpoint and provider options."""
    source = crud.crud_ai_model.create_with_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={
            "name": "New routing",
            "provider_name": "openai-compatible",
            "model_identifier": "model-a",
            "api_key": "synthetic-new-key",
            "api_endpoint": "https://new.example.com/v1",
            "meta_data": {
                "provider_runtime": {"responses_api_mode": "native"},
                "gateway": {"enabled": True},
            },
        },
    )
    target = crud.crud_ai_model.create_with_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={
            "name": "Target",
            "provider_name": source.provider_name,
            "model_identifier": "model-b",
            "api_key": "synthetic-old-key",
            "api_endpoint": "https://old.example.com/v1",
            "meta_data": {"pricing": {"input": 123}},
        },
    )
    updated = crud.crud_ai_model.update(
        db_session,
        db_obj=target,
        obj_in={"credentials_secret_id": source.credentials_secret_id},
    )
    assert updated.api_endpoint == source.api_endpoint
    assert updated.meta_data["provider_runtime"] == source.meta_data["provider_runtime"]
    assert updated.meta_data["pricing"] == {"input": 123}
    assert effective_gateway_alias(updated) == "openai-compatible/model-b"


def test_reuse_preserves_explicit_routing_choices(
    db_session: Session,
    test_user: models.User,
) -> None:
    """Inheritance fills omissions without replacing deliberately chosen routes."""
    source = crud.crud_ai_model.create_with_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={
            "name": "Source",
            "provider_name": "azure",
            "model_identifier": "deployment-a",
            "api_key": "synthetic-key",
            "api_endpoint": "https://source.example.com/",
            "meta_data": {
                "provider_runtime": {"api_version": "2025-04-01-preview"},
                "gateway": {"enabled": True, "model_alias": "example/source"},
            },
        },
    )
    reused = crud.crud_ai_model.create_with_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={
            "name": "Target",
            "provider_name": "azure",
            "model_identifier": "deployment-b",
            "credentials_secret_id": source.credentials_secret_id,
            "api_endpoint": "https://target.example.com/",
            "meta_data": {
                "provider_runtime": {"api_version": "2025-03-01-preview"},
                "gateway": {"model_alias": "example/target"},
            },
        },
    )
    assert reused.api_endpoint == "https://target.example.com/"
    assert reused.meta_data["provider_runtime"]["api_version"] == "2025-03-01-preview"
    assert effective_gateway_alias(reused) == "example/target"
    with pytest.raises(ValueError, match="already used"):
        crud.crud_ai_model.create_with_account(
            db_session,
            account_id=test_user.account_id,
            obj_in={
                "name": "Collision",
                "provider_name": "azure",
                "model_identifier": "deployment-b",
                "credentials_secret_id": source.credentials_secret_id,
                "meta_data": {"gateway": {"model_alias": "example/target"}},
            },
        )


def test_gateway_response_boundary_survives_storage_and_timing_projection(
    db_session: Session,
    ci_resources: tuple[Any, ...],
) -> None:
    """Persist the observed event and read it back through scoped timing CRUD."""
    from preloop.services.model_gateway_events import ModelGatewayEventEmitter
    from preloop.services.review_completion_evidence import execution_timings

    _, _, _, _, execution = owned_run(db_session, ci_resources)
    owner = ci_resources[0]
    agent_started = datetime.now(timezone.utc) - timedelta(seconds=5)
    crud.crud_flow_execution.record_startup_receipt(
        db_session,
        execution_id=execution.id,
        receipt={"agent_started_at": agent_started.isoformat()},
    )
    model = crud.crud_ai_model.create_with_account(
        db_session,
        account_id=owner.account_id,
        obj_in={
            "name": "Timing model",
            "provider_name": "openai",
            "model_identifier": "model-a",
            "api_key": "synthetic-upstream-key",
        },
    )
    key, _ = crud.crud_api_key.create_runtime_key(
        db_session,
        name="Timing runtime",
        account_id=owner.account_id,
        user_id=owner.id,
        context_data={
            "flow_execution_id": str(execution.id),
            "flow_id": str(execution.flow_id),
        },
    )
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="synthetic", user=owner, api_key=key)
    )
    service._begin_request_accounting()
    service._prefetch_upstream_stream(iter([{"id": "first"}]), provider="openai")
    observed = service._first_model_response_at
    service._record_gateway_request_inner(
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=60,
        ai_model=model,
        requested_model="model-a",
        response_payload={"usage": {"input_tokens": 1, "output_tokens": 1}},
        upstream_response=None,
        endpoint_kind="responses",
    )
    facts = crud.crud_api_usage.review_request_facts(
        db_session, execution_id=execution.id, account_id=owner.account_id
    )
    assert facts[0]["first_response_at"] == observed
    timing = execution_timings(db_session, execution, owner.account_id)
    assert 5 <= timing["first_model_response_seconds"] < 10
    assert timing["total_model_seconds"] == 60
    assert (
        crud.crud_api_usage.review_request_facts(
            db_session, execution_id=execution.id, account_id=uuid4()
        )
        == []
    )
    usage = crud.crud_api_usage.get(db_session, id=service.last_usage_id)
    event = ModelGatewayEventEmitter(db_session)._build_event(
        usage=usage, request_payload=None, response_payload=None
    )
    assert event["payload"]["first_response_at"] == observed
