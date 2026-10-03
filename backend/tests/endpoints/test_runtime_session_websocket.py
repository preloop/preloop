"""The session-scoped websocket behind `preloop sessions attach` (#1149)."""

from datetime import UTC, datetime
from typing import Any, Callable, get_type_hints

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from preloop.api.endpoints import websockets as ws_endpoint
from preloop.models import models
from preloop.models.crud import crud_account
from preloop.services.websocket_manager import manager


@pytest.mark.parametrize(
    ("name", "parameter"),
    [
        ("_session_read_allowed", "current_user"),
        ("_approval_read_allowed", "current_user"),
        ("_authorize_session_attach", "user"),
    ],
)
def test_session_authorizer_annotations_resolve(name: str, parameter: str) -> None:
    """Catch undefined annotations even on Python with deferred evaluation."""
    from inspect import unwrap

    authorizer = unwrap(getattr(ws_endpoint, name))
    assert get_type_hints(authorizer)[parameter] is models.User


@pytest.fixture
def attach_env(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> dict:
    """Route the endpoint's DB work and token check to the test transaction."""

    async def run_in_test_session(operation: Callable[[Session], Any]) -> Any:
        return operation(db_session)

    async def resolve(token: str) -> Any:
        return test_user if token == "good-token" else None

    monkeypatch.setattr(ws_endpoint, "run_db_async", run_in_test_session)
    monkeypatch.setattr(ws_endpoint, "_resolve_token_user", resolve)
    now = datetime.now(UTC).replace(tzinfo=None)
    session = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="attach-me",
        started_at=now,
    )
    db_session.add(session)
    db_session.flush()
    return {"session_id": str(session.id), "account_id": str(test_user.account_id)}


def _path(session_id: str, query: str = "") -> str:
    return f"/api/v1/ws/runtime-sessions/{session_id}{query}"


def _audit_actions(db: Session, session_id: str) -> list[tuple[str, dict]]:
    rows = (
        db.query(models.AuditLog)
        .filter(models.AuditLog.resource_id == session_id)
        .order_by(models.AuditLog.timestamp)
        .all()
    )
    return [(row.action, row.details or {}) for row in rows]


def test_attach_streams_the_session_and_audits_attach_and_detach(
    client: TestClient, db_session: Session, attach_env: dict
) -> None:
    session_id = attach_env["session_id"]
    with client.websocket_connect(
        _path(session_id, "?read_only=1"),
        headers={"Authorization": "Bearer good-token"},
    ) as socket:
        hello = socket.receive_json()
        assert hello["type"] == "attached"
        assert hello["runtime_session_id"] == session_id
        assert hello["ended_at"] is None
        socket.send_json({"type": "ping"})
        assert socket.receive_json() == {"type": "pong"}

        (connection_id,) = [
            cid
            for cid, stream in manager.session_streams.items()
            if stream.runtime_session_id == session_id
        ]
        assert manager.connection_accounts[connection_id] == attach_env["account_id"]

    assert not any(
        stream.runtime_session_id == session_id
        for stream in manager.session_streams.values()
    )
    actions = _audit_actions(db_session, session_id)
    assert [action for action, _ in actions] == [
        ws_endpoint.SESSION_ATTACH_AUDIT_ATTACHED,
        ws_endpoint.SESSION_ATTACH_AUDIT_DETACHED,
    ]
    assert actions[0][1]["read_only"] is True


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer bad-token"}])
def test_attach_without_a_valid_token_is_refused(
    client: TestClient, attach_env: dict, headers: dict
) -> None:
    with client.websocket_connect(
        _path(attach_env["session_id"]), headers=headers
    ) as socket:
        assert socket.receive_json()["error"] == "unauthorized"
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == ws_endpoint.SESSION_ATTACH_CLOSE_UNAUTHORIZED


def test_attach_without_session_read_is_refused(
    client: TestClient, attach_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(**_: Any) -> bool:
        raise HTTPException(status_code=403, detail="Permission denied")

    monkeypatch.setattr(ws_endpoint, "_session_read_allowed", denied)
    with client.websocket_connect(
        _path(attach_env["session_id"]), headers={"Authorization": "Bearer good-token"}
    ) as socket:
        refusal = socket.receive_json()
        assert refusal["error"] == "forbidden"
        assert "session read" in refusal["detail"]
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == ws_endpoint.SESSION_ATTACH_CLOSE_FORBIDDEN


def test_another_accounts_session_is_not_found(
    client: TestClient, db_session: Session, attach_env: dict
) -> None:
    other = crud_account.create(db_session, obj_in={"organization_name": "Other"})
    foreign = models.RuntimeSession(
        account_id=other.id,
        session_source_type="claude_code",
        session_source_id="not-yours",
        started_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db_session.add(foreign)
    db_session.flush()

    for session_id in (str(foreign.id), "not-a-uuid"):
        with client.websocket_connect(
            _path(session_id), headers={"Authorization": "Bearer good-token"}
        ) as socket:
            assert socket.receive_json()["error"] == "not_found"
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
        assert closed.value.code == ws_endpoint.SESSION_ATTACH_CLOSE_NOT_FOUND


def test_unknown_execution_is_not_found(client: TestClient, attach_env: dict) -> None:
    with client.websocket_connect(
        _path(
            attach_env["session_id"],
            "?execution_id=9f2b0000-0000-4000-8000-000000000002",
        ),
        headers={"Authorization": "Bearer good-token"},
    ) as socket:
        assert socket.receive_json()["detail"] == "Flow execution not found"


def test_approvals_are_withheld_without_approval_read(
    client: TestClient, attach_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(**_: Any) -> bool:
        raise HTTPException(status_code=403, detail="Permission denied")

    monkeypatch.setattr(ws_endpoint, "_approval_read_allowed", denied)
    with client.websocket_connect(
        _path(attach_env["session_id"]), headers={"Authorization": "Bearer good-token"}
    ) as socket:
        hello = socket.receive_json()
        assert hello["approvals_visible"] is False
        (stream,) = [
            s
            for s in manager.session_streams.values()
            if s.runtime_session_id == attach_env["session_id"]
        ]
        assert stream.approvals_visible is False
