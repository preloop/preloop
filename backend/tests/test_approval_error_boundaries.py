"""Approval values and overload responses survive session/transport boundaries."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.exceptions import ResponseValidationError
from fastapi.testclient import TestClient
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from preloop.api.app import create_app
from preloop.api.endpoints import approval_requests
from preloop.models import models
from preloop.models.schemas.approval_request import ApprovalRequestResponse
from preloop.services.approval_service import ApprovalService


@pytest.fixture
def approval_row(db_session: Session, test_user: models.User) -> models.ApprovalRequest:
    workflow = models.ApprovalWorkflow(account_id=test_user.account_id, name="boundary")
    tool = models.ToolConfiguration(
        account_id=test_user.account_id, tool_name="Read", tool_source="builtin"
    )
    db_session.add_all([workflow, tool])
    db_session.flush()
    row = models.ApprovalRequest(
        account_id=test_user.account_id,
        tool_configuration_id=tool.id,
        approval_workflow_id=workflow.id,
        tool_name="Read",
        tool_args={"path": "example.txt"},
        status="approved",
        approver_comment="Approved safely",
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_expired_detached_approval_repr_never_loads_attributes(
    db_session: Session, approval_row: models.ApprovalRequest
) -> None:
    request_id = approval_row.id
    db_session.expire(approval_row)
    db_session.expunge(approval_row)
    assert str(request_id) in repr(approval_row)
    assert "ApprovalRequest" in repr(approval_row)


@pytest.mark.parametrize("list_endpoint", [False, True])
def test_approval_read_response_survives_session_cleanup(
    db_session: Session,
    test_user: models.User,
    approval_row: models.ApprovalRequest,
    list_endpoint: bool,
) -> None:
    request_id = approval_row.id
    if list_endpoint:
        result = approval_requests.list_approval_requests(
            current_user=test_user,
            db=db_session,
            status=None,
            execution_id=None,
            limit=50,
            skip=0,
        )[0]
    else:
        with patch.object(approval_requests, "_record_viewed_event"):
            result = approval_requests.get_approval_request(
                request_id=request_id, current_user=test_user, db=db_session
            )
    db_session.expire(approval_row)
    db_session.expunge(approval_row)
    payload = ApprovalRequestResponse.model_validate(result).model_dump(mode="json")
    assert payload["id"] == str(request_id)
    assert payload["status"] == "approved"
    assert payload["tool_args"] == {"path": "example.txt"}


@pytest.mark.asyncio
async def test_final_approval_survives_poll_rollback_and_detach(
    db_session: Session, approval_row: models.ApprovalRequest
) -> None:
    request_id: UUID = approval_row.id

    poll_db = Session(
        bind=db_session.connection(), join_transaction_mode="create_savepoint"
    )

    @asynccontextmanager
    async def poll_session():
        try:
            yield poll_db
        finally:
            poll_db.rollback()
            poll_db.close()

    service = ApprovalService(AsyncMock(), "https://fixture.invalid")
    with (
        patch("preloop.models.db.session.get_async_db_session", poll_session),
        patch.object(
            ApprovalService,
            "get_approval_request_for_update",
            new=AsyncMock(
                side_effect=lambda _: poll_db.get(models.ApprovalRequest, request_id)
            ),
        ),
    ):
        result = await service.wait_for_approval(request_id)
    assert result.id == request_id
    assert result.status == "approved"
    assert result.approver_comment == "Approved safely"


@pytest.fixture
def overload_app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(PoolTimeout, create_app().exception_handlers[PoolTimeout])

    @app.get("/overloaded")
    async def overloaded() -> None:
        raise PoolTimeout("pool exhausted")

    @app.websocket("/socket")
    async def overloaded_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        raise PoolTimeout("pool exhausted")

    return app


def test_pool_exhaustion_http_remains_retryable(overload_app: FastAPI) -> None:
    with TestClient(overload_app) as client:
        response = client.get("/overloaded")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"


def test_pool_exhaustion_websocket_closes_with_retry_code(
    overload_app: FastAPI,
) -> None:
    with TestClient(overload_app) as client:
        with client.websocket_connect("/socket") as websocket:
            with pytest.raises(WebSocketDisconnect) as exc:
                websocket.receive_json()
            assert exc.value.code == 1013


@pytest.mark.asyncio
async def test_response_validation_error_does_not_fail_while_logging(
    db_session: Session, approval_row: models.ApprovalRequest
) -> None:
    error = ResponseValidationError(
        [
            {
                "loc": ("response", "id"),
                "type": "get_attribute_error",
                "msg": "cannot load",
                "input": approval_row,
            }
        ]
    )
    db_session.expire(approval_row)
    db_session.expunge(approval_row)
    handler = create_app().exception_handlers[Exception]
    response = await handler(
        Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/approval-requests",
                "headers": [],
            }
        ),
        error,
    )
    assert response.status_code == 500
