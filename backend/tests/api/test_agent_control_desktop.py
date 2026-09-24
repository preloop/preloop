"""Desktop capability on the Agent Control snapshot and managed-agent API."""

from __future__ import annotations

from preloop.api.endpoints.agent_control import (
    AgentControlConnectionManager,
    agent_control_manager,
)
from preloop.models.crud import crud_managed_agent, crud_managed_agent_enrollment


def test_snapshot_desktop_follows_presence_capabilities() -> None:
    """A presence envelope's desktop key is normalized onto the snapshot."""
    manager = AgentControlConnectionManager()
    manager.record_presence(
        "agent-desktop",
        {
            "capabilities": {
                "desktop": "vnc",
                "desktop_display": ":99",
                "interrupt": True,
            }
        },
    )
    snapshot = manager.snapshot("agent-desktop")
    assert snapshot["desktop"] == "vnc"
    assert snapshot["desktop_display"] == ":99"

    manager.record_presence(
        "agent-desktop",
        {"capabilities": {"desktop": "bogus", "desktop_display": ":99"}},
    )
    bogus = manager.snapshot("agent-desktop")
    assert bogus["desktop"] == "none"
    assert bogus["desktop_display"] is None

    manager.record_presence(
        "agent-desktop",
        {"capabilities": {"desktop": "rdp", "desktop_display": ":1"}},
    )
    assert manager.snapshot("agent-desktop")["desktop"] == "rdp"


def test_snapshot_desktop_survives_heartbeat_without_capabilities() -> None:
    """A heartbeat that omits capabilities must not clear the advertised desktop."""
    manager = AgentControlConnectionManager()
    manager.record_presence(
        "agent-desktop",
        {
            "capabilities": {
                "desktop": "vnc",
                "desktop_display": ":99",
                "interrupt": True,
            }
        },
    )
    manager.record_presence(
        "agent-desktop",
        {"observed_at": "2026-09-24T00:00:00+00:00"},
    )

    snapshot = manager.snapshot("agent-desktop")

    assert snapshot["desktop"] == "vnc"
    assert snapshot["desktop_display"] == ":99"
    assert snapshot["supports_interrupt"] is True


def test_managed_agent_api_includes_desktop(client, db_session, test_user) -> None:
    """GET /api/v1/agents/{id} carries the desktop field from presence."""
    enrolled = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "openclaw",
            "session_source_id": "openclaw-desktop-capability",
            "session_reference": "/tmp/openclaw.json",
            "runtime_principal_name": "OpenClaw Desktop Agent",
        },
    )
    assert enrolled.status_code == 201, enrolled.text
    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="openclaw",
        session_source_id="openclaw-desktop-capability",
    )
    assert managed_agent is not None
    crud_managed_agent_enrollment.create_for_agent(
        db_session,
        account_id=test_user.account_id,
        agent_id=managed_agent.id,
        created_by_user_id=test_user.id,
        enrollment_type="cli_managed_config",
        adapter_key="openclaw",
        managed_config={
            "preloop": {
                "control": {
                    "enabled": True,
                    "control_ws_url": "wss://example.com/api/v1/agents/control/ws",
                }
            }
        },
        validation_result={"control_channel_configured": True},
    )
    agent_id = str(managed_agent.id)
    agent_control_manager.record_presence(
        agent_id,
        {"capabilities": {"desktop": "vnc", "desktop_display": ":99"}},
    )
    try:
        detail = client.get(f"/api/v1/agents/{agent_id}")
        assert detail.status_code == 200, detail.text
        agent = detail.json()["agent"]
        assert agent["desktop"] == "vnc"
        assert agent["desktop_display"] == ":99"
    finally:
        agent_control_manager._presence.pop(agent_id, None)
