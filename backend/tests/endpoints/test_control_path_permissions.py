"""Permission consistency for account, MCP setup, and optimization controls."""

from datetime import UTC, datetime
from typing import Any, Callable
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.plugins import account_hooks
from preloop.schemas.gateway_usage import (
    RuntimeSessionOptimizationActionSpec,
    RuntimeSessionOptimizationApplyRequest,
)
from preloop.services.session_optimization import SessionOptimizationService
from preloop.utils import permissions


@pytest.fixture(autouse=True)
def oss_permissions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the OSS role checks with enforcement enabled."""
    from preloop.config import settings

    monkeypatch.setenv("DISABLE_RBAC", "false")
    monkeypatch.setattr(settings, "disable_rbac", False)
    monkeypatch.setattr(permissions, "_plugin_require_permission", None)
    monkeypatch.setattr(account_hooks, "get_authorizer", lambda: None)


def set_role(db: Session, user: models.User, name: str) -> None:
    """Replace the fixture's roles with one seeded system role."""
    for role in crud.crud_user_role.get_user_roles(db, user_id=user.id):
        crud.crud_user_role.remove_role(db, user_id=user.id, role_id=role.id)
    role = crud.crud_role.get_by_name(db, name=name)
    assert role is not None
    crud.crud_user_role.assign_role(db, user_id=user.id, role_id=role.id)


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_account_details_require_manage_account(
    client, db_session: Session, test_user: models.User, role: str
) -> None:
    set_role(db_session, test_user, role)
    account = crud.crud_account.get(db_session, id=test_user.account_id)
    before = (account.organization_name, account.default_runner_pool)
    response = client.patch(
        "/api/v1/account/details",
        json={
            "organization_name": "Example Organization",
            "default_runner_pool": "local",
        },
    )
    assert response.status_code == 403
    db_session.refresh(account)
    assert (account.organization_name, account.default_runner_pool) == before


@pytest.mark.parametrize("actor", ["owner", "admin", "primary", "superuser"])
def test_account_details_allow_account_managers(
    client, db_session: Session, test_user: models.User, actor: str
) -> None:
    set_role(db_session, test_user, actor if actor in {"owner", "admin"} else "viewer")
    if actor == "primary":
        account = crud.crud_account.get(db_session, id=test_user.account_id)
        crud.crud_account.update(
            db_session, db_obj=account, obj_in={"primary_user_id": test_user.id}
        )
    if actor == "superuser":
        crud.crud_user.update(
            db_session, db_obj=test_user, obj_in={"is_superuser": True}
        )
    response = client.patch(
        "/api/v1/account/details",
        json={
            "organization_name": "Example Organization",
            "default_runner_pool": "local",
        },
    )
    assert response.status_code == 200
    assert response.json()["organization_name"] == "Example Organization"
    assert response.json()["default_runner_pool"] == "local"


@pytest.mark.parametrize(
    "role,expected", [("viewer", 403), ("editor", 200), ("owner", 200)]
)
def test_mcp_oauth_token_requires_edit_permission(
    client, db_session: Session, test_user: models.User, role: str, expected: int
) -> None:
    set_role(db_session, test_user, role)
    server = crud.crud_mcp_server.create(
        db_session,
        obj_in={
            "name": "Example MCP",
            "url": "https://mcp.example.com/mcp",
            "transport": "http-streaming",
            "auth_type": "oauth",
            "account_id": test_user.account_id,
        },
    )
    response = client.post(f"/api/v1/mcp-servers/{server.id}/oauth/authorize-token")
    assert response.status_code == expected
    if expected == 200:
        import jwt
        from preloop.api.auth.jwt import ALGORITHM, SECRET_KEY

        claims = jwt.decode(
            response.json()["authorize_token"], SECRET_KEY, algorithms=[ALGORITHM]
        )
        assert claims["permission"] == "edit_mcp_servers"


@pytest.mark.parametrize("role,expected", [("editor", 403), ("owner", 201)])
def test_optimization_budget_requires_manage_budgets(
    client, db_session: Session, test_user: models.User, role: str, expected: int
) -> None:
    set_role(db_session, test_user, role)
    now = datetime.now(UTC)
    session = crud.crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="example-session",
        session_reference="example-session",
        started_at=now,
        last_activity_at=now,
    )
    request = {
        "suggestion_id": "example-budget",
        "suggestion_title": "Set a daily budget",
        "action": {
            "type": "set_budget",
            "params": {
                "subject_type": "account",
                "period": "daily",
                "hard_limit_usd": 10,
            },
        },
    }
    with patch.object(
        crud.crud_budget_policy, "create", wraps=crud.crud_budget_policy.create
    ) as create:
        response = client.post(
            f"/api/v1/billing/cost/runtime-sessions/{session.id}/optimizations/apply",
            json=request,
        )
        assert response.status_code == expected
        assert create.call_count == (1 if expected == 201 else 0)
    policies = crud.crud_budget_policy.get_policies_for_subject(
        db_session,
        account_id=test_user.account_id,
        subject_type="account",
        subject_id=None,
    )
    assert len(policies) == (1 if expected == 201 else 0)


@pytest.mark.parametrize(
    "action,permission",
    [
        ("scope_tools", "manage_tools"),
        ("disable_builtin_tools", "manage_tools"),
        ("enable_compression", "manage_policies"),
        ("cap_tool_results", "manage_policies"),
        ("set_budget", "manage_budgets"),
    ],
)
@pytest.mark.parametrize("edition", ["oss", "plugin"])
def test_optimization_service_checks_action_before_dispatch(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    permission: str,
    edition: str,
) -> None:
    set_role(db_session, test_user, "viewer")
    if edition == "plugin":

        def plugin_gate(name: str) -> Callable:
            def decorate(func: Callable) -> Callable:
                def checked(**kwargs: Any) -> None:
                    raise HTTPException(403, f"Required: {name}")

                return checked

            return decorate

        monkeypatch.setattr(permissions, "_plugin_require_permission", plugin_gate)
    account = crud.crud_account.get(db_session, id=test_user.account_id)
    request = RuntimeSessionOptimizationApplyRequest(
        suggestion_id="example",
        suggestion_title="Example suggestion",
        action=RuntimeSessionOptimizationActionSpec(type=action),
    )
    with patch.object(
        crud.crud_runtime_session, "get_account_session_summary"
    ) as summary:
        with pytest.raises(HTTPException) as exc:
            SessionOptimizationService(
                db_session
            ).apply_account_session_optimization_action(
                account=account,
                runtime_session_id="example",
                request=request,
                current_user=test_user,
            )
        assert exc.value.status_code == 403
        assert permission in exc.value.detail
        summary.assert_not_called()


@pytest.mark.parametrize("action", ["unknown", "open_events", "manage_output_filter"])
def test_optimization_rejects_unsupported_actions(
    client, db_session: Session, test_user: models.User, action: str
) -> None:
    response = client.post(
        "/api/v1/billing/cost/runtime-sessions/example/optimizations/apply",
        json={
            "suggestion_id": "example",
            "suggestion_title": "Example",
            "action": {"type": action},
        },
    )
    assert response.status_code == 400


def test_replay_requires_model_edit_permission(
    client, db_session: Session, test_user: models.User
) -> None:
    set_role(db_session, test_user, "viewer")
    with patch(
        "preloop.api.endpoints.session_optimization.run_session_replay"
    ) as replay:
        response = client.post(
            "/api/v1/billing/cost/runtime-sessions/example/replay",
            json={"consented": True},
        )
    assert response.status_code == 403
    replay.assert_not_called()


def test_optimization_apply_requires_declared_model_permission(
    client, db_session: Session, test_user: models.User
) -> None:
    set_role(db_session, test_user, "viewer")
    role = crud.crud_role.create(
        db_session,
        obj_in={
            "name": "budget_manager",
            "account_id": test_user.account_id,
            "is_system_role": False,
        },
    )
    permission = crud.crud_permission.get_by_name(db_session, name="manage_budgets")
    assert permission is not None
    crud.crud_role.assign_permission(
        db_session, role_id=role.id, permission_id=permission.id
    )
    crud.crud_user_role.assign_role(db_session, user_id=test_user.id, role_id=role.id)
    with patch.object(
        SessionOptimizationService, "apply_account_session_optimization_action"
    ) as apply:
        response = client.post(
            "/api/v1/billing/cost/runtime-sessions/example/optimizations/apply",
            json={
                "suggestion_id": "example",
                "suggestion_title": "Example",
                "action": {"type": "set_budget"},
            },
        )
    assert response.status_code == 403
    assert "edit_ai_models" in response.json()["detail"]
    apply.assert_not_called()


def test_mcp_oauth_continuation_requires_permission_claim(
    client, db_session: Session, test_user: models.User
) -> None:
    from preloop.api.auth.jwt import create_access_token

    server = crud.crud_mcp_server.create(
        db_session,
        obj_in={
            "name": "Example MCP",
            "url": "https://mcp.example.com/mcp",
            "transport": "http-streaming",
            "auth_type": "oauth",
            "account_id": test_user.account_id,
        },
    )
    token = create_access_token(
        {
            "sub": str(test_user.id),
            "purpose": "mcp_oauth_authorize",
            "server_id": str(server.id),
        }
    )
    with patch("preloop.api.endpoints.mcp_servers.httpx.AsyncClient") as outbound:
        response = client.get(
            f"/api/v1/mcp-servers/{server.id}/oauth/authorize",
            params={"code": token},
            follow_redirects=False,
        )
    assert response.status_code == 401
    outbound.assert_not_called()


def test_mcp_oauth_continuation_preserves_state_and_pkce(
    client,
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib.parse import parse_qs, urlparse

    import httpx
    from preloop.api.endpoints import mcp_servers

    set_role(db_session, test_user, "editor")
    server = crud.crud_mcp_server.create(
        db_session,
        obj_in={
            "name": "Example MCP",
            "url": "https://mcp.example.com/mcp",
            "transport": "http-streaming",
            "auth_type": "oauth",
            "account_id": test_user.account_id,
            "auth_config": {"client_id": "example-client"},
        },
    )
    token = client.post(
        f"/api/v1/mcp-servers/{server.id}/oauth/authorize-token"
    ).json()["authorize_token"]
    monkeypatch.setattr(mcp_servers, "_pending_oauth_states", {})
    monkeypatch.setattr(mcp_servers, "get_db_session", lambda: iter([db_session]))
    # RFC 7636 Appendix B gives an independent expected S256 challenge.
    expected_verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    expected_challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    scan = AsyncMock(return_value=[])
    monkeypatch.setattr(mcp_servers, "scan_mcp_server_tools", scan)
    outbound = AsyncMock()
    outbound.get.return_value = httpx.Response(
        200,
        json={
            "authorization_endpoint": "https://mcp.example.com/authorize",
            "token_endpoint": "https://mcp.example.com/token",
        },
    )
    outbound.post.return_value = httpx.Response(
        200,
        json={
            "access_token": "example-access",
            "refresh_token": "example-refresh",
        },
    )
    with patch.object(mcp_servers.httpx, "AsyncClient") as factory:
        factory.return_value.__aenter__.return_value = outbound
        with patch.object(
            mcp_servers.secrets,
            "token_urlsafe",
            side_effect=[expected_verifier, "example-oauth-state"],
        ):
            response = client.get(
                f"/api/v1/mcp-servers/{server.id}/oauth/authorize",
                params={"code": token},
                follow_redirects=False,
            )
        assert response.status_code == 302
        params = parse_qs(urlparse(response.headers["location"]).query)
        state = params["state"][0]
        pending = mcp_servers._pending_oauth_states[state]
        verifier = pending["code_verifier"]
        assert verifier == expected_verifier
        assert params["code_challenge"] == [expected_challenge]
        assert params["code_challenge_method"] == ["S256"]
        assert pending["permission"] == "edit_mcp_servers"
        assert pending["account_id"] == str(test_user.account_id)
        with patch.object(db_session, "close"):
            response = client.get(
                "/api/v1/mcp-servers/oauth/callback",
                params={"code": "provider-code", "state": state},
                follow_redirects=False,
            )
        assert response.status_code == 302
        assert response.headers["location"].endswith("#setup_mcp=success")
        assert outbound.post.call_args.kwargs["data"]["code_verifier"] == verifier
        assert state not in mcp_servers._pending_oauth_states
        scan.assert_awaited_once()
        response = client.get(
            "/api/v1/mcp-servers/oauth/callback",
            params={"code": "provider-code", "state": state},
        )
        assert response.status_code == 400
    db_session.refresh(server)
    assert server.auth_config["access_token"] == "example-access"


def test_mcp_oauth_callback_requires_gated_state(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.api.endpoints import mcp_servers

    monkeypatch.setattr(
        mcp_servers,
        "_pending_oauth_states",
        {
            "example-state": {
                "server_id": "example-server",
                "account_id": "example-account",
                "token_endpoint": "https://mcp.example.com/token",
                "client_id": "example-client",
                "code_verifier": "example-verifier",
                "callback_url": "https://example.com/callback",
            }
        },
    )
    with patch.object(mcp_servers.httpx, "AsyncClient") as outbound:
        response = client.get(
            "/api/v1/mcp-servers/oauth/callback",
            params={"code": "example", "state": "example-state"},
            follow_redirects=False,
        )
    assert response.status_code == 400
    outbound.assert_not_called()


def test_mcp_oauth_token_requires_account_server(
    client,
    db_session: Session,
    test_user: models.User,
) -> None:
    other_account = crud.crud_account.create(
        db_session, obj_in={"organization_name": "Example Organization"}
    )
    server = crud.crud_mcp_server.create(
        db_session,
        obj_in={
            "name": "Example MCP",
            "url": "https://mcp.example.com/mcp",
            "transport": "http-streaming",
            "auth_type": "oauth",
            "account_id": other_account.id,
        },
    )
    with patch("preloop.api.auth.jwt.create_access_token") as mint:
        response = client.post(f"/api/v1/mcp-servers/{server.id}/oauth/authorize-token")
    assert response.status_code == 404
    mint.assert_not_called()
