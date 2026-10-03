"""Tracker refresh after a GitHub repository moves or is renamed (#1159)."""

from unittest.mock import AsyncMock

import pytest

from preloop.models.models.project import Project
from preloop.sync.scanner.core import TrackerClient
from preloop.sync.trackers.github import GitHubTracker

from tests.endpoints.test_project_repository_transfer import (
    REPO_ID,
    _repo,
    build_moved_repo,
)


@pytest.fixture
def moved_repo(db_session, test_user):
    """Old and new owner organizations, one GitHub tracker each."""
    return build_moved_repo(db_session, test_user)


def _scanner(tracker, repos):
    client = TrackerClient(tracker, initialize_client=False)
    fake = AsyncMock()
    fake.get_projects.return_value = repos
    # The real transform, so the slug/meta_data contract is what ships.
    fake.transform_project = lambda data, org_id: GitHubTracker.transform_project(
        None, data, org_id
    )
    client.client = fake
    return client


@pytest.mark.asyncio
async def test_refresh_of_new_owner_does_not_duplicate_moved_repo(
    db_session,
    moved_repo,
):
    """The destination refresh must not create a second project for the repo."""
    scanner = _scanner(moved_repo["new_tracker"], [_repo("new-owner/widget")])

    result = await scanner.scan_projects(db_session, moved_repo["new_org"])

    assert result == []
    rows = db_session.query(Project).filter(Project.identifier == REPO_ID).all()
    assert [str(p.id) for p in rows] == [str(moved_repo["project"].id)]
    assert str(rows[0].organization_id) == str(moved_repo["old_org"].id)


@pytest.mark.asyncio
async def test_refresh_picks_up_rename_and_keeps_transfer_history(
    db_session,
    moved_repo,
):
    """Same org, same repository ID, new name: update in place, keep history."""
    project = moved_repo["project"]
    project.meta_data = {
        "full_name": "old-owner/widget",
        "repository_transfers": [{"to_full_name": "old-owner/widget"}],
    }
    db_session.flush()
    scanner = _scanner(moved_repo["old_tracker"], [_repo("old-owner/gadget")])

    await scanner.scan_projects(db_session, moved_repo["old_org"])

    db_session.expire_all()
    rows = db_session.query(Project).filter(Project.identifier == REPO_ID).all()
    assert len(rows) == 1
    assert rows[0].id == project.id
    assert rows[0].slug == "old-owner/gadget"
    assert rows[0].name == "gadget"
    assert rows[0].meta_data["full_name"] == "old-owner/gadget"
    assert rows[0].meta_data["repository_transfers"] == [
        {"to_full_name": "old-owner/widget"}
    ]


@pytest.mark.asyncio
async def test_refresh_locks_repository_identity_before_creating(
    db_session,
    moved_repo,
    monkeypatch,
):
    """New repositories are created under the same lock API writers take."""
    import preloop.sync.scanner.core as scanner_core

    locked = []
    monkeypatch.setattr(
        scanner_core,
        "lock_repository_identity",
        lambda db, **kw: locked.append((kw["host"], kw["identifier"])),
    )
    scanner = _scanner(moved_repo["new_tracker"], [_repo("new-owner/fresh", "555")])

    created = await scanner.scan_projects(db_session, moved_repo["new_org"])

    assert [p.identifier for p in created] == ["555"]
    assert locked == [("github.com", "555")]


@pytest.mark.asyncio
async def test_refresh_releases_the_lock_when_it_refuses_a_duplicate(
    db_session, moved_repo, monkeypatch
):
    """The refused path ends the transaction instead of keeping the lock."""
    import preloop.sync.scanner.core as scanner_core

    events = []
    real_release = scanner_core.release_transaction
    monkeypatch.setattr(
        scanner_core,
        "lock_repository_identity",
        lambda db, **kw: events.append("lock"),
    )
    monkeypatch.setattr(
        scanner_core,
        "release_transaction",
        lambda db: (events.append("release"), real_release(db)),
    )
    scanner = _scanner(moved_repo["new_tracker"], [_repo("new-owner/widget")])

    await scanner.scan_projects(db_session, moved_repo["new_org"])

    assert "lock" in events
    assert events[events.index("lock") + 1 :] == ["release"]
