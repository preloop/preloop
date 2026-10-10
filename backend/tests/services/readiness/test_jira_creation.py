"""Creation evidence never reuses the issue mapper's current-time fallback."""

from typing import Any

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from preloop.sync.exceptions import TrackerPermissionError
from preloop.sync.trackers.jira import JiraTracker


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "created,expected",
    [
        ("2026-01-01T08:00:00Z", datetime(2026, 1, 1, 8, tzinfo=UTC)),
        (None, None),
        ("invalid", None),
    ],
)
async def test_authoritative_jira_creation(created: Any, expected: Any) -> Any:
    tracker = JiraTracker(
        str(uuid4()),
        "synthetic",
        {"url": "https://example.atlassian.net", "username": "fixture@example.com"},
        initialize_client=False,
    )
    tracker._make_request = AsyncMock(
        return_value={"key": "EXAMPLE-1", "fields": {"created": created}}
    )
    evidence = await tracker.get_ticket_creation_evidence("EXAMPLE-1")
    assert evidence.created_at == expected
    assert evidence.source_field == "fields.created"
    assert evidence.issue_key == "EXAMPLE-1"
    assert evidence.retrieved_at.tzinfo is not None


@pytest.mark.asyncio
async def test_unreadable_creation_remains_null() -> Any:
    tracker = JiraTracker(
        str(uuid4()),
        "synthetic",
        {"url": "https://example.atlassian.net", "username": "fixture@example.com"},
        initialize_client=False,
    )
    tracker._make_request = AsyncMock(
        side_effect=TrackerPermissionError("synthetic denied")
    )
    evidence = await tracker.get_ticket_creation_evidence("EXAMPLE-1")
    assert evidence.created_at is None
    assert evidence.reason == "ticket_creation_unreadable"
