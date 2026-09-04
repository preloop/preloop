"""Tests for CI-failure retrigger of a bound implementation flow."""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from preloop.services import flow_ci_feedback as mod
from preloop.services.flow_ci_feedback import (
    CI_FAILURE_KEY,
    DEFAULT_CI_FAILURE_CAP,
    bind_ci_failure_resume_or_skip,
    ci_failure_cap,
    extract_ci_failure,
    flow_requires_ci_failure_resume,
)

PR_URL = "https://github.com/preloop/preloop/pull/353"
MR_URL = "https://gitlab.com/acme/backend/-/merge_requests/10"
BRANCH = "preloop/issue-42"


class FakeFlow:
    def __init__(self, trigger_event_types=None, **attrs):
        self.id = uuid4()
        self.name = "impl"
        self.trigger_event_types = trigger_event_types or [
            "issue_labeled",
            "check_run",
            "pipeline",
        ]
        for key, value in attrs.items():
            setattr(self, key, value)


class FakeExecution:
    def __init__(self, result=None, trigger_event_details=None):
        self.id = uuid4()
        self.result = result
        self.trigger_event_details = trigger_event_details


def bound_execution(pr_url=PR_URL, branch=BRANCH):
    return FakeExecution(result={"pr_url": pr_url, "pr_source_branch": branch})


@pytest.fixture
def crud(monkeypatch):
    """Patch the CRUD surface flow_ci_feedback uses. No database needed."""

    fake = MagicMock()
    fake.get_by_flow.return_value = []
    fake.get_running_by_flow.return_value = []
    fake.get_by_result_pr_url.return_value = None
    monkeypatch.setattr(mod, "crud_flow_execution", fake)
    monkeypatch.setattr("preloop.services.flow_pr_binding.crud_flow_execution", fake)
    return fake


def github_check_run(conclusion="failure", status="completed"):
    return {
        "payload": {
            "action": "completed",
            "repository": {
                "html_url": "https://github.com/preloop/preloop",
                "full_name": "preloop/preloop",
            },
            "check_run": {
                "name": "backend-tests",
                "status": status,
                "conclusion": conclusion,
                "head_sha": "abc123def456",
                "html_url": "https://github.com/preloop/preloop/runs/9",
                "check_suite": {"head_branch": BRANCH},
                "pull_requests": [{"html_url": PR_URL, "number": 353}],
            },
        }
    }


def github_check_run_without_pr(conclusion="failure"):
    event = github_check_run(conclusion)
    event["payload"]["check_run"].pop("pull_requests", None)
    return event


def github_workflow_run(conclusion="failure"):
    return {
        "payload": {
            "workflow_run": {
                "name": "CI",
                "status": "completed",
                "conclusion": conclusion,
                "head_branch": BRANCH,
                "head_sha": "abc123def456",
                "html_url": "https://github.com/preloop/preloop/actions/runs/7",
            }
        }
    }


def github_check_suite(conclusion="failure"):
    return {
        "payload": {
            "repository": {"html_url": "https://github.com/preloop/preloop"},
            "check_suite": {
                "status": "completed",
                "conclusion": conclusion,
                "head_branch": BRANCH,
                "head_sha": "abc123def456",
                "app": {"name": "GitHub Actions"},
            },
        }
    }


def gitlab_pipeline(status="failed", with_mr=True):
    payload = {
        "object_kind": "pipeline",
        "project": {"web_url": "https://gitlab.com/acme/backend"},
        "object_attributes": {
            "id": 4242,
            "status": status,
            "ref": BRANCH,
            "sha": "abc123def456",
        },
    }
    if with_mr:
        payload["merge_request"] = {"iid": 10, "url": MR_URL}
    return {"payload": payload}


def gitlab_job(status="failed"):
    return {
        "payload": {
            "object_kind": "build",
            "project": {"web_url": "https://gitlab.com/acme/backend"},
            "build_id": 77,
            "build_name": "rspec",
            "build_status": status,
            "ref": BRANCH,
            "sha": "abc123def456",
        }
    }


class TestExtractCiFailure:
    def test_github_check_run_failure(self):
        failure = extract_ci_failure("check_run", github_check_run())
        assert failure["provider"] == "github"
        assert failure["name"] == "backend-tests"
        assert failure["url"] == "https://github.com/preloop/preloop/runs/9"
        assert failure["conclusion"] == "failure"
        assert failure["head_sha"] == "abc123def456"
        assert failure["branch"] == BRANCH
        assert failure["pr_url"] == PR_URL
        assert failure["repo"] == "preloop/preloop"

    def test_github_check_run_success_ignored(self):
        assert extract_ci_failure("check_run", github_check_run("success")) is None

    def test_github_check_run_in_progress_ignored(self):
        event = github_check_run(conclusion="failure", status="in_progress")
        assert extract_ci_failure("check_run", event) is None

    def test_github_cancelled_ignored(self):
        assert extract_ci_failure("check_run", github_check_run("cancelled")) is None

    def test_github_workflow_run_failure(self):
        failure = extract_ci_failure("workflow_run", github_workflow_run())
        assert failure["name"] == "CI"
        assert failure["url"].endswith("/actions/runs/7")

    def test_github_check_suite_failure_derives_url(self):
        failure = extract_ci_failure("check_suite", github_check_suite())
        assert failure["name"] == "GitHub Actions"
        assert failure["url"] == (
            "https://github.com/preloop/preloop/commit/abc123def456/checks"
        )

    def test_gitlab_pipeline_failure(self):
        failure = extract_ci_failure("pipeline", gitlab_pipeline())
        assert failure["provider"] == "gitlab"
        assert failure["url"] == "https://gitlab.com/acme/backend/-/pipelines/4242"
        assert failure["pr_url"] == MR_URL
        assert failure["conclusion"] == "failed"

    def test_gitlab_pipeline_success_ignored(self):
        assert extract_ci_failure("pipeline", gitlab_pipeline("success")) is None

    def test_gitlab_job_failure(self):
        failure = extract_ci_failure("job", gitlab_job())
        assert failure["name"] == "rspec"
        assert failure["url"] == "https://gitlab.com/acme/backend/-/jobs/77"
        assert failure["branch"] == BRANCH

    def test_gitlab_job_allowed_failure_ignored(self):
        event = gitlab_job()
        event["payload"]["build_allow_failure"] = True
        assert extract_ci_failure("job", event) is None

    def test_unknown_event_type(self):
        assert (
            extract_ci_failure("push", {"payload": {"ref": "refs/heads/main"}}) is None
        )


class TestFlowRequiresCiFailureResume:
    def test_implementation_flow(self):
        assert flow_requires_ci_failure_resume(FakeFlow()) is True

    def test_plain_pipeline_flow_unchanged(self):
        flow = FakeFlow(trigger_event_types=["pipeline"])
        assert flow_requires_ci_failure_resume(flow) is False

    def test_gitlab_issue_and_pipeline_does_not_require_resume(self):
        flow = FakeFlow(trigger_event_types=["issue_labeled", "pipeline"])
        assert flow_requires_ci_failure_resume(flow) is False

    def test_flow_without_ci_types(self):
        flow = FakeFlow(trigger_event_types=["issue_labeled", "comment_created"])
        assert flow_requires_ci_failure_resume(flow) is False

    def test_mock_types_are_ignored(self):
        assert flow_requires_ci_failure_resume(MagicMock()) is False


class TestCiFailureCap:
    def test_default(self):
        assert ci_failure_cap(FakeFlow()) == DEFAULT_CI_FAILURE_CAP

    def test_speculative_flow_attrs_are_ignored(self):
        assert (
            ci_failure_cap(FakeFlow(ci_failure_resume_limit=2))
            == DEFAULT_CI_FAILURE_CAP
        )


class TestBindCiFailureResumeOrSkip:
    def test_github_check_run_maps_to_resume(self, crud):
        execution = bound_execution()
        crud.get_by_flow.return_value = [execution]
        flow = FakeFlow()
        event = github_check_run()

        resume = bind_ci_failure_resume_or_skip(MagicMock(), flow, "check_run", event)

        assert resume["execution_id"] == str(execution.id)
        assert resume["pr_url"] == PR_URL
        assert resume["source_branch"] == BRANCH
        assert event["_resume"] == resume
        assert event[CI_FAILURE_KEY] == {
            "provider": "github",
            "name": "backend-tests",
            "url": "https://github.com/preloop/preloop/runs/9",
            "conclusion": "failure",
            "head_sha": "abc123def456",
            "pr_url": PR_URL,
        }

    def test_gitlab_pipeline_maps_to_resume_via_mr_url(self, crud, monkeypatch):
        execution = bound_execution(pr_url=MR_URL)
        monkeypatch.setattr(
            mod, "find_bound_execution", MagicMock(return_value=execution)
        )
        flow = FakeFlow(trigger_event_types=["issue_labeled", "pipeline"])
        event = gitlab_pipeline()

        resume = bind_ci_failure_resume_or_skip(MagicMock(), flow, "pipeline", event)

        assert resume["pr_url"] == MR_URL
        assert event[CI_FAILURE_KEY]["provider"] == "gitlab"
        assert event[CI_FAILURE_KEY]["url"].endswith("/-/pipelines/4242")

    def test_gitlab_job_maps_to_resume_via_branch(self, crud):
        execution = bound_execution(pr_url=MR_URL)
        crud.get_by_flow.return_value = [execution]
        flow = FakeFlow(trigger_event_types=["issue_labeled", "job"])
        event = gitlab_job()

        resume = bind_ci_failure_resume_or_skip(MagicMock(), flow, "job", event)

        assert resume["execution_id"] == str(execution.id)
        assert event[CI_FAILURE_KEY]["name"] == "rspec"

    def test_unbound_pr_is_ignored(self, crud):
        crud.get_by_flow.return_value = [
            FakeExecution(
                result={
                    "pr_url": "https://github.com/preloop/preloop/pull/999",
                    "pr_source_branch": BRANCH,
                }
            )
        ]
        event = github_check_run()

        assert (
            bind_ci_failure_resume_or_skip(MagicMock(), FakeFlow(), "check_run", event)
            is None
        )
        assert "_resume" not in event
        assert CI_FAILURE_KEY not in event

    def test_execution_without_pr_url_is_ignored(self, crud):
        crud.get_by_flow.return_value = [
            FakeExecution(result={"pr_source_branch": BRANCH})
        ]
        assert (
            bind_ci_failure_resume_or_skip(
                MagicMock(), FakeFlow(), "check_run", github_check_run()
            )
            is None
        )

    def test_passing_run_does_not_resume(self, crud):
        crud.get_by_flow.return_value = [bound_execution()]
        assert (
            bind_ci_failure_resume_or_skip(
                MagicMock(), FakeFlow(), "check_run", github_check_run("success")
            )
            is None
        )

    def test_running_execution_for_same_pr_skips(self, crud):
        execution = bound_execution()
        crud.get_by_flow.return_value = [execution]
        crud.get_running_by_flow.return_value = [execution]
        assert (
            bind_ci_failure_resume_or_skip(
                MagicMock(), FakeFlow(), "check_run", github_check_run()
            )
            is None
        )

    def test_running_execution_for_other_pr_does_not_skip(self, crud):
        execution = bound_execution()
        crud.get_by_flow.return_value = [execution]
        crud.get_running_by_flow.return_value = [
            FakeExecution(
                result={"pr_url": "https://github.com/preloop/preloop/pull/9"}
            )
        ]
        assert (
            bind_ci_failure_resume_or_skip(
                MagicMock(), FakeFlow(), "check_run", github_check_run()
            )
            is not None
        )

    def test_per_pr_cap_stops_further_resumes(self, crud):
        execution = bound_execution()
        prior = [
            FakeExecution(
                trigger_event_details={CI_FAILURE_KEY: {"pr_url": PR_URL}},
            )
            for _ in range(DEFAULT_CI_FAILURE_CAP)
        ]
        crud.get_by_flow.return_value = [execution, *prior]
        assert (
            bind_ci_failure_resume_or_skip(
                MagicMock(), FakeFlow(), "check_run", github_check_run()
            )
            is None
        )

    def test_under_cap_still_resumes(self, crud):
        execution = bound_execution()
        prior = [
            FakeExecution(trigger_event_details={CI_FAILURE_KEY: {"pr_url": PR_URL}})
            for _ in range(DEFAULT_CI_FAILURE_CAP - 1)
        ]
        crud.get_by_flow.return_value = [execution, *prior]
        assert (
            bind_ci_failure_resume_or_skip(
                MagicMock(), FakeFlow(), "check_run", github_check_run()
            )
            is not None
        )

    def test_branch_fallback_scopes_to_the_same_repo(self, crud):
        crud.get_by_flow.return_value = [
            bound_execution(pr_url="https://github.com/other/other/pull/1")
        ]
        assert (
            bind_ci_failure_resume_or_skip(
                MagicMock(), FakeFlow(), "check_run", github_check_run_without_pr()
            )
            is None
        )

    def test_branch_fallback_matches_same_repo(self, crud):
        execution = bound_execution()
        crud.get_by_flow.return_value = [execution]
        resume = bind_ci_failure_resume_or_skip(
            MagicMock(), FakeFlow(), "check_run", github_check_run_without_pr()
        )
        assert resume["execution_id"] == str(execution.id)

    def test_cap_counts_only_the_same_pr(self, crud):
        execution = bound_execution()
        prior = [
            FakeExecution(
                trigger_event_details={
                    CI_FAILURE_KEY: {
                        "pr_url": "https://github.com/preloop/preloop/pull/1"
                    }
                }
            )
            for _ in range(DEFAULT_CI_FAILURE_CAP)
        ]
        crud.get_by_flow.return_value = [execution, *prior]
        assert (
            bind_ci_failure_resume_or_skip(
                MagicMock(), FakeFlow(), "check_run", github_check_run()
            )
            is not None
        )
