"""Preloop identifies itself on upstream MCP requests (#2134).

Every request to an upstream MCP server carries ``User-Agent: Preloop/<version>``;
``tools/call`` sessions also carry ``X-Preloop-Correlation-Id`` equal to the
correlation id on the ``tool_call`` audit row. A server can opt out of all
identity headers and opt in to ``X-Preloop-Instance``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from preloop import __version__
from preloop.services import mcp_client_pool, upstream_identity
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.litellm_routing import preloop_client_headers
from preloop.services.mcp_client_pool import MCPClient
from preloop.services.upstream_identity import (
    CORRELATION_ID_HEADER,
    INSTANCE_HEADER,
    derive_instance_id,
    preloop_user_agent,
)
from tests.services.test_mcp_outbound_protocol import Peer, install_peer
from tests.services.test_proxied_tool_error_audit import _audit_kwargs, _setup

UA = f"Preloop/{__version__}"
CORRELATION = CORRELATION_ID_HEADER.lower()
INSTANCE = INSTANCE_HEADER.lower()


def _client(monkeypatch: pytest.MonkeyPatch, peer: Peer, **kwargs: Any) -> MCPClient:
    install_peer(monkeypatch, peer)
    return MCPClient("http://example.com/mcp", **kwargs)


def test_user_agent_is_shared_with_the_model_path() -> None:
    assert preloop_user_agent() == UA
    assert preloop_client_headers() == {"User-Agent": UA}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_type,auth_config,auth_header",
    [
        ("bearer", {"token": "synthetic-token"}, "authorization"),
        ("api_key", {"api_key": "synthetic-key"}, "x-api-key"),
        ("none", {}, None),
    ],
)
async def test_every_request_carries_user_agent_and_auth_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    auth_type: str,
    auth_config: dict,
    auth_header: str | None,
) -> None:
    peer = Peer()
    client = _client(monkeypatch, peer, auth_type=auth_type, auth_config=auth_config)
    await client.connect()
    await client.list_tools()
    await client.call_tool("echo", {}, correlation_id="corr-1")
    assert {"server/discover", "tools/list", "tools/call"} <= set(peer.methods)
    for _, _, headers in peer.calls:
        assert headers["user-agent"] == UA
        assert INSTANCE not in headers
        if auth_header:
            assert auth_header in headers


@pytest.mark.asyncio
async def test_legacy_initialize_carries_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = Peer(version="2025-06-18", probe_status=400)
    client = _client(monkeypatch, peer)
    await client.connect()
    assert "initialize" in peer.methods
    for _, _, headers in peer.calls:
        assert headers["user-agent"] == UA
        assert CORRELATION not in headers


@pytest.mark.asyncio
async def test_correlation_id_only_on_the_call_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = Peer()
    client = _client(monkeypatch, peer)
    await client.connect()
    await client.list_tools()
    discovery_requests = len(peer.calls)
    await client.call_tool("echo", {}, correlation_id="corr-42")
    for _, _, headers in peer.calls[:discovery_requests]:
        assert CORRELATION not in headers
    call_session = peer.calls[discovery_requests:]
    assert [m for m, _, _ in call_session if m == "tools/call"] == ["tools/call"]
    for _, _, headers in call_session:
        assert headers[CORRELATION] == "corr-42"


@pytest.mark.asyncio
async def test_opt_out_removes_all_identity_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(upstream_identity.INSTANCE_ID_ENV, "gate-a")
    peer = Peer()
    client = _client(
        monkeypatch,
        peer,
        auth_type="bearer",
        auth_config={
            "token": "synthetic-token",
            "send_identity_headers": False,
            "send_instance_id": True,
        },
    )
    await client.connect()
    await client.call_tool("echo", {}, correlation_id="corr-1")
    for _, _, headers in peer.calls:
        assert not headers.get("user-agent", "").startswith("Preloop/")
        assert CORRELATION not in headers
        assert INSTANCE not in headers
        assert headers["authorization"] == "Bearer synthetic-token"


@pytest.mark.asyncio
async def test_instance_header_is_opt_in_per_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(upstream_identity, "_instance_id_cache", None)
    monkeypatch.delenv(upstream_identity.INSTANCE_ID_ENV, raising=False)
    installation = uuid4()
    monkeypatch.setattr(
        "preloop.services.instance_service.get_or_create_instance",
        lambda: MagicMock(instance_uuid=installation),
    )
    peer = Peer()
    client = _client(monkeypatch, peer, auth_config={"send_instance_id": True})
    await client.connect()
    expected = derive_instance_id(str(installation))
    assert expected != str(installation) and len(expected) == 32
    for _, _, headers in peer.calls:
        assert headers[INSTANCE] == expected


def test_instance_id_is_stable_and_not_the_installation_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = str(uuid4())
    assert derive_instance_id(installation) == derive_instance_id(installation)
    assert installation.replace("-", "") not in derive_instance_id(installation)
    monkeypatch.setenv(upstream_identity.INSTANCE_ID_ENV, "gate-a")
    assert upstream_identity.instance_id() == "gate-a"


@pytest.mark.asyncio
async def test_instance_id_not_resolved_unless_opted_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = MagicMock(side_effect=AssertionError("resolved"))
    monkeypatch.setattr(mcp_client_pool, "instance_id", resolver)
    peer = Peer()
    client = _client(monkeypatch, peer)
    await client.connect()
    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_wire_correlation_id_equals_tool_call_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_context = UserContext(
        user_id=str(uuid4()),
        account_id=str(uuid4()),
        username="testuser",
        has_tracker=True,
        enabled_default_tools=[],
        enabled_proxied_tools=[],
    )
    mcp, pooled, audit_service, _ = _setup(monkeypatch, user_context)
    peer = Peer()
    upstream = _client(monkeypatch, peer)
    await upstream.connect()
    connect_requests = len(peer.calls)

    async def call_echo(name: str, arguments: dict, **kwargs: Any) -> Any:
        # The pool hands the proxy this client; route to the peer's tool.
        return await upstream.call_tool("echo", {}, **kwargs)

    pooled.call_tool = call_echo

    await mcp.call_tool("safe_tool", {"ok": "yes"})

    audit = _audit_kwargs(audit_service)
    assert audit["result"] == "executed"
    correlation_id = audit["correlation_id"]
    assert correlation_id
    calls = [h for m, _, h in peer.calls[connect_requests:] if m == "tools/call"]
    assert len(calls) == 1
    assert calls[0]["user-agent"] == UA
    assert calls[0][CORRELATION] == correlation_id


@pytest.mark.parametrize("key", ["send_identity_headers", "send_instance_id"])
def test_identity_flags_must_be_booleans(key: str) -> None:
    from pydantic import ValidationError

    from preloop.models.schemas.mcp_server import MCPServerCreate

    base = {"name": "s", "url": "http://example.com/mcp", "auth_type": "none"}
    MCPServerCreate(**base, auth_config={key: False})
    with pytest.raises(ValidationError):
        MCPServerCreate(**base, auth_config={key: "false"})


def test_approval_correlation_id_comes_from_the_created_row(db_session) -> None:
    from preloop.models import models
    from preloop.models.crud.audit_log import crud_audit_log

    account = models.Account(organization_name="Synthetic correlation")
    db_session.add(account)
    db_session.flush()
    approval_id = str(uuid4())
    for action, correlation in (
        ("approval_created", "paused-call"),
        ("approval_approved", None),
        ("approval_created", None),
    ):
        details = {"approval_id": approval_id}
        if correlation:
            details["correlation_id"] = correlation
        crud_audit_log.log_action(
            db_session,
            account_id=account.id,
            action=action,
            resource_type="approval",
            resource_id=approval_id,
            status="created",
            details=details,
        )
    assert (
        crud_audit_log.get_approval_correlation_id(
            db_session, account_id=account.id, approval_id=approval_id
        )
        == "paused-call"
    )
    assert (
        crud_audit_log.get_approval_correlation_id(
            db_session, account_id=account.id, approval_id=str(uuid4())
        )
        is None
    )
