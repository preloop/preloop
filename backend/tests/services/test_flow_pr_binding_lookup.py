"""Deterministic PR binding and the publication-aware retry boundary.

The container's post-execution block creates the PR with deterministic
code, so the orchestrator must not depend on the ``PRELOOP_PR_OPENED`` log
line to learn about it. When the line is missing, the PR is looked up by
head branch through the tracker client and bound with ``record_opened_pr``.
The same evidence stops ``_retry_decision`` from relaunching an attempt
whose post-exec block pushed and then failed (prod executions 0976028b and
e8f21789, 2026-09-26: ``PR create HTTP 201`` then
``PRELOOP_PROVENANCE_FAILED: unbound variable``).
"""

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models.crud import crud_account, crud_flow, crud_user
from preloop.models.models import Account, Flow
from preloop.models.models.user import User
from preloop.models.schemas.flow import FlowCreate
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.sync.trackers.github import GitHubTracker

BRANCH = "preloop/issue-951-0976028b"
PR_URL = "https://github.com/acme/app/pull/964"
MARKER = (
    f'PRELOOP_PR_OPENED {{"url": "{PR_URL}", "branch": "{BRANCH}", '
    '"provider": "github"}'
)
CRASH_AFTER_201 = (
    "Upstream model provider timed out (HTTP 504). "
    "Creating pull request on GitHub... PR create HTTP 201\n"
    "/tmp/preloop/agent-script.sh: line 1838: PRELOOP_PROVENANCE_FAILED: "
    "unbound variable"
)
PRE_PUSH_TRANSIENT = "Upstream model provider timed out (HTTP 504) after 3 attempts."


def _github_client(*, prs: List[Dict[str, Any]], branch_exists: bool = False):
    client = GitHubTracker("tracker-1", "token", {"owner": "acme", "repo": "app"})
    client.list_pull_requests = AsyncMock(return_value={"items": prs})
    client.branch_exists = AsyncMock(return_value=branch_exists)
    return client


def _listed(url: str = PR_URL, branch: str = BRANCH) -> Dict[str, Any]:
    return {"number": 964, "url": url, "source_branch": branch, "state": "open"}


# --------------------------------------------------------------------------
# Unit level: an orchestrator shell with no database.
# --------------------------------------------------------------------------


def _bare_orchestrator(context: Dict[str, Any] | None = None):
    orchestrator = FlowExecutionOrchestrator.__new__(FlowExecutionOrchestrator)
    orchestrator._opened_pr = None
    orchestrator._opened_pr_bound = False
    orchestrator._opened_pr_by_lookup = False
    orchestrator._remote_branch_published = False
    orchestrator._isolated_publication_policy = None
    orchestrator.db = MagicMock()
    orchestrator.flow = MagicMock(git_clone_config=None, account_id=uuid4())
    orchestrator.execution_log = MagicMock(result={})
    orchestrator.execution_log.id = uuid4()
    orchestrator.execution_logger = MagicMock()
    orchestrator.execution_logger.get_agent_output_lines.return_value = []
    orchestrator.execution_logger.get_actions_taken.return_value = []
    orchestrator._execution_context = (
        context
        if context is not None
        else {
            "git_clone_config": {"enabled": True, "create_pull_request": True},
            "_git_target_branch": BRANCH,
            "_git_source_branch": "main",
            "trigger_project_id": "project-1",
        }
    )
    return orchestrator


def _failed(error_message: str, **extra):
    return {
        "status": "FAILED",
        "exit_code": 1,
        "error_message": error_message,
        "failure_analysis": {"transient": True},
        **extra,
    }


class TestLookupBinding:
    @pytest.mark.asyncio
    async def test_happy_path_marker_binds_without_a_lookup(self, monkeypatch):
        orchestrator = _bare_orchestrator()
        calls = []
        monkeypatch.setattr(
            "preloop.services.flow_orchestrator.record_opened_pr",
            lambda db, execution_id, url, source_branch=None: calls.append(
                (url, source_branch)
            ),
        )
        clients = AsyncMock()
        monkeypatch.setattr(orchestrator, "_publication_tracker_clients", clients)

        orchestrator._note_opened_pr(MARKER)
        bound = await orchestrator._bind_published_pr_by_branch()

        assert bound["url"] == PR_URL
        assert calls == [(PR_URL, BRANCH)]
        clients.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_marker_binds_by_head_branch_lookup(self, monkeypatch):
        orchestrator = _bare_orchestrator()
        calls = []
        monkeypatch.setattr(
            "preloop.services.flow_orchestrator.record_opened_pr",
            lambda db, execution_id, url, source_branch=None, opened_at=None: (
                calls.append((execution_id, url, source_branch, opened_at))
            ),
        )
        client = _github_client(prs=[_listed()])
        monkeypatch.setattr(
            orchestrator,
            "_publication_tracker_clients",
            AsyncMock(return_value=[client]),
        )

        bound = await orchestrator._bind_published_pr_by_branch()

        assert bound == {"url": PR_URL, "branch": BRANCH, "provider": "github"}
        assert calls == [(orchestrator.execution_log.id, PR_URL, BRANCH, None)]
        assert client.list_pull_requests.await_args.kwargs["head_branch"] == BRANCH
        assert orchestrator._opened_pr_bound is True

    @pytest.mark.asyncio
    async def test_lookup_carries_the_forge_created_at(self, monkeypatch):
        orchestrator = _bare_orchestrator()
        calls = []
        monkeypatch.setattr(
            "preloop.services.flow_orchestrator.record_opened_pr",
            lambda db, execution_id, url, source_branch=None, opened_at=None: (
                calls.append(opened_at)
            ),
        )
        listed = {**_listed(), "created_at": "2026-09-20T08:00:00Z"}
        client = _github_client(prs=[listed])
        monkeypatch.setattr(
            orchestrator,
            "_publication_tracker_clients",
            AsyncMock(return_value=[client]),
        )

        bound = await orchestrator._bind_published_pr_by_branch()

        assert bound["created_at"] == "2026-09-20T08:00:00Z"
        assert calls == ["2026-09-20T08:00:00Z"]

    @pytest.mark.asyncio
    async def test_bitbucket_lookup_goes_through_the_tracker_interface(
        self, monkeypatch
    ):
        from preloop.sync.trackers.bitbucket import BitbucketTracker

        orchestrator = _bare_orchestrator()
        calls = []
        monkeypatch.setattr(
            "preloop.services.flow_orchestrator.record_opened_pr",
            lambda db, execution_id, url, source_branch=None, opened_at=None: (
                calls.append((url, opened_at))
            ),
        )
        client = BitbucketTracker.__new__(BitbucketTracker)
        pr_url = "https://bitbucket.org/acme/app/pull-requests/7"
        client.list_pull_requests = AsyncMock(
            return_value={
                "items": [
                    _listed(
                        url="https://bitbucket.org/acme/app/pull-requests/6",
                        branch="other",
                    ),
                    {**_listed(url=pr_url), "created_at": "2026-09-20T08:00:00Z"},
                ]
            }
        )
        monkeypatch.setattr(
            orchestrator,
            "_publication_tracker_clients",
            AsyncMock(return_value=[client]),
        )

        bound = await orchestrator._bind_published_pr_by_branch()

        assert bound["url"] == pr_url and bound["provider"] == "bitbucket"
        assert calls == [(pr_url, "2026-09-20T08:00:00Z")]

    @pytest.mark.asyncio
    async def test_tracker_without_pull_requests_finds_nothing(self):
        from preloop.sync.trackers.jira import JiraTracker

        client = JiraTracker.__new__(JiraTracker)
        assert await client.find_open_pull_request_for_branch(BRANCH) is None

    def test_listing_guard_ignores_other_branches_and_urlless_items(self):
        from preloop.sync.trackers.base import BaseTracker

        listing = {
            "items": [
                "garbage",
                _listed(branch="other"),
                {**_listed(), "url": ""},
                _listed(),
            ]
        }
        assert BaseTracker._first_listed_for_branch(listing, BRANCH) == _listed()
        assert BaseTracker._first_listed_for_branch(None, BRANCH) is None

    @pytest.mark.asyncio
    async def test_gitlab_lookup_filters_by_source_branch(self, monkeypatch):
        from preloop.sync.trackers.gitlab import GitLabTracker

        orchestrator = _bare_orchestrator()
        monkeypatch.setattr(
            "preloop.services.flow_orchestrator.record_opened_pr",
            lambda *a, **k: None,
        )
        client = GitLabTracker.__new__(GitLabTracker)
        mr_url = "https://gitlab.com/acme/app/-/merge_requests/5"
        client.list_merge_requests = AsyncMock(
            return_value={"items": [_listed(url=mr_url)]}
        )
        monkeypatch.setattr(
            orchestrator,
            "_publication_tracker_clients",
            AsyncMock(return_value=[client]),
        )

        bound = await orchestrator._bind_published_pr_by_branch()

        assert bound["url"] == mr_url and bound["provider"] == "gitlab"
        assert client.list_merge_requests.await_args.kwargs["source_branch"] == BRANCH

    @pytest.mark.asyncio
    async def test_lookup_ignores_a_pr_from_another_branch(self, monkeypatch):
        orchestrator = _bare_orchestrator()
        record = MagicMock()
        monkeypatch.setattr(
            "preloop.services.flow_orchestrator.record_opened_pr", record
        )
        client = _github_client(prs=[_listed(branch="someone-else")])
        monkeypatch.setattr(
            orchestrator,
            "_publication_tracker_clients",
            AsyncMock(return_value=[client]),
        )

        assert await orchestrator._bind_published_pr_by_branch() is None
        record.assert_not_called()

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda o: setattr(o, "_isolated_publication_policy", object()),
            lambda o: o._execution_context["git_clone_config"].update(
                create_pull_request=False
            ),
            lambda o: o._execution_context.pop("_git_target_branch"),
            lambda o: setattr(o.execution_log, "result", {"pr_url": PR_URL}),
        ],
        ids=["isolated", "create_pr_off", "no_target_branch", "already_bound"],
    )
    @pytest.mark.asyncio
    async def test_lookup_is_skipped(self, monkeypatch, mutate):
        orchestrator = _bare_orchestrator()
        mutate(orchestrator)
        clients = AsyncMock(return_value=[_github_client(prs=[_listed()])])
        monkeypatch.setattr(orchestrator, "_publication_tracker_clients", clients)

        assert await orchestrator._bind_published_pr_by_branch() is None
        clients.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tracker_error_leaves_the_run_unbound(self, monkeypatch):
        orchestrator = _bare_orchestrator()
        client = _github_client(prs=[])
        client.list_pull_requests = AsyncMock(side_effect=RuntimeError("502"))
        monkeypatch.setattr(
            orchestrator,
            "_publication_tracker_clients",
            AsyncMock(return_value=[client]),
        )

        assert await orchestrator._bind_published_pr_by_branch() is None
        assert orchestrator._opened_pr is None


class TestRetryDecisionEvidence:
    def test_pr_create_2xx_in_the_tail_blocks_the_retry(self):
        orchestrator = _bare_orchestrator()
        reason = orchestrator._retry_decision(_failed(CRASH_AFTER_201))
        assert reason is not None and "pull request create response" in reason

    def test_pushing_line_in_the_agent_output_blocks_the_retry(self):
        orchestrator = _bare_orchestrator()
        orchestrator.execution_logger.get_agent_output_lines.return_value = [
            f"Found 1 commits on {BRANCH}, pushing...",
        ]
        reason = orchestrator._retry_decision(_failed(PRE_PUSH_TRANSIENT))
        assert reason is not None and "commits being pushed" in reason

    def test_bound_pr_blocks_the_retry(self):
        orchestrator = _bare_orchestrator()
        orchestrator._opened_pr = {"url": PR_URL, "branch": BRANCH}
        reason = orchestrator._retry_decision(_failed(PRE_PUSH_TRANSIENT))
        assert reason is not None and PR_URL in reason

    def test_pushed_branch_blocks_the_retry(self):
        orchestrator = _bare_orchestrator()
        orchestrator._remote_branch_published = True
        reason = orchestrator._retry_decision(_failed(PRE_PUSH_TRANSIENT))
        assert reason is not None and BRANCH in reason

    def test_genuine_pre_push_transient_failure_is_still_retried(self):
        orchestrator = _bare_orchestrator()
        orchestrator.execution_logger.get_agent_output_lines.return_value = [
            "Attempt 3 failed with status 504. Max attempts reached.",
            # The evidence upload runs from an EXIT trap on every failure;
            # it is not proof that anything was pushed.
            "PRELOOP_EVIDENCE committed art-1",
        ]
        assert orchestrator._retry_decision(_failed(PRE_PUSH_TRANSIENT)) is None

    def test_resume_pr_found_by_lookup_is_not_publication_evidence(self):
        """A resume pushes onto a PR that existed before this run."""
        orchestrator = _bare_orchestrator()
        orchestrator._execution_context["_git_source_branch"] = BRANCH
        orchestrator._opened_pr = {"url": PR_URL, "branch": BRANCH}
        orchestrator._opened_pr_by_lookup = True
        assert orchestrator._retry_decision(_failed(PRE_PUSH_TRANSIENT)) is None

    @pytest.mark.asyncio
    async def test_resume_run_does_not_probe_the_branch(self, monkeypatch):
        orchestrator = _bare_orchestrator()
        orchestrator._execution_context["trigger_event_data"] = {
            "_resume": {"source_branch": BRANCH}
        }
        clients = AsyncMock(return_value=[_github_client(prs=[], branch_exists=True)])
        monkeypatch.setattr(orchestrator, "_publication_tracker_clients", clients)
        await orchestrator._probe_remote_branch_published()
        assert orchestrator._remote_branch_published is False
        clients.assert_not_awaited()


# --------------------------------------------------------------------------
# End to end through ``run()`` with a real database session.
# --------------------------------------------------------------------------


@pytest.fixture
def account(db_session: Session) -> Account:
    return crud_account.create(
        db_session,
        obj_in={"organization_name": f"Org {uuid4().hex[:8]}", "is_active": True},
    )


@pytest.fixture
def user(db_session: Session, account: Account) -> User:
    created = crud_user.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "email": f"pr_binding_{uuid4().hex[:8]}@example.com",
            "username": f"pr_binding_{uuid4().hex[:8]}",
            "full_name": "PR Binding",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )
    db_session.flush()
    account.primary_user_id = created.id
    db_session.add(account)
    db_session.commit()
    return created


@pytest.fixture
def flow(db_session: Session, account: Account, user: User) -> Flow:
    created = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Issue implementer",
            description="implements issues",
            trigger_event_source="github",
            trigger_event_types=["issue_labeled"],
            prompt_template="Implement {{payload.issue.title}}",
            agent_type="codex",
            agent_config={},
            account_id=account.id,
        ),
        account_id=account.id,
    )
    created.git_clone_config = {"enabled": True, "create_pull_request": True}
    db_session.add(created)
    db_session.commit()
    return created


@pytest.fixture
def event_data():
    return {
        "source": "github",
        "type": "issue_labeled",
        "event_id": f"evt_{uuid4().hex[:8]}",
        "payload": {"issue": {"title": "Fix it", "number": 951}},
        "account_id": str(uuid4()),
    }


async def _run(db_session, flow, event_data, monitor, client):
    """Run the orchestrator; the executor stamps the branch like container.py."""

    def start(context):
        context["_git_target_branch"] = BRANCH
        context["_git_source_branch"] = "main"
        return "session-under-test"

    executor = AsyncMock()
    executor.start = AsyncMock(side_effect=start)
    executor.cleanup = AsyncMock()
    nats = AsyncMock()
    nats.is_connected = True
    with (
        patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=executor,
        ),
        patch.object(
            FlowExecutionOrchestrator, "_monitor_agent_execution", side_effect=monitor
        ),
        patch.object(
            FlowExecutionOrchestrator,
            "_publication_tracker_clients",
            AsyncMock(return_value=[client]),
        ),
        patch("preloop.services.flow_orchestrator.asyncio.sleep", AsyncMock()),
    ):
        orchestrator = FlowExecutionOrchestrator(
            db=db_session,
            flow_id=flow.id,
            trigger_event_data=event_data,
            nats_client=nats,
        )
        await orchestrator.run()
    return orchestrator


def _monitor(results):
    calls = []

    async def monitor(session_reference, agent_executor):
        calls.append(session_reference)
        return dict(results[min(len(calls), len(results)) - 1])

    return monitor, calls


def _agent(status, error_message=None, exit_code=1, output_summary=""):
    return {
        "status": status,
        "error_message": error_message,
        "exit_code": exit_code,
        "output_summary": output_summary,
        "actions_taken": [],
        "mcp_usage_logs": [],
    }


@pytest.mark.asyncio
class TestEndToEnd:
    async def test_happy_path_marker_binds_the_pr(self, db_session, flow, event_data):
        monitor, calls = _monitor(
            [_agent("SUCCEEDED", exit_code=0, output_summary=f"pushing\n{MARKER}\n")]
        )
        client = _github_client(prs=[])
        orchestrator = await _run(db_session, flow, event_data, monitor, client)

        assert len(calls) == 1
        assert orchestrator.execution_log.result["pr_url"] == PR_URL
        assert orchestrator.execution_log.result["pr_source_branch"] == BRANCH
        client.list_pull_requests.assert_not_awaited()

    async def test_crash_after_201_binds_by_lookup_and_is_not_retried(
        self, db_session, flow, event_data
    ):
        monitor, calls = _monitor([_agent("FAILED", CRASH_AFTER_201)])
        client = _github_client(prs=[_listed()])
        with patch("preloop.services.flow_feedback.register_thread") as register_thread:
            orchestrator = await _run(db_session, flow, event_data, monitor, client)

        assert len(calls) == 1, "a post-exec failure after the PR exists is final"
        # Bound through record_opened_pr -> bind_publication -> register_thread,
        # so the review-feedback thread is created like the marker path does.
        register_thread.assert_called_once()
        _db, execution, url, branch = register_thread.call_args.args
        assert execution.id == orchestrator.execution_log.id
        assert (url, branch) == (PR_URL, BRANCH)
        assert orchestrator.execution_log.status == "FAILED"
        assert orchestrator.execution_log.result["pr_url"] == PR_URL
        assert orchestrator.execution_log.result["pr_source_branch"] == BRANCH

    async def test_pushed_branch_without_pr_suppresses_the_retry(
        self, db_session, flow, event_data
    ):
        monitor, calls = _monitor(
            [_agent("FAILED", PRE_PUSH_TRANSIENT), _agent("SUCCEEDED", exit_code=0)]
        )
        client = _github_client(prs=[], branch_exists=True)
        orchestrator = await _run(db_session, flow, event_data, monitor, client)

        assert len(calls) == 1
        assert orchestrator.execution_log.status == "FAILED"
        client.branch_exists.assert_awaited_with(BRANCH)

    async def test_genuine_pre_push_transient_failure_is_retried(
        self, db_session, flow, event_data
    ):
        monitor, calls = _monitor(
            [_agent("FAILED", PRE_PUSH_TRANSIENT), _agent("SUCCEEDED", exit_code=0)]
        )
        client = _github_client(prs=[], branch_exists=False)
        orchestrator = await _run(db_session, flow, event_data, monitor, client)

        assert len(calls) == 2
        assert orchestrator.execution_log.status == "SUCCEEDED"
