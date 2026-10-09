"""API surface for same-named MCP tools (#1135): warnings, prefix, audit."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from preloop.models.models.mcp_server import MCPServer

SHADOW_TEXT = (
    "Tool 'read_scope' on MCP server 'newer' is shadowed by MCP server "
    "'older', which was added earlier and exposes the same name. Agents see "
    "and call only the tool from 'older'. Set a tool prefix on this server to "
    "expose both."
)


@pytest.fixture(autouse=True)
def mock_event_bus_connect():
    with patch(
        "preloop.sync.services.event_bus.EventBus.connect", new_callable=AsyncMock
    ):
        yield


@pytest.fixture(autouse=True)
def upstream():
    """Every server URL exposes ``read_scope``; validation always succeeds."""
    client = MagicMock()
    client.list_tools = AsyncMock(
        return_value=[
            SimpleNamespace(name="read_scope", description="d", inputSchema={})
        ]
    )
    pool = MagicMock(
        get_client=AsyncMock(return_value=client), close_client=AsyncMock()
    )
    with (
        patch("preloop.services.mcp_client_pool.MCPClient") as validator,
        patch(
            "preloop.services.mcp_tool_discovery.get_mcp_client_pool",
            return_value=pool,
        ),
        patch(
            "preloop.services.mcp_client_pool.get_mcp_client_pool",
            return_value=pool,
        ),
    ):
        validator.return_value = AsyncMock()
        yield client


@pytest.fixture
def audit_calls():
    calls = []
    with patch(
        "preloop.utils.audit.log_config_change",
        side_effect=lambda db, **kwargs: calls.append(kwargs),
    ):
        yield calls


def _create(client: TestClient, name: str, **extra):
    response = client.post(
        "/api/v1/mcp-servers",
        json={"name": name, "url": f"http://{name}.example.test/mcp", **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _age(db_session, server_id: str) -> None:
    server = db_session.get(MCPServer, server_id)
    server.created_at = datetime(2026, 1, 1)
    db_session.commit()


def _tools(client: TestClient, server_id: str):
    response = client.get(f"/api/v1/mcp-servers/{server_id}/tools")
    assert response.status_code == 200, response.text
    return response.json()


def _collisions(calls):
    return [c for c in calls if c["config_type"] == "mcp_tool_collision"]


def test_create_newer_server_returns_shadow_warning_and_audits(
    client: TestClient, db_session, test_user, audit_calls
):
    older = _create(client, "older")
    assert older["warnings"] == []
    _age(db_session, older["id"])

    newer = _create(client, "newer")
    assert newer["warnings"] == [SHADOW_TEXT]
    assert newer["tool_prefix"] is None

    [tool] = _tools(client, newer["id"])
    assert tool["shadowed"] is True
    assert tool["exposed_name"] == "read_scope"
    assert tool["warnings"] == [SHADOW_TEXT]
    assert _tools(client, older["id"])[0]["shadowed"] is False

    [event] = _collisions(audit_calls)
    assert event["action"] == "shadowed"
    assert event["new_value"] == {
        "owner_server_id": older["id"],
        "owner_server_name": "older",
        "shadowed_server_id": newer["id"],
        "shadowed_server_name": "newer",
        "tool_names": ["read_scope"],
    }

    # Scan and detail repeat the warning; the audit event is not repeated.
    scan = client.post(f"/api/v1/mcp-servers/{newer['id']}/scan")
    assert scan.status_code == 200, scan.text
    assert scan.json()["warnings"] == [SHADOW_TEXT]
    detail = client.get(f"/api/v1/mcp-servers/{newer['id']}").json()
    assert detail["warnings"] == [SHADOW_TEXT]
    listed = {
        s["name"]: s["warnings"] for s in client.get("/api/v1/mcp-servers").json()
    }
    assert listed == {"older": [], "newer": [SHADOW_TEXT]}
    assert len(_collisions(audit_calls)) == 1

    # Console Tools page data: the shadowed row is flagged with the text.
    rows = client.get("/api/v1/tools/summary").json()
    flagged = [r for r in rows if r["source"] == "mcp" and r["shadowed"]]
    assert [(r["source_name"], r["warnings"]) for r in flagged] == [
        ("newer", [SHADOW_TEXT])
    ]


def test_prefix_on_update_exposes_both_and_clears_warning(
    client: TestClient, db_session, test_user, audit_calls
):
    older = _create(client, "older")
    _age(db_session, older["id"])
    newer = _create(client, "newer")

    updated = client.put(
        f"/api/v1/mcp-servers/{newer['id']}", json={"tool_prefix": "crm"}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["tool_prefix"] == "crm"
    assert updated.json()["warnings"] == []
    [tool] = _tools(client, newer["id"])
    assert (tool["exposed_name"], tool["shadowed"]) == ("crm_read_scope", False)
    assert [e["action"] for e in _collisions(audit_calls)] == [
        "shadowed",
        "unshadowed",
    ]
    names = {
        r["name"]
        for r in client.get("/api/v1/tools/summary").json()
        if r["source"] == "mcp"
    }
    assert names == {"read_scope", "crm_read_scope"}


def test_create_with_prefix_never_shadows(client: TestClient, db_session, test_user):
    older = _create(client, "older")
    _age(db_session, older["id"])
    newer = _create(client, "newer", tool_prefix="crm")
    assert newer["warnings"] == []
    assert _tools(client, newer["id"])[0]["exposed_name"] == "crm_read_scope"


@pytest.mark.parametrize("how", ["delete", "disable"])
def test_owner_delete_or_disable_unshadows(
    client: TestClient, db_session, test_user, audit_calls, how
):
    older = _create(client, "older")
    _age(db_session, older["id"])
    newer = _create(client, "newer")
    assert _tools(client, newer["id"])[0]["shadowed"] is True

    if how == "delete":
        response = client.delete(f"/api/v1/mcp-servers/{older['id']}")
    else:
        response = client.put(
            f"/api/v1/mcp-servers/{older['id']}", json={"status": "disabled"}
        )
    assert response.status_code == 200, response.text
    [tool] = _tools(client, newer["id"])
    assert tool["shadowed"] is False and tool["warnings"] == []
    assert _collisions(audit_calls)[-1]["action"] == "unshadowed"
    assert client.get(f"/api/v1/mcp-servers/{newer['id']}").json()["warnings"] == []


@pytest.mark.parametrize("prefix", ["CRM", "crm-x", "a" * 33])
def test_invalid_prefix_is_rejected(client: TestClient, test_user, prefix):
    response = client.post(
        "/api/v1/mcp-servers",
        json={
            "name": "bad",
            "url": "http://bad.example.test/mcp",
            "tool_prefix": prefix,
        },
    )
    assert response.status_code == 422
