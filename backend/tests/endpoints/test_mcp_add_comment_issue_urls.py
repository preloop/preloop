"""add_comment resolves GitLab issue URLs in either form to the exact issue."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.api.endpoints import mcp
from preloop.models.models import Issue, Organization, Project, Tracker
from preloop.models.models.user import User

HOST = "https://gitlab.example.com"


def _issue(db: Session, user: User, key: str) -> Issue:
    """Persist a GitLab issue with its tracker, organization and project."""
    tracker = Tracker(
        name=f"gitlab-{key}",
        account_id=user.account_id,
        tracker_type="gitlab",
        api_key="test_key",
        url=HOST,
    )
    db.add(tracker)
    db.commit()
    org = Organization(name=key, identifier=key, tracker_id=tracker.id)
    db.add(org)
    db.commit()
    project_path = key.split("#")[0]
    project = Project(
        name=project_path,
        identifier=project_path,
        slug=project_path,
        organization_id=org.id,
    )
    db.add(project)
    db.commit()
    issue = Issue(
        title="Login fails",
        description="Steps",
        project_id=project.id,
        tracker_id=tracker.id,
        external_id=f"ext-{key}",
        key=key,
    )
    db.add(issue)
    db.commit()
    return issue


async def _comment(db: Session, user: User, target: str) -> tuple[Any, MagicMock]:
    """Call add_comment with auth and the tracker client mocked."""
    tracker = MagicMock()
    created = MagicMock()
    created.id = "note-1"
    created.meta_data = {"url": f"{HOST}/acme/team/api/-/issues/12#note_1"}
    tracker.add_comment = AsyncMock(return_value=created)
    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as request,
        patch(
            "preloop.api.endpoints.mcp.get_user_from_token_if_valid",
            new_callable=AsyncMock,
            return_value=user,
        ),
        patch("preloop.api.endpoints.mcp.get_db", return_value=iter([db])),
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
            return_value=tracker,
        ),
    ):
        request.return_value.headers = {"authorization": "Bearer testtoken"}
        response = await mcp.add_comment(target=target, comment="Looks good")
    return response, tracker


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["issues", "work_items"])
async def test_gitlab_url_in_either_form_comments_on_that_issue(
    db_session: Session, test_user: User, word: str
) -> None:
    _issue(db_session, test_user, "acme/team/api#12")
    response, tracker = await _comment(
        db_session, test_user, f"{HOST}/acme/team/api/-/{word}/12"
    )
    assert response.status == "created"
    tracker.add_comment.assert_awaited_once()
    assert tracker.add_comment.await_args.args[0] == "acme/team/api#12"


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["issues", "work_items"])
async def test_url_for_unsynced_project_never_reaches_a_suffix_match(
    db_session: Session, test_user: User, word: str
) -> None:
    _issue(db_session, test_user, "acme/team/api#12")
    with pytest.raises(HTTPException) as err:
        await _comment(db_session, test_user, f"{HOST}/team/api/-/{word}/12")
    assert err.value.status_code == 404
    assert "project is not synced in this account" in err.value.detail
