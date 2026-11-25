"""MCP pull request tools route Azure Repos URLs to the Azure DevOps handlers.

Also covers the wiring the reviewer flow relies on: the trigger resource key,
the clone URL, the git credential username and the manual-run payload.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from preloop.api.endpoints import mcp
from preloop.models.models.organization import Organization
from preloop.models.models.project import Project
from preloop.models.models.tracker import Tracker
from preloop.models.models.user import User
from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.services.preset_runner import build_pull_request_trigger_payload
from preloop.services.tracker_git_token import resolve_tracker_git_username

pytestmark = pytest.mark.asyncio

PR_URL = "https://dev.azure.com/fabrikam/My%20Project/_git/web/pullrequest/7"


class FakeAzure:
    """Stands in for AzureDevOpsTracker and records calls."""

    tracker_type = "azure_devops"
    organization = "fabrikam"

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    async def get_pull_request(self, number: int, repo: Any = None) -> Dict:
        self.calls.append(("get", number, repo))
        return {
            "pullRequestId": number,
            "title": "t",
            "status": "active",
            "sourceRefName": "refs/heads/f",
            "targetRefName": "refs/heads/main",
        }

    def pull_request_url(self, number: int, repo: Any = None) -> str:
        return PR_URL

    async def get_pull_request_threads(self, number: int, repo: Any = None) -> List:
        self.calls.append(("threads", number))
        return []

    async def add_pull_request_comment(self, number: int, body: str, **kw: Any):
        self.calls.append(("comment", number, body, kw.get("thread_id")))
        return {"thread_id": 9, "comment_id": 1, "url": f"{PR_URL}?discussionId=9"}


def test_parse_azure_pr_url() -> None:
    parsed = mcp._parse_pr_identifier(PR_URL)
    assert parsed["platform"] == "azure_devops"
    assert parsed["project_path"] == "fabrikam/My Project/web"
    assert parsed["pr_number"] == "7"


async def test_get_pull_request_and_add_comment_dispatch(
    db_session: Session, test_user: User
) -> None:
    tracker = Tracker(
        name="ado",
        account_id=test_user.account_id,
        tracker_type="azure_devops",
        api_key="pat",
        url="https://dev.azure.com",
        connection_details={"organization": "fabrikam"},
    )
    db_session.add(tracker)
    db_session.commit()
    organization = Organization(
        name="fabrikam", identifier="fabrikam", tracker_id=tracker.id
    )
    db_session.add(organization)
    db_session.commit()
    project = Project(
        name="web",
        identifier="5febef5a-833d-4e14-b9c0-14cb638f91e6",
        slug="fabrikam/My Project/web",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    fake = FakeAzure()
    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer t"}
        mock_auth.return_value = (MagicMock(wraps=db_session), test_user)
        mock_get_tracker.return_value = fake
        result = await mcp.get_pull_request(pull_request=PR_URL, include_diff=False)
        assert result.number == 7
        assert result.source_branch == "f"
        assert mock_get_tracker.await_args.args[1] == project.id
        with (
            patch(
                "preloop.api.endpoints.mcp.get_user_from_token_if_valid",
                new_callable=AsyncMock,
                return_value=test_user,
            ),
            patch("preloop.api.endpoints.mcp._get_tool_db", return_value=db_session),
            patch("preloop.api.endpoints.mcp._guard_delivery_report_write"),
            patch.object(db_session, "close"),
        ):
            comment = await mcp.add_comment(target=PR_URL, comment="hello")
        assert comment.comment_id == "9"
    assert ("comment", 7, "hello", None) in fake.calls


async def test_create_pull_request_is_refused_for_azure() -> None:
    from fastapi import HTTPException

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch("preloop.models.crud.crud_project") as crud_project,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer t"}
        mock_auth.return_value = (MagicMock(), MagicMock(account_id="a"))
        crud_project.get_by_slug_or_identifier.return_value = MagicMock()
        mock_get_tracker.return_value = FakeAzure()
        with pytest.raises(HTTPException) as exc:
            await mcp.create_pull_request(
                project="https://dev.azure.com/fabrikam/p/_git/web",
                title="t",
                source_branch="f",
                target_branch="main",
            )
    assert exc.value.status_code == 400


def test_resource_key_uses_repository_guid() -> None:
    key = FlowTriggerService._extract_resource_key(
        {
            "source": "azure_devops",
            "payload": {
                "repository": {"id": "guid-1"},
                "pull_request": {"number": 7},
            },
        }
    )
    assert key == "azure_devops:guid-1:pr:7"


def test_git_username_uses_provider_default() -> None:
    assert (
        resolve_tracker_git_username(SimpleNamespace(tracker_type="azure_devops"))
        is None
    )


def test_manual_review_payload_matches_webhook_shape() -> None:
    project = SimpleNamespace(
        id="p1",
        name="web",
        slug="fabrikam/My Project/web",
        identifier="guid-1",
        meta_data={"full_name": "fabrikam/My Project/web", "default_branch": "main"},
    )
    tracker = SimpleNamespace(id="t1", account_id="a1", tracker_type="azure_devops")
    data = build_pull_request_trigger_payload(
        {
            "number": 7,
            "title": "t",
            "url": PR_URL,
            "author": "Jane Doe",
            "source_branch": "f",
            "target_branch": "main",
            "head_sha": "abc",
        },
        project,
        tracker,
    )
    payload = data["payload"]
    assert data["source"] == "azure_devops"
    assert payload["repository"]["clone_url"] == (
        "https://dev.azure.com/fabrikam/My%20Project/_git/web"
    )
    assert payload["object_attributes"]["url"] == PR_URL
    assert payload["pull_request"]["head"] == {"ref": "f", "sha": "abc"}


WORK_ITEM_URL = "https://dev.azure.com/fabrikam/My%20Project/_workitems/edit/42"
WORK_ITEM_KEY = "fabrikam/My Project#42"


class FakeBoards:
    """AzureDevOpsTracker work item methods, recorded."""

    tracker_type = "azure_devops"

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    async def add_comment(self, issue_id: str, comment: str) -> Any:
        self.calls.append(("add", issue_id, comment))
        return SimpleNamespace(id="50", url=f"{WORK_ITEM_URL}#50")

    async def update_comment(self, issue_id: str, comment_id: str, body: str) -> Any:
        self.calls.append(("update", issue_id, comment_id, body))
        return SimpleNamespace(id=comment_id, url=f"{WORK_ITEM_URL}#{comment_id}")


async def test_work_item_url_and_key_route_to_the_issue_path(
    db_session: Session, test_user: User
) -> None:
    from preloop.models.models.issue import Issue

    tracker = Tracker(
        name="ado-boards",
        account_id=test_user.account_id,
        tracker_type="azure_devops",
        api_key="pat",
        url="https://dev.azure.com",
        connection_details={"organization": "fabrikam"},
    )
    db_session.add(tracker)
    db_session.commit()
    organization = Organization(
        name="fabrikam", identifier="fabrikam", tracker_id=tracker.id
    )
    db_session.add(organization)
    db_session.commit()
    project = Project(
        name="My Project",
        identifier="5febef5a-833d-4e14-b9c0-14cb638f91e7",
        slug="fabrikam/My Project/My Project",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()
    issue = Issue(
        title="preloop-test item",
        external_id="42",
        key=WORK_ITEM_KEY,
        external_url="https://dev.azure.com/fabrikam/My%20Project/_workitems/edit/42",
        project_id=project.id,
        tracker_id=tracker.id,
    )
    db_session.add(issue)
    db_session.commit()

    # The key looks like a pull request slug but names a stored work item.
    assert mcp._azure_work_item_target(db_session, WORK_ITEM_KEY, test_user.account_id)
    assert mcp._azure_work_item_target(db_session, WORK_ITEM_URL, test_user.account_id)
    assert not mcp._azure_work_item_target(
        db_session, "fabrikam/web#42", test_user.account_id
    )
    found = mcp._find_issue_by_identifier(
        db_session, WORK_ITEM_URL + "?view=details", test_user.account_id
    )
    assert found.id == issue.id

    fake = FakeBoards()
    with (
        patch(
            "preloop.api.endpoints.mcp.get_user_from_token_if_valid",
            new_callable=AsyncMock,
            return_value=test_user,
        ),
        patch("preloop.api.endpoints.mcp._get_tool_db", return_value=db_session),
        patch("preloop.api.endpoints.mcp._guard_delivery_report_write"),
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
            return_value=fake,
        ),
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
            return_value=(db_session, test_user),
        ),
        patch.object(db_session, "close"),
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer t"}
        by_key = await mcp.add_comment(target=WORK_ITEM_KEY, comment="from key")
        by_url = await mcp.add_comment(target=WORK_ITEM_URL, comment="from url")
        updated = await mcp.update_comment(
            target=WORK_ITEM_URL, comment_id="50", body="edited"
        )
    assert (by_key.comment_id, by_url.comment_id) == ("50", "50")
    assert fake.calls == [
        ("add", WORK_ITEM_KEY, "from key"),
        ("add", WORK_ITEM_KEY, "from url"),
        ("update", WORK_ITEM_KEY, "50", "edited"),
    ]
    assert updated.status == "updated"
    assert updated.url == f"{WORK_ITEM_URL}#50"


async def test_create_pull_request_missing_project_explains_remote_inputs() -> None:
    """A caller without a checkout gets actionable repository guidance."""
    from fastapi import HTTPException

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as request,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user", new_callable=AsyncMock
        ) as auth,
        patch("preloop.models.crud.crud_project") as projects,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client", new_callable=AsyncMock
        ) as tracker,
    ):
        request.return_value.headers = {"authorization": "Bearer test"}
        auth.return_value = (MagicMock(), MagicMock(account_id="account-1"))
        projects.get_by_slug_or_identifier.return_value = None
        with pytest.raises(HTTPException) as exc:
            await mcp.create_pull_request(
                project="unknown",
                title="Example",
                source_branch="feature",
                target_branch="main",
            )
    assert exc.value.status_code == 404
    assert "No local checkout is required" in exc.value.detail
    assert "repository URL" in exc.value.detail
    assert "branches must already exist" in exc.value.detail
    assert "Azure DevOps" in exc.value.detail
    tracker.assert_not_awaited()
    assert (
        projects.get_by_slug_or_identifier.call_args.kwargs["account_id"] == "account-1"
    )
