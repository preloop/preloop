"""Tests for MCP server management endpoints."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from preloop.models.models.mcp_server import MCPServer
from preloop.models.models.mcp_tool import MCPTool


@pytest.fixture(autouse=True)
def mock_event_bus_connect():
    """Auto-mock the event bus connect method to avoid NATS connection attempts."""
    with patch(
        "preloop.sync.services.event_bus.EventBus.connect", new_callable=AsyncMock
    ) as mock_connect:
        yield mock_connect


@pytest.fixture(autouse=True)
def mock_mcp_client():
    """Auto-mock MCPClient to avoid actual connection attempts."""
    with patch("preloop.services.mcp_client_pool.MCPClient") as mock_client_class:
        mock_instance = AsyncMock()
        mock_instance.connect = AsyncMock()
        mock_instance.close = AsyncMock()
        mock_client_class.return_value = mock_instance
        yield mock_instance


@pytest.fixture(autouse=True)
def mock_tool_scan():
    """Auto-mock tool scanning to avoid actual MCP server connections."""
    with patch(
        "preloop.api.endpoints.mcp_servers.scan_mcp_server_tools",
        new_callable=AsyncMock,
    ) as mock_scan:
        mock_scan.return_value = []
        yield mock_scan


def test_list_mcp_servers_empty(client: TestClient, db_session):
    """Test listing MCP servers when none exist."""
    response = client.get("/api/v1/mcp-servers")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize("count", [1, 12])
def test_list_oauth_servers_batches_state_without_decrypting(
    client: TestClient, db_session, test_user, count: int
) -> None:
    """Listing encrypted grants has constant query cost and never opens secrets."""
    from sqlalchemy import event

    from preloop.models import models
    from preloop.models.crud.upstream_mcp_oauth import crud_upstream_mcp_oauth

    for index in range(count):
        server = models.MCPServer(
            name=f"OAuth {index}",
            url=f"https://example.com/{index}/mcp",
            transport="http-streaming",
            auth_type="oauth",
            account_id=test_user.account_id,
            status="active",
        )
        db_session.add(server)
        db_session.flush()
        crud_upstream_mcp_oauth.protect_config(
            db_session,
            server,
            {"client_id": "public-client", "access_token": "private-token"},
        )
    db_session.commit()
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany) -> None:
        if "upstream_mcp_oauth" in statement.lower():
            statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with patch(
            "preloop.services.secret_service.SecretService.resolve_secret_reference",
            side_effect=AssertionError("Listing must not decrypt credentials"),
        ):
            response = client.get("/api/v1/mcp-servers")
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert response.status_code == 200
    assert len(response.json()) == count
    assert len(statements) == 1
    for server_data in response.json():
        assert server_data["connection_state"] == "connected"
        assert server_data["auth_config"] == {"client_id": "public-client"}

    # Editing fetches detail on demand, preserving redacted credential markers.
    server_data = response.json()[0]
    detail = client.get(f"/api/v1/mcp-servers/{server_data['id']}")
    assert detail.status_code == 200
    assert detail.json()["auth_config"]["access_token"] == "***REDACTED***"
    from preloop.models.crud import crud_mcp_server

    stored = db_session.get(models.MCPServer, uuid.UUID(server_data["id"]))
    crud_mcp_server.update(
        db_session,
        db_obj=stored,
        obj_in={"name": "Renamed OAuth", "auth_config": detail.json()["auth_config"]},
    )
    assert (
        crud_upstream_mcp_oauth.config_for_edit(db_session, stored)["access_token"]
        == "private-token"
    )


def test_list_mcp_servers_with_data(client: TestClient, db_session, test_user):
    """Test listing MCP servers with existing data.

    This test verifies that:
    1. MCP servers are properly retrieved from the database
    2. UUID fields (id, account_id) are correctly serialized to strings
    3. The response matches the MCPServerResponse schema
    """
    server = MCPServer(
        name="Test MCP Server",
        url="http://localhost:8080/mcp",
        transport="http-streaming",
        auth_type="none",
        account_id=test_user.account_id,
        status="active",
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)

    response = client.get("/api/v1/mcp-servers")
    assert response.status_code == 200

    response_json = response.json()
    assert len(response_json) == 1

    server_data = response_json[0]
    assert server_data["name"] == "Test MCP Server"
    assert server_data["url"] == "http://localhost:8080/mcp"

    # Critical: Verify UUID fields are serialized to strings, not UUID objects
    assert isinstance(server_data["id"], str)
    assert isinstance(server_data["account_id"], str)
    assert server_data["id"] == str(server.id)
    assert server_data["account_id"] == str(server.account_id)


def test_create_mcp_server_success(client: TestClient, db_session, test_user):
    """Test creating an MCP server successfully.

    This test specifically verifies the bug fix:
    - account_id should be serialized to string in the response
    - The schema should accept UUID from the model and serialize it
    """
    server_data = {
        "name": "New MCP Server",
        "url": "http://localhost:8080/mcp",
        "transport": "http-streaming",
        "auth_type": "none",
    }

    response = client.post("/api/v1/mcp-servers", json=server_data)
    assert response.status_code == 201

    response_json = response.json()
    assert response_json["name"] == "New MCP Server"
    assert response_json["url"] == "http://localhost:8080/mcp"
    assert response_json["status"] == "active"

    # Critical: Verify UUID serialization (this is the bug we're testing for)
    assert isinstance(response_json["id"], str)
    assert isinstance(response_json["account_id"], str)

    # Verify the UUID can be parsed
    try:
        uuid.UUID(response_json["id"])
        uuid.UUID(response_json["account_id"])
    except ValueError:
        pytest.fail("id or account_id is not a valid UUID string")

    # Verify it was actually saved to the database
    server_id = uuid.UUID(response_json["id"])
    db_server = db_session.query(MCPServer).filter(MCPServer.id == server_id).first()
    assert db_server is not None
    assert db_server.name == "New MCP Server"
    assert db_server.account_id == test_user.account_id


def test_create_mcp_server_duplicate_name(client: TestClient, db_session, test_user):
    """Test creating an MCP server with a duplicate name."""
    # Create first server
    server = MCPServer(
        name="Duplicate Server",
        url="http://localhost:8080/mcp",
        transport="http-streaming",
        auth_type="none",
        account_id=test_user.account_id,
        status="active",
    )
    db_session.add(server)
    db_session.commit()

    # Try to create another with the same name
    server_data = {
        "name": "Duplicate Server",
        "url": "http://localhost:9090/mcp",
        "transport": "http-streaming",
        "auth_type": "none",
    }

    response = client.post("/api/v1/mcp-servers", json=server_data)
    assert response.status_code == 400
    assert "already exists" in response.json()["detail"]


def test_create_mcp_server_with_connection_error(
    client: TestClient, db_session, test_user, mock_mcp_client
):
    """Test creating an MCP server when connection validation fails.

    The server should still be created but with error status.
    """
    # Make the mock client raise an error on connect
    mock_mcp_client.connect.side_effect = ConnectionRefusedError("Connection refused")

    server_data = {
        "name": "Failing Server",
        "url": "http://unreachable:8080/mcp",
        "transport": "http-streaming",
        "auth_type": "none",
    }

    response = client.post("/api/v1/mcp-servers", json=server_data)
    assert response.status_code == 201

    response_json = response.json()
    assert response_json["name"] == "Failing Server"
    assert response_json["status"] == "error"
    assert response_json["last_error"] is not None
    assert "Could not connect" in response_json["last_error"]
    assert "reachability from Preloop" in response_json["last_error"]

    # Still verify UUID serialization works even with error status
    assert isinstance(response_json["id"], str)
    assert isinstance(response_json["account_id"], str)


def test_get_mcp_server_success(client: TestClient, db_session, test_user):
    """Test getting a single MCP server by ID."""
    server = MCPServer(
        name="Test Server",
        url="http://localhost:8080/mcp",
        transport="http-streaming",
        auth_type="none",
        account_id=test_user.account_id,
        status="active",
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)

    response = client.get(f"/api/v1/mcp-servers/{server.id}")
    assert response.status_code == 200

    response_json = response.json()
    assert response_json["name"] == "Test Server"
    assert isinstance(response_json["id"], str)
    assert isinstance(response_json["account_id"], str)


def test_get_mcp_server_not_found(client: TestClient, db_session, test_user):
    """Test getting a non-existent MCP server."""
    fake_id = uuid.uuid4()
    response = client.get(f"/api/v1/mcp-servers/{fake_id}")
    assert response.status_code == 404


def test_list_mcp_server_tools_returns_discovered_tools(
    client: TestClient, db_session, test_user
):
    """Regression test: listing discovered tools must not 500.

    ``GET /api/v1/mcp-servers/{id}/tools`` validates ORM ``MCPTool`` rows whose
    ``id`` and ``mcp_server_id`` are UUIDs. The response schema must accept
    those UUIDs and serialize them to strings instead of raising a validation
    error, which the endpoint previously surfaced as a 500.
    """
    server = MCPServer(
        name="Tools Server",
        url="http://localhost:8080/mcp",
        transport="http-streaming",
        auth_type="none",
        account_id=test_user.account_id,
        status="active",
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)

    tool = MCPTool(
        mcp_server_id=server.id,
        name="create_issue",
        description="Create an issue",
        input_schema={"type": "object", "properties": {}},
        discovered_at="2026-01-01T00:00:00Z",
    )
    db_session.add(tool)
    db_session.commit()
    db_session.refresh(tool)

    response = client.get(f"/api/v1/mcp-servers/{server.id}/tools")
    assert response.status_code == 200

    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["name"] == "create_issue"
    assert payload[0]["input_schema"] == {"type": "object", "properties": {}}
    # UUID fields must round-trip as strings, not raw UUIDs.
    assert isinstance(payload[0]["id"], str)
    assert isinstance(payload[0]["mcp_server_id"], str)
    assert payload[0]["id"] == str(tool.id)
    assert payload[0]["mcp_server_id"] == str(server.id)


def _oauth_server(db_session, test_user):
    server = MCPServer(
        name="OAuth Server",
        url="https://ext.example/mcp",
        transport="http-streaming",
        auth_type="oauth",
        account_id=test_user.account_id,
        status="active",
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


def test_oauth_authorize_token_is_short_lived_and_scoped(
    client: TestClient, db_session, test_user
):
    """The mint endpoint returns a purpose- and server-scoped token."""
    import jwt

    from preloop.api.auth.jwt import ALGORITHM, SECRET_KEY

    server = _oauth_server(db_session, test_user)
    resp = client.post(f"/api/v1/mcp-servers/{server.id}/oauth/authorize-token")
    assert resp.status_code == 200
    token = resp.json()["authorize_token"]

    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    assert payload["purpose"] == "mcp_oauth_authorize"
    assert payload["server_id"] == str(server.id)
    assert payload["sub"] == str(test_user.id)


def test_oauth_authorize_rejects_plain_access_token(
    client: TestClient, db_session, test_user
):
    """A reusable access token must NOT be accepted at the authorize endpoint."""
    from preloop.api.auth.jwt import create_access_token

    server = _oauth_server(db_session, test_user)
    # A normal access token (no purpose claim) — the old vulnerable form.
    plain = create_access_token({"sub": str(test_user.id), "scopes": []})
    resp = client.get(
        f"/api/v1/mcp-servers/{server.id}/oauth/authorize?code={plain}",
        follow_redirects=False,
    )
    assert resp.status_code == 401


def test_oauth_authorize_rejects_token_for_other_server(
    client: TestClient, db_session, test_user
):
    """An authorize token minted for one server can't be used for another."""
    from datetime import timedelta

    from preloop.api.auth.jwt import create_access_token

    server = _oauth_server(db_session, test_user)
    other_token = create_access_token(
        {
            "sub": str(test_user.id),
            "purpose": "mcp_oauth_authorize",
            "server_id": str(uuid.uuid4()),
        },
        expires_delta=timedelta(seconds=120),
    )
    resp = client.get(
        f"/api/v1/mcp-servers/{server.id}/oauth/authorize?code={other_token}",
        follow_redirects=False,
    )
    assert resp.status_code == 401


def test_update_mcp_server_success(client: TestClient, db_session, test_user):
    """Test updating an MCP server."""
    server = MCPServer(
        name="Original Name",
        url="http://localhost:8080/mcp",
        transport="http-streaming",
        auth_type="none",
        account_id=test_user.account_id,
        status="active",
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)

    update_data = {
        "name": "Updated Name",
        "status": "disabled",
    }

    response = client.put(f"/api/v1/mcp-servers/{server.id}", json=update_data)
    assert response.status_code == 200

    response_json = response.json()
    assert response_json["name"] == "Updated Name"
    assert response_json["status"] == "disabled"

    # Verify UUID serialization on update
    assert isinstance(response_json["id"], str)
    assert isinstance(response_json["account_id"], str)


def test_delete_mcp_server_success(client: TestClient, db_session, test_user):
    """Test deleting an MCP server."""
    server = MCPServer(
        name="Server to Delete",
        url="http://localhost:8080/mcp",
        transport="http-streaming",
        auth_type="none",
        account_id=test_user.account_id,
        status="active",
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)

    response = client.delete(f"/api/v1/mcp-servers/{server.id}")
    assert response.status_code == 200

    # Verify the server is deleted from database
    deleted = db_session.query(MCPServer).filter(MCPServer.id == server.id).first()
    assert deleted is None


def test_delete_mcp_server_not_found(client: TestClient, db_session, test_user):
    """Test deleting a non-existent MCP server."""
    fake_id = uuid.uuid4()
    response = client.delete(f"/api/v1/mcp-servers/{fake_id}")
    assert response.status_code == 404


def test_schema_serialization_with_real_model(db_session, test_user):
    """Direct unit test for schema serialization with real database model.

    This test verifies the fix at the schema level:
    - MCPServer model has account_id as UUID
    - MCPServerResponse schema should accept UUID and serialize to string
    """
    from preloop.models.schemas.mcp_server import MCPServerResponse

    # Create a real MCPServer model instance with UUID account_id
    server = MCPServer(
        name="Schema Test Server",
        url="http://localhost:8080/mcp",
        transport="http-streaming",
        auth_type="none",
        account_id=test_user.account_id,  # This is a UUID
        status="active",
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)

    # Verify the model has UUID types
    assert isinstance(server.id, uuid.UUID)
    assert isinstance(server.account_id, uuid.UUID)

    # This should not raise a ValidationError anymore
    response = MCPServerResponse.model_validate(server)

    # Verify the response has serialized UUIDs to strings
    response_dict = response.model_dump()
    assert isinstance(response_dict["id"], str)
    assert isinstance(response_dict["account_id"], str)
    assert response_dict["id"] == str(server.id)
    assert response_dict["account_id"] == str(server.account_id)


def _server_row(db_session, test_user, name="Evict Server", token="TOKEN-A"):
    server = MCPServer(
        name=name,
        url="http://localhost:8080/mcp",
        transport="http-streaming",
        auth_type="bearer",
        auth_config={"token": token},
        account_id=test_user.account_id,
        status="active",
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


def test_update_mcp_server_evicts_pooled_client(
    client: TestClient, db_session, test_user
):
    """PUT drops this process's pooled client so the new token is used (#1365)."""
    server = _server_row(db_session, test_user)
    pool = AsyncMock()
    with patch(
        "preloop.services.mcp_client_pool.get_mcp_client_pool", return_value=pool
    ):
        response = client.put(
            f"/api/v1/mcp-servers/{server.id}",
            json={"auth_config": {"token": "TOKEN-B"}},
        )
    assert response.status_code == 200
    pool.close_client.assert_awaited_once_with(str(server.id))
    db_session.refresh(server)
    assert server.auth_config == {"token": "TOKEN-B"}


def test_delete_mcp_server_evicts_client_and_unregisters_tools(
    client: TestClient, db_session, test_user
):
    """DELETE evicts the pooled client and removes local wrappers (#1365, #1366)."""
    from preloop.services import mcp_http

    server = _server_row(db_session, test_user, name="Delete Evict")
    db_session.add(
        MCPTool(
            mcp_server_id=server.id,
            name="read_scope",
            description="d",
            input_schema={"type": "object", "properties": {}},
            discovered_at="2026-01-01T00:00:00Z",
        )
    )
    db_session.commit()
    pool = AsyncMock()
    fake_mcp = MagicMock()
    with (
        patch(
            "preloop.services.mcp_client_pool.get_mcp_client_pool",
            return_value=pool,
        ),
        patch.object(mcp_http, "_mcp_server_instance", fake_mcp),
    ):
        response = client.delete(f"/api/v1/mcp-servers/{server.id}")
    assert response.status_code == 200
    pool.close_client.assert_awaited_once_with(str(server.id))
    fake_mcp.unregister_proxied_tools.assert_called_once_with(
        str(test_user.account_id), str(server.id), ["read_scope"]
    )


def test_create_mcp_server_authentication_error_is_actionable_and_safe(
    client: TestClient, db_session, mock_mcp_client
):
    """Failed validation persists the safe cause rather than a TaskGroup wrapper."""
    import httpx

    request = httpx.Request("GET", "https://example.com/mcp?token=private")
    response = httpx.Response(401, request=request)
    error = httpx.HTTPStatusError("token=private", request=request, response=response)
    mock_mcp_client.connect.side_effect = ExceptionGroup("opaque TaskGroup", [error])
    result = client.post(
        "/api/v1/mcp-servers",
        json={
            "name": "Example auth server",
            "url": "https://example.com/mcp",
            "transport": "http-streaming",
            "auth_type": "none",
        },
    )
    assert result.status_code == 201
    assert result.json()["status"] == "error"
    assert "HTTP 401" in result.json()["last_error"]
    assert "authentication" in result.json()["last_error"]
    assert "private" not in result.json()["last_error"]
    assert "TaskGroup" not in result.json()["last_error"]


@pytest.fixture
def synthetic_oauth(monkeypatch):
    """A complete synthetic public-client provider, including separate issuer."""
    import httpx
    from preloop.services.upstream_mcp_oauth import OAuthHTTP
    from preloop.api.endpoints import mcp_servers

    requests = []
    resource = "https://ext.example/mcp"
    issuer = "https://identity.example.com"

    async def resolve(host, port):
        return ["93.184.216.34"]

    def handle(request):
        requests.append(request)
        path = request.url.path
        if path == "/mcp":
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="https://ext.example/metadata"'
                },
            )
        if path == "/metadata":
            return httpx.Response(
                200, json={"resource": resource, "authorization_servers": [issuer]}
            )
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "issuer": issuer,
                    "authorization_endpoint": issuer + "/authorize",
                    "token_endpoint": issuer + "/token",
                    "registration_endpoint": issuer + "/register",
                },
            )
        if path == "/register":
            return httpx.Response(201, json={"client_id": "synthetic-public-client"})
        if path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-access-token",
                    "refresh_token": "synthetic-refresh-token",
                    "expires_in": 3600,
                },
            )
        return httpx.Response(404)

    monkeypatch.setattr(
        mcp_servers,
        "OAuthHTTP",
        lambda: OAuthHTTP(transport=httpx.MockTransport(handle), resolver=resolve),
    )
    return requests


def test_oauth_durable_callback_and_disconnect(
    client, db_session, test_user, synthetic_oauth
):
    from urllib.parse import parse_qs, urlsplit
    from preloop.models import models

    server = _oauth_server(db_session, test_user)
    ticket = client.post(
        f"/api/v1/mcp-servers/{server.id}/oauth/authorize-token"
    ).json()["authorize_token"]
    authorization = client.get(
        f"/api/v1/mcp-servers/{server.id}/oauth/authorize",
        params={"code": ticket},
        follow_redirects=False,
    )
    assert authorization.status_code == 302
    assert authorization.headers["referrer-policy"] == "no-referrer"
    state = parse_qs(urlsplit(authorization.headers["location"]).query)["state"][0]
    assert (
        client.get(f"/api/v1/mcp-servers/{server.id}").json()["connection_state"]
        == "connecting"
    )
    callback = client.get(
        "/api/v1/mcp-servers/oauth/callback",
        params={"code": "synthetic-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 302
    assert callback.headers["location"].endswith("setup_mcp=success")
    read = client.get(f"/api/v1/mcp-servers/{server.id}").json()
    assert read["connection_state"] == "connected"
    assert read["auth_config"]["access_token"] == "***REDACTED***"
    assert "synthetic-access-token" not in str(read)
    assert "synthetic-refresh-token" not in str(read)
    # Echoed write-only markers must preserve the encrypted grant.
    edit = client.put(
        f"/api/v1/mcp-servers/{server.id}",
        json={"auth_config": read["auth_config"], "name": "Renamed"},
    )
    assert edit.status_code == 200
    assert edit.json()["connection_state"] == "connected"
    assert (
        client.get(
            "/api/v1/mcp-servers/oauth/callback",
            params={"code": "synthetic-code", "state": state},
        ).status_code
        == 400
    )
    assert sum(request.url.path == "/token" for request in synthetic_oauth) == 1
    disconnected = client.post(f"/api/v1/mcp-servers/{server.id}/oauth/disconnect")
    assert disconnected.status_code == 200
    assert disconnected.json()["connection_state"] == "disconnected"
    assert "access_token" not in disconnected.json()["auth_config"]
    assert db_session.query(models.UpstreamMCPOAuthTransaction).count() == 0


def test_callback_wrong_browser_and_provider_denial_do_not_exchange(
    client, db_session, test_user, synthetic_oauth
):
    from urllib.parse import parse_qs, urlsplit

    server = _oauth_server(db_session, test_user)
    ticket = client.post(
        f"/api/v1/mcp-servers/{server.id}/oauth/authorize-token"
    ).json()["authorize_token"]
    authorization = client.get(
        f"/api/v1/mcp-servers/{server.id}/oauth/authorize",
        params={"code": ticket},
        follow_redirects=False,
    )
    state = parse_qs(urlsplit(authorization.headers["location"]).query)["state"][0]
    cookies = dict(client.cookies)
    client.cookies.clear()
    bad = client.get(
        "/api/v1/mcp-servers/oauth/callback",
        params={"state": state, "code": "synthetic-code"},
    )
    assert bad.status_code == 400
    client.cookies.update(cookies)
    denied = client.get(
        "/api/v1/mcp-servers/oauth/callback",
        params={"state": state, "error": "access_denied"},
        follow_redirects=False,
    )
    assert denied.headers["location"].endswith("setup_mcp=error")
    assert not any(request.url.path == "/token" for request in synthetic_oauth)
    assert (
        client.get(f"/api/v1/mcp-servers/{server.id}").json()["connection_state"]
        == "reconnect_required"
    )
