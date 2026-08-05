"""Commit status targeting for flow executions (issue #175).

A flow can watch several repositories. The commit status must land on the
repository that actually triggered the execution, not on
``flow.trigger_project_ids[0]``, and a failure to post it must be visible on
the execution rather than swallowed into a server log line.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from sqlalchemy.orm import Session

from preloop.models.crud import (
    crud_account,
    crud_flow,
    crud_organization,
    crud_project,
    crud_tracker,
    crud_user,
)
from preloop.models.models import Account, Flow
from preloop.models.models.user import User
from preloop.models.schemas.flow import FlowCreate
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator


@pytest.fixture
def account(db_session: Session) -> Account:
    return crud_account.create(
        db_session,
        obj_in={
            "organization_name": f"Commit Status Org {uuid4().hex[:8]}",
            "is_active": True,
        },
    )


@pytest.fixture
def account_user(db_session: Session, account: Account) -> User:
    user = crud_user.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "email": f"status_{uuid4().hex[:8]}@example.com",
            "username": f"status_user_{uuid4().hex[:8]}",
            "full_name": "Commit Status User",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "test_password",
            "user_source": "local",
        },
    )
    db_session.flush()
    return user


@pytest.fixture
def github_projects(db_session: Session, account: Account, account_user: User):
    """A GitHub tracker with one org and two repos, like Alex's setup."""
    tracker = crud_tracker.create(
        db_session,
        obj_in={
            "name": "GitHub",
            "tracker_type": "github",
            "url": "https://github.com",
            "api_key": "test_key",
            "account_id": str(account.id),
            "is_active": True,
        },
    )
    db_session.flush()

    organization = crud_organization.create(
        db_session,
        obj_in={
            "name": "acme",
            "identifier": "acme",
            "tracker_id": str(tracker.id),
            "is_active": True,
        },
    )
    db_session.flush()

    projects = {}
    for repo_name in ("cursor-config", "mender-mcu"):
        project = crud_project.create(
            db_session,
            obj_in={
                "name": repo_name,
                "identifier": f"id-{repo_name}",
                "slug": f"acme/{repo_name}",
                "organization_id": str(organization.id),
                "is_active": True,
            },
        )
        db_session.flush()
        projects[repo_name] = project

    return {"tracker": tracker, "organization": organization, "projects": projects}


@pytest.fixture
def multi_repo_flow(db_session: Session, account: Account, github_projects) -> Flow:
    """A PR-reviewer style flow watching two repositories."""
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Pull Request Reviewer",
            description="Reviews PRs across several repos",
            trigger_event_source=str(github_projects["tracker"].id),
            trigger_event_types=["pull_request_opened"],
            prompt_template="Review {{payload.pull_request.title}}",
            agent_type="claude",
            agent_config={},
            account_id=account.id,
        ),
        account_id=account.id,
    )
    flow.trigger_project_ids = [
        str(github_projects["projects"]["cursor-config"].id),
        str(github_projects["projects"]["mender-mcu"].id),
    ]
    db_session.add(flow)
    db_session.commit()
    db_session.refresh(flow)
    return flow


@pytest.fixture
def mock_nats_client():
    client = AsyncMock()
    client.is_connected = True
    client.publish = AsyncMock()
    return client


def _pull_request_event(account: Account, tracker_id: str, repo_full_name: str):
    """Webhook payload for a PR opened in ``repo_full_name``."""
    return {
        "source": "github",
        "type": "pull_request_opened",
        "account_id": str(account.id),
        "tracker_id": str(tracker_id),
        "payload": {
            "repository": {
                "full_name": repo_full_name,
                "name": repo_full_name.split("/")[-1],
            },
            "pull_request": {
                "number": 7,
                "title": "Bump toolchain",
                "head": {"sha": "a" * 40},
            },
        },
    }


def _make_orchestrator(db_session, flow, event_data, nats_client):
    orchestrator = FlowExecutionOrchestrator(
        db=db_session,
        flow_id=flow.id,
        trigger_event_data=event_data,
        nats_client=nats_client,
    )
    orchestrator._get_flow_details()
    orchestrator.execution_log = MagicMock(id=uuid4())
    return orchestrator


class TestCommitStatusProjectResolution:
    """The status must target the repo that triggered the run."""

    @pytest.mark.asyncio
    async def test_uses_second_watched_repo_when_it_triggered_the_flow(
        self, db_session, account, github_projects, multi_repo_flow, mock_nats_client
    ):
        """A PR in the SECOND trigger project resolves to that project.

        This is the exact reproduction from #175: before the fix the status
        went to cursor-config (index 0) and GitHub answered 422.
        """
        mender = github_projects["projects"]["mender-mcu"]
        orchestrator = _make_orchestrator(
            db_session,
            multi_repo_flow,
            _pull_request_event(
                account, github_projects["tracker"].id, "acme/mender-mcu"
            ),
            mock_nats_client,
        )

        resolved = await orchestrator._resolve_commit_status_project_id()

        assert resolved == str(mender.id)
        assert resolved != str(github_projects["projects"]["cursor-config"].id)

    @pytest.mark.asyncio
    async def test_uses_first_watched_repo_when_it_triggered_the_flow(
        self, db_session, account, github_projects, multi_repo_flow, mock_nats_client
    ):
        """The previously-working case must keep working."""
        cursor_config = github_projects["projects"]["cursor-config"]
        orchestrator = _make_orchestrator(
            db_session,
            multi_repo_flow,
            _pull_request_event(
                account, github_projects["tracker"].id, "acme/cursor-config"
            ),
            mock_nats_client,
        )

        resolved = await orchestrator._resolve_commit_status_project_id()

        assert resolved == str(cursor_config.id)

    @pytest.mark.asyncio
    async def test_explicit_project_id_on_the_event_wins(
        self, db_session, account, github_projects, multi_repo_flow, mock_nats_client
    ):
        """A project_id already resolved upstream is used as-is."""
        mender = github_projects["projects"]["mender-mcu"]
        event = _pull_request_event(
            account, github_projects["tracker"].id, "acme/mender-mcu"
        )
        event["project_id"] = str(mender.id)

        orchestrator = _make_orchestrator(
            db_session, multi_repo_flow, event, mock_nats_client
        )

        assert await orchestrator._resolve_commit_status_project_id() == str(mender.id)

    @pytest.mark.asyncio
    async def test_unmappable_repository_warns_instead_of_guessing(
        self, db_session, account, github_projects, multi_repo_flow, mock_nats_client
    ):
        """A repo we cannot map must NOT fall back to trigger_project_ids[0].

        Falling back would post the check to a repo the SHA does not exist in.
        """
        orchestrator = _make_orchestrator(
            db_session,
            multi_repo_flow,
            _pull_request_event(
                account, github_projects["tracker"].id, "acme/not-a-known-repo"
            ),
            mock_nats_client,
        )

        resolved = await orchestrator._resolve_commit_status_project_id()

        assert resolved is None
        warning = _published_warning(mock_nats_client)
        assert warning is not None
        assert "acme/not-a-known-repo" in warning["payload"]["message"]

    @pytest.mark.asyncio
    async def test_single_project_flow_without_repo_context_still_posts(
        self, db_session, account, github_projects, mock_nats_client
    ):
        """A one-repo flow triggered manually keeps its unambiguous target."""
        cursor_config = github_projects["projects"]["cursor-config"]
        flow = crud_flow.create(
            db=db_session,
            flow_in=FlowCreate(
                name="Single Repo Flow",
                description="Watches one repo",
                trigger_event_source=str(github_projects["tracker"].id),
                trigger_event_types=["pull_request_opened"],
                prompt_template="Review",
                agent_type="claude",
                agent_config={},
                account_id=account.id,
            ),
            account_id=account.id,
        )
        flow.trigger_project_ids = [str(cursor_config.id)]
        db_session.add(flow)
        db_session.commit()

        orchestrator = _make_orchestrator(
            db_session,
            flow,
            {"source": "github", "type": "manual", "payload": {}},
            mock_nats_client,
        )

        resolved = await orchestrator._resolve_commit_status_project_id()

        assert resolved == str(cursor_config.id)
        assert _published_warning(mock_nats_client) is None

    @pytest.mark.asyncio
    async def test_multi_repo_flow_without_repo_context_warns_about_the_guess(
        self, db_session, multi_repo_flow, mock_nats_client
    ):
        """With no repo context and several candidates, say the target is a guess."""
        orchestrator = _make_orchestrator(
            db_session,
            multi_repo_flow,
            {"source": "github", "type": "manual", "payload": {}},
            mock_nats_client,
        )

        resolved = await orchestrator._resolve_commit_status_project_id()

        assert resolved == str(multi_repo_flow.trigger_project_ids[0])
        warning = _published_warning(mock_nats_client)
        assert warning is not None
        assert "ambiguous" in warning["payload"]["message"].lower()

    @pytest.mark.asyncio
    async def test_flow_with_no_trigger_projects_is_silent(
        self, db_session, account, github_projects, mock_nats_client
    ):
        """Flows not tied to a repo are a normal case, not a warning."""
        flow = crud_flow.create(
            db=db_session,
            flow_in=FlowCreate(
                name="Repo-less Flow",
                description="No trigger projects",
                trigger_event_source=str(github_projects["tracker"].id),
                trigger_event_types=["pull_request_opened"],
                prompt_template="Review",
                agent_type="claude",
                agent_config={},
                account_id=account.id,
            ),
            account_id=account.id,
        )

        orchestrator = _make_orchestrator(
            db_session,
            flow,
            {"source": "github", "type": "manual", "payload": {}},
            mock_nats_client,
        )

        assert await orchestrator._resolve_commit_status_project_id() is None
        assert _published_warning(mock_nats_client) is None


class TestCommitStatusClient:
    """The tracker client must be built from the resolved project."""

    @pytest.mark.asyncio
    async def test_tracker_client_is_built_for_the_triggering_project(
        self,
        db_session,
        account,
        account_user,
        github_projects,
        multi_repo_flow,
        mock_nats_client,
    ):
        mender = github_projects["projects"]["mender-mcu"]
        orchestrator = _make_orchestrator(
            db_session,
            multi_repo_flow,
            _pull_request_event(
                account, github_projects["tracker"].id, "acme/mender-mcu"
            ),
            mock_nats_client,
        )

        fake_client = MagicMock()
        with patch(
            "preloop.api.common.get_tracker_client",
            new=AsyncMock(return_value=fake_client),
        ) as mock_get_client:
            client = await orchestrator._get_tracker_client_for_status()

        assert client is fake_client
        assert mock_get_client.await_args.kwargs["project_id"] == mender.id

    @pytest.mark.asyncio
    async def test_no_client_when_the_repository_cannot_be_mapped(
        self,
        db_session,
        account,
        account_user,
        github_projects,
        multi_repo_flow,
        mock_nats_client,
    ):
        orchestrator = _make_orchestrator(
            db_session,
            multi_repo_flow,
            _pull_request_event(
                account, github_projects["tracker"].id, "acme/somebody-elses-repo"
            ),
            mock_nats_client,
        )

        with patch(
            "preloop.api.common.get_tracker_client",
            new=AsyncMock(side_effect=AssertionError("must not be called")),
        ):
            assert await orchestrator._get_tracker_client_for_status() is None


class TestCommitStatusFailureVisibility:
    """A failed status POST must reach the execution timeline."""

    @pytest.mark.asyncio
    async def test_failed_post_emits_an_execution_warning(
        self, db_session, account, github_projects, multi_repo_flow, mock_nats_client
    ):
        """The 422 from #175 used to be invisible while the flow showed green."""
        orchestrator = _make_orchestrator(
            db_session,
            multi_repo_flow,
            _pull_request_event(
                account, github_projects["tracker"].id, "acme/mender-mcu"
            ),
            mock_nats_client,
        )

        failing_client = MagicMock()
        failing_client.connection_details = {"owner": "acme", "repo": "mender-mcu"}
        failing_client.create_commit_status = AsyncMock(
            side_effect=RuntimeError("422 - No commit found for SHA")
        )
        orchestrator._tracker_client = failing_client

        # Must not raise: a status failure never fails the execution.
        await orchestrator._update_commit_status("success", "All good")

        warning = _published_warning(mock_nats_client)
        assert warning is not None
        message = warning["payload"]["message"]
        assert "acme/mender-mcu" in message
        assert "No commit found for SHA" in message
        assert warning["payload"]["details"]["state"] == "success"

    @pytest.mark.asyncio
    async def test_successful_post_emits_no_warning(
        self, db_session, account, github_projects, multi_repo_flow, mock_nats_client
    ):
        orchestrator = _make_orchestrator(
            db_session,
            multi_repo_flow,
            _pull_request_event(
                account, github_projects["tracker"].id, "acme/mender-mcu"
            ),
            mock_nats_client,
        )

        client = MagicMock()
        client.connection_details = {"owner": "acme", "repo": "mender-mcu"}
        client.create_commit_status = AsyncMock(return_value={"state": "success"})
        orchestrator._tracker_client = client

        await orchestrator._update_commit_status("success", "All good")

        client.create_commit_status.assert_awaited_once()
        assert _published_warning(mock_nats_client) is None

    @pytest.mark.asyncio
    async def test_repeated_failures_warn_once_per_execution(
        self, db_session, account, github_projects, multi_repo_flow, mock_nats_client
    ):
        """pending + success would otherwise duplicate the same line."""
        orchestrator = _make_orchestrator(
            db_session,
            multi_repo_flow,
            _pull_request_event(
                account, github_projects["tracker"].id, "acme/mender-mcu"
            ),
            mock_nats_client,
        )

        failing_client = MagicMock()
        failing_client.connection_details = {"owner": "acme", "repo": "mender-mcu"}
        failing_client.create_commit_status = AsyncMock(
            side_effect=RuntimeError("422 - No commit found for SHA")
        )
        orchestrator._tracker_client = failing_client

        await orchestrator._update_commit_status("success", "All good")
        await orchestrator._update_commit_status("success", "All good")

        assert len(_published_warnings(mock_nats_client)) == 1


def _published_warnings(nats_client):
    """All execution_warning messages published to NATS."""
    import json

    warnings = []
    for call in nats_client.publish.await_args_list:
        payload = json.loads(call.args[1].decode())
        if payload.get("type") == "execution_warning":
            warnings.append(payload)
    return warnings


def _published_warning(nats_client):
    warnings = _published_warnings(nats_client)
    return warnings[0] if warnings else None
