"""Tests for short-lived WebSocket database sessions."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.services.session_manager import SessionManager, WebSocketSession


@pytest.mark.asyncio
async def test_create_session_uses_short_lived_db() -> None:
    manager = SessionManager()
    websocket = MagicMock()
    captured: dict[str, object] = {}

    async def fake_run_db_async(operation):
        db = MagicMock()
        captured["operation"] = operation
        return operation(db)

    with patch(
        "preloop.services.session_manager.run_db_async",
        side_effect=fake_run_db_async,
    ):
        session = await manager.create_session(
            websocket=websocket,
            user=None,
            fingerprint="abc12345",
            ip_address="127.0.0.1",
            user_agent="test",
        )

    assert isinstance(session, WebSocketSession)
    assert session.fingerprint == "abc12345"
    assert "operation" in captured
    db = MagicMock()
    captured["operation"](db)
    db.add.assert_called_once()
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_end_session_does_not_require_request_scoped_db() -> None:
    manager = SessionManager()
    session_id = str(uuid.uuid4())
    manager.sessions[session_id] = WebSocketSession(
        id=session_id,
        connection_id=str(uuid.uuid4()),
        websocket=MagicMock(),
        user_id=None,
        account_id=None,
        username=None,
        fingerprint="abc12345",
        ip_address="127.0.0.1",
        user_agent="test",
        connected_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
        metadata={},
    )

    with patch(
        "preloop.services.session_manager.run_db_async",
        new=AsyncMock(return_value=None),
    ) as mock_run_db:
        await manager.end_session(session_id)

    mock_run_db.assert_awaited_once()
    assert session_id not in manager.sessions


@pytest.mark.asyncio
async def test_upgrade_session_persists_without_request_scoped_db() -> None:
    manager = SessionManager()
    session_id = str(uuid.uuid4())
    manager.sessions[session_id] = WebSocketSession(
        id=session_id,
        connection_id=str(uuid.uuid4()),
        websocket=MagicMock(),
        user_id=None,
        account_id=None,
        username=None,
        fingerprint="abc12345",
        ip_address="127.0.0.1",
        user_agent="test",
        connected_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
        metadata={},
    )

    user = MagicMock()
    user.id = uuid.uuid4()
    user.account_id = uuid.uuid4()
    user.username = "alice"

    with patch(
        "preloop.services.session_manager.run_db_async",
        new=AsyncMock(return_value=None),
    ) as mock_run_db:
        updated = await manager.upgrade_session(session_id, user)

    assert updated is not None
    assert updated.username == "alice"
    mock_run_db.assert_awaited_once()
