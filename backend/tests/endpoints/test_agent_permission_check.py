"""Endpoint tests for ``POST /api/v1/agents/permission-check``.

The OpenCode runtime plugin (``@preloop-ai/opencode-plugin``) calls this
endpoint from its ``tool.execute.before`` hook with ``source: "opencode"``.
These tests pin that the endpoint accepts that source, forwards it to the
permission service unchanged, and stamps it into ``tool_input`` as the
``_preloop_source`` marker approver surfaces read.
"""

from unittest.mock import AsyncMock, patch

PERMISSION_CHECK_URL = "/api/v1/agents/permission-check"
TOKEN_URL = "/api/v1/auth/runtime-sessions/token"


def _issue_opencode_runtime_token(client) -> str:
    response = client.post(
        TOKEN_URL,
        json={
            "session_source_type": "opencode",
            "session_source_id": "opencode-laptop",
            "session_reference": "/home/dev/.config/opencode/opencode.json",
            "runtime_principal_name": "Laptop OpenCode",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["token"]


def _post_permission_check(client, token: str, payload: dict):
    return client.post(
        PERMISSION_CHECK_URL,
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )


def test_permission_check_accepts_source_opencode_and_stamps_marker(client):
    """A Bash call from the OpenCode plugin reaches the service with its source."""
    token = _issue_opencode_runtime_token(client)
    decide = AsyncMock(return_value=("allow", "Approved via Preloop.", "req-1", False))

    with patch(
        "preloop.api.endpoints.agent_permission.request_agent_permission", decide
    ):
        response = _post_permission_check(
            client,
            token,
            {
                "source": "opencode",
                "tool_name": "Bash",
                "tool_input": {"command": "npm test", "description": "run tests"},
                "session_id": "ses_abc",
                "cwd": "/home/dev/project",
                "agent_reasoning": "run tests",
            },
        )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "decision": "allow",
        "reason": "Approved via Preloop.",
        "request_id": "req-1",
        "timed_out": False,
    }
    decide.assert_awaited_once()
    kwargs = decide.await_args.kwargs
    assert kwargs["source"] == "opencode"
    assert kwargs["tool_name"] == "Bash"
    assert kwargs["tool_input"]["_preloop_source"] == "opencode"
    assert kwargs["tool_input"]["command"] == "npm test"
    assert kwargs["tool_input"]["cwd"] == "/home/dev/project"
    assert kwargs["managed_agent_name"] == "Laptop OpenCode"
    assert kwargs["client_decision"] is None


def test_permission_check_returns_deny_for_opencode_edit(client):
    """Denies (including timed-out ones) are passed through verbatim."""
    token = _issue_opencode_runtime_token(client)
    decide = AsyncMock(return_value=("deny", "Approval timed out", "req-2", True))

    with patch(
        "preloop.api.endpoints.agent_permission.request_agent_permission", decide
    ):
        response = _post_permission_check(
            client,
            token,
            {
                "source": "opencode",
                "tool_name": "Edit",
                "tool_input": {"file_path": "/etc/hosts", "filePath": "/etc/hosts"},
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision"] == "deny"
    assert body["reason"] == "Approval timed out"
    assert body["timed_out"] is True
    assert decide.await_args.kwargs["tool_input"]["_preloop_source"] == "opencode"
    assert decide.await_args.kwargs["tool_input"]["file_path"] == "/etc/hosts"


def test_permission_check_requires_runtime_bearer(client):
    """Without a runtime bearer token the endpoint rejects the call."""
    response = client.post(
        PERMISSION_CHECK_URL,
        json={"source": "opencode", "tool_name": "Bash"},
    )
    assert response.status_code == 401
