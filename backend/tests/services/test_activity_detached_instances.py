"""Regression tests for detached ORM instances after short-lived DB sessions."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from preloop.models.models import Event
from preloop.services.activity_tracker import (
    _activity_broadcast_payload,
    _persist_activity_event,
)


def test_activity_broadcast_payload_reads_attributes_before_session_close() -> None:
    activity = Event(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        event_type="page_view",
        timestamp=datetime.now(timezone.utc),
        path="/agents",
        action=None,
        event_data={"source": "test"},
    )

    payload = _activity_broadcast_payload(activity)

    assert payload["event_type"] == "page_view"
    assert payload["path"] == "/agents"
    assert payload["account_id"] == str(activity.account_id)


def test_persist_activity_event_returns_detached_payload() -> None:
    db = MagicMock()
    activity = Event(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        event_type="action",
        timestamp=datetime.now(timezone.utc),
        action="agent_onboarded",
        event_data={"agent": "claude-code"},
    )

    def _refresh(obj: Event) -> None:
        obj.id = uuid.uuid4()

    db.refresh.side_effect = _refresh

    payload = _persist_activity_event(db, activity)

    assert isinstance(payload, dict)
    assert payload["event_type"] == "action"
    assert payload["action"] == "agent_onboarded"
    assert payload["id"] == str(activity.id)
    db.add.assert_called_once()
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_handle_activity_does_not_touch_detached_event_after_persist() -> None:
    from unittest.mock import AsyncMock, patch

    from preloop.services.activity_tracker import handle_activity
    from preloop.services.session_manager import WebSocketSession

    session = WebSocketSession(
        id=str(uuid.uuid4()),
        connection_id=str(uuid.uuid4()),
        websocket=MagicMock(),
        user_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        username="alice",
        fingerprint="fp123456",
        ip_address="127.0.0.1",
        user_agent="test",
        connected_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
        metadata={},
    )

    payload = {
        "id": str(uuid.uuid4()),
        "session_id": session.id,
        "user_id": str(session.user_id),
        "account_id": str(session.account_id),
        "event_type": "page_view",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "path": "/agents",
        "action": None,
        "event_data": {},
    }

    with patch(
        "preloop.services.activity_tracker.run_db_async",
        new=AsyncMock(return_value=payload),
    ):
        with patch("preloop.services.activity_tracker.session_manager") as mock_sm:
            mock_sm.update_activity = MagicMock()
            with patch("preloop.sync.services.event_bus.event_bus_service") as mock_bus:
                mock_bus.nc.is_connected = True
                mock_bus.nc.publish = AsyncMock()
                await handle_activity(
                    {"event": "page_view", "path": "/agents"},
                    session,
                )

    mock_bus.nc.publish.assert_awaited_once()
