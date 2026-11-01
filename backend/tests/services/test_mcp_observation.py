"""Safe internal dispatch extension events never contain call payloads."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from preloop.services.mcp_observation import (
    MCPDispatchObservation,
    publish_dispatch_observation,
)


def test_publisher_passes_only_validated_identity() -> None:
    observer = MagicMock()
    account, server, session = (str(uuid4()) for _ in range(3))
    with patch(
        "preloop.plugins.base.get_plugin_manager",
        return_value=SimpleNamespace(get_service=lambda name: observer),
    ):
        publish_dispatch_observation(
            account_id=account,
            server_id=server,
            registry_revision="2026-10-16T12:00:00",
            runtime_session_id=session,
        )
    event = observer.observe.call_args.args[0]
    assert set(event.model_dump()) == {
        "account_id",
        "server_id",
        "registry_revision",
        "observed_at",
        "runtime_session_id",
    }
    assert str(event.account_id) == account and str(event.server_id) == server


def test_missing_revision_and_no_plugin_emit_nothing() -> None:
    observer = MagicMock()
    with patch(
        "preloop.plugins.base.get_plugin_manager",
        return_value=SimpleNamespace(get_service=lambda name: observer),
    ):
        publish_dispatch_observation(
            account_id=str(uuid4()),
            server_id=str(uuid4()),
            registry_revision=None,
            runtime_session_id=None,
        )
    observer.observe.assert_not_called()
    with patch(
        "preloop.plugins.base.get_plugin_manager",
        return_value=SimpleNamespace(get_service=lambda name: None),
    ):
        publish_dispatch_observation(
            account_id=str(uuid4()),
            server_id=str(uuid4()),
            registry_revision="2026-10-16T12:00:00",
            runtime_session_id=None,
        )


def test_observer_failure_does_not_change_execution_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    observer = MagicMock()
    observer.observe.side_effect = RuntimeError("synthetic-sensitive-detail")
    with patch(
        "preloop.plugins.base.get_plugin_manager",
        return_value=SimpleNamespace(get_service=lambda name: observer),
    ):
        publish_dispatch_observation(
            account_id=str(uuid4()),
            server_id=str(uuid4()),
            registry_revision="2026-10-16T12:00:00",
            runtime_session_id=None,
        )
    observer.observe.assert_called_once()
    assert "RuntimeError" in caplog.text
    assert "synthetic-sensitive-detail" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize(
    "field",
    ["url", "arguments", "headers", "prompt", "auth", "tool_name", "server_name"],
)
def test_contract_refuses_content_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        MCPDispatchObservation.model_validate(
            {
                "account_id": uuid4(),
                "server_id": uuid4(),
                "registry_revision": "2026-10-16T12:00:00",
                "observed_at": datetime.now(UTC),
                field: "synthetic-content",
            }
        )


def test_revision_cannot_be_a_label_or_endpoint() -> None:
    with pytest.raises(ValidationError):
        MCPDispatchObservation(
            account_id=uuid4(),
            server_id=uuid4(),
            registry_revision="http://example.com",
            observed_at=datetime.now(UTC),
        )


@pytest.mark.asyncio
async def test_dispatch_uses_snapshot_identity_and_works_without_a_session() -> None:
    from preloop.services.dynamic_fastmcp import DynamicFastMCP, GrantDispatchSnapshot
    from preloop.services.dynamic_mcp_server import UserContext

    account_id, server_id = str(uuid4()), str(uuid4())
    snapshot = GrantDispatchSnapshot(
        account_id=account_id,
        tool_name="read",
        server_name="same-label",
        upstream_name="read",
        registry_revision="2026-10-16T12:00:00",
        client_config={
            "server_id": server_id,
            "url": "http://example.com/mcp",
            "auth_config": {"token": "synthetic-secret"},
        },
    )
    context = UserContext(
        user_id=str(uuid4()), account_id=account_id, username="synthetic"
    )
    gateway = DynamicFastMCP("synthetic")
    gateway._proxied_tool_servers["read"] = str(uuid4())
    observer = MagicMock()
    with patch(
        "preloop.plugins.base.get_plugin_manager",
        return_value=SimpleNamespace(get_service=lambda name: observer),
    ):
        await gateway._observe_mcp_dispatch(snapshot, context)
    event = observer.observe.call_args.args[0]
    assert str(event.server_id) == server_id
    assert event.runtime_session_id is None
    assert "synthetic-secret" not in repr(event)
    assert "example.com" not in repr(event)


def test_plugin_lookup_failure_does_not_change_execution_outcome() -> None:
    with patch(
        "preloop.plugins.base.get_plugin_manager",
        side_effect=RuntimeError("synthetic plugin failure"),
    ):
        publish_dispatch_observation(
            account_id=str(uuid4()),
            server_id=str(uuid4()),
            registry_revision="2026-10-16T12:00:00",
            runtime_session_id=None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, ImportError])
async def test_wrapper_preserves_completed_result_when_observation_raises(
    error_type: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from unittest.mock import AsyncMock
    from mcp.types import TextContent
    from preloop.services import dynamic_fastmcp as dispatch
    from preloop.services.dynamic_mcp_server import UserContext

    account_id = str(uuid4())
    gateway = dispatch.DynamicFastMCP("synthetic")
    gateway.set_user_context_provider(
        lambda: UserContext(
            user_id=str(uuid4()), account_id=account_id, username="synthetic"
        )
    )
    snapshot = dispatch.GrantDispatchSnapshot(
        account_id=account_id,
        tool_name="read",
        server_name="synthetic",
        upstream_name="read",
        registry_revision="2026-10-16T12:00:00",
        client_config={"server_id": str(uuid4())},
    )
    client = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=[TextContent(type="text", text="synthetic-ok")]
        )
    )
    monkeypatch.setattr(
        dispatch,
        "get_mcp_client_pool",
        lambda: SimpleNamespace(get_client=AsyncMock(return_value=client)),
    )
    monkeypatch.setattr(gateway, "_grant_dispatch_denial", AsyncMock(return_value=None))
    monkeypatch.setattr(gateway, "_halt_dispatch_denial", AsyncMock(return_value=None))
    monkeypatch.setattr(
        gateway,
        "_observe_mcp_dispatch",
        AsyncMock(side_effect=error_type("synthetic-sensitive-detail")),
    )
    monkeypatch.setattr(
        "preloop.services.approval_helper.require_approval",
        AsyncMock(return_value=(True, None)),
    )
    monkeypatch.setattr(dispatch, "apply_output_filters", lambda items, **kwargs: items)
    wrapper = gateway._create_proxied_tool_wrapper(
        "read", account_id, "Synthetic read", {"properties": {}}
    )
    token = dispatch._grant_dispatch_var.set(snapshot)
    try:
        result = await wrapper()
    finally:
        dispatch._grant_dispatch_var.reset(token)
    assert result == "synthetic-ok"
    client.call_tool.assert_awaited_once_with("read", {}, correlation_id=None)
    assert error_type.__name__ in caplog.text
    assert "synthetic-sensitive-detail" not in caplog.text


def test_plugin_import_failure_is_safe_and_diagnostic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import builtins
    from typing import Any

    original_import = builtins.__import__

    def importing(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "preloop.plugins.base":
            raise ImportError("synthetic-sensitive-detail")
        return original_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=importing):
        publish_dispatch_observation(
            account_id=str(uuid4()),
            server_id=str(uuid4()),
            registry_revision="2026-10-16T12:00:00",
            runtime_session_id=None,
        )
    assert "ImportError" in caplog.text
    assert "synthetic-sensitive-detail" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
