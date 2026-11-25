"""Conversation branch planning exercised through the real checkpoint guard."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from preloop.agents.checkpoint_client import capture, restore
from preloop.agents.container import ContainerAgentExecutor


def git(repo: Path, *args: str) -> str:
    """Run Git against an isolated local repository."""
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE
    ).strip()


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, Path]:
    """Create a real bare upstream and a committed source checkout."""
    upstream = tmp_path / "upstream.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(upstream))
    source = tmp_path / "source"
    git(tmp_path, "clone", str(upstream), str(source))
    git(source, "config", "user.name", "Jane Doe")
    git(source, "config", "user.email", "jane@example.com")
    (source / "answer.txt").write_text("amber\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "Initial fixture")
    git(source, "push", "origin", "main")
    return upstream, source


def context(upstream: Path, execution_id: str) -> dict[str, Any]:
    """Build the same single-repository plan as a hosted conversation."""
    return {
        "execution_id": execution_id,
        "flow_name": "Repository questions",
        "git_clone_config": {
            "enabled": True,
            "source_branch": "main",
            "create_pull_request": False,
            "repositories": [
                {"repository_url": str(upstream), "clone_path": "/workspace"}
            ],
        },
        "trigger_event_data": {"_conversation_id": "example-conversation"},
    }


def run_shell(
    script: str, workspace: Path, tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    """Run production shell with only its container paths mapped locally."""
    return subprocess.run(
        ["bash", "-c", "set -e\n" + script.replace("/workspace", str(workspace))],
        env={
            **os.environ,
            "PRELOOP_DISABLE_TELEMETRY": "true",
            "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("remote_moved", [False, True])
@pytest.mark.parametrize("settings_edited", [False, True])
@pytest.mark.parametrize("harness", ["opencode", "deepseek"])
def test_conversation_resume_reuses_checkpoint_branch_through_guard(
    tmp_path: Path,
    repository: tuple[Path, Path],
    remote_moved: bool,
    settings_edited: bool,
    harness: str,
) -> None:
    """Two resumes keep saved commits/files, even after source and settings edits."""
    upstream, source = repository
    executor = ContainerAgentExecutor(harness, {}, image="test:latest")
    original = context(upstream, "11111111-first")
    workspace = tmp_path / "workspace"
    clone = executor._prepare_git_clone_command(original)
    result = run_shell(clone, workspace, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    branch = git(workspace, "branch", "--show-current")
    assert branch == "preloop/repository-questions-11111111"
    head = git(workspace, "rev-parse", "HEAD")
    (workspace / "notes.txt").write_text("uncommitted conversation work")
    if remote_moved:
        (source / "answer.txt").write_text("blue\n")
        git(source, "commit", "-am", "Move source branch")
        git(source, "push", "origin", "main")
    for index in (2, 3):
        archive = capture(workspace, max_bytes=2_000_000)
        restored = tmp_path / f"restored-{index}"
        restore(archive, restored)
        resumed = context(upstream, f"{index}" * 8 + "-resume")
        if settings_edited:
            resumed["flow_name"] = "Edited flow name"
            resumed["git_clone_config"]["target_branch"] = "edited-target"
        resumed["trigger_event_data"]["_resume"] = {
            "execution_id": original["execution_id"]
        }
        resumed["checkpoint_env"] = {"PRELOOP_CHECKPOINT_GET_TOKEN": "fixture"}
        resumed["conversation_workspace_repositories"] = json.loads(
            (restored / ".preloop-checkpoint.json").read_text()
        )["repositories"]
        clone = executor._prepare_git_clone_command(resumed)
        script = executor._wrap_clone_with_workspace_restore(clone, resumed)
        result = run_shell(script, restored, tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert resumed["_git_source_branch"] == resumed["_git_target_branch"] == branch
        assert "skipping git clone" in result.stdout
        assert git(restored, "rev-parse", "HEAD") == head
        assert git(restored, "branch", "--show-current") == branch
        assert (restored / "answer.txt").read_text() == "amber\n"
        assert (restored / "notes.txt").read_text() == "uncommitted conversation work"
        workspace, original = restored, resumed


@pytest.mark.parametrize("records", [[], [{"path": ".", "branch": "-unsafe"}]])
def test_conversation_missing_branch_fails_closed(
    records: list[dict[str, str]],
) -> None:
    """A missing or unsafe recorded branch must never disable the guard."""
    executor = ContainerAgentExecutor("opencode", {}, image="test:latest")
    resumed = context(Path("/example.git"), "22222222-resume")
    resumed["conversation_workspace_repositories"] = records
    with pytest.raises(ValueError, match="branch is missing"):
        executor._prepare_git_clone_command(resumed)


def test_conversation_guard_still_rejects_different_checkout(
    tmp_path: Path, repository: tuple[Path, Path]
) -> None:
    """The checkpoint binding does not excuse a different restored branch."""
    upstream, source = repository
    executor = ContainerAgentExecutor("opencode", {}, image="test:latest")
    resumed = context(upstream, "22222222-resume")
    resumed["conversation_workspace_repositories"] = [
        {"path": ".", "branch": "preloop/recorded-11111111"}
    ]
    resumed["checkpoint_env"] = {"PRELOOP_CHECKPOINT_GET_TOKEN": "fixture"}
    clone = executor._prepare_git_clone_command(resumed)
    result = run_shell(
        executor._wrap_clone_with_workspace_restore(clone, resumed), source, tmp_path
    )
    assert result.returncode != 0
    assert "PRELOOP_CHECKPOINT branch_mismatch" in result.stdout
    assert git(source, "branch", "--show-current") == "main"
