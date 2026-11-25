"""Provider payloads fetched on demand follow the real scanner persistence path."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.api.endpoints import mcp
from preloop.models import models
from preloop.models.crud import crud_issue, crud_issue_embedding
from preloop.sync.trackers.azure_devops import AzureDevOpsTracker
from preloop.sync.trackers.github import GitHubTracker
from preloop.sync.trackers.gitlab import GitLabTracker
from preloop.sync.trackers.jira import JiraTracker


CASES = [
    ("github", "acme/web#12", "https://github.com/acme/web/issues/12"),
    (
        "gitlab",
        "acme/team/web#12",
        "https://gitlab.example.com/acme/team/web/-/issues/12",
    ),
    (
        "gitlab",
        "acme/team/web#12",
        "https://gitlab.example.com/acme/team/web/-/work_items/12?show=1#note_4",
    ),
    ("jira", "PROJ-12", "https://jira.example.com/browse/PROJ-12"),
    (
        "azure_devops",
        "acme/My Project#12",
        "https://dev.azure.com/acme/My%20Project/_workitems/edit/12",
    ),
]


def _project(db: Session, account_id: Any, kind: str, key: str) -> models.Project:
    """Create generic discovered identities, including Azure's repository home."""
    host = {
        "github": "https://github.com",
        "gitlab": "https://gitlab.example.com",
        "jira": "https://jira.example.com",
        "azure_devops": "https://dev.azure.com",
    }[kind]
    tracker = models.Tracker(
        name="Example tracker",
        account_id=account_id,
        tracker_type=kind,
        api_key="test-token",
        url=host,
        connection_details={"organization": "acme"},
    )
    org = models.Organization(name="acme", identifier="acme", tracker=tracker)
    path = key.rsplit("#", 1)[0] if "#" in key else "PROJ"
    slug = path + "/web" if kind == "azure_devops" else path
    project = models.Project(
        name="web",
        identifier="42",
        slug=slug,
        organization=org,
        meta_data={"project": "My Project", "repository": "web"}
        if kind == "azure_devops"
        else {},
    )
    db.add(project)
    db.flush()
    db.add(
        models.TrackerScopeRule(
            tracker_id=tracker.id,
            scope_type="ORGANIZATION",
            rule_type="INCLUDE",
            identifier="acme",
        )
    )
    db.commit()
    return project


def _client(project: models.Project, key: str, url: str) -> Any:
    """Real client transforms with mocked provider transport, never live I/O."""
    tracker = project.organization.tracker
    kind = tracker.tracker_type
    timestamp = "2026-01-02T03:04:05Z"
    raw = {
        "id": 10012,
        "number": 12,
        "iid": 12,
        "key": key,
        "title": "Example issue",
        "body": "Example description",
        "description": "Example description",
        "state": "open",
        "labels": [],
        "created_at": timestamp,
        "updated_at": timestamp,
        "html_url": url,
        "web_url": url,
        "url": url,
        "comments": 0,
    }
    if kind == "github":
        client = GitHubTracker(
            str(tracker.id), "test-token", {"owner": "acme", "repo": "web"}
        )
        client._make_request = AsyncMock(return_value=raw)
    elif kind == "gitlab":
        client = GitLabTracker(
            str(tracker.id),
            "",
            {"url": tracker.url, "project_id": "42"},
            initialize_client=False,
        )
        client.gl = MagicMock()
        upstream_project = SimpleNamespace(
            path_with_namespace=project.slug, issues=MagicMock()
        )
        raw.pop("comments")
        client._make_request = AsyncMock(
            side_effect=[upstream_project, SimpleNamespace(attributes=raw, iid=12)]
        )
    elif kind == "jira":
        client = JiraTracker(
            str(tracker.id), "", {"url": tracker.url}, initialize_client=False
        )
        client._make_request = AsyncMock(
            return_value={
                "id": "10012",
                "key": key,
                "fields": {
                    "project": {"key": "PROJ"},
                    "summary": "Example issue",
                    "description": "Example description",
                    "created": timestamp,
                    "updated": timestamp,
                    "status": {
                        "id": "1",
                        "name": "Open",
                        "statusCategory": {"key": "new"},
                    },
                },
            }
        )
    else:
        client = AzureDevOpsTracker(
            str(tracker.id),
            "test-token",
            {"organization": "acme", "repo_full_name": "acme/My Project/web"},
        )
        client._work_item = AsyncMock(
            return_value={
                "id": 12,
                "rev": 1,
                "fields": {
                    "System.TeamProject": "My Project",
                    "System.Title": "Example issue",
                    "System.Description": "Example description",
                    "System.State": "New",
                    "System.WorkItemType": "Issue",
                    "System.CreatedDate": timestamp,
                    "System.ChangedDate": timestamp,
                },
            }
        )
        client._comments = AsyncMock(return_value=[])
        client._category_of = AsyncMock(return_value="Proposed")
    client.get_issue = AsyncMock(wraps=client.get_issue)
    return client


def _authenticate(
    monkeypatch: pytest.MonkeyPatch, db: Session, user: models.User
) -> None:
    """Bind tool authentication and its database session to this test."""
    monkeypatch.setattr(
        mcp,
        "get_http_request",
        lambda: SimpleNamespace(headers={"authorization": "Bearer test-token"}),
    )
    monkeypatch.setattr(
        mcp, "get_user_from_token_if_valid", AsyncMock(return_value=user)
    )
    monkeypatch.setattr(mcp, "get_db", lambda: iter([db]))
    # Endpoint teardown must not detach fixtures between consecutive calls.
    monkeypatch.setattr(db, "close", lambda: None)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,key,url", CASES)
@pytest.mark.parametrize("use_url", [False, True])
async def test_stored_like_scanner_and_reused(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    key: str,
    url: str,
    use_url: bool,
) -> None:
    """Both forms persist real rows; subsequent reads need no tracker request."""
    project = _project(db_session, test_user.account_id, kind, key)
    client = _client(project, key, url)
    factory = AsyncMock(return_value=client)
    monkeypatch.setattr(mcp, "get_tracker_client", factory)
    embeddings = MagicMock()
    monkeypatch.setattr(crud_issue_embedding, "create_embeddings", embeddings)
    _authenticate(monkeypatch, db_session, test_user)
    reference = url if use_url else key
    first = await mcp.get_issue(issue=reference)
    second = await mcp.get_issue(issue=reference)
    assert first.id == second.id
    assert first.key == key
    assert first.title == "Example issue"
    assert first.description == "Example description"
    client.get_issue.assert_awaited_once_with(key if kind == "jira" else "12")
    factory.assert_awaited_once()
    stored = crud_issue.get_by_key(db_session, key=key, account_id=test_user.account_id)
    assert stored is not None
    assert stored.project_id == project.id
    assert stored.tracker_id == project.organization.tracker_id
    assert stored.external_id == ("12" if kind == "azure_devops" else "10012")
    assert stored.updated_at.year == stored.last_updated_external.year == 2026
    if kind != "github":  # GitHub's existing scanner reconstructs the page URL.
        assert stored.external_url == url
    embeddings.assert_called_once_with(db_session, issue_id=stored.id)


@pytest.mark.asyncio
async def test_other_accounts_project_is_never_consulted(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An identical project key on another account cannot supply credentials."""
    other = models.Account(organization_name="Other example account")
    db_session.add(other)
    db_session.commit()
    _project(db_session, other.id, "github", "acme/web#12")
    factory = AsyncMock()
    monkeypatch.setattr(mcp, "get_tracker_client", factory)
    with pytest.raises(
        mcp.IssueNotFoundError, match="project is not synced in this account"
    ):
        await mcp._find_or_sync_issue(db_session, "acme/web#12", test_user)
    factory.assert_not_awaited()
    own = _project(db_session, test_user.account_id, "github", "acme/web#12")
    factory.return_value = _client(
        own, "acme/web#12", "https://github.com/acme/web/issues/12"
    )
    monkeypatch.setattr(crud_issue_embedding, "create_embeddings", MagicMock())
    result = await mcp._find_or_sync_issue(db_session, "acme/web#12", test_user)
    assert result.project_id == own.id
    assert factory.await_args.args[1] == own.id


@pytest.mark.asyncio
async def test_github_pull_request_is_not_stored(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub identifies PRs through a pull_request field in its issues API."""
    project = _project(db_session, test_user.account_id, "github", "acme/web#12")
    client = _client(project, "acme/web#12", "https://github.com/acme/web/issues/12")
    client._make_request.return_value["pull_request"] = {
        "url": "https://api.github.com/repos/acme/web/pulls/12"
    }
    monkeypatch.setattr(mcp, "get_tracker_client", AsyncMock(return_value=client))
    _authenticate(monkeypatch, db_session, test_user)
    with pytest.raises(HTTPException) as caught:
        await mcp.get_issue(issue="acme/web#12")
    assert caught.value.status_code == 404
    assert "get_pull_request" in caught.value.detail
    assert (
        crud_issue.get_by_key(
            db_session, key="acme/web#12", account_id=test_user.account_id
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,key,url", CASES[:2] + CASES[3:])
async def test_add_comment_key_syncs_missing_issue(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    key: str,
    url: str,
) -> None:
    """General comments on qualified issue keys go through issue ingestion."""
    project = _project(db_session, test_user.account_id, kind, key)
    client = _client(project, key, url)
    client.add_comment = AsyncMock(
        return_value=SimpleNamespace(id="50", meta_data={"url": url})
    )
    monkeypatch.setattr(mcp, "get_tracker_client", AsyncMock(return_value=client))
    monkeypatch.setattr(crud_issue_embedding, "create_embeddings", MagicMock())
    _authenticate(monkeypatch, db_session, test_user)
    response = await mcp.add_comment(target=key, comment="Example comment")
    assert response.status == "created"
    client.get_issue.assert_awaited_once()
    client.add_comment.assert_awaited_once_with(key, "Example comment")


@pytest.mark.asyncio
async def test_stored_credentials_use_existing_factory(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The normal authorization and credential factory builds the lookup client."""
    from preloop.api import common

    project = _project(db_session, test_user.account_id, "github", "acme/web#12")
    client = _client(project, "acme/web#12", "https://github.com/acme/web/issues/12")
    factory = AsyncMock(return_value=client)
    monkeypatch.setattr(common, "create_tracker_client", factory)
    monkeypatch.setattr(crud_issue_embedding, "create_embeddings", MagicMock())
    result = await mcp._find_or_sync_issue(db_session, "acme/web#12", test_user)
    assert result.project_id == project.id
    assert factory.await_args.kwargs["tracker_id"] == str(
        project.organization.tracker_id
    )
    assert factory.await_args.kwargs["api_key"] == "test-token"
    assert factory.await_args.kwargs["request_timeout"] == 20.0
    assert factory.await_args.kwargs["raise_on_error"] is True


@pytest.mark.asyncio
async def test_update_issue_syncs_before_writing(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A metadata update accepts an issue absent from the local cache."""
    project = _project(db_session, test_user.account_id, "github", "acme/web#12")
    client = _client(project, "acme/web#12", "https://github.com/acme/web/issues/12")
    client.update_issue = AsyncMock()
    monkeypatch.setattr(mcp, "get_tracker_client", AsyncMock(return_value=client))
    monkeypatch.setattr(crud_issue_embedding, "create_embeddings", MagicMock())
    _authenticate(monkeypatch, db_session, test_user)
    response = await mcp.update_issue(issue="acme/web#12", title="Updated example")
    assert response.title == "Updated example"
    assert client.update_issue.await_args.args[0] == "acme/web#12"
    assert client.update_issue.await_args.args[1].title == "Updated example"
    client.get_issue.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_work_item_comment_syncs_first(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Azure comment edits also resolve work items that have not synced yet."""
    kind, key, url = CASES[-1]
    project = _project(db_session, test_user.account_id, kind, key)
    client = _client(project, key, url)
    client.update_comment = AsyncMock(return_value=SimpleNamespace(id="50", url=url))
    monkeypatch.setattr(mcp, "get_tracker_client", AsyncMock(return_value=client))
    monkeypatch.setattr(crud_issue_embedding, "create_embeddings", MagicMock())
    _authenticate(monkeypatch, db_session, test_user)
    response = await mcp.update_comment(
        target=key, comment_id="50", body="Updated example"
    )
    assert response.status == "updated"
    client.get_issue.assert_awaited_once()
    client.update_comment.assert_awaited_once_with(key, "50", "Updated example")


@pytest.mark.asyncio
async def test_triage_provider_syncs_first(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Triage reads and writes share the live resolution path."""
    project = _project(db_session, test_user.account_id, "github", "acme/web#12")
    client = _client(project, "acme/web#12", "https://github.com/acme/web/issues/12")
    monkeypatch.setattr(mcp, "get_tracker_client", AsyncMock(return_value=client))
    monkeypatch.setattr(crud_issue_embedding, "create_embeddings", MagicMock())
    stored, provider = await mcp._triage_provider(db_session, test_user, "acme/web#12")
    assert stored.key == "acme/web#12"
    assert provider.number == "12"
    client.get_issue.assert_awaited_once()
