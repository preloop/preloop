"""End-to-end flow for a consent-gated tool call.

A source checks consent itself, and Preloop asks a consent provider about
every call through grant introspection. The test walks the documented steps
against two local mocks: a token introspection endpoint (an ASGI app reached
through the introspector's client factory, because introspection requires
HTTPS) and a streamable HTTP MCP source on loopback.

Steps: apply the policy through ``POST /api/v1/policies/upload`` (what
``preloop policy apply`` calls), scan the server, register an agent and mint
its credential, then initialize, send ``notifications/initialized`` and call
the tool on ``/mcp/v1``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4
from unittest.mock import patch

import fastapi
import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from preloop import __version__
from preloop.api.app import create_app
from preloop.api.auth import get_current_active_user
from preloop.api.auth.ci import get_current_actor
from preloop.models.crud import (
    crud_account,
    crud_audit_log,
    crud_role,
    crud_user,
    crud_user_role,
)
from preloop.models.db.session import (
    get_async_engine_if_initialized,
    get_session_factory,
)
from preloop.models.models.user import User
from preloop.services import grant_introspection
from preloop.services.audit.async_writer import writer as audit_writer
from preloop.services.mcp_http import get_mcp_lifespan_manager
from preloop.services.upstream_identity import CORRELATION_ID_HEADER

SOURCE_TOKEN = "source-token-example"
CLIENT_ID = "preloop-example-client"
CLIENT_SECRET = "example-client-secret"
CONSENT_ID = "consent-example-001"
LEDGER_HASH = "sha256:0f1e2d3c4b5a69788796a5b4c3d2e1f0"
OWNER = "did:example:jane-doe-0001"
INTROSPECTION_URL = "https://consent.example.com/v1/token/introspect"
ACCEPT = "application/json, text/event-stream"
PROTOCOL = "2025-11-25"


@dataclass
class ConsentProvider:
    """Mock OAuth token introspection endpoint with HTTP Basic client auth."""

    revoked: bool = False
    calls: int = 0

    def app(self) -> Starlette:
        """Return the introspection endpoint as an ASGI app."""

        async def introspect(request: Request) -> JSONResponse:
            self.calls += 1
            expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode())
            if request.headers.get("authorization") != f"Basic {expected.decode()}":
                return JSONResponse({"error": "invalid_client"}, status_code=401)
            form = await request.form()
            if self.revoked or form.get("token") != SOURCE_TOKEN:
                return JSONResponse({"active": False})
            return JSONResponse(
                {
                    "active": True,
                    "scope": "daily",
                    "sub": "agent-example",
                    "client_id": "source-example",
                    "consent_id": CONSENT_ID,
                }
            )

        return Starlette(
            routes=[Route("/v1/token/introspect", introspect, methods=["POST"])]
        )


@dataclass
class Source:
    """Mock MCP source with one tool; records the headers of every request."""

    requests: list[dict[str, str]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    call_headers: list[dict[str, str]] = field(default_factory=list)

    def app(self) -> ASGIApp:
        """Return the source as a streamable HTTP ASGI app."""
        mcp = FastMCP("consent-source")

        @mcp.tool
        def read_scope(scope: str, owner: str) -> str:
            """Read one scope of the owner's record."""
            self.tool_calls.append({"scope": scope, "owner": owner})
            self.call_headers.append(get_http_headers(include_all=True))
            return json.dumps(
                {
                    "scope": scope,
                    "entries": [{"value": 72}],
                    "consent_id": CONSENT_ID,
                    "ledger_entry_hash": LEDGER_HASH,
                    "owner": owner,
                }
            )

        inner = mcp.http_app(path="/mcp", stateless_http=True)
        records = self.requests

        async def recording(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http":
                records.append({k.decode(): v.decode() for k, v in scope["headers"]})
            await inner(scope, receive, send)

        recording.lifespan = inner.lifespan  # type: ignore[attr-defined]
        return recording


@pytest.fixture
def source() -> Iterator[tuple[Source, str]]:
    """Serve the mock source on loopback from a background thread."""
    state = Source()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(state.app(), log_level="error", lifespan="on")
    )
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(sockets=[listener])), daemon=True
    )
    thread.start()
    for _ in range(500):
        if server.started:
            break
        threading.Event().wait(0.01)
    assert server.started
    try:
        yield state, f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()


def _policy(source_url: str) -> str:
    return f"""
version: "1.0"
metadata:
  name: Consent-gated source
mcp_servers:
  - name: consent-source
    url: {source_url}
    auth_type: bearer
    auth_config:
      token: {SOURCE_TOKEN}
      introspection:
        endpoint: {INTROSPECTION_URL}
        client_id: {CLIENT_ID}
        client_secret: {CLIENT_SECRET}
        client_auth: basic
        consent_ref_claim: consent_id
        max_cache_ttl_seconds: 0
        negative_cache_ttl_seconds: 0
        fail_open: false
tools:
  - name: read_scope
    source: consent-source
    conditions:
      - expression: "args.scope == 'daily'"
        action: allow
        description: daily is in the grant
      - expression: "true"
        action: deny
        description: every other scope
sensitive_data:
  reference_only:
    - id: consent-reads
      scope: {{tools: [read_scope], servers: [consent-source]}}
      keep_fields:
        - "$result.consent_id"
        - "$result.ledger_entry_hash"
      approver_view: redacted
"""


def _rpc(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    if response.headers["content-type"].startswith("text/event-stream"):
        data = [ln[6:] for ln in response.text.splitlines() if ln.startswith("data: ")]
        message: dict[str, Any] = json.loads(data[-1])
    else:
        message = response.json()
    return message


@dataclass
class Flow:
    """The documented flow, ready to call the tool."""

    client: TestClient
    token: str
    provider: ConsentProvider
    source: Source
    db: Session
    user: User
    unreachable: list[bool]

    def headers(self, token: str | None = None) -> dict[str, str]:
        """Headers from the documented curl example."""
        return {
            "Authorization": f"Bearer {token or self.token}",
            "Content-Type": "application/json",
            "Accept": ACCEPT,
            "MCP-Protocol-Version": PROTOCOL,
        }

    def call(self, scope: str) -> dict[str, Any]:
        """Call read_scope and return the JSON-RPC result."""
        body = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "read_scope",
                "arguments": {"scope": scope, "owner": OWNER},
            },
        }
        response = self.client.post("/mcp/v1", headers=self.headers(), json=body)
        result: dict[str, Any] = _rpc(response)["result"]
        return result

    def audit(self) -> list[Any]:
        """Audit rows of the account, oldest first, once queued writes land."""
        deadline = time.monotonic() + 10
        while audit_writer.pending_count and time.monotonic() < deadline:
            time.sleep(0.01)
        assert audit_writer.pending_count == 0, "audit writes still pending"
        self.db.expire_all()
        rows = crud_audit_log.get_by_account(
            self.db, account_id=self.user.account_id, limit=500
        )
        return sorted(rows, key=lambda r: r.timestamp)


@pytest.fixture
def owner() -> Iterator[User]:
    """Commit an account and owner through the real session factory.

    The flow crosses detached audit writers and the salt loader, which open
    their own sessions, so the fixture data must be committed. The account
    and its rows (cascade) are deleted afterwards.
    """
    db = get_session_factory()()
    account = crud_account.create(
        db, obj_in={"organization_name": "Example Org", "is_active": True}
    )
    suffix = uuid4().hex[:12]
    user = crud_user.create(
        db,
        obj_in={
            "account_id": account.id,
            "email": f"jane.doe+{suffix}@example.com",
            "username": f"jane-{suffix}",
            "full_name": "Jane Doe",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "unused",
            "user_source": "local",
        },
    )
    role = crud_role.get_by_name(db, name="owner")
    assert role is not None
    crud_user_role.create(db, obj_in={"user_id": user.id, "role_id": role.id})
    db.commit()
    db.refresh(user)
    try:
        yield user
    finally:
        db.rollback()
        crud_account.delete(db, id=account.id)
        db.commit()
        db.close()


@pytest.fixture
def flow(owner: User, source: tuple[Source, str]) -> Iterator[Flow]:
    """Apply the policy, scan, mint an agent credential, initialize."""
    source_state, source_url = source
    provider = ConsentProvider()
    unreachable = [False]
    introspection_app = provider.app()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        transport: httpx.AsyncBaseTransport = (
            httpx.MockTransport(refuse)
            if unreachable[0]
            else httpx.ASGITransport(app=introspection_app)
        )
        return httpx.AsyncClient(transport=transport, **kwargs)

    app = create_app()
    db = get_session_factory()()

    def current_user() -> User:
        user = crud_user.get(db, id=owner.id)
        assert user is not None
        return user

    # Console calls run as the account owner; /mcp/v1 uses real bearer auth.
    app.dependency_overrides[get_current_active_user] = current_user
    app.dependency_overrides[get_current_actor] = current_user
    outer = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: fastapi.FastAPI) -> AsyncIterator[Any]:
        manager = get_mcp_lifespan_manager()
        assert manager is not None
        try:
            async with outer(application) as state:
                async with manager:
                    yield state
        finally:
            # Pooled async connections belong to this client's event loop.
            engine = get_async_engine_if_initialized()
            if engine is not None:
                await engine.dispose()

    app.router.lifespan_context = lifespan

    with (
        patch.object(
            grant_introspection.grant_introspector, "_client_factory", client_factory
        ),
        TestClient(app, base_url="http://localhost") as client,
    ):
        upload = client.post(
            "/api/v1/policies/upload",
            files={"file": ("policy.yaml", _policy(source_url), "application/x-yaml")},
        )
        assert upload.status_code == 200, upload.text
        assert not upload.json().get("errors"), upload.text

        servers = client.get("/api/v1/mcp-servers").json()
        server = next(s for s in servers if s["name"] == "consent-source")
        scan = client.post(f"/api/v1/mcp-servers/{server['id']}/scan")
        assert scan.status_code == 200, scan.text

        agent = client.post("/api/v1/agents", json={"display_name": "Example reader"})
        assert agent.status_code == 201, agent.text
        credential = client.post(
            f"/api/v1/agents/{agent.json()['id']}/credentials",
            json={"name": "reader", "scopes": ["mcp:read", "mcp:write"]},
        )
        assert credential.status_code == 201, credential.text
        token = credential.json()["token"]
        assert token.startswith("agt_")

        state = Flow(client, token, provider, source_state, db, owner, unreachable)
        init = client.post(
            "/mcp/v1",
            headers={
                k: v for k, v in state.headers().items() if k != "MCP-Protocol-Version"
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "consent-reader", "version": "1.0"},
                },
            },
        )
        assert _rpc(init)["result"]["protocolVersion"] == PROTOCOL
        notified = client.post(
            "/mcp/v1",
            headers=state.headers(),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert notified.status_code == 202, notified.text
        source_state.requests.clear()
        try:
            yield state
        finally:
            db.close()


def _assert_owner_absent(rows: list[Any]) -> None:
    for row in rows:
        text = json.dumps(row.details, default=str)
        assert OWNER not in text, (row.action, text)


def test_served_call_keeps_only_the_consent_references(flow: Flow) -> None:
    result = flow.call("daily")

    assert result["isError"] is False
    served = json.loads(result["content"][0]["text"])
    assert served["consent_id"] == CONSENT_ID
    assert flow.source.tool_calls == [{"scope": "daily", "owner": OWNER}]
    assert flow.provider.calls >= 1

    # Headers seen by the source while it ran the tool call.
    (headers,) = flow.source.call_headers
    assert headers["user-agent"] == f"Preloop/{__version__}"
    correlation_id = headers[CORRELATION_ID_HEADER.lower()]
    assert correlation_id
    for request in flow.source.requests:
        assert request["user-agent"] == f"Preloop/{__version__}"

    rows = flow.audit()
    allow = [r for r in rows if r.action == "policy_allow"]
    assert allow, [r.action for r in rows]
    assert allow[-1].details["grant"]["consent_ref"] == CONSENT_ID
    calls = [r for r in rows if r.action == "tool_call"]
    assert len(calls) == 1, [r.action for r in rows]
    kept = calls[0].details["tool_args"]["kept_result"]
    assert kept == {
        "$result.consent_id": CONSENT_ID,
        "$result.ledger_entry_hash": LEDGER_HASH,
    }
    row = calls[0].details
    assert row["tool_args"]["_preloop_reference"] is True
    assert row["tool_args"]["kept"] == {}
    assert "result" not in row
    stored = json.dumps(row, default=str)
    assert '"entries"' not in stored and '"value": 72' not in stored
    assert row["correlation_id"] == correlation_id
    _assert_owner_absent(rows)


def test_out_of_scope_call_is_refused_at_the_gate(flow: Flow) -> None:
    result = flow.call("weekly")

    assert result["isError"] is True
    assert result["content"][0]["text"] == "Access denied: every other scope"
    assert flow.source.tool_calls == []
    _assert_owner_absent(flow.audit())


def test_revoked_grant_is_refused_before_the_source(flow: Flow) -> None:
    flow.provider.revoked = True

    result = flow.call("daily")

    assert result["isError"] is True
    assert result["content"][0]["text"] == "Access denied: grant_inactive"
    assert flow.source.tool_calls == []
    rows = flow.audit()
    assert [r for r in rows if r.action == "policy_deny"], [r.action for r in rows]
    # Nothing was executed. Whether a ``declined`` row exists is pinned
    # separately below.
    assert not [r for r in rows if r.action == "tool_call" and r.status != "declined"]
    _assert_owner_absent(rows)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known gap: with the audit outbox off, the refused call's declined "
        "tool_call row is handed to the async writer from an asyncio.to_thread "
        "worker, which has no running loop, so the write is skipped. When "
        "this passes, the refusal audit is deterministic; update the guide."
    ),
)
def test_revoked_grant_leaves_a_declined_tool_call_row(flow: Flow) -> None:
    flow.provider.revoked = True

    flow.call("daily")

    declined = [
        r for r in flow.audit() if r.action == "tool_call" and r.status == "declined"
    ]
    assert len(declined) == 1
    assert "grant_inactive" in json.dumps(declined[0].details, default=str)


def test_unreachable_consent_provider_fails_closed(flow: Flow) -> None:
    flow.unreachable[0] = True

    result = flow.call("daily")

    assert result["isError"] is True
    assert result["content"][0]["text"] == "Access denied: introspection_unavailable"
    assert flow.source.tool_calls == []


def test_endpoint_needs_a_credential_and_both_accept_types(flow: Flow) -> None:
    """Real bearer auth refuses no credential; JSON alone in Accept is refused.

    The stateless initialize (no ``Mcp-Session-Id``) is pinned in
    ``test_mcp_protocol.py::test_legacy_initialize_and_call``.
    """
    body = {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}
    anonymous = {k: v for k, v in flow.headers().items() if k != "Authorization"}
    assert flow.client.post("/mcp/v1", headers=anonymous, json=body).status_code == 401
    json_only = {**flow.headers(), "Accept": "application/json"}
    assert flow.client.post("/mcp/v1", headers=json_only, json=body).status_code == 406
