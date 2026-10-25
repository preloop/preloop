"""Permission consistency on approval, policy, tool and MCP configuration routes."""

import asyncio
import inspect
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pytest_mock import MockerFixture
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.auth.ci import get_current_actor
from preloop.api.common import get_account_for_user
from preloop.api.endpoints import (
    approval_requests,
    audit_chain,
    event_webhooks,
    mcp_servers,
    policies,
    retention,
    tools,
)
from preloop.config import settings
from preloop.models import crud, models
from preloop.models.db.session import get_db_session
from preloop.plugins import account_hooks
from preloop.utils import permissions


@dataclass(frozen=True)
class RouteCase:
    """One mounted route and its first downstream operation."""

    module: Any
    handler: str
    method: str
    path: str
    permission: str
    downstream: str
    body: dict[str, Any] | None = None


ROUTES = [
    RouteCase(
        approval_requests,
        "get_approval_request_stats",
        "GET",
        "/api/v1/approval-requests/stats",
        "view_approvals",
        "crud_approval_request.expire_stale_pending",
        None,
    ),
    RouteCase(
        approval_requests,
        "list_approval_requests",
        "GET",
        "/api/v1/approval-requests",
        "view_approvals",
        "crud_approval_request.expire_stale_pending",
        None,
    ),
    RouteCase(
        approval_requests,
        "get_approval_original_args",
        "GET",
        "/api/v1/approval-requests/00000000-0000-0000-0000-000000000001/original-args",
        "decide_approvals",
        "crud_approval_request.get",
        None,
    ),
    RouteCase(
        approval_requests,
        "approve_request",
        "POST",
        "/api/v1/approval-requests/00000000-0000-0000-0000-000000000001/approve",
        "decide_approvals",
        "get_async_db_session",
        {"approved": True},
    ),
    RouteCase(
        approval_requests,
        "decline_request",
        "POST",
        "/api/v1/approval-requests/00000000-0000-0000-0000-000000000001/decline",
        "decide_approvals",
        "get_async_db_session",
        {"approved": False},
    ),
    RouteCase(
        approval_requests,
        "decide_request",
        "POST",
        "/api/v1/approval-requests/00000000-0000-0000-0000-000000000001/decide",
        "decide_approvals",
        "get_async_db_session",
        {"approved": True},
    ),
    RouteCase(
        approval_requests,
        "decide_requests_batch",
        "POST",
        "/api/v1/approval-requests/decide-batch",
        "decide_approvals",
        "ApprovalService",
        {"ids": ["00000000-0000-0000-0000-000000000001"], "approved": True},
    ),
    RouteCase(
        audit_chain,
        "rotate_signing_key",
        "POST",
        "/api/v1/signing/keys/rotate",
        "manage_policies",
        "record_signing.rotate_key",
        None,
    ),
    RouteCase(
        event_webhooks,
        "create_webhook_endpoint",
        "POST",
        "/api/v1/event-webhooks/endpoints",
        "manage_policies",
        "_reject_blocked_target",
        {"url": "https://example.com/hook"},
    ),
    RouteCase(
        event_webhooks,
        "update_webhook_endpoint",
        "PATCH",
        "/api/v1/event-webhooks/endpoints/00000000-0000-0000-0000-000000000001",
        "manage_policies",
        "_get_owned",
        {"description": "Updated"},
    ),
    RouteCase(
        event_webhooks,
        "delete_webhook_endpoint",
        "DELETE",
        "/api/v1/event-webhooks/endpoints/00000000-0000-0000-0000-000000000001",
        "manage_policies",
        "_get_owned",
        None,
    ),
    RouteCase(
        event_webhooks,
        "rotate_webhook_endpoint_secret",
        "POST",
        "/api/v1/event-webhooks/endpoints/00000000-0000-0000-0000-000000000001/secret/rotate",
        "manage_policies",
        "crud.crud_ci_subscription.rotate_human_secret",
        None,
    ),
    RouteCase(
        event_webhooks,
        "test_webhook_endpoint",
        "POST",
        "/api/v1/event-webhooks/endpoints/00000000-0000-0000-0000-000000000001/test",
        "manage_policies",
        "_get_owned",
        None,
    ),
    RouteCase(
        event_webhooks,
        "replay_webhook_event",
        "POST",
        "/api/v1/event-webhooks/deliveries/00000000-0000-0000-0000-000000000001/replay",
        "manage_policies",
        "outbox.replay_event",
        None,
    ),
    RouteCase(
        mcp_servers,
        "create_mcp_server",
        "POST",
        "/api/v1/mcp-servers",
        "create_mcp_servers",
        "crud_mcp_server.get_by_name",
        {"name": "Example", "url": "https://example.com/mcp"},
    ),
    RouteCase(
        mcp_servers,
        "update_mcp_server",
        "PUT",
        "/api/v1/mcp-servers/00000000-0000-0000-0000-000000000001",
        "edit_mcp_servers",
        "crud_mcp_server.get",
        {"name": "Updated"},
    ),
    RouteCase(
        mcp_servers,
        "delete_mcp_server",
        "DELETE",
        "/api/v1/mcp-servers/00000000-0000-0000-0000-000000000001",
        "delete_mcp_servers",
        "crud_mcp_server.get",
        None,
    ),
    RouteCase(
        mcp_servers,
        "scan_mcp_server",
        "POST",
        "/api/v1/mcp-servers/00000000-0000-0000-0000-000000000001/scan",
        "manage_mcp_servers",
        "crud_mcp_server.get",
        None,
    ),
    RouteCase(
        policies,
        "create_model_io_rule",
        "POST",
        "/api/v1/policies/model-io-rules",
        "manage_policies",
        "upsert_model_io_rule",
        {
            "id": "notice",
            "target": "model.request",
            "conditions": [
                {"expression": "true", "condition_type": "cel", "action": "notify"}
            ],
        },
    ),
    RouteCase(
        policies,
        "update_model_io_rule",
        "PUT",
        "/api/v1/policies/model-io-rules/00000000-0000-0000-0000-000000000001",
        "manage_policies",
        "load_model_io_rules",
        {
            "id": "notice",
            "target": "model.request",
            "conditions": [
                {"expression": "true", "condition_type": "cel", "action": "notify"}
            ],
        },
    ),
    RouteCase(
        policies,
        "patch_model_io_rule",
        "PATCH",
        "/api/v1/policies/model-io-rules/00000000-0000-0000-0000-000000000001",
        "manage_policies",
        "load_model_io_rules",
        {"enabled": False},
    ),
    RouteCase(
        policies,
        "remove_model_io_rule",
        "DELETE",
        "/api/v1/policies/model-io-rules/00000000-0000-0000-0000-000000000001",
        "manage_policies",
        "delete_model_io_rule",
        None,
    ),
    RouteCase(
        policies,
        "create_policy_version",
        "POST",
        "/api/v1/policies/versions",
        "manage_policies",
        "PolicyVersionService",
        {},
    ),
    RouteCase(
        policies,
        "update_version_tag",
        "PUT",
        "/api/v1/policies/versions/00000000-0000-0000-0000-000000000001/tag",
        "manage_policies",
        "PolicyVersionService",
        {"tag": "example"},
    ),
    RouteCase(
        policies,
        "remove_version_tag",
        "DELETE",
        "/api/v1/policies/versions/00000000-0000-0000-0000-000000000001/tag",
        "manage_policies",
        "PolicyVersionService",
        None,
    ),
    RouteCase(
        policies,
        "rollback_to_version",
        "POST",
        "/api/v1/policies/versions/00000000-0000-0000-0000-000000000001/rollback",
        "manage_policies",
        "PolicyVersionService",
        {},
    ),
    RouteCase(
        policies,
        "delete_policy_version",
        "DELETE",
        "/api/v1/policies/versions/00000000-0000-0000-0000-000000000001",
        "manage_policies",
        "PolicyVersionService",
        None,
    ),
    RouteCase(
        policies,
        "prune_policy_versions",
        "POST",
        "/api/v1/policies/versions/prune",
        "manage_policies",
        "PolicyVersionService",
        {},
    ),
    RouteCase(
        retention,
        "update_retention_settings",
        "PUT",
        "/api/v1/retention/settings",
        "manage_policies",
        "retention_policy.set_retention",
        {"classes": {"audit": 365}},
    ),
    RouteCase(
        retention,
        "create_legal_hold",
        "POST",
        "/api/v1/retention/holds",
        "manage_policies",
        "place_hold",
        {
            "resource_type": "runtime_session",
            "resource_id": "example-session",
            "reason": "Example retention reason",
        },
    ),
    RouteCase(
        retention,
        "release_legal_hold",
        "POST",
        "/api/v1/retention/holds/00000000-0000-0000-0000-000000000001/release",
        "manage_policies",
        "release_hold",
        {"reason": "Example release reason"},
    ),
    RouteCase(
        tools,
        "create_tool_configuration",
        "POST",
        "/api/v1/tool-configurations",
        "manage_tools",
        "crud_tool_configuration.get_multi_by_account",
        {
            "tool_name": "get_issue",
            "account_id": "00000000-0000-0000-0000-000000000002",
        },
    ),
    RouteCase(
        tools,
        "update_tool_configuration",
        "PUT",
        "/api/v1/tool-configurations/00000000-0000-0000-0000-000000000001",
        "manage_tools",
        "crud_tool_configuration.get",
        {"is_enabled": False},
    ),
    RouteCase(
        tools,
        "update_tool_approval_condition",
        "PUT",
        "/api/v1/tool-configurations/00000000-0000-0000-0000-000000000001/condition",
        "manage_tools",
        "crud_tool_configuration.get",
        {"approval_condition": "true"},
    ),
    RouteCase(
        tools,
        "delete_tool_configuration",
        "DELETE",
        "/api/v1/tool-configurations/00000000-0000-0000-0000-000000000001",
        "manage_tools",
        "crud_tool_configuration.get",
        None,
    ),
    RouteCase(
        tools,
        "create_or_update_tool_approval_condition",
        "PUT",
        "/api/v1/tool-configurations/00000000-0000-0000-0000-000000000001/approval-condition",
        "manage_tools",
        "crud_tool_configuration.get",
        {"tool_configuration_id": "00000000-0000-0000-0000-000000000001"},
    ),
    RouteCase(
        tools,
        "delete_tool_approval_condition",
        "DELETE",
        "/api/v1/tool-configurations/00000000-0000-0000-0000-000000000001/approval-condition",
        "manage_tools",
        "crud_tool_configuration.get",
        None,
    ),
    RouteCase(
        tools,
        "create_access_rule",
        "POST",
        "/api/v1/tool-configurations/00000000-0000-0000-0000-000000000001/access-rules",
        "manage_tools",
        "crud_tool_configuration.get",
        {"action": "deny"},
    ),
    RouteCase(
        tools,
        "update_access_rule",
        "PUT",
        "/api/v1/access-rules/00000000-0000-0000-0000-000000000001",
        "manage_tools",
        "crud_tool_access_rule.get",
        {"action": "deny"},
    ),
    RouteCase(
        tools,
        "delete_access_rule",
        "DELETE",
        "/api/v1/access-rules/00000000-0000-0000-0000-000000000001",
        "manage_tools",
        "crud_tool_access_rule.get",
        None,
    ),
]


@pytest.fixture(autouse=True)
def oss_permissions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the real fallback with checks enabled and no extension hooks."""
    monkeypatch.setenv("DISABLE_RBAC", "false")
    monkeypatch.setattr(settings, "disable_rbac", False)
    monkeypatch.setattr(permissions, "_plugin_require_permission", None)
    monkeypatch.setattr(account_hooks, "_authorizer", None)


@pytest.fixture
def permission_actor(db_session: Session, test_user: models.User) -> models.User:
    """A second authenticated user with no roles in the current account."""
    actor = models.User(
        id=uuid4(),
        account_id=test_user.account_id,
        email="member@example.com",
        username=f"member-{uuid4().hex}",
        hashed_password="unused",
        is_active=True,
        is_superuser=False,
    )
    db_session.add(actor)
    db_session.flush()
    return actor


def _grant_permission(
    db: Session, user: models.User, permission_name: str, *, foreign: bool = False
) -> None:
    """Grant exactly the requested permission through an account role."""
    account_id = user.account_id
    if foreign:
        account = models.Account(organization_name="Example organization")
        db.add(account)
        db.flush()
        account_id = account.id
    permission = crud.crud_permission.get_by_name(db, name=permission_name)
    assert permission is not None
    role = models.Role(
        name=f"example-role-{uuid4().hex}",
        account_id=account_id,
        is_system_role=False,
    )
    db.add(role)
    db.flush()
    db.add_all(
        [
            models.RolePermission(role_id=role.id, permission_id=permission.id),
            models.UserRole(user_id=user.id, role_id=role.id),
        ]
    )
    db.flush()


@pytest.fixture
def route_client(
    db_session: Session, permission_actor: models.User
) -> Iterator[TestClient]:
    """Serve the real routers with authenticated user and session dependencies."""
    app = FastAPI()
    for module in (
        approval_requests,
        audit_chain,
        event_webhooks,
        mcp_servers,
        policies,
        retention,
        tools,
    ):
        app.include_router(module.router, prefix="/api/v1")
    app.include_router(audit_chain.signing_router, prefix="/api/v1")
    app.dependency_overrides[get_current_active_user] = lambda: permission_actor
    app.dependency_overrides[get_current_actor] = lambda: permission_actor
    app.dependency_overrides[get_db_session] = lambda: db_session
    account = crud.crud_account.get(db_session, id=permission_actor.account_id)
    app.dependency_overrides[get_account_for_user] = lambda: account
    app.dependency_overrides[event_webhooks._account_for_actor] = lambda: account
    app.dependency_overrides[approval_requests._async_db_session] = lambda: MagicMock()
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("case", ROUTES, ids=lambda case: case.handler)
@pytest.mark.parametrize("grant", ["none", "foreign", "holder", "primary", "superuser"])
def test_route_permission_boundary(
    case: RouteCase,
    grant: str,
    permission_actor: models.User,
    db_session: Session,
    route_client: TestClient,
    mocker: MockerFixture,
) -> None:
    """Deny non-holders before work; authorized users reach the handler operation.

    Downstream work stops at a marker response so this test isolates the
    permission boundary from each service's existing behavior tests.
    """
    if grant in {"holder", "foreign"}:
        _grant_permission(
            db_session, permission_actor, case.permission, foreign=grant == "foreign"
        )
    elif grant == "primary":
        account = crud.crud_account.get(db_session, id=permission_actor.account_id)
        account.primary_user_id = permission_actor.id
        db_session.flush()
    elif grant == "superuser":
        permission_actor.is_superuser = True
        db_session.flush()

    target: Any = case.module
    parts = case.downstream.split(".")
    for part in parts[:-1]:
        target = getattr(target, part)
    downstream = mocker.patch.object(
        target, parts[-1], side_effect=HTTPException(409, "Handler operation reached")
    )
    writes = [mocker.spy(db_session, name) for name in ("add", "delete", "commit")]
    response = route_client.request(case.method, case.path, json=case.body)

    if grant in {"none", "foreign"}:
        assert response.status_code == 403, response.text
        assert case.permission in response.json()["detail"]
        downstream.assert_not_called()
        for write in writes:
            write.assert_not_called()
    else:
        downstream.assert_called_once()
        # Some handlers translate downstream service exceptions to 500.
        assert response.status_code in {409, 500}, response.text


@pytest.mark.parametrize("bound_owner", [False, True])
def test_single_decision_preserves_private_owner_consent(
    bound_owner: bool,
    permission_actor: models.User,
    db_session: Session,
    mocker: MockerFixture,
) -> None:
    """Only a bound human owner may use the private-consent exception."""
    request_id = uuid4()
    belongs = mocker.patch(
        "preloop.models.crud.owner_consent.belongs_to_owner", return_value=bound_owner
    )
    kwargs = dict(request_id=request_id, current_user=permission_actor, db=db_session)
    if bound_owner:
        approval_requests._check_approval_permission("decide_approvals", **kwargs)
    else:
        with pytest.raises(HTTPException) as denied:
            approval_requests._check_approval_permission("decide_approvals", **kwargs)
        assert denied.value.status_code == 403
    belongs.assert_called_once_with(
        db_session,
        approval_id=request_id,
        account_id=permission_actor.account_id,
        owner_user_id=permission_actor.id,
    )


def test_batch_requires_global_permission_even_for_private_owner(
    permission_actor: models.User,
    db_session: Session,
    route_client: TestClient,
    mocker: MockerFixture,
) -> None:
    """The batch dependency never uses the single-resource owner exception."""
    belongs = mocker.patch(
        "preloop.models.crud.owner_consent.belongs_to_owner", return_value=True
    )
    service = mocker.patch.object(approval_requests, "ApprovalService")
    response = route_client.post(
        "/api/v1/approval-requests/decide-batch",
        json={"ids": [str(UUID(int=1))], "approved": True},
    )
    assert response.status_code == 403
    belongs.assert_not_called()
    service.assert_not_called()


@pytest.mark.parametrize(
    "case",
    [
        case
        for case in ROUTES
        if inspect.iscoroutinefunction(getattr(case.module, case.handler))
    ],
    ids=lambda case: case.handler,
)
def test_async_route_permission_check_runs_off_event_loop(
    case: RouteCase,
    route_client: TestClient,
    mocker: MockerFixture,
) -> None:
    """Synchronous permission queries must not block the request event loop."""
    real_holds_permission = permissions.user_holds_permission
    query_on_loop: list[bool] = []

    def holds_permission(
        db: Session, current_user: models.User, permission_name: str
    ) -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            query_on_loop.append(False)
        else:
            query_on_loop.append(True)
        return real_holds_permission(db, current_user, permission_name)

    checked = mocker.patch.object(
        permissions, "user_holds_permission", side_effect=holds_permission
    )
    response = route_client.request(case.method, case.path, json=case.body)

    assert response.status_code == 403, response.text
    checked.assert_called_once()
    assert query_on_loop == [False]


def test_approval_read_checks_enforce_view_permission_in_oss(
    db_session: Session, permission_actor: models.User
) -> None:
    """Approval reads use the same OSS fallback as decisions."""
    with pytest.raises(HTTPException) as denied:
        approval_requests._check_approval_permission(
            "view_approvals",
            request_id=uuid4(),
            current_user=permission_actor,
            db=db_session,
        )
    assert denied.value.status_code == 403
    _grant_permission(db_session, permission_actor, "view_approvals")
    approval_requests._check_approval_permission(
        "view_approvals",
        request_id=uuid4(),
        current_user=permission_actor,
        db=db_session,
    )
