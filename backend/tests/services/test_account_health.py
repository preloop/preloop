"""Operator account health: counts and ids for one account and window."""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.endpoints.account_health import require_platform_operator
from preloop.models import models
from preloop.models.crud import crud_api_traffic
from preloop.services.account_health import (
    AccountHealthWindowError,
    build_account_health,
    resolve_window,
)

NOW = datetime.now(timezone.utc).replace(microsecond=0)
NAIVE_NOW = NOW.replace(tzinfo=None)


def _flow(db: Session, account_id: Any) -> models.Flow:
    flow = models.Flow(
        account_id=account_id,
        name=f"Health flow {uuid4().hex[:6]}",
        prompt_template="Example",
        agent_config={},
    )
    db.add(flow)
    db.flush()
    return flow


def _execution(db: Session, flow: models.Flow, **fields: Any) -> models.FlowExecution:
    fields.setdefault("created_at", NAIVE_NOW - timedelta(hours=1))
    execution = models.FlowExecution(flow_id=flow.id, **fields)
    db.add(execution)
    db.flush()
    return execution


@pytest.fixture
def seeded(db_session: Session, test_user: models.User) -> dict[str, Any]:
    account_id = test_user.account_id
    flow = _flow(db_session, account_id)
    first_auth = _execution(
        db_session,
        flow,
        status="FAILED",
        failure_category="model_auth",
        error_message="customer text that must not be returned",
        created_at=NAIVE_NOW - timedelta(hours=3),
    )
    _execution(db_session, flow, status="FAILED", failure_category="model_auth")
    _execution(db_session, flow, status="TIMEOUT")
    hollow = _execution(db_session, flow, status="SUCCEEDED", tool_calls_count=0)
    busy = _execution(db_session, flow, status="SUCCEEDED", tool_calls_count=4)
    # A failed model attempt still counts as a model request.
    for execution, error_class in (
        (busy, "upstream_overloaded"),
        (busy, None),
        (busy, "client_cancelled"),
        (busy, "budget_exceeded"),
    ):
        db_session.add(
            models.ApiUsage(
                account_id=account_id,
                flow_execution_id=execution.id,
                endpoint="/openai/v1/chat/completions",
                method="POST",
                status_code=529 if error_class else 200,
                duration=0.1,
                action_type="model_gateway",
                provider_name="openai",
                error_class=error_class,
                timestamp=NAIVE_NOW - timedelta(hours=1),
            )
        )
    # Out of window: ignored.
    _execution(
        db_session,
        flow,
        status="FAILED",
        failure_category="tool_error",
        created_at=NAIVE_NOW - timedelta(days=3),
    )
    crud_api_traffic.add_rollups(
        db_session,
        rows=[
            {
                "bucket_start": NOW - timedelta(minutes=30),
                "account_id": account_id,
                "principal_kind": "api_key",
                "method": "GET",
                "route_template": "/api/v1/flows/{flow_id}",
                "status_class": 4,
                "request_count": 7,
                "total_duration_ms": 70,
                "max_duration_ms": 10,
            }
        ],
    )
    crud_api_traffic.add_events(
        db_session,
        events=[
            {
                "occurred_at": NOW - timedelta(minutes=30),
                "kind": "empty_after_create",
                "account_id": account_id,
                "principal_kind": "session",
                "principal_id": test_user.id,
                "method": "GET",
                "route_template": "/api/v1/projects",
                "status_code": 200,
                "duration_ms": 3,
                "reason_code": None,
            }
        ],
    )
    db_session.flush()
    return {"account_id": account_id, "first_auth": first_auth, "hollow": hollow}


def test_health_reports_every_signal_without_content(
    db_session: Session, seeded: dict[str, Any]
) -> None:
    since, until = resolve_window(None, None)
    report = build_account_health(
        db_session, account_id=seeded["account_id"], since=since, until=until
    )

    failed = {row["category"]: row for row in report["failed_executions"]}
    assert set(failed) == {"model_auth", "timeout"}
    assert failed["model_auth"]["count"] == 2
    assert failed["model_auth"]["first_execution_id"] == seeded["first_auth"].id
    hollow = report["hollow_successes"]
    assert hollow["zero_tool_calls"]["count"] == 1
    assert hollow["zero_tool_calls"]["sample_execution_ids"] == [seeded["hollow"].id]
    assert hollow["zero_model_requests"]["count"] == 1
    assert report["route_problems"][0]["count"] == 7
    assert report["empty_after_create"][0]["route_template"] == "/api/v1/projects"
    [gateway] = report["gateway_upstream_errors"]
    assert (gateway["provider"], gateway["error_class"], gateway["count"]) == (
        "openai",
        "upstream_overloaded",
        1,
    )
    assert "customer text" not in str(report)


def test_window_is_bounded() -> None:
    with pytest.raises(AccountHealthWindowError):
        resolve_window(NOW - timedelta(days=40), NOW)
    with pytest.raises(AccountHealthWindowError):
        resolve_window(NOW, NOW - timedelta(hours=1))


def test_endpoint_is_for_platform_operators_only(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    seeded: dict[str, Any],
) -> None:
    path = f"/api/v1/operator/accounts/{seeded['account_id']}/health"
    assert client.get(path).status_code == 403

    test_user.is_superuser = True
    db_session.flush()
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.json()["failed_executions"][0]["category"] == "model_auth"
    assert client.get(f"/api/v1/operator/accounts/{uuid4()}/health").status_code == 404


def test_run_scoped_keys_of_a_superuser_are_refused(test_user: models.User) -> None:
    from fastapi import HTTPException

    test_user.is_superuser = True
    test_user._auth_api_key = models.ApiKey(
        context_data={"flow_execution_id": str(uuid4())}, credential_type="legacy"
    )
    with pytest.raises(HTTPException) as exc:
        require_platform_operator(test_user)
    assert exc.value.status_code == 403
    test_user._auth_api_key = models.ApiKey(context_data=None, credential_type="legacy")
    assert require_platform_operator(test_user) is test_user
