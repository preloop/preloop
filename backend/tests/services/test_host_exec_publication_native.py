"""Host publication on every native profile, GitHub and GitLab (#1069)."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from preloop.services.host_exec import (
    HOST_EXEC_HARNESSES,
    ISOLATED_PUBLICATION_UNAVAILABLE,
    PULL_REQUEST_UNAVAILABLE,
    host_exec_unavailable_reason,
)
from preloop.services.host_exec_delivery import build_host_exec_publication
from preloop.services.host_exec_publication import (
    HOST_PUBLICATION_AGENT_TYPES,
    HOST_PUBLICATION_LEASE_KEY,
    HOST_PUBLICATION_MAX_TEMPLATE_BYTES,
    PUBLICATION_READ_ONLY,
    apply_host_publication_completion,
    open_and_bind_host_pull_request,
    trusted_host_publication_receipt,
)

from .test_host_exec_publication import (  # noqa: F401 - fixture import
    HEAD,
    ONE_REPO,
    FakeBitbucket,
    _jira_context,
    _orchestrator,
    _reject,
    _runner,
    bound,
)

NATIVE_AGENT_TYPES = [
    "codex",
    "claude_code",
    "cursor",
    "devin",
    "opencode",
    "copilot",
    "deepseek",
]
LEGACY = {**ONE_REPO, "publication_mode": "legacy"}
BRANCH = "preloop/issue-PROJ-7-1a2b3c4d"


class FakeGitHub(FakeBitbucket):
    """GitHub-shaped tracker: ``create_pull_request``."""

    tracker_type = "github"


class FakeGitLab:
    """GitLab-shaped tracker: ``create_merge_request`` only."""

    tracker_type = "gitlab"

    def __init__(self):
        self.mrs = []

    async def list_open_pull_requests_by_source_branch(self, branch):
        return {"items": [mr for mr in self.mrs if mr["source_branch"] == branch]}

    async def create_merge_request(
        self, *, title, source_branch, target_branch, description
    ):
        mr = {
            "url": f"https://gitlab.example.com/acme/app/-/merge_requests/{len(self.mrs) + 1}",
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
        }
        self.mrs.append(mr)
        return mr


# --- Validation ------------------------------------------------------------


def test_every_native_agent_type_may_publish():
    assert set(NATIVE_AGENT_TYPES) == set(HOST_PUBLICATION_AGENT_TYPES)


@pytest.mark.parametrize("agent_type", NATIVE_AGENT_TYPES)
def test_legacy_publication_is_admitted_for_each_harness(agent_type):
    for clone in (ONE_REPO, LEGACY):
        assert (
            host_exec_unavailable_reason(git_clone_config=clone, agent_type=agent_type)
            is None
        )


@pytest.mark.parametrize("agent_type", NATIVE_AGENT_TYPES)
def test_isolated_publication_stays_rejected_for_each_harness(agent_type):
    isolated = {**ONE_REPO, "publication_mode": "isolated"}
    reason = host_exec_unavailable_reason(
        git_clone_config=isolated, agent_type=agent_type
    )
    assert reason == ISOLATED_PUBLICATION_UNAVAILABLE
    assert "publication_mode legacy" in reason


@pytest.mark.parametrize("agent_type", NATIVE_AGENT_TYPES)
def test_read_only_flow_cannot_publish(agent_type):
    read_only = {**ONE_REPO, "publication_mode": "read_only"}
    assert (
        host_exec_unavailable_reason(git_clone_config=read_only, agent_type=agent_type)
        == PUBLICATION_READ_ONLY
    )


def test_read_only_review_without_publication_is_unchanged():
    review = {**ONE_REPO, "create_pull_request": False, "publication_mode": "read_only"}
    assert (
        host_exec_unavailable_reason(git_clone_config=review, agent_type="codex")
        is None
    )


@pytest.mark.parametrize("agent_type", NATIVE_AGENT_TYPES)
def test_save_needs_a_publishing_runner_for_each_harness(monkeypatch, agent_type):
    harness = HOST_EXEC_HARNESSES[agent_type]
    _reject(monkeypatch, [_runner(harness, "host_publication")], agent_type=agent_type)
    with pytest.raises(HTTPException) as caught:
        _reject(monkeypatch, [_runner(harness)], agent_type=agent_type)
    assert caught.value.detail == PULL_REQUEST_UNAVAILABLE
    assert "allow_publish" in PULL_REQUEST_UNAVAILABLE


# --- Plan and receipt --------------------------------------------------------


def test_plan_carries_configured_template():
    checkout = {"repositories": [{"path": "app", "branch": "main", "token": "t"}]}
    plan = build_host_exec_publication(
        checkout,
        target_branch=BRANCH,
        flow_name="Implementer",
        execution_id=str(uuid4()),
        pull_request_template=".github/PULL_REQUEST_TEMPLATE/feature.md",
    )
    assert plan["pull_request_template"] == ".github/PULL_REQUEST_TEMPLATE/feature.md"
    assert plan["pull_request_provider"] == "github"
    gitlab = build_host_exec_publication(
        checkout,
        target_branch=BRANCH,
        flow_name="f",
        execution_id=str(uuid4()),
        template_provider="gitlab",
    )
    assert gitlab["pull_request_provider"] == "gitlab"
    plain = build_host_exec_publication(
        checkout, target_branch=BRANCH, flow_name="f", execution_id=str(uuid4())
    )
    assert "pull_request_template" not in plain


def test_receipt_keeps_bounded_template_on_push_only():
    pushed = {"status": "pushed", "branch": BRANCH, "head_sha": HEAD}
    template = "## Summary\n\n## Testing\n"
    got = trusted_host_publication_receipt(
        {"host_publication": {**pushed, "pr_template": template}}
    )
    assert got["pr_template"] == template
    huge = "x" * (HOST_PUBLICATION_MAX_TEMPLATE_BYTES + 1)
    got = trusted_host_publication_receipt(
        {"host_publication": {**pushed, "pr_template": huge}}
    )
    assert "pr_template" not in got
    failed = {"status": "failed", "branch": BRANCH, "pr_template": template}
    assert "pr_template" not in trusted_host_publication_receipt(
        {"host_publication": failed}
    )


# --- Opening on GitHub and GitLab --------------------------------------------


@pytest.mark.asyncio
async def test_github_pull_request_is_opened_and_bound(bound):  # noqa: F811
    forge, execution_id = FakeGitHub(), uuid4()
    found = await open_and_bind_host_pull_request(
        object(),
        execution_id=execution_id,
        client=forge,
        branch=BRANCH,
        base_branch="main",
        title="t",
        description="d",
    )
    assert found["provider"] == "github" and found["source"] == "control_plane_create"
    assert bound[0][1] == forge.prs[0]["url"]


@pytest.mark.asyncio
async def test_gitlab_merge_request_is_opened_and_bound(bound):  # noqa: F811
    forge, execution_id = FakeGitLab(), uuid4()
    found = await open_and_bind_host_pull_request(
        object(),
        execution_id=execution_id,
        client=forge,
        branch=BRANCH,
        base_branch="main",
        title="t",
        description="d",
    )
    assert found["provider"] == "gitlab"
    assert forge.mrs[0]["target_branch"] == "main"
    assert bound[0][1] == forge.mrs[0]["url"]


# --- End to end: completion receipt to pull request ---------------------------


def _github_context(execution_id, **clone):
    context = _jira_context(
        execution_id,
        repositories=[
            {"repository_url": "https://github.com/acme/app.git", "tracker_id": "gh"}
        ],
    )
    context["git_clone_config"].update(clone)
    context["trigger_tracker_id"] = "gh"
    context["git_credentials_map"] = {
        "gh": {"token": "ghs_installation_token", "tracker_type": "github"}
    }
    return context


def _complete(branch, template=None):
    """Run the runner completion through the trusted receipt binding."""
    receipt = {"status": "pushed", "branch": branch, "head_sha": HEAD}
    if template:
        receipt["pr_template"] = template
    status, error, result = apply_host_publication_completion(
        "SUCCEEDED",
        None,
        # An agent-authored receipt is always dropped.
        {"status": "success", "host_publication": {"status": "pushed"}},
        {"host_publication": receipt},
        {HOST_PUBLICATION_LEASE_KEY: {"mode": "legacy"}},
    )
    assert status == "SUCCEEDED" and error is None
    assert result["host_publication"]["head_sha"] == HEAD
    return result


def _orchestrate(monkeypatch, execution_id, context, forge, result):
    orch = _orchestrator(monkeypatch, None, clients=[forge], context=context)
    orch.execution_log = SimpleNamespace(id=execution_id, result=result)
    orch.flow = SimpleNamespace(
        name="Implementer",
        account_id=context["account_id"],
        agent_config={"host_exec_profile": "copilot-seat"},
        git_clone_config=context["git_clone_config"],
    )
    return orch


@pytest.mark.asyncio
async def test_completion_to_github_pull_request_end_to_end(
    monkeypatch,
    bound,  # noqa: F811
):
    execution_id = uuid4()
    context = _github_context(execution_id)
    branch = f"preloop/issue-PROJ-7-{str(execution_id)[:8]}"
    template = "## Summary\n\n## Checklist\n- [ ] tests\n"
    forge = FakeGitHub()
    orch = _orchestrate(
        monkeypatch, execution_id, context, forge, _complete(branch, template)
    )
    assert await orch._open_host_published_pr("SUCCEEDED") is None
    assert len(forge.prs) == 1
    pr = forge.prs[0]
    assert pr["source_branch"] == branch and pr["target_branch"] == "main"
    assert orch._opened_pr == {"url": pr["url"], "branch": branch}
    assert bound[0][1] == pr["url"]


@pytest.mark.asyncio
async def test_configured_title_and_template_fill_the_pull_request(
    monkeypatch,
    bound,  # noqa: F811
):
    execution_id = uuid4()
    context = _github_context(execution_id, pull_request_title="Fix {issue_key}")
    branch = f"preloop/issue-PROJ-7-{str(execution_id)[:8]}"
    forge = FakeGitHub()
    captured = {}
    original = forge.create_pull_request

    async def create(**kwargs):
        captured.update(kwargs)
        return await original(**kwargs)

    forge.create_pull_request = create
    orch = _orchestrate(
        monkeypatch, execution_id, context, forge, _complete(branch, "## Checklist")
    )
    assert await orch._open_host_published_pr("SUCCEEDED") is None
    assert captured["description"] == "## Checklist"
    assert captured["title"]


@pytest.mark.asyncio
async def test_publication_approval_blocks_the_pull_request(
    monkeypatch,
    bound,  # noqa: F811
):
    execution_id = uuid4()
    context = _github_context(execution_id, publication_approval="required")
    branch = f"preloop/issue-PROJ-7-{str(execution_id)[:8]}"
    forge = FakeGitHub()
    orch = _orchestrate(monkeypatch, execution_id, context, forge, _complete(branch))
    error = await orch._open_host_published_pr("SUCCEEDED")
    assert error.startswith("publication_failed")
    assert "publication_approval" in error and HEAD in error
    assert forge.creates == 0 and bound == []


@pytest.mark.asyncio
async def test_publication_approval_binds_the_exact_pushed_head(
    monkeypatch,
    bound,  # noqa: F811
):
    import preloop.services.product_provenance as provenance

    seen = []
    monkeypatch.setattr(
        provenance,
        "enforce_saved_publication_approval",
        lambda db, **kwargs: seen.append(kwargs),
    )
    execution_id = uuid4()
    context = _github_context(execution_id, publication_approval=True)
    branch = f"preloop/issue-PROJ-7-{str(execution_id)[:8]}"
    forge = FakeGitHub()
    orch = _orchestrate(monkeypatch, execution_id, context, forge, _complete(branch))
    assert await orch._open_host_published_pr("SUCCEEDED") is None
    assert seen[0]["execution_id"] == str(execution_id)
    assert seen[0]["candidates"] == [
        {
            "repository_url": "https://github.com/acme/app.git",
            "branch": branch,
            "base": "main",
            "head_sha": HEAD,
        }
    ]
    assert len(forge.prs) == 1


# --- Continuation base -------------------------------------------------------


@pytest.mark.asyncio
async def test_continuation_binds_approval_to_the_pull_request_base(
    monkeypatch,
    bound,  # noqa: F811
):
    """A continuation clones the PR head; the base is the open PR's target."""
    import preloop.services.product_provenance as provenance

    seen = []
    monkeypatch.setattr(
        provenance,
        "enforce_saved_publication_approval",
        lambda db, **kwargs: seen.append(kwargs),
    )
    execution_id = uuid4()
    branch = f"preloop/issue-PROJ-7-{str(execution_id)[:8]}"
    context = _github_context(execution_id, publication_approval="required")
    context["git_clone_config"]["repositories"][0]["branch"] = branch
    context["trigger_event_data"]["_resume"] = {"source_branch": branch}
    forge = FakeGitHub()
    forge.prs.append(
        {
            "url": "https://github.com/acme/app/pull/9",
            "source_branch": branch,
            "target_branch": "release",
        }
    )
    orch = _orchestrate(monkeypatch, execution_id, context, forge, _complete(branch))
    orch.trigger_event_data = context["trigger_event_data"]

    async def resume_context():
        return context

    monkeypatch.setattr(orch, "prepare_host_exec_checkout_context", resume_context)
    monkeypatch.setattr(
        "preloop.services.host_exec_delivery.build_host_exec_checkout",
        lambda ctx: {
            "repositories": [
                {"url": "https://github.com/acme/app.git", "branch": branch}
            ]
        },
    )
    assert await orch._open_host_published_pr("SUCCEEDED") is None
    assert seen[0]["candidates"][0]["base"] == "release"
    assert forge.creates == 0
    assert orch._opened_pr["url"] == "https://github.com/acme/app/pull/9"


@pytest.mark.asyncio
async def test_base_falls_back_to_default_when_checkout_is_the_pushed_branch():
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    base = await FlowExecutionOrchestrator._host_publication_base(
        FakeGitHub(),
        BRANCH,
        branch=BRANCH,
        configured={"repositories": [{"default_branch": "develop"}]},
        is_continuation=False,
    )
    assert base == "develop"
    first = await FlowExecutionOrchestrator._host_publication_base(
        FakeGitHub(), "main", branch=BRANCH, configured={}, is_continuation=False
    )
    assert first == "main"
