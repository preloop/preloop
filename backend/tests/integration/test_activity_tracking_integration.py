"""Integration test for activity tracking through WebSocket."""

import pytest
import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from preloop.services.activity_tracker import handle_activity
from preloop.services.session_manager import session_manager, WebSocketSession
from preloop.models.models import Event, Account, User
from preloop.models.db.session import get_db_session


@pytest.fixture
def db():
    """Get a database session."""
    db = next(get_db_session())
    yield db
    db.close()


@pytest.fixture
def test_account(db: Session):
    """Create a test account."""
    account = Account(
        id=uuid.uuid4(),
        organization_name="Test Organization",
        is_active=True,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    yield account

    # Cleanup
    db.query(Event).filter(Event.account_id == account.id).delete()
    db.query(User).filter(User.account_id == account.id).delete()
    db.query(Account).filter(Account.id == account.id).delete()
    db.commit()


@pytest.fixture
def test_user(db: Session, test_account: Account):
    """Create a test user."""
    user = User(
        id=uuid.uuid4(),
        username=f"testuser_{uuid.uuid4().hex[:8]}",
        email=f"test_{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="fakehash",
        account_id=test_account.id,
        is_active=True,
        email_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    yield user

    # User will be deleted by test_account cleanup


@pytest.mark.asyncio
async def test_page_view_activity_creates_event(
    db: Session, test_account: Account, test_user: User
):
    """Test that a page_view activity message creates an Event in the database."""

    # Create a mock WebSocket session (simulating authenticated user)
    session = WebSocketSession(
        id=str(uuid.uuid4()),
        connection_id=str(uuid.uuid4()),
        websocket=None,  # We won't actually use the WebSocket
        user_id=test_user.id,
        account_id=test_account.id,
        username=test_user.username,
        fingerprint="test_fingerprint",
        ip_address="127.0.0.1",
        user_agent="TestBrowser/1.0",
        connected_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
        metadata={},
    )

    # Register the session
    session_manager.sessions[session.id] = session

    try:
        # Simulate activity message from frontend
        activity_data = {
            "event": "page_view",
            "path": "/admin/accounts",
            "referrer": "/admin/",
            "metadata": {
                "browser": "TestBrowser",
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Count events before
        count_before = (
            db.query(Event)
            .filter(
                Event.event_type == "page_view",
                Event.session_id == uuid.UUID(session.id),
            )
            .count()
        )

        # Handle the activity
        await handle_activity(activity_data, session)

        # Count events after
        count_after = (
            db.query(Event)
            .filter(
                Event.event_type == "page_view",
                Event.session_id == uuid.UUID(session.id),
            )
            .count()
        )

        # Should have created one new event
        assert count_after == count_before + 1, "Should create one new page_view event"

        # Verify the event details
        event = (
            db.query(Event)
            .filter(
                Event.event_type == "page_view",
                Event.session_id == uuid.UUID(session.id),
            )
            .order_by(Event.timestamp.desc())
            .first()
        )

        assert event is not None, "Event should exist"
        assert event.event_type == "page_view"
        assert event.path == "/admin/accounts"
        assert event.referrer == "/admin/"
        assert event.user_id == test_user.id
        assert event.account_id == test_account.id
        assert event.ip_address == "127.0.0.1"

        print("✓ Page view event created successfully")
        print(f"  Event ID: {event.id}")
        print(f"  Path: {event.path}")
        print(f"  Timestamp: {event.timestamp}")

    finally:
        # Cleanup
        if session.id in session_manager.sessions:
            del session_manager.sessions[session.id]

        # Delete test events
        db.query(Event).filter(Event.session_id == uuid.UUID(session.id)).delete()
        db.commit()


@pytest.mark.asyncio
async def test_anonymous_page_view_activity(db: Session, test_account: Account):
    """Test that anonymous users (no user_id) can create page_view events."""

    # Create a mock session for anonymous user (no user_id)
    session = WebSocketSession(
        id=str(uuid.uuid4()),
        connection_id=str(uuid.uuid4()),
        websocket=None,
        user_id=None,  # Anonymous user
        account_id=test_account.id,
        username=None,
        fingerprint="anonymous_fingerprint_123",
        ip_address="192.168.1.100",
        user_agent="AnonymousBrowser/1.0",
        connected_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
        metadata={},
    )

    session_manager.sessions[session.id] = session

    try:
        activity_data = {
            "event": "page_view",
            "path": "/",
            "referrer": None,
            "metadata": {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Handle the activity
        await handle_activity(activity_data, session)

        # Verify the event was created
        event = (
            db.query(Event)
            .filter(
                Event.event_type == "page_view",
                Event.session_id == uuid.UUID(session.id),
            )
            .first()
        )

        assert event is not None, "Anonymous user should be able to create events"
        assert event.user_id is None, "Event should not have user_id for anonymous"
        assert event.fingerprint == "anonymous_fingerprint_123"
        assert event.path == "/"

        print("✓ Anonymous page view event created successfully")

    finally:
        # Cleanup
        if session.id in session_manager.sessions:
            del session_manager.sessions[session.id]
        db.query(Event).filter(Event.session_id == uuid.UUID(session.id)).delete()
        db.commit()


@pytest.mark.asyncio
async def test_action_activity_creates_event(
    db: Session, test_account: Account, test_user: User
):
    """Test that action events are created correctly."""

    session = WebSocketSession(
        id=str(uuid.uuid4()),
        connection_id=str(uuid.uuid4()),
        websocket=None,
        user_id=test_user.id,
        account_id=test_account.id,
        username=test_user.username,
        fingerprint="test_fingerprint",
        ip_address="127.0.0.1",
        user_agent="TestBrowser/1.0",
        connected_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
        metadata={},
    )

    session_manager.sessions[session.id] = session

    try:
        activity_data = {
            "event": "action",
            "action": "click_signup_button",
            "metadata": {
                "element": "button",
                "text": "Sign Up",
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        await handle_activity(activity_data, session)

        event = (
            db.query(Event)
            .filter(
                Event.event_type == "action", Event.session_id == uuid.UUID(session.id)
            )
            .first()
        )

        assert event is not None
        assert event.event_type == "action"
        assert event.action == "click_signup_button"
        assert event.element == "button"
        assert event.element_text == "Sign Up"

        print("✓ Action event created successfully")

    finally:
        if session.id in session_manager.sessions:
            del session_manager.sessions[session.id]
        db.query(Event).filter(Event.session_id == uuid.UUID(session.id)).delete()
        db.commit()


@pytest.mark.asyncio
async def test_conversion_activity_creates_event(
    db: Session, test_account: Account, test_user: User
):
    """Test that conversion events are created correctly."""

    session = WebSocketSession(
        id=str(uuid.uuid4()),
        connection_id=str(uuid.uuid4()),
        websocket=None,
        user_id=test_user.id,
        account_id=test_account.id,
        username=test_user.username,
        fingerprint="test_fingerprint",
        ip_address="127.0.0.1",
        user_agent="TestBrowser/1.0",
        connected_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
        metadata={},
    )

    session_manager.sessions[session.id] = session

    try:
        activity_data = {
            "event": "conversion",
            "conversion_event": "signup_completed",
            "value": 99.99,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        await handle_activity(activity_data, session)

        event = (
            db.query(Event)
            .filter(
                Event.event_type == "conversion",
                Event.session_id == uuid.UUID(session.id),
            )
            .first()
        )

        assert event is not None
        assert event.event_type == "conversion"
        assert event.conversion_event == "signup_completed"
        assert event.conversion_value == 99.99

        print("✓ Conversion event created successfully")

    finally:
        if session.id in session_manager.sessions:
            del session_manager.sessions[session.id]
        db.query(Event).filter(Event.session_id == uuid.UUID(session.id)).delete()
        db.commit()


@pytest.mark.asyncio
async def test_multiple_page_views_in_sequence(
    db: Session, test_account: Account, test_user: User
):
    """Test tracking multiple page views in sequence (simulating navigation)."""

    session = WebSocketSession(
        id=str(uuid.uuid4()),
        connection_id=str(uuid.uuid4()),
        websocket=None,
        user_id=test_user.id,
        account_id=test_account.id,
        username=test_user.username,
        fingerprint="test_fingerprint",
        ip_address="127.0.0.1",
        user_agent="TestBrowser/1.0",
        connected_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
        metadata={},
    )

    session_manager.sessions[session.id] = session

    try:
        pages = [
            "/admin/",
            "/admin/accounts",
            "/admin/accounts/123",
            "/admin/users",
        ]

        for i, path in enumerate(pages):
            activity_data = {
                "event": "page_view",
                "path": path,
                "referrer": pages[i - 1] if i > 0 else None,
                "metadata": {},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            await handle_activity(activity_data, session)
            await asyncio.sleep(0.1)  # Small delay between page views

        # Count all page views for this session
        count = (
            db.query(Event)
            .filter(
                Event.event_type == "page_view",
                Event.session_id == uuid.UUID(session.id),
            )
            .count()
        )

        assert count == len(pages), f"Should have {len(pages)} page views, got {count}"

        # Verify the sequence
        events = (
            db.query(Event)
            .filter(
                Event.event_type == "page_view",
                Event.session_id == uuid.UUID(session.id),
            )
            .order_by(Event.timestamp)
            .all()
        )

        for i, event in enumerate(events):
            assert event.path == pages[i], f"Page {i} path mismatch"
            if i > 0:
                assert event.referrer == pages[i - 1], f"Page {i} referrer mismatch"

        print(f"✓ Successfully tracked {len(pages)} page views in sequence")

    finally:
        if session.id in session_manager.sessions:
            del session_manager.sessions[session.id]
        db.query(Event).filter(Event.session_id == uuid.UUID(session.id)).delete()
        db.commit()


if __name__ == "__main__":
    # Run the tests
    import sys

    sys.exit(pytest.main([__file__, "-v", "-s"]))
