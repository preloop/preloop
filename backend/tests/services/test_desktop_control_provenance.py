"""Desktop grants cannot be manufactured through arbitrary tool approvals."""

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.api.endpoints import desktop
from preloop.models import models
from preloop.models.crud import crud_account, crud_user
from preloop.models.db.session import SyncApprovalSession
from preloop.services.approval_service import ApprovalService
from tests.permission_helpers import grant_permission


@pytest.fixture
def control_rows(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    colleague = crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "username": "desktop-colleague",
            "email": "colleague@example.com",
            "is_active": True,
        },
    )
    account = crud_account.create(
        db_session, obj_in={"organization_name": "Another example account"}
    )
    outsider = crud_user.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "username": "desktop-outsider",
            "email": "outsider@example.com",
            "is_active": True,
        },
    )
    views = {}
    for name, owner in (
        ("own", test_user),
        ("colleague", colleague),
        ("foreign", outsider),
    ):
        endpoint = models.DesktopEndpoint(
            account_id=owner.account_id,
            endpoint_id=str(uuid4()),
            hostname="example.com",
        )
        db_session.add(endpoint)
        db_session.flush()
        view = models.DesktopView(
            account_id=owner.account_id,
            user_id=owner.id,
            endpoint_pk=endpoint.id,
            state="live",
            mode="view_only",
            expires_at=datetime.utcnow() + timedelta(minutes=10),
        )
        db_session.add(view)
        views[name] = view
    workflow = models.ApprovalWorkflow(account_id=test_user.account_id, name="Desktop")
    tool = models.ToolConfiguration(
        account_id=test_user.account_id, tool_name="desktop_control"
    )
    db_session.add_all([workflow, tool])
    grant_permission(db_session, colleague, "decide_approvals")
    db_session.commit()

    async def run(operation: Any) -> Any:
        return operation(db_session)

    monkeypatch.setattr(desktop, "_run_db", run)
    monkeypatch.setattr(desktop.broker, "get_desktop_bus", AsyncMock())
    monkeypatch.setattr(desktop.broker, "publish_json", AsyncMock())
    monkeypatch.setattr(ApprovalService, "_broadcast_approval_update", AsyncMock())
    return {
        "views": views,
        "colleague": colleague,
        "outsider": outsider,
        "workflow": workflow,
        "tool": tool,
        "service": ApprovalService(SyncApprovalSession(db_session), "http://localhost"),
    }


def fabricated_request(
    db: Session, rows: dict[str, Any], user: models.User, target: str
) -> models.ApprovalRequest:
    request = models.ApprovalRequest(
        account_id=user.account_id,
        tool_configuration_id=rows["tool"].id,
        approval_workflow_id=rows["workflow"].id,
        tool_name="desktop_control",
        tool_args={
            "view_id": str(rows["views"][target].id),
            "requested_by": str(uuid4()),
            "reason": "Synthetic arbitrary tool call",
        },
        status="approved",
        responses=[{"user_id": str(user.id), "decision": "approved"}],
    )
    db.add(request)
    db.commit()
    return request


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["own", "colleague", "foreign"])
async def test_fabricated_approval_cannot_grant_a_desktop(
    control_rows: dict[str, Any],
    db_session: Session,
    test_user: models.User,
    target: str,
) -> None:
    request = fabricated_request(db_session, control_rows, test_user, target)
    await control_rows["service"]._apply_desktop_control(request)
    db_session.refresh(control_rows["views"][target])
    assert control_rows["views"][target].mode == "view_only"
    desktop.broker.publish_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_generic_tool_creation_cannot_supply_desktop_provenance(
    control_rows: dict[str, Any], db_session: Session, test_user: models.User
) -> None:
    with pytest.raises(HTTPException) as exc:
        await control_rows["service"].create_approval_request(
            account_id=str(test_user.account_id),
            tool_configuration_id=control_rows["tool"].id,
            approval_workflow_id=control_rows["workflow"].id,
            tool_name="desktop_control",
            tool_args={
                "view_id": str(control_rows["views"]["foreign"].id),
                "requested_by": str(uuid4()),
            },
            rule_context={
                "desktop_control": {
                    "view_id": str(control_rows["views"]["own"].id),
                    "requester_id": str(test_user.id),
                }
            },
        )
    assert exc.value.status_code == 403
    assert db_session.query(models.ApprovalRequest).count() == 0


@pytest.mark.asyncio
async def test_unbound_request_cannot_self_approve_with_spoofed_args(
    control_rows: dict[str, Any], db_session: Session, test_user: models.User
) -> None:
    request = fabricated_request(db_session, control_rows, test_user, "own")
    request.status = "pending"
    request.responses = []
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await control_rows["service"].approve_request(request.id, user_id=test_user.id)
    assert exc.value.status_code == 403
    db_session.refresh(request)
    assert request.status == "pending"
    assert control_rows["views"]["own"].mode == "view_only"


@pytest.mark.asyncio
@pytest.mark.parametrize("notify", [False, True])
@pytest.mark.parametrize("forged_context", [False, True])
async def test_generic_entry_points_reserve_desktop_name_and_context(
    control_rows: dict[str, Any],
    db_session: Session,
    test_user: models.User,
    notify: bool,
    forged_context: bool,
) -> None:
    service = control_rows["service"]
    kwargs = {
        "account_id": str(test_user.account_id),
        "tool_configuration_id": control_rows["tool"].id,
        "tool_name": "synthetic_tool" if forged_context else "desktop_control",
        "tool_args": {
            "view_id": str(control_rows["views"]["foreign"].id),
            "requested_by": str(uuid4()),
            "desktop_view_id": str(control_rows["views"]["own"].id),
            "desktop_requester_id": str(test_user.id),
        },
        "rule_context": {"desktop_control": {}} if forged_context else None,
    }
    if notify:
        kwargs["approval_workflow"] = control_rows["workflow"]
        create = service.create_and_notify
    else:
        kwargs["approval_workflow_id"] = control_rows["workflow"].id
        create = service.create_approval_request
    with pytest.raises(HTTPException) as exc:
        await create(**kwargs)
    assert exc.value.status_code == 403
    assert db_session.query(models.ApprovalRequest).count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["colleague", "foreign"])
async def test_internal_creation_checks_account_and_owner(
    control_rows: dict[str, Any],
    db_session: Session,
    test_user: models.User,
    target: str,
) -> None:
    with pytest.raises(HTTPException) as exc:
        await control_rows["service"].create_approval_request(
            account_id=str(test_user.account_id),
            tool_configuration_id=control_rows["tool"].id,
            approval_workflow_id=control_rows["workflow"].id,
            tool_name="desktop_control",
            tool_args={},
            desktop_view_id=control_rows["views"][target].id,
            desktop_requester_id=(
                control_rows["outsider"].id if target == "foreign" else test_user.id
            ),
        )
    assert exc.value.status_code == 403
    assert db_session.query(models.ApprovalRequest).count() == 0


@pytest.mark.asyncio
async def test_bound_approval_ignores_tampered_arguments(
    control_rows: dict[str, Any],
    db_session: Session,
    test_user: models.User,
) -> None:
    from preloop.models.crud import crud_desktop
    from preloop.services.approval_rule_context import SOURCE_TOOL_DEFAULT_WORKFLOW

    service = control_rows["service"]
    view = control_rows["views"]["own"]
    colleague = control_rows["colleague"]
    request = await service.create_approval_request(
        account_id=str(test_user.account_id),
        tool_configuration_id=control_rows["tool"].id,
        approval_workflow_id=control_rows["workflow"].id,
        tool_name="desktop_control",
        tool_args={"view_id": str(view.id), "requested_by": str(test_user.id)},
        desktop_view_id=view.id,
        desktop_requester_id=test_user.id,
    )
    assert request.rule_context["source"] == SOURCE_TOOL_DEFAULT_WORKFLOW
    assert request.rule_context["decision"] == "require_approval"
    assert request.rule_context["rule_name"] == "Desktop control"
    request.tool_args = {
        "view_id": str(control_rows["views"]["foreign"].id),
        "requested_by": str(colleague.id),
    }
    db_session.commit()
    assert (
        crud_desktop.pending_control_approval(
            db_session,
            account_id=test_user.account_id,
            view_id=view.id,
            tool_name="desktop_control",
        ).id
        == request.id
    )
    with pytest.raises(HTTPException):
        await service.approve_request(request.id, user_id=test_user.id)
    with pytest.raises(HTTPException):
        await service._auto_approve_request(request.id, "Synthetic AI decision")
    await service.approve_request(request.id, user_id=colleague.id)
    db_session.refresh(view)
    assert view.mode == "control"
    for name in ("colleague", "foreign"):
        db_session.refresh(control_rows["views"][name])
        assert control_rows["views"][name].mode == "view_only"
    desktop.broker.publish_json.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    ["account", "owner", "pending", "anonymous", "self", "inactive", "permission"],
)
async def test_grant_callback_rechecks_persisted_binding_and_decision(
    control_rows: dict[str, Any],
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    invalid: str,
) -> None:
    request = fabricated_request(db_session, control_rows, test_user, "own")
    colleague = control_rows["colleague"]
    requester_id = test_user.id
    if invalid == "account":
        requester_id = control_rows["outsider"].id
    elif invalid == "owner":
        requester_id = colleague.id
    request.rule_context = {
        "desktop_control": {
            "view_id": str(
                control_rows["views"]["foreign" if invalid == "account" else "own"].id
            ),
            "requester_id": str(requester_id),
        }
    }
    request.responses = [{"user_id": str(colleague.id), "decision": "approved"}]
    if invalid == "pending":
        request.status = "pending"
    elif invalid == "anonymous":
        request.responses = []
    elif invalid == "self":
        request.responses = [{"user_id": str(test_user.id), "decision": "approved"}]
    elif invalid == "inactive":
        colleague.is_active = False
    elif invalid == "permission":
        monkeypatch.setattr(desktop, "user_holds_permission", lambda *a: False)
    db_session.commit()
    await desktop.apply_control_decision(
        approval_id=str(request.id), account_id=str(test_user.account_id)
    )
    for view in control_rows["views"].values():
        db_session.refresh(view)
        assert view.mode == "view_only"
    desktop.broker.publish_json.assert_not_awaited()


def test_desktop_polling_routes_declare_control_request_schema() -> None:
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(desktop.router)
    schema = app.openapi()
    for path in (
        "/desktop/views/{view_id}",
        "/agents/{agent_id}/desktop-views/{view_id}",
    ):
        response = schema["paths"][path]["get"]["responses"]["200"]
        assert response["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/DesktopViewResponse"
        }
    assert (
        "control_request"
        in schema["components"]["schemas"]["DesktopViewResponse"]["properties"]
    )
