"""Every call shape a webhook receiver or the CLI uses to return a decision.

Regression for issue 1128: ``POST /approve`` with ``{"comment": ...}`` was a
422 because ``approved`` was required even where the path names the decision,
the CLI's ``{"reason": ...}`` hit the same 422, and the webhook payload had no
URL a system could call to decide.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from preloop.models.crud import crud_approval_workflow
from preloop.models.models.approval_request import ApprovalRequest
from preloop.models.models.tool_configuration import ToolConfiguration
from preloop.models.schemas.approval_request import ApprovalDecision
from preloop.models.schemas.tool_configuration import ApprovalWorkflowCreate

TOKEN = "call-shape-token"


def _pending(db_session, test_user) -> ApprovalRequest:
    workflow = crud_approval_workflow.create(
        db_session,
        obj_in=ApprovalWorkflowCreate(name="Shapes WF", approval_type="manual"),
        account_id=str(test_user.account_id),
    )
    db_session.flush()
    tool_config = ToolConfiguration(
        tool_name="deploy",
        tool_source="builtin",
        account_id=test_user.account_id,
        approval_workflow_id=workflow.id,
    )
    db_session.add(tool_config)
    db_session.flush()
    row = ApprovalRequest(
        account_id=test_user.account_id,
        tool_configuration_id=tool_config.id,
        approval_workflow_id=workflow.id,
        execution_id="exec-shapes",
        tool_name="deploy",
        tool_args={},
        status="pending",
        requested_at=datetime.now(UTC),
        approval_token=TOKEN,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _row_as(row: ApprovalRequest, status: str) -> ApprovalRequest:
    """The authenticated route serializes the real row, so return the row."""
    row.status = status
    row.resolved_at = datetime.now(UTC)
    return row


def _resolved(row: ApprovalRequest, status: str) -> MagicMock:
    updated = MagicMock()
    updated.id = row.id
    updated.tool_name = row.tool_name
    updated.tool_args = {}
    updated.agent_reasoning = None
    updated.status = status
    updated.requested_at = row.requested_at
    updated.expires_at = None
    updated.resolved_at = datetime.now(UTC)
    return updated


class TestSchema:
    def test_body_without_approved_is_valid(self):
        assert ApprovalDecision().approved is None
        assert ApprovalDecision(comment="ok").comment == "ok"

    def test_reason_is_folded_into_comment(self):
        assert ApprovalDecision(reason="legacy").comment == "legacy"
        assert ApprovalDecision(reason="r", comment="c").comment == "c"


# Authenticated API: /api/v1/approval-requests/{id}/approve|decline|decide


@pytest.fixture
def service(db_session):
    """Patch ApprovalService so the route runs end to end over HTTP."""
    base = "preloop.api.endpoints.approval_requests"
    with (
        patch(f"{base}.get_async_db_session") as get_session,
        patch(f"{base}.ApprovalService") as service_cls,
        patch(f"{base}.attributed_async", new=AsyncMock(side_effect=lambda _db, r: r)),
        patch(f"{base}._advance_security_maintenance", new=AsyncMock()),
    ):
        get_session.return_value.__aenter__.return_value = AsyncMock()
        svc = AsyncMock()
        service_cls.return_value = svc
        yield svc


@pytest.mark.parametrize(
    "body",
    [None, {}, {"comment": "looks right"}, {"approved": True, "comment": "x"}],
    ids=["no-body", "empty", "comment-only", "legacy-approved-true"],
)
def test_authenticated_approve_shapes(
    client: TestClient, db_session, test_user, service, body
):
    row = _pending(db_session, test_user)
    service.get_approval_request.return_value = row
    service.approve_request.side_effect = lambda *a, **k: _row_as(row, "approved")
    kwargs = {} if body is None else {"json": body}
    response = client.post(f"/api/v1/approval-requests/{row.id}/approve", **kwargs)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "approved"
    sent_comment = service.approve_request.await_args.args[1]
    assert sent_comment == (body or {}).get("comment")


@pytest.mark.parametrize(
    "body",
    [None, {"comment": "no"}, {"approved": False, "comment": "no"}],
    ids=["no-body", "comment-only", "legacy-approved-false"],
)
def test_authenticated_decline_shapes(
    client: TestClient, db_session, test_user, service, body
):
    row = _pending(db_session, test_user)
    service.get_approval_request.return_value = row
    service.decline_request.side_effect = lambda *a, **k: _row_as(row, "declined")
    kwargs = {} if body is None else {"json": body}
    response = client.post(f"/api/v1/approval-requests/{row.id}/decline", **kwargs)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "declined"
    assert service.decline_request.await_args.args[1] == (body or {}).get("comment")


def test_cli_reason_body_is_accepted_and_stored(client, db_session, test_user, service):
    row = _pending(db_session, test_user)
    service.get_approval_request.return_value = row
    service.approve_request.side_effect = lambda *a, **k: _row_as(row, "approved")
    response = client.post(
        f"/api/v1/approval-requests/{row.id}/approve", json={"reason": "from cli"}
    )
    assert response.status_code == 200, response.text
    assert service.approve_request.await_args.args[1] == "from cli"


@pytest.mark.parametrize("route,approved", [("approve", False), ("decline", True)])
def test_contradictory_approved_is_refused(
    client, db_session, test_user, service, route, approved
):
    row = _pending(db_session, test_user)
    service.get_approval_request.return_value = row
    response = client.post(
        f"/api/v1/approval-requests/{row.id}/{route}", json={"approved": approved}
    )
    assert response.status_code == 400
    service.approve_request.assert_not_called()
    service.decline_request.assert_not_called()


def test_decide_still_requires_approved(client, db_session, test_user, service):
    row = _pending(db_session, test_user)
    service.get_approval_request.return_value = row
    response = client.post(
        f"/api/v1/approval-requests/{row.id}/decide", json={"comment": "?"}
    )
    assert response.status_code == 422
    assert "/approve" in response.json()["detail"]
    service.approve_request.assert_not_called()


@pytest.mark.parametrize("approved,status", [(True, "approved"), (False, "declined")])
def test_decide_with_approved(client, db_session, test_user, service, approved, status):
    row = _pending(db_session, test_user)
    service.get_approval_request.return_value = row
    service.approve_request.side_effect = lambda *a, **k: _row_as(row, "approved")
    service.decline_request.side_effect = lambda *a, **k: _row_as(row, "declined")
    response = client.post(
        f"/api/v1/approval-requests/{row.id}/decide",
        json={"approved": approved, "comment": "c"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == status


# Token API: the URLs carried in the webhook payload's "decision" block


@pytest.fixture
def token_service():
    base = "preloop.api.endpoints.public_approval"
    with (
        patch(f"{base}.get_async_db_session") as get_session,
        patch(f"{base}.ApprovalService") as service_cls,
    ):
        get_session.return_value.__aenter__.return_value = AsyncMock()
        svc = AsyncMock()
        service_cls.return_value = svc
        yield svc


@pytest.mark.parametrize(
    "body", [None, {"comment": "consent recorded"}], ids=["no-body", "comment"]
)
@pytest.mark.parametrize(
    "route,status", [("approve", "approved"), ("decline", "declined")]
)
def test_token_routes(
    client, db_session, test_user, token_service, body, route, status
):
    row = _pending(db_session, test_user)
    token_service.approve_request.return_value = _resolved(row, "approved")
    token_service.decline_request.return_value = _resolved(row, "declined")
    kwargs = {} if body is None else {"json": body}
    response = client.post(
        f"/approval/{row.id}/{route}", params={"token": TOKEN}, **kwargs
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == status
    called = getattr(token_service, f"{route}_request")
    assert called.await_args.args[1] == (body or {}).get("comment")


def test_token_route_rejects_a_wrong_token(
    client, db_session, test_user, token_service
):
    row = _pending(db_session, test_user)
    response = client.post(f"/approval/{row.id}/approve", params={"token": "wrong"})
    assert response.status_code == 404
    token_service.approve_request.assert_not_called()


def test_token_decide_keeps_working(client, db_session, test_user, token_service):
    row = _pending(db_session, test_user)
    token_service.decline_request.return_value = _resolved(row, "declined")
    response = client.post(
        f"/approval/{row.id}/decide",
        params={"token": TOKEN},
        json={"action": "decline", "comment": "no"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "declined"
