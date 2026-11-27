"""The console and Talk select through the same audited relevance service."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.schemas.session_find import SessionFindRequest
from preloop.services.session_find import find_sessions
from preloop.services.session_search_audit import SearchActor
from preloop.services.session_search_index import index_transcript_message


def test_find_combines_identity_content_and_parent_context(
    db_session: Session,
    create_account: Any,
) -> None:
    """A content-only child match retains its unmatching parent and snippet."""
    account = create_account()
    now = datetime.now(UTC).replace(tzinfo=None)
    parent = models.RuntimeSession(
        account_id=account.id,
        session_source_type="custom",
        session_source_id="parent",
        title="Catalog migration",
        started_at=now,
        last_activity_at=now,
    )
    db_session.add(parent)
    db_session.flush()
    child = models.RuntimeSession(
        account_id=account.id,
        session_source_type="custom",
        session_source_id="child",
        title="Schema review",
        parent_session_id=parent.id,
        started_at=now,
        last_activity_at=now,
    )
    db_session.add(child)
    db_session.flush()
    with patch(
        "preloop.services.session_search_index.settings.model_gateway_capture_content",
        True,
    ):
        index_transcript_message(
            db_session,
            account_id=account.id,
            runtime_session_id=child.id,
            source_id="synthetic-turn",
            text="Investigate quasar ordering in the middle of the session",
            role="user",
            occurred_at=now,
        )
    db_session.flush()
    result = find_sessions(
        db_session,
        account=account,
        payload=SessionFindRequest(query="quasar"),
        actor=SearchActor(),
    )
    assert [row.id for row in result.items] == [str(child.id)]
    assert [row.id for row in result.related_sessions] == [str(parent.id)]
    assert "quasar" in result.matching_snippets[str(child.id)]
    assert result.matching_turns[str(child.id)] == "synthetic-turn"
    scoped = find_sessions(
        db_session,
        account=account,
        payload=SessionFindRequest(query="quasar", session_ids=[parent.id]),
        actor=SearchActor(),
    )
    assert scoped.items == []
    assert scoped.matching_snippets == {}
    assert scoped.content_search["results"] == []
    title = find_sessions(
        db_session,
        account=account,
        payload=SessionFindRequest(query="Catalog"),
        actor=SearchActor(),
    )
    assert [row.id for row in title.items] == [str(parent.id)]
    with patch(
        "preloop.services.session_find.session_search_audit.audited_search"
    ) as search:
        find_sessions(
            db_session,
            account=account,
            payload=SessionFindRequest(),
            actor=SearchActor(),
        )
        search.assert_not_called()


def test_agent_filter_includes_registry_principal(
    db_session: Session,
    create_account: Any,
) -> None:
    """Sessions keyed by registry id remain selectable in the agent's picker."""
    account = create_account()
    now = datetime.now(UTC).replace(tzinfo=None)
    agent = models.ManagedAgent(
        account_id=account.id,
        display_name="Example agent",
        agent_kind="claude_code",
        session_source_type="claude_code",
        session_source_id="example-device",
        lifecycle_updated_at=now,
        last_seen_at=now,
    )
    db_session.add(agent)
    db_session.flush()
    session = models.RuntimeSession(
        account_id=account.id,
        session_source_type="managed_agent",
        session_source_id="example-run",
        runtime_principal_type="managed_agent",
        runtime_principal_id=str(agent.id),
        started_at=now,
        last_activity_at=now,
    )
    db_session.add(session)
    db_session.flush()
    response = find_sessions(
        db_session,
        account=account,
        payload=SessionFindRequest(agent=str(agent.id)),
        actor=SearchActor(),
    )
    assert [row.id for row in response.items] == [str(session.id)]


def test_content_failure_preserves_identity_matches(
    db_session: Session,
    create_account: Any,
) -> None:
    """A failed content index must not hide a known session title."""
    from sqlalchemy.exc import SQLAlchemyError

    account = create_account()
    now = datetime.now(UTC).replace(tzinfo=None)
    session = models.RuntimeSession(
        account_id=account.id,
        session_source_type="custom",
        session_source_id="example-session",
        title="Catalog review",
        started_at=now,
        last_activity_at=now,
    )
    db_session.add(session)
    db_session.commit()
    session_id = str(session.id)
    with patch(
        "preloop.services.session_find.session_search_audit.audited_search",
        side_effect=SQLAlchemyError("synthetic unavailable index"),
    ):
        result = find_sessions(
            db_session,
            account=account,
            payload=SessionFindRequest(query="Catalog"),
            actor=SearchActor(),
        )
    assert [row.id for row in result.items] == [session_id]
    assert result.search_warning


def test_identity_failure_preserves_content_matches(
    db_session: Session,
    create_account: Any,
) -> None:
    """A failed identity predicate still allows scoped content hydration."""
    from sqlalchemy.exc import SQLAlchemyError
    from preloop.services.runtime_session_explorer import RuntimeSessionExplorerService

    account = create_account()
    now = datetime.now(UTC).replace(tzinfo=None)
    session = models.RuntimeSession(
        account_id=account.id,
        session_source_type="custom",
        session_source_id="example-content-session",
        title="Catalog review",
        started_at=now,
        last_activity_at=now,
    )
    db_session.add(session)
    db_session.flush()
    with patch(
        "preloop.services.session_search_index.settings.model_gateway_capture_content",
        True,
    ):
        index_transcript_message(
            db_session,
            account_id=account.id,
            runtime_session_id=session.id,
            source_id="synthetic-content-turn",
            text="Investigate quasar ordering",
            role="user",
            occurred_at=now,
        )
    db_session.commit()
    session_id = str(session.id)
    original = RuntimeSessionExplorerService.list_account_sessions

    def fail_identity(self: Any, **kwargs: Any) -> Any:
        if kwargs.get("query"):
            raise SQLAlchemyError("synthetic identity read failure")
        return original(self, **kwargs)

    with patch.object(
        RuntimeSessionExplorerService, "list_account_sessions", fail_identity
    ):
        result = find_sessions(
            db_session,
            account=account,
            payload=SessionFindRequest(query="quasar"),
            actor=SearchActor(),
        )
    assert [row.id for row in result.items] == [session_id]
    assert "Identity search" in result.search_warning
    assert "quasar" in result.matching_snippets[session_id]


@pytest.mark.parametrize(
    "scope",
    [
        "agent",
        "status",
        "session_source_type",
        "session_ids",
        "default_window",
        "has_artifacts",
        "workspace",
    ],
)
def test_content_ranking_applies_scope_before_limit(
    db_session: Session, create_account: Any, scope: str
) -> None:
    """A stronger foreign or old match cannot consume the scoped first page."""
    from datetime import timedelta

    account = create_account()
    now = datetime.now(UTC).replace(tzinfo=None)
    agent = models.ManagedAgent(
        account_id=account.id,
        display_name="Example selector",
        agent_kind="codex",
        session_source_type="managed_agent",
        session_source_id="example-selector",
        lifecycle_updated_at=now,
        last_seen_at=now,
    )
    db_session.add(agent)
    db_session.flush()
    rows = []
    for index in range(2):
        occurred = (
            now - timedelta(days=45)
            if index == 0 and scope == "default_window"
            else now
        )
        row = models.RuntimeSession(
            account_id=account.id,
            session_source_type="custom" if index == 0 else "codex",
            session_source_id=f"scope-example-{index}",
            runtime_principal_type="managed_agent",
            runtime_principal_id="another-agent" if index == 0 else str(agent.id),
            title="Unrelated title",
            started_at=occurred,
            last_activity_at=occurred,
            ended_at=occurred if index == 0 else None,
            terminal_location={
                "device_id": "example-device",
                "multiplexer": "herdr",
                "workspace_id": f"workspace-{index}",
                "workspace_label": f"Workspace {index}",
                "tab_id": f"tab-{index}",
                "tab_label": "Example tab",
                "pane_id": f"pane-{index}",
                "pane_label": "Example pane",
                "state": "idle",
                "observed_at": occurred.isoformat(),
            },
        )
        db_session.add(row)
        db_session.flush()
        with patch(
            "preloop.services.session_search_index.settings.model_gateway_capture_content",
            True,
        ):
            index_transcript_message(
                db_session,
                account_id=account.id,
                runtime_session_id=row.id,
                source_id=f"scope-turn-{index}",
                text="quasar " * (20 if index == 0 else 1),
                role="assistant",
                occurred_at=occurred,
            )
        rows.append(row)
    db_session.add(
        models.RuntimeSessionArtifact(
            account_id=account.id,
            runtime_session_id=rows[1].id,
            kind="document",
            source="agent",
            content_type="text/plain",
            size_bytes=1,
            sha256="a" * 64,
            manifest={},
            availability="available",
        )
    )
    db_session.flush()
    from preloop.schemas.session_search import (
        SessionSearchRequest,
        SessionSearchFilters,
    )
    from preloop.services.session_search_audit import audited_search

    unscoped = audited_search(
        db_session,
        account_id=account.id,
        actor=SearchActor(),
        request=SessionSearchRequest(
            query="quasar",
            limit=1,
            filters=SessionSearchFilters(
                start_date=(now - timedelta(days=60)).replace(tzinfo=UTC)
            ),
        ),
    )
    assert str(unscoped.results[0].runtime_session_id) == str(rows[0].id)
    filters = {
        "agent": {"agent": str(agent.id)},
        "status": {"status": "active"},
        "session_source_type": {"session_source_type": "codex"},
        "session_ids": {"session_ids": [rows[1].id]},
        "default_window": {},
        "has_artifacts": {"has_artifacts": "document"},
        "workspace": {"workspace_id": "workspace-1"},
    }[scope]
    result = find_sessions(
        db_session,
        account=account,
        payload=SessionFindRequest(query="quasar", limit=1, **filters),
        actor=SearchActor(),
    )
    assert [row.id for row in result.items] == [str(rows[1].id)]
    assert not result.has_more
