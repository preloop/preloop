"""Tracker refresh after a GitHub repository moves or is renamed (#1159)."""

from unittest.mock import AsyncMock

import pytest

from preloop.models.models.project import Project
from preloop.sync.scanner.core import TrackerClient
from preloop.sync.trackers.github import GitHubTracker

from tests.endpoints.test_project_repository_transfer import (  # noqa: F401
    REPO_ID,
    _repo,
    moved_repo,
)


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
    moved_repo,  # noqa: F811
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
    moved_repo,  # noqa: F811
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
