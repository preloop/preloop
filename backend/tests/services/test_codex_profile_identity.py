"""The server-owned runtime token carries the flow's tool profile."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from pytest_mock import MockerFixture

from preloop.services.dynamic_fastmcp import create_user_context_from_scope
from preloop.services.flow_runtime_token import create_flow_runtime_token


@pytest.mark.parametrize("profile", [None, "default", "lean"])
def test_runtime_token_profile(profile: str | None, mocker: MockerFixture) -> None:
    """Omitted/default tokens stay unchanged; lean survives authenticated scope."""
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

    account_id = UUID("00000000-0000-0000-0000-000000000001")
    user = SimpleNamespace(
        id=account_id, account_id=account_id, username="fixture", is_active=True
    )
    account = SimpleNamespace(id=account_id, primary_user_id=user.id)
    user.account = account
    flow = SimpleNamespace(
        id=account_id,
        account_id=account_id,
        name="Fixture flow",
        ai_model_id=None,
        agent_config={} if profile is None else {"codex_tools": profile},
        git_clone_config={},
        trigger_config={},
        allowed_mcp_tools=[{"name": "get_issue"}],
        allowed_mcp_servers=[],
    )
    mocker.patch(
        "preloop.services.flow_runtime_token.crud_account.get", return_value=account
    )
    mocker.patch("preloop.services.flow_runtime_token.crud_user.get", return_value=user)
    mocker.patch(
        "preloop.services.flow_runtime_token.execution_model_id", return_value=None
    )
    create = mocker.patch(
        "preloop.services.flow_runtime_token.crud_api_key.create_runtime_key",
        return_value=(SimpleNamespace(id=account_id), "fixture-token"),
    )
    assert create_flow_runtime_token(
        MagicMock(), flow=flow, execution_id=account_id
    ) == ("fixture-token", account_id)
    data = create.call_args.kwargs["context_data"]
    assert ("codex_tools" in data) is (profile == "lean")
    authenticated = MagicMock(spec=AuthenticatedUser)
    authenticated.access_token = MagicMock()
    authenticated.access_token.user = user
    authenticated.access_token.api_key = SimpleNamespace(
        id=account_id,
        name="fixture",
        context_data=data,
        scopes=["mcp:read", "mcp:write"],
    )
    mocker.patch(
        "preloop.services.dynamic_fastmcp.get_db",
        side_effect=lambda: iter([MagicMock()]),
    )
    mocker.patch("preloop.services.dynamic_fastmcp.has_tracker", return_value=True)
    mocker.patch(
        "preloop.services.dynamic_fastmcp.get_tracker_types", return_value=["github"]
    )
    context = create_user_context_from_scope({"user": authenticated})
    assert context is not None
    assert context.codex_tools == (profile or "default")
    assert context.allowed_flow_tools == ["get_issue"]
