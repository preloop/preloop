"""Console "Enable desktop" through Agent Control (#1566).

The API sends ``desktop.install`` over the agent's control channel and reads
the plugin's ``command_result`` / ``command_error`` back from the persisted
command row.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.models import models
from preloop.models.crud import crud_managed_agent
from preloop.plugins import account_hooks
from preloop.plugins.account_hooks import Decision
from tests.endpoints.test_agent_control import (
    _issue_runtime_token,
    _mark_agent_control_configured,
)


@pytest.fixture
def nats():
    with patch("preloop.api.endpoints.agent_control.get_nats_client") as get:
        client = MagicMock()
        client.is_connected = True
        client.publish = AsyncMock()
        client.subscribe = AsyncMock(return_value=AsyncMock())
        get.return_value = client
        yield client


def _agent(client, db_session, test_user, source_id, *, configured=True):
    token = _issue_runtime_token(client, session_source_id=source_id)
    agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="openclaw",
        session_source_id=source_id,
    )
    if configured:
        _mark_agent_control_configured(db_session, test_user, agent)
    return agent, token["token"]


def _audit(db_session, action):
    db_session.expire_all()
    return (
        db_session.query(models.AuditLog).filter(models.AuditLog.action == action).all()
    )


def _reply(client, token, request_id, name, payload):
    with client.websocket_connect(f"/api/v1/agents/control/ws?token={token}") as ws:
        assert ws.receive_json()["type"] == "presence"
        ws.send_json(
            {
                "type": "status",
                "name": "command_ack",
                "message_id": f"ack-{request_id}",
                "payload": {"command_id": request_id},
            }
        )
        ws.send_json(
            {
                "type": "status",
                "name": name,
                "message_id": f"res-{request_id}",
                "payload": {"command_id": request_id, **payload},
            }
        )
        ws.send_json({"type": "heartbeat", "message_id": "hb-x", "payload": {}})
        while ws.receive_json().get("name") != "heartbeat":
            pass


def test_install_is_sent_over_agent_control_and_reports_done(
    nats, client, db_session, test_user
):
    agent, token = _agent(client, db_session, test_user, "openclaw-desk-ok")
    base = f"/api/v1/agents/{agent.id}/desktop/install"

    sent = client.post(base)
    assert sent.status_code == 202, sent.text
    request_id = sent.json()["request_id"]
    assert sent.json()["state"] == "pending"
    published = [
        call.args[1] for call in nats.publish.await_args_list if len(call.args) > 1
    ]
    assert any(b'"desktop.install"' in body for body in published)
    assert client.get(f"{base}/{request_id}").json() == {
        "request_id": request_id,
        "state": "pending",
        "error": None,
        "command": f"preloop desktop enable --agent {agent.id}",
    }

    _reply(
        client,
        token,
        request_id,
        "command_result",
        {"status": "completed", "desktop_install": {"ok": True}},
    )
    done = client.get(f"{base}/{request_id}").json()
    assert done["state"] == "done" and done["error"] is None
    assert len(_audit(db_session, "desktop.install.requested")) == 1
    assert len(_audit(db_session, "desktop.install.succeeded")) == 1


@pytest.mark.parametrize(
    ("name", "payload", "state", "error"),
    [
        (
            "command_error",
            {"status": "failed", "error": "desktop_unsupported_distro"},
            "failed",
            "desktop_unsupported_distro",
        ),
        (
            "command_result",
            {
                "status": "completed",
                "desktop_install": {"ok": False, "error": "desktop_privilege_required"},
            },
            "failed",
            "desktop_privilege_required",
        ),
        # A plugin that predates desktop.install: generic error or a
        # result without desktop_install both read as unsupported.
        (
            "command_error",
            {"status": "failed", "error": "unsupported agent-control command"},
            "unsupported",
            None,
        ),
        (
            "command_result",
            {"status": "completed", "reply_text": ""},
            "unsupported",
            None,
        ),
        # Any other plugin failure is a failure, not "unsupported".
        (
            "command_error",
            {"status": "failed", "error": "command_failed"},
            "failed",
            "desktop_failed",
        ),
    ],
)
def test_install_failures_and_old_plugins(
    nats, client, db_session, test_user, name, payload, state, error
):
    agent, token = _agent(client, db_session, test_user, f"openclaw-desk-{state}")
    base = f"/api/v1/agents/{agent.id}/desktop/install"
    request_id = client.post(base).json()["request_id"]

    _reply(client, token, request_id, name, payload)

    body = client.get(f"{base}/{request_id}").json()
    assert (body["state"], body["error"]) == (state, error)
    if state == "failed":
        assert len(_audit(db_session, "desktop.install.failed")) == 1


def test_without_a_control_channel_nothing_is_sent(nats, client, db_session, test_user):
    agent, _ = _agent(
        client, db_session, test_user, "openclaw-desk-none", configured=False
    )
    response = client.post(f"/api/v1/agents/{agent.id}/desktop/install")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "desktop_install_unavailable"
    assert detail["command"] == f"preloop desktop enable --agent {agent.id}"
    nats.publish.assert_not_awaited()
    assert _audit(db_session, "desktop.install.requested") == []


def test_install_needs_manage_agents(nats, client, db_session, test_user):
    agent, _ = _agent(client, db_session, test_user, "openclaw-desk-perm")

    def deny(ctx, action, resource):
        if action == "manage_agents":
            return Decision("deny", reason="no")
        return Decision("allow")

    account_hooks.register_authorizer(deny)
    try:
        response = client.post(f"/api/v1/agents/{agent.id}/desktop/install")
    finally:
        account_hooks.reset_account_hooks()
    assert response.status_code == 403
    nats.publish.assert_not_awaited()


def test_unknown_request_and_other_agents_are_404(nats, client, db_session, test_user):
    agent, _ = _agent(client, db_session, test_user, "openclaw-desk-a")
    other, _ = _agent(client, db_session, test_user, "openclaw-desk-b")
    request_id = client.post(f"/api/v1/agents/{agent.id}/desktop/install").json()[
        "request_id"
    ]
    assert (
        client.get(
            f"/api/v1/agents/{other.id}/desktop/install/{request_id}"
        ).status_code
        == 404
    )
    assert (
        client.get(f"/api/v1/agents/{agent.id}/desktop/install/nope").status_code == 404
    )


def test_a_repeated_result_is_audited_once(nats, client, db_session, test_user):
    agent, token = _agent(client, db_session, test_user, "openclaw-desk-twice")
    base = f"/api/v1/agents/{agent.id}/desktop/install"
    request_id = client.post(base).json()["request_id"]
    ok = {"status": "completed", "desktop_install": {"ok": True}}
    _reply(client, token, request_id, "command_result", ok)
    _reply(client, token, request_id, "command_result", ok)
    _reply(
        client,
        token,
        request_id,
        "command_result",
        {"status": "completed", "desktop_install": {"ok": False}},
    )
    assert client.get(f"{base}/{request_id}").json()["state"] == "done"
    assert len(_audit(db_session, "desktop.install.succeeded")) == 1
    assert _audit(db_session, "desktop.install.failed") == []


def test_malformed_agent_id_is_404(nats, client):
    assert client.post("/api/v1/agents/not-a-uuid/desktop/install").status_code == 404


def test_offline_control_socket_is_the_structured_409(
    nats, client, db_session, test_user
):
    agent, _ = _agent(client, db_session, test_user, "openclaw-desk-offline")
    agent.runtime_session_id = None
    db_session.commit()
    response = client.post(f"/api/v1/agents/{agent.id}/desktop/install")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "desktop_install_unavailable"
    assert detail["command"] == f"preloop desktop enable --agent {agent.id}"
