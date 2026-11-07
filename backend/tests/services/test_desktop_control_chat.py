"""Desktop approvals reach eligible colleagues through the real chat queue."""

from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterator
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_chat, crud_user
from preloop.services import chat_worker
from preloop.utils import permissions
from preloop.utils.encryption import encrypt_value


@pytest.fixture
def desktop_chat(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    colleague = crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "username": "desktop-colleague",
            "email": "colleague@example.com",
            "hashed_password": "unused",
            "is_active": True,
        },
    )
    # This colleague can view and decide approvals in both editions. With
    # the RBAC plugin installed those permissions need an actual role.
    granted = []
    for name in ("view_approvals", "decide_approvals"):
        permission = (
            db_session.query(models.Permission).filter_by(name=name).one_or_none()
        )
        if permission is None:
            permission = models.Permission(
                name=name, description=name, category="approvals"
            )
            db_session.add(permission)
        granted.append(permission)
    role = models.Role(account_id=test_user.account_id, name="desktop_decider")
    db_session.add(role)
    db_session.flush()
    db_session.add_all(
        [
            models.RolePermission(role_id=role.id, permission_id=permission.id)
            for permission in granted
        ]
        + [models.UserRole(user_id=colleague.id, role_id=role.id)]
    )
    workflow = models.ApprovalWorkflow(
        account_id=test_user.account_id,
        name="Desktop approvals",
        approver_user_ids=[test_user.id],
    )
    connection = models.ChatConnection(
        account_id=test_user.account_id,
        name="Desktop chat",
        provider="slack",
        workspace_id="synthetic-workspace",
        credentials_encrypted=encrypt_value(
            '{"bot_token":"synthetic-token","verification_secret":"synthetic-secret"}'
        ),
    )
    db_session.add_all([workflow, connection])
    db_session.flush()
    tool = models.ToolConfiguration(
        account_id=test_user.account_id,
        tool_name="desktop_control",
        approval_workflow_id=workflow.id,
    )
    db_session.add(tool)
    db_session.flush()
    endpoint = models.DesktopEndpoint(
        account_id=test_user.account_id,
        endpoint_id="synthetic-desktop",
        hostname="example.com",
    )
    db_session.add(endpoint)
    db_session.flush()
    view = models.DesktopView(
        account_id=test_user.account_id,
        user_id=test_user.id,
        endpoint_pk=endpoint.id,
        state="live",
        mode="view_only",
        expires_at=datetime.utcnow() + timedelta(minutes=10),
    )
    db_session.add(view)
    db_session.flush()
    request = models.ApprovalRequest(
        account_id=test_user.account_id,
        tool_configuration_id=tool.id,
        approval_workflow_id=workflow.id,
        tool_name="desktop_control",
        tool_args={"requested_by": str(test_user.id)},
        rule_context={
            "desktop_control": {
                "view_id": str(view.id),
                "requester_id": str(test_user.id),
            }
        },
        status="pending",
    )
    db_session.add(request)
    for user in (test_user, colleague):
        db_session.add(
            models.ChatIdentity(
                account_id=test_user.account_id,
                connection_id=connection.id,
                user_id=user.id,
                external_user_id=str(user.id),
            )
        )
    db_session.commit()

    @contextmanager
    def shared_session() -> Iterator[Session]:
        yield db_session

    monkeypatch.setattr(chat_worker, "get_session_factory", lambda: shared_session)
    return {"request": request, "colleague": colleague, "workflow": workflow}


@pytest.mark.asyncio
@pytest.mark.parametrize("include_requester", [False, True])
async def test_owner_requested_desktop_control_reaches_colleagues_chat(
    desktop_chat: dict[str, Any],
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    include_requester: bool,
) -> None:
    request, colleague = desktop_chat["request"], desktop_chat["colleague"]
    count = chat_worker.enqueue_approval_notifications(
        test_user.account_id,
        request.id,
        [colleague.id, test_user.id] if include_requester else [colleague.id],
    )
    assert count == 1
    row = db_session.query(models.ChatWork).one()
    assert row.user_id == colleague.id
    assert row.payload["approval_request_id"] == str(request.id)
    assert desktop_chat["workflow"].approver_user_ids == [test_user.id]

    # Keep the worker's eligibility rechecks; only the API read and provider
    # transport are stubbed so this fixture sends no external messages.
    monkeypatch.setattr(chat_worker.ChatBroker, "request", AsyncMock(return_value={}))
    send = AsyncMock(return_value="synthetic-message")
    monkeypatch.setattr(chat_worker, "send_private_reply", send)
    assert await chat_worker.process_one(db_session, None)
    db_session.refresh(row)
    assert row.status == "delivered"
    send.assert_awaited_once()


@pytest.mark.parametrize(
    "blocked",
    [
        "requester",
        "inactive",
        "permission",
        "settled",
        "missing_requester",
        "other_account",
    ],
)
def test_desktop_chat_eligibility_rechecks_decision_policy(
    desktop_chat: dict[str, Any],
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    blocked: str,
) -> None:
    request, user = desktop_chat["request"], desktop_chat["colleague"]
    if blocked == "requester":
        user = test_user
    elif blocked == "inactive":
        user.is_active = False
    elif blocked == "permission":
        monkeypatch.setattr(permissions, "user_holds_permission", lambda *a: False)
    elif blocked == "settled":
        request.status = "approved"
    elif blocked == "missing_requester":
        request.rule_context = {}
    elif blocked == "other_account":
        account = models.Account(organization_name="Another account")
        db_session.add(account)
        db_session.flush()
        user.account_id = account.id
    db_session.flush()
    assert not crud_chat.eligible_approver(db_session, user, request.id)


def test_desktop_chat_eligibility_ignores_spoofed_tool_args(
    desktop_chat: dict[str, Any], db_session: Session, test_user: models.User
) -> None:
    request, colleague = desktop_chat["request"], desktop_chat["colleague"]
    request.tool_args = {"requested_by": str(colleague.id)}
    db_session.flush()
    assert not crud_chat.eligible_approver(db_session, test_user, request.id)
    assert crud_chat.eligible_approver(db_session, colleague, request.id)


def test_regular_chat_approvals_keep_workflow_eligibility(
    desktop_chat: dict[str, Any], db_session: Session, test_user: models.User
) -> None:
    request = desktop_chat["request"]
    request.tool_name = "synthetic_tool"
    db_session.flush()
    assert crud_chat.eligible_approver(db_session, test_user, request.id)
    assert not crud_chat.eligible_approver(
        db_session, desktop_chat["colleague"], request.id
    )
