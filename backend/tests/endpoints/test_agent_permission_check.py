"""Endpoint tests for ``POST /api/v1/agents/permission-check``.

The OpenCode runtime plugin (``@preloop-ai/opencode-plugin``) calls this
endpoint from its ``tool.execute.before`` hook with ``source: "opencode"``.
These tests pin that the endpoint accepts that source, forwards it to the
permission service unchanged, and stamps it into ``tool_input`` as the
``_preloop_source`` marker approver surfaces read.
"""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session, sessionmaker

PERMISSION_CHECK_URL = "/api/v1/agents/permission-check"
TOKEN_URL = "/api/v1/auth/runtime-sessions/token"


@pytest.fixture(autouse=True)
def permission_identity_session(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker owns its Session while sharing the test's rollback transaction."""
    factory = sessionmaker(
        bind=db_session.connection(), join_transaction_mode="create_savepoint"
    )
    monkeypatch.setattr(
        "preloop.api.endpoints.agent_permission.get_session_factory", lambda: factory
    )


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


def test_permission_check_accepts_source_opencode_and_stamps_marker(client, db_session):
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
        # The hook carries operator notes back with the decision; this session
        # has none, which is the common case and costs nothing.
        "operator_note": None,
    }
    decide.assert_awaited_once()
    kwargs = decide.await_args.kwargs
    assert kwargs["source"] == "opencode"
    assert kwargs["tool_name"] == "Bash"
    assert kwargs["tool_input"]["_preloop_source"] == "opencode"
    assert kwargs["tool_input"]["command"] == "npm test"
    assert kwargs["tool_input"]["cwd"] == "/home/dev/project"
    assert kwargs["managed_agent_name"] == "Laptop OpenCode"
    from preloop.models.crud import crud_api_key

    assert kwargs["api_key_id"] == crud_api_key.get_by_key(db_session, key=token).id
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


_REPOSITORY = {
    "remote": "github.com/example/repo",
    "toplevel": "/tmp/example",
    "relative_path": "sub/dir",
    "source": "hook_cwd",
}


def test_permission_check_stores_repository_marker(client, db_session):
    """A hook repository observation is stored beside the source marker."""
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
                "tool_input": {
                    "command": "npm test",
                    "_preloop_repository": {"remote": "spoofed.example/nope"},
                },
                "repository": _REPOSITORY,
            },
        )

    assert response.status_code == 200, response.text
    stored = decide.await_args.kwargs["tool_input"]
    assert stored["_preloop_repository"] == {
        **_REPOSITORY,
        "no_remote": False,
    }
    assert stored["_preloop_source"] == "opencode"
    assert stored["command"] == "npm test"
    assert "spoofed.example" not in str(stored["_preloop_repository"])


def test_permission_check_omits_repository_marker_when_absent(client):
    """No repository field means no marker, even if tool_input tries to set one."""
    token = _issue_opencode_runtime_token(client)
    decide = AsyncMock(return_value=("allow", "", "req-1", False))

    with patch(
        "preloop.api.endpoints.agent_permission.request_agent_permission", decide
    ):
        response = _post_permission_check(
            client,
            token,
            {
                "source": "opencode",
                "tool_name": "Bash",
                "tool_input": {
                    "_preloop_repository": {"remote": "spoofed.example/nope"},
                },
            },
        )

    assert response.status_code == 200, response.text
    assert "_preloop_repository" not in decide.await_args.kwargs["tool_input"]


def test_permission_check_rejects_invalid_repository(client):
    """Oversized fields and unknown keys are rejected before a decision."""
    token = _issue_opencode_runtime_token(client)
    oversized = _post_permission_check(
        client,
        token,
        {
            "tool_name": "Bash",
            "repository": {**_REPOSITORY, "remote": "github.com/" + ("a" * 512)},
        },
    )
    assert oversized.status_code == 422, oversized.text

    multibyte = _post_permission_check(
        client,
        token,
        {
            "tool_name": "Bash",
            "repository": {**_REPOSITORY, "toplevel": "é" * 300},
        },
    )
    assert multibyte.status_code == 422, multibyte.text

    extra = _post_permission_check(
        client,
        token,
        {
            "tool_name": "Bash",
            "repository": {**_REPOSITORY, "policy_scope": "example/repo"},
        },
    )
    assert extra.status_code == 422, extra.text


def test_permission_check_puts_repository_on_approval_tool_args(client, db_session):
    """The marker is stored on the approval row's tool_args, not a new column."""
    from datetime import datetime, timezone

    from preloop.models import models
    from preloop.models.crud import crud_api_key

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
                "tool_input": {"command": "git status"},
                "repository": _REPOSITORY,
            },
        )

    assert response.status_code == 200, response.text
    tool_input = decide.await_args.kwargs["tool_input"]
    api_key = crud_api_key.get_by_key(db_session, key=token)
    workflow = models.ApprovalWorkflow(
        account_id=api_key.account_id,
        name="Agent Tool Approvals",
        workflow_type="simple",
    )
    tool = models.ToolConfiguration(
        account_id=api_key.account_id,
        tool_name="Bash",
        tool_source="agent",
    )
    db_session.add_all([workflow, tool])
    db_session.flush()
    # request_agent_permission persists this same dict as ApprovalRequest.tool_args.
    approval = models.ApprovalRequest(
        account_id=api_key.account_id,
        tool_configuration_id=tool.id,
        approval_workflow_id=workflow.id,
        tool_name="Bash",
        tool_args=tool_input,
        requested_at=datetime.now(timezone.utc),
        status="pending",
    )
    db_session.add(approval)
    db_session.flush()
    assert approval.tool_args["_preloop_repository"]["remote"] == (
        "github.com/example/repo"
    )
    assert approval.tool_args["_preloop_repository"]["relative_path"] == "sub/dir"
    assert approval.tool_args["command"] == "git status"
    assert approval.tool_args["_preloop_source"] == "opencode"


def test_permission_check_requires_runtime_bearer(client):
    """Without a runtime bearer token the endpoint rejects the call."""
    response = client.post(
        PERMISSION_CHECK_URL,
        json={"source": "opencode", "tool_name": "Bash"},
    )
    assert response.status_code == 401
