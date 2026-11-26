"""On-demand issue lookup uses account-owned projects and the scanner ingest."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from preloop.api.endpoints import mcp
from preloop.models import models
from preloop.services import issue_lookup
from preloop.schemas.tracker_models import Issue, IssueStatus
from preloop.sync.exceptions import TrackerAuthenticationError, TrackerResponseError


@pytest.fixture
def lookup(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Mock persistence and clients without opening any network connection."""
    user = SimpleNamespace(account_id=uuid4())
    tracker = models.Tracker(
        id=uuid4(),
        account_id=user.account_id,
        tracker_type="github",
        url="https://github.com",
        connection_details={},
    )
    org = models.Organization(id=uuid4(), identifier="acme", tracker=tracker)
    project = models.Project(
        id=uuid4(),
        identifier="42",
        slug="acme/web",
        name="web",
        organization=org,
        organization_id=org.id,
    )
    db = MagicMock()
    for method in ("get", "get_by_key", "get_by_key_postfix", "get_by_external_url"):
        monkeypatch.setattr(mcp.crud_issue, method, MagicMock(return_value=None))
    monkeypatch.setattr(
        mcp.crud_issue, "list_by_issue_number", MagicMock(return_value=[])
    )
    projects = MagicMock(return_value=[project])
    monkeypatch.setattr(
        issue_lookup.crud_project, "list_active_for_tracker_types", projects
    )
    client = MagicMock()
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    upstream = Issue(
        id="10012",
        key="acme/web#12",
        title="Example issue",
        status=IssueStatus(id="open", name="Open", category="open"),
        created_at=now,
        updated_at=now,
        url="https://github.com/acme/web/issues/12",
        tracker_type="github",
        project_key="acme/web",
    )
    client.get_issue = AsyncMock(return_value=upstream)
    factory = AsyncMock(return_value=client)
    monkeypatch.setattr(mcp, "get_tracker_client", factory)
    return SimpleNamespace(
        user=user,
        db=db,
        tracker=tracker,
        project=project,
        client=client,
        factory=factory,
        projects=projects,
        upstream=upstream,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference", ["acme/web#12", "https://github.com/acme/web/issues/12"]
)
async def test_fetch_and_ingest_then_database_hit(
    lookup: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    reference: str,
) -> None:
    """A second resolution reads the row and does not call the provider again."""
    from preloop.sync.scanner.core import TrackerClient

    stored = models.Issue(id=uuid4(), key="acme/web#12")
    ingest = MagicMock(return_value=(stored, 1))
    monkeypatch.setattr(TrackerClient, "ingest_issue", ingest, raising=False)
    found = await mcp._find_or_sync_issue(lookup.db, reference, lookup.user)
    assert found is stored
    lookup.client.get_issue.assert_awaited_once_with("12")
    lookup.factory.assert_awaited_once_with(
        lookup.project.organization_id,
        lookup.project.id,
        lookup.db,
        lookup.user,
        request_timeout=20.0,
    )
    ingest.assert_called_once()
    monkeypatch.setattr(mcp.crud_issue, "get_by_key", MagicMock(return_value=stored))
    assert await mcp._find_or_sync_issue(lookup.db, reference, lookup.user) is stored
    lookup.client.get_issue.assert_awaited_once()
    assert lookup.projects.call_args.kwargs["account_id"] == lookup.user.account_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (TrackerResponseError("secret response", status_code=404), 404, "looked up on"),
        (TrackerAuthenticationError("secret token"), 502, "401"),
        (TrackerResponseError("secret response", status_code=403), 502, "403"),
        (TrackerResponseError("secret response", status_code=429), 502, "429"),
        (TrackerResponseError("secret response", status_code=503), 502, "503"),
        (TimeoutError("secret response"), 504, "timed out"),
    ],
)
async def test_upstream_errors_are_safe(
    lookup: SimpleNamespace,
    error: Exception,
    status: int,
    detail: str,
) -> None:
    """Provider failures retain status without exposing payloads or credentials."""
    lookup.client.get_issue.side_effect = error
    with pytest.raises((HTTPException, mcp.IssueNotFoundError)) as caught:
        await mcp._find_or_sync_issue(lookup.db, "acme/web#12", lookup.user)
    exc = caught.value
    assert getattr(exc, "status_code", 404) == status
    message = str(getattr(exc, "detail", exc))
    assert detail in message
    assert "secret" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["12", "#12", "web#12"])
async def test_ambiguous_or_partial_forms_do_not_fetch(
    lookup: SimpleNamespace,
    reference: str,
) -> None:
    """Unqualified references keep the database-only behavior."""
    with pytest.raises(mcp.IssueNotFoundError):
        await mcp._find_or_sync_issue(lookup.db, reference, lookup.user)
    lookup.factory.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference", ["acme/other#12", "https://github.com/team/web/issues/12"]
)
async def test_project_must_already_be_synced(
    lookup: SimpleNamespace,
    reference: str,
) -> None:
    """No suffix guessing or silent project creation is allowed."""
    with pytest.raises(
        mcp.IssueNotFoundError, match="project is not synced in this account"
    ):
        await mcp._find_or_sync_issue(lookup.db, reference, lookup.user)
    lookup.factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_projects_do_not_fetch(lookup: SimpleNamespace) -> None:
    """An unqualified key must resolve to exactly one stored project."""
    lookup.projects.return_value = [lookup.project, lookup.project]
    with pytest.raises(mcp.IssueNotFoundError):
        await mcp._find_or_sync_issue(lookup.db, "acme/web#12", lookup.user)
    lookup.factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_url_host_must_match_tracker(lookup: SimpleNamespace) -> None:
    """A URL cannot select a different server's credentials by repository path."""
    with pytest.raises(mcp.IssueNotFoundError, match="project is not synced"):
        await mcp._find_or_sync_issue(
            lookup.db, "https://elsewhere.example.com/acme/web/issues/12", lookup.user
        )
    lookup.factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_timeout_is_bounded(
    lookup: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel a stalled client call rather than waiting for its own timeout."""
    from preloop.services import issue_lookup

    monkeypatch.setattr(issue_lookup, "ISSUE_LOOKUP_TIMEOUT_SECONDS", 0.01)

    async def stalled(_: str) -> None:
        await asyncio.Event().wait()

    lookup.client.get_issue.side_effect = stalled
    with pytest.raises(HTTPException) as caught:
        await mcp._find_or_sync_issue(lookup.db, "acme/web#12", lookup.user)
    assert caught.value.status_code == 504


@pytest.mark.asyncio
async def test_compliance_batches_stay_database_only(lookup: SimpleNamespace) -> None:
    """Batches cannot fan out to one live request per missing issue."""
    estimate = await mcp._process_single_issue_estimate(
        "acme/web#12", lookup.db, lookup.user, "DoR"
    )
    improve = await mcp._process_single_issue_compliance(
        "acme/web#12", lookup.db, lookup.user
    )
    assert not estimate.success
    assert not improve.success
    lookup.factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_logs_only_form_type_and_outcome(
    lookup: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Neither identifiers nor provider response bodies enter fallback logs."""
    lookup.client.get_issue.side_effect = TrackerResponseError(
        "secret response", status_code=503
    )
    with caplog.at_level("INFO"), pytest.raises(HTTPException):
        await mcp._find_or_sync_issue(lookup.db, "acme/web#12", lookup.user)
    records = [r for r in caplog.records if r.name == "preloop.services.issue_lookup"]
    assert len(records) == 1
    assert (
        records[0].getMessage()
        == "Issue lookup fallback form=key tracker=github outcome=upstream_503"
    )
    assert "secret" not in caplog.text
    assert "acme/web" not in caplog.text


@pytest.mark.asyncio
async def test_wrong_upstream_identity_is_not_ingested(
    lookup: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A moved Jira issue or wrong project response cannot be stored in this project."""
    from preloop.sync.scanner.core import TrackerClient

    ingest = MagicMock()
    monkeypatch.setattr(TrackerClient, "ingest_issue", ingest)
    lookup.upstream.key = "acme/other#12"
    with pytest.raises(HTTPException) as caught:
        await mcp._find_or_sync_issue(lookup.db, "acme/web#12", lookup.user)
    assert caught.value.status_code == 502
    ingest.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind, slug, reference",
    [
        ("jira", "PROJ", "PROJ-12"),
        ("gitlab", "acme/team/web", "acme/team/web#12"),
    ],
)
async def test_ambiguous_keys_do_not_fetch(
    lookup: SimpleNamespace,
    kind: str,
    slug: str,
    reference: str,
) -> None:
    """Duplicate project keys across trackers remain unresolved without I/O."""
    lookup.tracker.tracker_type = kind
    lookup.project.slug = slug
    lookup.projects.return_value = [lookup.project, lookup.project]
    with pytest.raises(mcp.IssueNotFoundError):
        await mcp._find_or_sync_issue(lookup.db, reference, lookup.user)
    lookup.factory.assert_not_awaited()
