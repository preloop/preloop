"""update_pull_request records a receipt only for a delivered review (EE #202)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.api.endpoints import mcp
from preloop.models.crud import (
    crud_flow,
    crud_flow_execution,
    crud_review_publication_receipt,
)
from preloop.models.models import Organization, Project, Tracker
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate

HEAD = "e" * 40


def _execution(db_session: Session, user) -> str:
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Receipt Reviewer",
            prompt_template="Review",
            trigger_event_source="github",
            trigger_event_types=["pull_request_opened"],
            agent_type="codex",
            agent_config={},
            account_id=user.account_id,
        ),
        account_id=user.account_id,
    )
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )
    # The endpoint closes its session before provider I/O.
    db_session.commit()
    return str(execution.id)


def _project(db_session: Session, user, tracker_type: str = "github") -> None:
    tracker = Tracker(
        name=f"{tracker_type}-tracker",
        account_id=user.account_id,
        tracker_type=tracker_type,
        api_key="test_key",
        url=f"https://{tracker_type}.example.com",
    )
    db_session.add(tracker)
    db_session.commit()
    org = Organization(name="org", identifier="org", tracker_id=tracker.id)
    db_session.add(org)
    db_session.commit()
    db_session.add(
        Project(
            name="example-org/example-repo",
            identifier="example-org/example-repo",
            slug="example-org/example-repo",
            organization_id=org.id,
        )
    )
    db_session.commit()


async def _call(db_session, user, tracker, execution_id, *, context_id=None, **kw):
    user._auth_api_key = SimpleNamespace(
        context_data={"flow_execution_id": execution_id} if execution_id else {}
    )
    context = SimpleNamespace(flow_execution_id=context_id)
    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as req,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
            return_value=(db_session, user),
        ),
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
            return_value=tracker,
        ),
        patch(
            "preloop.services.dynamic_fastmcp_http.get_current_user_context",
            return_value=context,
        ),
    ):
        req.return_value.headers = {"authorization": "Bearer t"}
        return await mcp.update_pull_request(
            pull_request="example-org/example-repo#7", **kw
        )


def _github_tracker(**review):
    tracker = MagicMock()
    tracker.tracker_type = "github"
    tracker.submit_pull_request_review = AsyncMock(
        return_value={"id": 555, "html_url": "https://x", "commit_id": HEAD, **review}
    )
    tracker.add_pull_request_reaction = AsyncMock(return_value={})
    return tracker


def _receipts(db_session, user, execution_id):
    return crud_review_publication_receipt.list_for_execution(
        db_session, account_id=user.account_id, execution_id=execution_id
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "comment", "request_changes"])
async def test_github_review_records_bound_receipt(db_session, test_user, action):
    _project(db_session, test_user)
    execution_id = _execution(db_session, test_user)
    await _call(
        db_session,
        test_user,
        _github_tracker(),
        execution_id,
        review_action=action,
        review_body="Findings",
    )
    [receipt] = _receipts(db_session, test_user, execution_id)
    assert receipt.provider == "github"
    assert receipt.repository == "example-org/example-repo"
    assert receipt.pull_request == "7"
    assert receipt.head_sha == HEAD
    assert receipt.review_action == action
    assert receipt.provider_review_id == "555"
    assert (
        crud_review_publication_receipt.list_for_execution(
            db_session, account_id=uuid4(), execution_id=execution_id
        )
        == []
    )
    assert (
        crud_review_publication_receipt.list_for_execution(
            db_session, account_id=test_user.account_id, execution_id=uuid4()
        )
        == []
    )


@pytest.mark.asyncio
async def test_reaction_only_records_nothing(db_session, test_user):
    _project(db_session, test_user)
    execution_id = _execution(db_session, test_user)
    tracker = _github_tracker()
    with patch.object(mcp, "_record_review_receipt") as record:
        try:
            await _call(
                db_session, test_user, tracker, execution_id, add_reaction="eyes"
            )
        except HTTPException:
            pass
    record.assert_not_called()
    assert _receipts(db_session, test_user, execution_id) == []


@pytest.mark.asyncio
async def test_provider_failure_records_nothing(db_session, test_user):
    _project(db_session, test_user)
    execution_id = _execution(db_session, test_user)
    tracker = _github_tracker()
    tracker.submit_pull_request_review = AsyncMock(
        side_effect=RuntimeError("401 Bad credentials")
    )
    with pytest.raises(HTTPException):
        await _call(
            db_session,
            test_user,
            tracker,
            execution_id,
            review_action="approve",
            review_body="LGTM",
        )
    assert _receipts(db_session, test_user, execution_id) == []


@pytest.mark.asyncio
async def test_no_execution_credential_records_nothing(db_session, test_user):
    _project(db_session, test_user)
    execution_id = _execution(db_session, test_user)
    await _call(
        db_session,
        test_user,
        _github_tracker(),
        None,
        review_action="approve",
        review_body="LGTM",
    )
    assert _receipts(db_session, test_user, execution_id) == []


@pytest.mark.asyncio
async def test_conflicting_credential_and_context_record_nothing(db_session, test_user):
    _project(db_session, test_user)
    execution_id = _execution(db_session, test_user)
    await _call(
        db_session,
        test_user,
        _github_tracker(),
        execution_id,
        context_id=str(uuid4()),
        review_action="approve",
        review_body="LGTM",
    )
    assert _receipts(db_session, test_user, execution_id) == []


@pytest.mark.asyncio
async def test_execution_of_another_account_records_nothing(db_session, test_user):
    _project(db_session, test_user)
    await _call(
        db_session,
        test_user,
        _github_tracker(),
        str(uuid4()),
        review_action="approve",
        review_body="LGTM",
    )
    assert crud_review_publication_receipt.get_multi(db_session) == []


@pytest.mark.asyncio
async def test_receipt_write_failure_is_logged_not_raised(
    db_session, test_user, caplog
):
    _project(db_session, test_user)
    execution_id = _execution(db_session, test_user)
    with patch.object(
        crud_review_publication_receipt,
        "record",
        side_effect=RuntimeError("database unavailable"),
    ):
        response = await _call(
            db_session,
            test_user,
            _github_tracker(),
            execution_id,
            review_action="approve",
            review_body="LGTM",
        )
    assert response.status == "updated"
    assert "Could not record the review publication receipt" in caplog.text


@pytest.mark.asyncio
async def test_gitlab_approval_records_receipt_without_head(db_session, test_user):
    _project(db_session, test_user, tracker_type="gitlab")
    execution_id = _execution(db_session, test_user)
    tracker = MagicMock()
    tracker.tracker_type = "gitlab"
    tracker.approve_merge_request = AsyncMock(return_value={"id": 9})
    tracker.create_mr_discussion = AsyncMock(return_value={"id": "d1"})
    await _call(
        db_session,
        test_user,
        tracker,
        execution_id,
        review_action="approve",
        review_body="LGTM",
    )
    [receipt] = _receipts(db_session, test_user, execution_id)
    assert receipt.provider == "gitlab"
    assert receipt.head_sha is None
    assert receipt.provider_review_id == "9"


@pytest.mark.asyncio
async def test_real_github_tracker_shape_binds_the_head(db_session, test_user):
    """The tracker's own return value, not a mock dict, carries commit_id."""
    from preloop.sync.trackers.github import GitHubTracker

    _project(db_session, test_user)
    execution_id = _execution(db_session, test_user)
    tracker = GitHubTracker(
        str(uuid4()), "k", {"owner": "example-org", "repo": "example-repo"}
    )
    tracker._request = AsyncMock(
        return_value={
            "id": 77,
            "state": "APPROVED",
            "user": {"login": "bot"},
            "commit_id": HEAD,
        }
    )
    await _call(
        db_session,
        test_user,
        tracker,
        execution_id,
        review_action="approve",
        review_body="LGTM",
    )
    [receipt] = _receipts(db_session, test_user, execution_id)
    assert receipt.head_sha == HEAD
    assert receipt.provider_review_id == "77"


def test_prior_receipt_lookup_needs_the_same_head(db_session, test_user):
    """A headless receipt cannot prove this head was already reviewed."""
    first = _execution(db_session, test_user)
    second = _execution(db_session, test_user)
    common = dict(
        account_id=test_user.account_id,
        provider="gitlab",
        repository="example-org/example-repo",
        pull_request="7",
        review_action="approve",
    )
    crud_review_publication_receipt.record(
        db_session, execution_id=first, head_sha=None, **common
    )
    lookup = dict(
        account_id=test_user.account_id,
        repository="example-org/example-repo",
        pull_request="7",
        head_sha=HEAD,
    )
    assert crud_review_publication_receipt.find_for_target(db_session, **lookup) is None
    crud_review_publication_receipt.record(
        db_session, execution_id=second, head_sha=HEAD.upper(), **common
    )
    found = crud_review_publication_receipt.find_for_target(db_session, **lookup)
    assert found is not None and str(found.execution_id) == second


@pytest.mark.asyncio
@pytest.mark.parametrize(("posted", "expected"), [("c-42", "c-42"), ("7", None)])
async def test_bitbucket_receipt_records_the_summary_comment_id(
    db_session, test_user, posted, expected
):
    from preloop.schemas.mcp import UpdatePullRequestResponse

    _project(db_session, test_user, tracker_type="bitbucket")
    execution_id = _execution(db_session, test_user)
    tracker = MagicMock()
    tracker.tracker_type = "bitbucket"
    with patch(
        "preloop.api.endpoints.mcp.mcp_bitbucket.update_pull_request",
        new_callable=AsyncMock,
        return_value=UpdatePullRequestResponse(
            pull_request_id=posted, status="updated", message="ok", url="https://x"
        ),
    ):
        await _call(
            db_session,
            test_user,
            tracker,
            execution_id,
            review_action="approve",
            review_body="LGTM",
        )
    [receipt] = _receipts(db_session, test_user, execution_id)
    assert receipt.provider == "bitbucket"
    assert receipt.provider_review_id == expected


def _gitlab_project() -> SimpleNamespace:
    # GitLab projects store the numeric id as ``identifier`` and the
    # ``path_with_namespace`` as ``slug``.
    return SimpleNamespace(identifier="4242", slug="example-group/example-project")


@pytest.mark.parametrize(
    ("project_path", "owner", "repo", "expected"),
    [
        # A numeric GitLab project id resolves to the project's path.
        ("4242", None, None, "example-group/example-project"),
        (" 4242 ", None, None, "example-group/example-project"),
        # A path the agent passed is kept as given.
        (
            "example-group/example-project",
            "example-group",
            "example-project",
            "example-group/example-project",
        ),
        (
            "example-group/sub/project",
            "example-group/sub",
            "project",
            "example-group/sub/project",
        ),
        # Nothing parsed: the resolved project's path.
        (None, None, None, "example-group/example-project"),
    ],
)
def test_receipt_repository_resolves_numeric_gitlab_ids(
    project_path, owner, repo, expected
):
    assert (
        mcp._review_receipt_repository(project_path, owner, repo, _gitlab_project())
        == expected
    )


def test_receipt_repository_keeps_numeric_id_without_resolved_path():
    # No path to resolve to: record what was given, so the gate fails closed
    # rather than inventing a target.
    project = SimpleNamespace(identifier="4242", slug=None)
    assert mcp._review_receipt_repository("4242", None, None, project) == "4242"
