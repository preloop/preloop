"""Execute the hosted clone plan against a local repository with two heads."""

import logging
import os
from pathlib import Path
import shlex
import subprocess

import pytest

from preloop.agents.container import ContainerAgentExecutor


@pytest.mark.parametrize("event", ["opened", "note", "nullable_last_commit"])
def test_gitlab_review_checks_out_mr_head_despite_configured_default(
    tmp_path: Path, event: str
) -> None:
    """A default clone branch must not replace the MR's advertised commit."""
    env = {
        **os.environ,
        "PRELOOP_DISABLE_TELEMETRY": "true",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    source = tmp_path / "source"
    source.mkdir()

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(source), *args], env=env, text=True
        ).strip()

    git("init", "-b", "master")
    git("config", "user.name", "Jane Doe")
    git("config", "user.email", "jane@example.com")
    (source / "example.txt").write_text("base\n")
    git("add", ".")
    git("commit", "-m", "Create base")
    base = git("rev-parse", "HEAD")
    git("checkout", "-b", "feature/example")
    (source / "example.txt").write_text("review this change\n")
    git("commit", "-am", "Update example")
    head = git("rev-parse", "HEAD")
    git("checkout", "master")

    mr = {
        "iid": 7,
        "source_branch": "feature/example",
        "target_branch": "master",
        "last_commit": {"id": head},
    }
    payload = {"object_kind": "merge_request", "object_attributes": mr}
    if event == "note":
        payload = {
            "object_kind": "note",
            "object_attributes": {"noteable_type": "MergeRequest"},
            "merge_request": mr,
        }
    elif event == "nullable_last_commit":
        mr.update(last_commit=None, sha=head)
    context = {
        "trigger_event_data": {"payload": payload},
        "execution_id": "test-execution",
        "flow_name": "Pull Request Reviewer",
    }
    executor = ContainerAgentExecutor.__new__(ContainerAgentExecutor)
    executor.logger = logging.getLogger(__name__)
    branch, target, commit, _, _ = executor._resolve_git_branch_plan(context, {})
    assert branch == "feature/example"
    # Exercise the actual per-repository branch override as well as the shell.
    clone = tmp_path / "checkout"
    blocks = executor._build_repository_clone_command_block(
        repo_config={
            "repository_url": str(source),
            "clone_path": str(clone),
            "branch": "master",
        },
        repo_index=0,
        execution_context=context,
        source_branch=branch,
        target_branch=target,
        commit_sha=commit,
        trigger_data=context["trigger_event_data"],
    )
    assert blocks
    script = "\n".join(blocks).replace(
        "cd /workspace", f"cd {shlex.quote(str(tmp_path))}"
    )
    result = subprocess.run(
        ["bash", "-e", "-c", script], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    actual = subprocess.check_output(
        ["git", "-C", str(clone), "rev-parse", "HEAD"], env=env, text=True
    ).strip()
    assert actual == head
    assert actual != base
    assert commit == head
