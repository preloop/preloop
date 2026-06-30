"""Unit tests for the in-memory WebSocket SessionManager."""

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from preloop.models.models import Event
from preloop.services.session_manager import SessionManager, WebSocketSession


@pytest.fixture
def captured_events():
    """Patch run_db_async to capture persisted Event objects without a DB."""
    events: list[Event] = []

    async def _run_db_async(operation):
        db = MagicMock()

        def _add(obj):
            events.append(obj)

        db.add.side_effect = _add
        return operation(db)

    with patch(
        "preloop.services.session_manager.run_db_async",
        side_effect=_run_db_async,
    ):
        yield events


@pytest.fixture
def mock_user():
    user = MagicMock()
    user.id = uuid.uuid4()
    user.account_id = uuid.uuid4()
    user.username = "alice"
    return user


@pytest.fixture
def manager():
    return SessionManager()


def _make_ws():
    return MagicMock(name="websocket")


# --- WebSocketSession dataclass --------------------------------------------


def _build_session(**overrides):
    now = datetime.now(timezone.utc)
    defaults = dict(
        id=str(uuid.uuid4()),
        connection_id=str(uuid.uuid4()),
        websocket=_make_ws(),
        user_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        username="bob",
        fingerprint=None,
        ip_address="1.2.3.4",
        user_agent="agent",
        connected_at=now,
        last_activity=now,
    )
    defaults.update(overrides)
    return WebSocketSession(**defaults)


def test_session_is_authenticated_true_with_user_id():
    session = _build_session(user_id=uuid.uuid4())
    assert session.is_authenticated is True


def test_session_is_authenticated_false_without_user_id():
    session = _build_session(user_id=None)
    assert session.is_authenticated is False


def test_display_name_prefers_username():
    session = _build_session(username="charlie")
    assert session.display_name == "charlie"


def test_display_name_falls_back_to_user_id():
    uid = uuid.uuid4()
    session = _build_session(username=None, user_id=uid)
    assert session.display_name == str(uid)


def test_display_name_uses_fingerprint_for_anonymous():
    session = _build_session(username=None, user_id=None, fingerprint="abcdefgh12345")
    assert session.display_name == "anonymous abcdefgh"


def test_display_name_anonymous_when_nothing_known():
    session = _build_session(username=None, user_id=None, fingerprint=None)
    assert session.display_name == "anonymous"


# --- create_session --------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_authenticated(manager, mock_user, captured_events):
    session = await manager.create_session(
        websocket=_make_ws(),
        user=mock_user,
        fingerprint=None,
        ip_address="10.0.0.1",
        user_agent="ua",
    )
    assert session.user_id == mock_user.id
    assert session.account_id == mock_user.account_id
    assert session.username == "alice"
    assert session.is_authenticated is True
    # Registered in both lookup maps.
    assert manager.get_session(session.id) is session
    assert manager.get_session_by_connection(session.connection_id) is session
    # A session_start event was persisted.
    assert len(captured_events) == 1
    assert captured_events[0].event_type == "session_start"
    assert captured_events[0].event_data["authenticated"] is True


@pytest.mark.asyncio
async def test_create_session_anonymous(manager, captured_events):
    session = await manager.create_session(
        websocket=_make_ws(),
        user=None,
        fingerprint="fp123456",
        ip_address="10.0.0.2",
        user_agent="ua",
    )
    assert session.user_id is None
    assert session.account_id is None
    assert session.is_authenticated is False
    assert captured_events[0].event_data["authenticated"] is False


# --- end_session -----------------------------------------------------------


@pytest.mark.asyncio
async def test_end_session_removes_and_persists(manager, mock_user, captured_events):
    session = await manager.create_session(
        websocket=_make_ws(),
        user=mock_user,
        fingerprint=None,
        ip_address="10.0.0.1",
        user_agent="ua",
    )
    captured_events.clear()
    await manager.end_session(session.id)
    assert manager.get_session(session.id) is None
    assert manager.get_session_by_connection(session.connection_id) is None
    assert len(captured_events) == 1
    end_event = captured_events[0]
    assert end_event.event_type == "session_end"
    assert "duration_seconds" in end_event.event_data


@pytest.mark.asyncio
async def test_end_session_nonexistent_is_noop(manager, captured_events):
    await manager.end_session(str(uuid.uuid4()))
    assert captured_events == []


# --- lookups / activity ----------------------------------------------------


def test_get_session_by_connection_unknown_returns_none(manager):
    assert manager.get_session_by_connection("nope") is None


def test_get_active_sessions_count(manager):
    assert manager.get_active_sessions_count() == 0
    manager.sessions["a"] = _build_session()
    manager.sessions["b"] = _build_session()
    assert manager.get_active_sessions_count() == 2


def test_update_activity_advances_timestamp(manager):
    session = _build_session()
    old = session.last_activity
    manager.sessions[session.id] = session
    manager.update_activity(session.id)
    assert session.last_activity >= old


def test_update_activity_unknown_session_is_noop(manager):
    # Should not raise for an unknown id.
    manager.update_activity("missing")


def test_get_sessions_for_account_isolates_by_account(manager):
    account_a = uuid.uuid4()
    account_b = uuid.uuid4()
    s1 = _build_session(account_id=account_a)
    s2 = _build_session(account_id=account_a)
    s3 = _build_session(account_id=account_b)
    for s in (s1, s2, s3):
        manager.sessions[s.id] = s
    result = manager.get_sessions_for_account(account_a)
    assert len(result) == 2
    assert s1 in result and s2 in result
    assert s3 not in result


# --- upgrade_session -------------------------------------------------------


@pytest.mark.asyncio
async def test_upgrade_anonymous_session(manager, mock_user, captured_events):
    session = await manager.create_session(
        websocket=_make_ws(),
        user=None,
        fingerprint="fp",
        ip_address="10.0.0.3",
        user_agent="ua",
    )
    captured_events.clear()
    upgraded = await manager.upgrade_session(session.id, mock_user)
    assert upgraded is session
    assert session.user_id == mock_user.id
    assert session.account_id == mock_user.account_id
    assert session.username == "alice"
    assert captured_events[0].event_type == "session_authenticated"


@pytest.mark.asyncio
async def test_upgrade_nonexistent_session_returns_none(manager, mock_user):
    result = await manager.upgrade_session(str(uuid.uuid4()), mock_user)
    assert result is None


@pytest.mark.asyncio
async def test_upgrade_already_authenticated_is_noop(
    manager, mock_user, captured_events
):
    session = await manager.create_session(
        websocket=_make_ws(),
        user=mock_user,
        fingerprint=None,
        ip_address="10.0.0.1",
        user_agent="ua",
    )
    captured_events.clear()
    other_user = MagicMock()
    other_user.id = uuid.uuid4()
    result = await manager.upgrade_session(session.id, other_user)
    # Returns the existing session unchanged and persists no event.
    assert result is session
    assert session.user_id == mock_user.id
    assert captured_events == []
