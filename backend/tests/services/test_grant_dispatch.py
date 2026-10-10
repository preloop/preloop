"""Grant firewall and immutable upstream dispatch identity."""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from preloop.services import dynamic_fastmcp as gateway
from preloop.services.grant_introspection import GrantResult


CONFIG = {
    "endpoint": "https://issuer.test/introspect",
    "client_id": "gateway",
    "client_secret": "synthetic-secret",
    "required_scopes": ["read"],
}
BINDING = {
    "available": True,
    "active": True,
    "scope": ["read"],
    "sub": "subject",
    "client_id": "client",
    "exp": None,
    "consent_ref": "consent",
    "cached": False,
}


Runtime = tuple[
    gateway.DynamicFastMCP,
    gateway.UserContext,
    AsyncMock,
    MagicMock,
    MagicMock,
    Callable[..., Any],
]


def server() -> SimpleNamespace:
    return SimpleNamespace(
        id="server-a",
        name="First owner",
        url="https://upstream.test/mcp",
        transport="streamable-http",
        auth_type="bearer",
        tool_prefix="first",
        auth_config={"token": "synthetic-bearer", "introspection": deepcopy(CONFIG)},
    )


@pytest.mark.asyncio
async def test_snapshot_releases_db_before_introspection_and_copies_credentials() -> (
    None
):
    row = server()
    db = MagicMock()
    introspect = AsyncMock(return_value=GrantResult(deepcopy(BINDING)))

    async def evaluate(*args: Any, **kwargs: Any) -> GrantResult:
        db.close.assert_called_once()
        return await introspect(*args, **kwargs)

    with (
        patch.object(gateway, "get_db", return_value=iter([db])),
        patch.object(
            gateway, "_resolve_proxied_tool_server", return_value=row
        ) as resolve,
        patch.object(gateway.grant_introspector, "evaluate", side_effect=evaluate),
    ):
        snapshot, grant = await gateway._prepare_grant_dispatch("account", "first_read")
    resolve.assert_called_once_with(db, "account", "first_read")
    introspect.assert_awaited_once()
    assert introspect.await_args.args[0] == "synthetic-bearer"
    assert introspect.await_args.kwargs == {"server_id": "server-a"}
    row.auth_config["token"] = "rotated"
    row.auth_config["introspection"]["required_scopes"].append("admin")
    assert snapshot.client_config["auth_config"]["token"] == "synthetic-bearer"
    assert snapshot.client_config["auth_config"]["introspection"][
        "required_scopes"
    ] == ["read"]
    assert snapshot.upstream_name == "read"
    assert grant.binding == BINDING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_type,key", [("bearer", "token"), ("oauth", "access_token")]
)
async def test_introspection_uses_exact_forwarded_auth_token(
    auth_type: str, key: str
) -> None:
    row = server()
    row.auth_type = auth_type
    row.auth_config = {
        key: "actual-forwarded",
        "token" if key != "token" else "access_token": "other",
        "introspection": CONFIG,
    }
    db = MagicMock()
    with (
        patch.object(gateway, "get_db", return_value=iter([db])),
        patch.object(gateway, "_resolve_proxied_tool_server", return_value=row),
        patch.object(
            gateway.grant_introspector, "evaluate", new_callable=AsyncMock
        ) as evaluate,
    ):
        evaluate.return_value = GrantResult(deepcopy(BINDING))
        await gateway._prepare_grant_dispatch("account", "first_read")
    assert evaluate.await_args.args[0] == "actual-forwarded"


@pytest.mark.asyncio
async def test_no_introspection_config_keeps_existing_server_behavior() -> None:
    row = server()
    row.auth_config.pop("introspection")
    with (
        patch.object(gateway, "get_db", return_value=iter([MagicMock()])),
        patch.object(gateway, "_resolve_proxied_tool_server", return_value=row),
        patch.object(
            gateway.grant_introspector, "evaluate", new_callable=AsyncMock
        ) as evaluate,
    ):
        snapshot, grant = await gateway._prepare_grant_dispatch("account", "first_read")
    assert snapshot.upstream_name == "read"
    assert grant is None
    evaluate.assert_not_awaited()


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> Runtime:
    """Isolate gateway guards while exercising actual grant/policy/dispatch wiring."""
    from uuid import uuid4
    from fastmcp.tools import Tool
    from preloop.services.dynamic_mcp_server import UserContext
    from preloop.services.policy.schema import SensitiveDataConfig
    from preloop.services.sensitive_data.storage import prime_cache

    user = UserContext(
        user_id=str(uuid4()),
        account_id=str(uuid4()),
        username="caller",
        has_tracker=True,
        enabled_default_tools=[],
        enabled_proxied_tools=[],
    )
    mcp = gateway.DynamicFastMCP("grant-test")
    mcp.set_user_context_provider(lambda: user)
    mcp._proxied_tool_servers["first_read"] = "server-a"
    mcp._proxied_tool_server_names["first_read"] = "stale-list-name"
    monkeypatch.setattr(
        mcp,
        "list_tools",
        AsyncMock(return_value=[Tool(name="first_read", parameters={})]),
    )
    monkeypatch.setattr(gateway, "get_db", lambda: iter([MagicMock()]))
    monkeypatch.setattr(gateway.kill_switch_service, "tools_halted", lambda *a: False)
    monkeypatch.setattr(
        gateway.crud_tool_configuration, "get_multi_by_account", lambda *a, **k: []
    )
    monkeypatch.setattr(gateway, "_load_sensitive_data_policy", lambda *a: (None, None))
    prime_cache(user.account_id, SensitiveDataConfig())
    monkeypatch.setattr(mcp, "_persist_tool_call_activity", MagicMock())
    monkeypatch.setattr(mcp, "_halt_dispatch_denial", AsyncMock(return_value=None))
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=MagicMock())
    session.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "preloop.models.db.session.get_async_db_session", lambda: session
    )
    policy = AsyncMock(return_value=("allow", None, None))
    monkeypatch.setattr(
        "preloop.services.policy_evaluator.evaluate_policy_async", policy
    )
    deny_audit = MagicMock()
    monkeypatch.setattr(
        "preloop.services.policy_evaluator._log_policy_decision_async", deny_audit
    )
    audit = MagicMock()
    monkeypatch.setattr(
        "preloop.plugins.base.get_plugin_manager",
        lambda: SimpleNamespace(get_service=lambda *a: audit),
    )
    wrapper = mcp._create_proxied_tool_wrapper(
        "first_read", user.account_id, "Read", {"properties": {}}
    )
    mcp.tool()(wrapper)
    mcp._registered_proxied_tools.add(
        f"account_{user.account_id.replace('-', '_')}_first_read"
    )
    return mcp, user, policy, deny_audit, audit, wrapper


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["grant_inactive", "scope_not_granted", "introspection_unavailable"]
)
async def test_central_hard_denial_before_policy_approval_or_upstream(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    mcp, user, policy, deny_audit, audit, _ = runtime
    row = server()
    monkeypatch.setattr(gateway, "_resolve_proxied_tool_server", lambda *a: row)
    monkeypatch.setattr(
        gateway.grant_introspector,
        "evaluate",
        AsyncMock(return_value=GrantResult(deepcopy(BINDING), reason)),
    )
    pool = MagicMock(get_client=AsyncMock())
    monkeypatch.setattr(gateway, "get_mcp_client_pool", lambda: pool)
    approval = AsyncMock()
    monkeypatch.setattr("preloop.services.approval_helper.require_approval", approval)
    result = await mcp.call_tool("first_read", {})
    assert result.is_error and reason in result.content[0].text
    policy.assert_not_awaited()
    approval.assert_not_awaited()
    pool.get_client.assert_not_awaited()
    assert deny_audit.call_args.kwargs["extra_details"]["grant"] == BINDING
    assert deny_audit.call_args.kwargs["rule_description"] == reason
    assert audit.log_tool_call_async.call_args.kwargs["grant"] == BINDING
    assert gateway._grant_dispatch_var.get() is None


@pytest.mark.asyncio
async def test_policy_and_approval_share_exact_prefix_owner_and_token_snapshot(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, user, policy, _, audit, wrapper = runtime
    row = server()
    resolve = MagicMock(return_value=row)
    monkeypatch.setattr(gateway, "_resolve_proxied_tool_server", resolve)
    monkeypatch.setattr(
        gateway.grant_introspector,
        "evaluate",
        AsyncMock(return_value=GrantResult(deepcopy(BINDING))),
    )
    pool = MagicMock(get_client=AsyncMock())
    client = MagicMock(call_tool=AsyncMock(return_value=[]))
    pool.get_client.return_value = client
    monkeypatch.setitem(wrapper.__globals__, "get_mcp_client_pool", lambda: pool)
    monkeypatch.setattr(gateway, "get_mcp_client_pool", lambda: pool)

    async def approve(**kwargs: Any) -> tuple[bool, str]:
        assert kwargs["server_name"] == "First owner"
        row.id = "replacement-owner"
        row.auth_config["token"] = "rotated-token"
        row.tool_prefix = "replacement"
        return True, ""

    monkeypatch.setattr("preloop.services.approval_helper.require_approval", approve)
    await mcp.call_tool("first_read", {})
    resolve.assert_called_once()
    assert policy.await_args.kwargs["extra_bindings"]["grant"] == BINDING
    assert policy.await_args.kwargs["server_name"] == "First owner"
    assert policy.await_args.kwargs["extra_details"]["grant"] == BINDING
    assert pool.get_client.await_args.kwargs["server_id"] == "server-a"
    assert (
        pool.get_client.await_args.kwargs["auth_config"]["token"] == "synthetic-bearer"
    )
    client.call_tool.assert_awaited_once_with("read", {})
    assert audit.log_tool_call_async.call_args.kwargs["grant"] == BINDING
    assert gateway._grant_dispatch_var.get() is None


@pytest.mark.asyncio
async def test_direct_wrapper_and_async_approval_replay_do_not_bypass_grant(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, user, policy, deny_audit, _, wrapper = runtime
    monkeypatch.setattr(gateway, "_resolve_proxied_tool_server", lambda *a: server())
    evaluate = AsyncMock(return_value=GrantResult(deepcopy(BINDING), "grant_inactive"))
    monkeypatch.setattr(gateway.grant_introspector, "evaluate", evaluate)
    approval = AsyncMock()
    monkeypatch.setattr("preloop.services.approval_helper.require_approval", approval)
    # Direct generated wrapper invocation and durable approval replay both gate.
    result = await wrapper()
    assert result.is_error and "grant_inactive" in result.content[0].text
    internal = f"account_{user.account_id.replace('-', '_')}_first_read"
    result = await mcp.call_registered_tool_without_policy(
        internal, {}, account_id=user.account_id
    )
    assert result.is_error and "grant_inactive" in result.content[0].text
    assert evaluate.await_count == 2
    assert deny_audit.call_count == 2
    policy.assert_not_awaited()
    approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_revocation_during_human_wait_is_checked_on_same_snapshot(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, user, policy, deny_audit, audit, wrapper = runtime
    row = server()
    resolve = MagicMock(return_value=row)
    monkeypatch.setattr(gateway, "_resolve_proxied_tool_server", resolve)
    inactive = {**deepcopy(BINDING), "active": False}
    evaluate = AsyncMock(
        side_effect=[
            GrantResult(deepcopy(BINDING)),
            GrantResult(inactive, "grant_inactive"),
        ]
    )
    monkeypatch.setattr(gateway.grant_introspector, "evaluate", evaluate)
    pool = MagicMock(get_client=AsyncMock())
    monkeypatch.setitem(wrapper.__globals__, "get_mcp_client_pool", lambda: pool)

    async def approve(**kwargs: Any) -> tuple[bool, str]:
        row.auth_config["token"] = "rotated-token"
        return True, ""

    monkeypatch.setattr("preloop.services.approval_helper.require_approval", approve)
    result = await mcp.call_tool("first_read", {})
    assert result.is_error and "grant_inactive" in result.content[0].text
    resolve.assert_called_once()
    assert evaluate.await_count == 2
    assert [call.args[0] for call in evaluate.await_args_list] == [
        "synthetic-bearer",
        "synthetic-bearer",
    ]
    pool.get_client.assert_not_awaited()
    assert deny_audit.call_args.kwargs["extra_details"]["grant"] == inactive
    assert audit.log_tool_call_async.call_args.kwargs["grant"] == inactive


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["timeout", "server_error", "inactive", "missing_scope"]
)
async def test_real_introspection_failure_never_forwards_runtime_tool(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from preloop.services.grant_introspection import GrantIntrospector

    mcp, _, policy, deny_audit, _, wrapper = runtime
    monkeypatch.setattr(gateway, "_resolve_proxied_tool_server", lambda *a: server())

    def respond(request: httpx.Request) -> httpx.Response:
        if mode == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if mode == "server_error":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(
            200, json={"active": mode != "inactive", "scope": "other"}
        )

    transport = httpx.MockTransport(respond)
    introspector = GrantIntrospector(
        client_factory=lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs)
    )
    monkeypatch.setattr(gateway, "grant_introspector", introspector)
    pool = MagicMock(get_client=AsyncMock())
    monkeypatch.setitem(wrapper.__globals__, "get_mcp_client_pool", lambda: pool)
    result = await mcp.call_tool("first_read", {})
    reason = (
        "grant_inactive"
        if mode == "inactive"
        else "scope_not_granted"
        if mode == "missing_scope"
        else "introspection_unavailable"
    )
    assert result.is_error and reason in result.content[0].text
    policy.assert_not_awaited()
    pool.get_client.assert_not_awaited()
    assert deny_audit.call_args.kwargs["rule_description"] == reason


@pytest.mark.asyncio
async def test_sensitive_server_scope_uses_snapshot_owner_over_stale_listing(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.services.policy.schema import SensitiveDataConfig
    from preloop.services.sensitive_data.detectors import DetectorConfig

    mcp, _, policy, _, _, wrapper = runtime
    monkeypatch.setattr(gateway, "_resolve_proxied_tool_server", lambda *a: server())
    monkeypatch.setattr(
        gateway.grant_introspector,
        "evaluate",
        AsyncMock(return_value=GrantResult(deepcopy(BINDING))),
    )
    config = SensitiveDataConfig.model_validate(
        {
            "rules": [
                {
                    "id": "owner-cards",
                    "on": ["tool.args"],
                    "types": ["credit_card"],
                    "action": "deny",
                    "scope": {"servers": ["First owner"]},
                }
            ]
        }
    )
    monkeypatch.setattr(
        gateway, "_load_sensitive_data_policy", lambda *a: (config, DetectorConfig())
    )
    pool = MagicMock(get_client=AsyncMock())
    monkeypatch.setitem(wrapper.__globals__, "get_mcp_client_pool", lambda: pool)
    result = await mcp.call_tool(
        "first_read", {"note": "synthetic card 4111 1111 1111 1111"}
    )
    assert result.is_error and "owner-cards" in result.content[0].text
    policy.assert_not_awaited()
    pool.get_client.assert_not_awaited()


@pytest.fixture
def restricted_runtime(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> tuple[Runtime, MagicMock, SimpleNamespace]:
    """Use the actual restricted dispatch wiring with isolated authority state."""
    from uuid import uuid4
    from preloop.models.crud import crud_restricted_runtime

    mcp, user, *_ = runtime
    user.credential_type = "restricted_runtime"
    user.api_key_id = str(uuid4())
    row = server()
    row.id = str(uuid4())
    row.auth_config.pop("introspection")
    monkeypatch.setattr(gateway, "_resolve_proxied_tool_server", lambda *a: row)
    current = SimpleNamespace(revoked=False)

    def check(*args: Any, **kwargs: Any) -> None:
        assert str(kwargs["account_id"]) == user.account_id
        assert str(kwargs["api_key_id"]) == user.api_key_id
        assert str(kwargs["server_id"]) == row.id
        assert kwargs["upstream_tool"] == "read"
        assert kwargs["scope"] == "mcp:write"
        if current.revoked:
            raise crud_restricted_runtime.RestrictedRuntimeDeniedError("revoked")

    authorize = MagicMock(side_effect=check)
    monkeypatch.setattr(crud_restricted_runtime, "authorize", authorize)
    return runtime, authorize, current


@pytest.mark.asyncio
async def test_restricted_denial_precedes_policy_approval_and_upstream(
    restricted_runtime: tuple[Runtime, MagicMock, SimpleNamespace],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, authorize, current = restricted_runtime
    mcp, _, policy, *_ = runtime
    current.revoked = True
    approval = AsyncMock()
    monkeypatch.setattr("preloop.services.approval_helper.require_approval", approval)
    pool = MagicMock(get_client=AsyncMock())
    monkeypatch.setattr(gateway, "get_mcp_client_pool", lambda: pool)
    result = await mcp.call_tool("first_read", {})
    assert result.is_error and "restricted runtime" in result.content[0].text
    authorize.assert_called_once()
    policy.assert_not_awaited()
    approval.assert_not_awaited()
    pool.get_client.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("wait", ["approval", "connection"])
async def test_restricted_revocation_after_wait_never_dispatches(
    restricted_runtime: tuple[Runtime, MagicMock, SimpleNamespace],
    monkeypatch: pytest.MonkeyPatch,
    wait: str,
) -> None:
    runtime, authorize, current = restricted_runtime
    mcp, _, _, _, _, wrapper = runtime
    client = MagicMock(call_tool=AsyncMock(return_value=[]))

    async def connect(**kwargs: Any) -> Any:
        if wait == "connection":
            current.revoked = True
        return client

    pool = MagicMock(get_client=AsyncMock(side_effect=connect))
    monkeypatch.setitem(wrapper.__globals__, "get_mcp_client_pool", lambda: pool)

    async def approve(**kwargs: Any) -> tuple[bool, str]:
        if wait == "approval":
            current.revoked = True
        return True, ""

    monkeypatch.setattr("preloop.services.approval_helper.require_approval", approve)
    result = await mcp.call_tool("first_read", {})
    assert result.is_error and "restricted runtime" in result.content[0].text
    assert authorize.call_count >= 3
    if wait == "approval":
        pool.get_client.assert_not_awaited()
    else:
        pool.get_client.assert_awaited_once()
    client.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_restricted_builtin_and_approval_replay_are_unsupported(
    restricted_runtime: tuple[Runtime, MagicMock, SimpleNamespace],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastmcp.tools import Tool

    runtime, authorize, _ = restricted_runtime
    mcp, user, policy, *_ = runtime
    user.allowed_flow_tools = ["search"]
    monkeypatch.setattr(
        mcp, "list_tools", AsyncMock(return_value=[Tool(name="search", parameters={})])
    )
    result = await mcp.call_tool("search", {})
    assert result.is_error and "exact MCP resource" in result.content[0].text
    authorize.assert_not_called()
    policy.assert_not_awaited()
    internal = f"account_{user.account_id.replace('-', '_')}_first_read"
    result = await mcp.call_registered_tool_without_policy(
        internal, {}, account_id=user.account_id
    )
    assert result.is_error and "approval replay unsupported" in result.content[0].text


@pytest.mark.asyncio
async def test_restricted_protocol_denies_resources_and_prompts_before_handlers(
    restricted_runtime: tuple[Runtime, MagicMock, SimpleNamespace],
) -> None:
    runtime, _, _ = restricted_runtime
    mcp, _, _, _, _, _ = runtime
    handler = MagicMock(return_value="synthetic-protected-data")

    def protected_resource() -> str:
        return handler()

    mcp.resource("resource://protected")(protected_resource)
    assert await mcp.list_resources() == []
    assert await mcp.list_resource_templates() == []
    assert await mcp.list_prompts() == []
    with pytest.raises(PermissionError):
        await mcp.read_resource("resource://protected")
    with pytest.raises(PermissionError):
        await mcp.get_prompt("synthetic-protected-prompt")
    handler.assert_not_called()


@pytest.mark.asyncio
async def test_restricted_denial_precedes_upstream_introspection(
    restricted_runtime: tuple[Runtime, MagicMock, SimpleNamespace],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _, current = restricted_runtime
    mcp, user, *_ = runtime
    current.revoked = True
    row = server()
    from uuid import uuid4

    row.id = str(uuid4())
    monkeypatch.setattr(gateway, "_resolve_proxied_tool_server", lambda *a: row)
    introspect = AsyncMock(return_value=GrantResult(deepcopy(BINDING)))
    monkeypatch.setattr(gateway.grant_introspector, "evaluate", introspect)
    result = await mcp.call_tool("first_read", {})
    assert result.is_error
    introspect.assert_not_awaited()


@pytest.mark.asyncio
async def test_restricted_listing_cannot_reuse_cached_resource_authority(
    restricted_runtime: tuple[Runtime, MagicMock, SimpleNamespace],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastmcp.tools import Tool
    from preloop.models.crud import crud_account

    runtime, authorize, _ = restricted_runtime
    mcp, user, *_ = runtime
    user.mcp_tools_cache = [Tool(name="formerly_permitted", parameters={})]

    def check_current_resource(*args: Any, **kwargs: Any) -> None:
        from preloop.models.crud import crud_restricted_runtime

        if kwargs["server_id"] is not None:
            raise crud_restricted_runtime.RestrictedRuntimeDeniedError("narrowed")

    authorize.side_effect = check_current_resource
    monkeypatch.setattr(
        "preloop.services.mcp_tool_discovery._get_proxied_tools_sync",
        lambda *a: [],
    )
    monkeypatch.setattr(
        crud_account, "get", lambda *a, **k: SimpleNamespace(meta_data={})
    )
    assert await gateway.DynamicFastMCP.list_tools(mcp) == []
    assert authorize.call_count == 2
    assert all(call.kwargs["scope"] == "mcp:read" for call in authorize.call_args_list)


@pytest.mark.asyncio
async def test_legacy_resource_protocol_still_executes_handlers(
    runtime: Runtime,
) -> None:
    mcp, *_ = runtime
    handler = MagicMock(return_value="synthetic-legacy-data")

    def protected_resource() -> str:
        return handler()

    mcp.resource("resource://legacy")(protected_resource)
    assert len(await mcp.list_resources()) == 1
    await mcp.read_resource("resource://legacy")
    handler.assert_called_once()
