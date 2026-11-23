"""A clone with no configured branch follows the repository's default branch.

Each case runs the real clone script against a local repository whose only
branch is ``master``, so a literal ``main`` fallback fails the run.
"""

import os
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

from preloop.agents.codex import CodexAgent
from preloop.agents.container import ContainerAgentExecutor
from preloop.agents.gemini import GeminiAgent
from preloop.agents.opencode import OpenCodeAgent

HARNESSES = [ContainerAgentExecutor, CodexAgent, OpenCodeAgent, GeminiAgent]


def _executor(executor_class: type[ContainerAgentExecutor]) -> ContainerAgentExecutor:
    if executor_class is ContainerAgentExecutor:
        return executor_class(
            agent_type="codex", config={}, image="test-image", use_kubernetes=False
        )
    return executor_class(config={})


@pytest.fixture
def git_env(tmp_path: Path) -> dict[str, str]:
    """Git configuration and identity confined to the test directory."""
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Jane Doe",
        "GIT_AUTHOR_EMAIL": "jane@example.com",
        "GIT_COMMITTER_NAME": "Jane Doe",
        "GIT_COMMITTER_EMAIL": "jane@example.com",
        "PRELOOP_DISABLE_TELEMETRY": "true",
        "PRELOOP_GIT_CREDENTIALS": "",
    }


@pytest.fixture
def git(git_env: dict[str, str]) -> Callable[..., str]:
    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], env=git_env, check=True, capture_output=True, text=True
        ).stdout.strip()

    return run


@pytest.fixture
def master_repo(tmp_path: Path, git: Callable[..., str]) -> Path:
    """A bare repository whose only branch, and default branch, is ``master``."""
    source = tmp_path / "source"
    git("init", "-b", "master", str(source))
    (source / "README.md").write_text("example\n")
    git("-C", str(source), "add", "README.md")
    git("-C", str(source), "commit", "-m", "Initial example")
    git("-C", str(source), "branch", "release")
    bare = tmp_path / "example.git"
    git("clone", "--bare", str(source), str(bare))
    return bare


def _run_clone(
    executor: ContainerAgentExecutor,
    context: dict[str, Any],
    tmp_path: Path,
    git_env: dict[str, str],
) -> tuple[subprocess.CompletedProcess[str], str]:
    command = executor._prepare_git_clone_command(context)
    workspace = tmp_path / "workspace"
    script = command.replace("/workspace", str(workspace)).replace(
        "/tmp/preloop-checkpoint-client.py", str(tmp_path / "no-checkpoint-client.py")
    )
    result = subprocess.run(
        ["bash", "-c", script],
        env=git_env,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=tmp_path,
    )
    return result, command


def _context(
    repository: dict[str, Any], *, trigger: dict[str, Any] | None = None, **config
) -> dict[str, Any]:
    return {
        "flow_id": "example-flow",
        "execution_id": "example-execution",
        "flow_name": "reviewer",
        "trigger_event_data": trigger or {},
        "git_clone_config": {
            "enabled": True,
            "repositories": [{"clone_path": "/workspace", **repository}],
            **config,
        },
    }


@pytest.mark.parametrize("executor_class", HARNESSES)
def test_unconfigured_branch_follows_the_remote_head(
    executor_class: type[ContainerAgentExecutor],
    master_repo: Path,
    tmp_path: Path,
    git_env: dict[str, str],
    git: Callable[..., str],
) -> None:
    """Nothing names a branch: the clone takes the remote HEAD, never ``main``."""
    executor = _executor(executor_class)
    context = _context({"repository_url": master_repo.as_uri()})
    result, command = _run_clone(executor, context, tmp_path, git_env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert " -b main " not in command
    checkout = tmp_path / "workspace"
    assert git("-C", str(checkout), "branch", "--show-current").startswith(
        "preloop/reviewer-"
    )
    assert git("-C", str(checkout), "rev-parse", "master") == git(
        "-C", str(master_repo), "rev-parse", "master"
    )
    assert "Following the repository default branch master" in result.stdout


@pytest.mark.parametrize("executor_class", HARNESSES)
def test_synced_project_default_branch_is_the_source(
    executor_class: type[ContainerAgentExecutor],
    master_repo: Path,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    """The orchestrator's ``default_branch`` reaches the clone and publication."""
    executor = _executor(executor_class)
    context = _context(
        {"repository_url": master_repo.as_uri(), "default_branch": "master"}
    )
    result, command = _run_clone(executor, context, tmp_path, git_env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "git clone -b master " in command
    assert context["_git_source_branch"] == "master"


def test_gitlab_payload_default_branch_is_the_source(
    master_repo: Path, tmp_path: Path, git_env: dict[str, str]
) -> None:
    """A GitLab event that names no branch still carries the project's default."""
    executor = _executor(ContainerAgentExecutor)
    trigger = {
        "source": "gitlab",
        "payload": {
            "object_kind": "note",
            "project": {
                "path_with_namespace": "group/example",
                "default_branch": "master",
            },
        },
    }
    context = _context({"repository_url": master_repo.as_uri()}, trigger=trigger)
    result, command = _run_clone(executor, context, tmp_path, git_env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "git clone -b master " in command
    assert context["_git_source_branch"] == "master"


def test_github_payload_default_branch_is_the_source(
    master_repo: Path, tmp_path: Path, git_env: dict[str, str]
) -> None:
    """GitHub events carry ``repository.default_branch``; it is used the same way."""
    executor = _executor(ContainerAgentExecutor)
    trigger = {
        "source": "github",
        "payload": {"repository": {"full_name": "o/r", "default_branch": "master"}},
    }
    context = _context({"repository_url": master_repo.as_uri()}, trigger=trigger)
    result, command = _run_clone(executor, context, tmp_path, git_env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "git clone -b master " in command


@pytest.mark.parametrize("executor_class", HARNESSES)
def test_explicit_branch_still_wins_over_the_default(
    executor_class: type[ContainerAgentExecutor],
    master_repo: Path,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    executor = _executor(executor_class)
    context = _context(
        {"repository_url": master_repo.as_uri(), "default_branch": "master"},
        source_branch="release",
    )
    result, command = _run_clone(executor, context, tmp_path, git_env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "git clone -b release " in command
    assert context["_git_source_branch"] == "release"


def test_github_pull_request_source_branch_is_unchanged(
    master_repo: Path, tmp_path: Path, git_env: dict[str, str]
) -> None:
    """A PR trigger's head branch is still the source, ahead of the default."""
    executor = _executor(ContainerAgentExecutor)
    trigger = {
        "source": "github",
        "payload": {
            "pull_request": {"number": 7, "head": {"ref": "release"}},
            "repository": {"full_name": "o/r", "default_branch": "master"},
        },
    }
    context = _context({"repository_url": master_repo.as_uri()}, trigger=trigger)
    result, command = _run_clone(executor, context, tmp_path, git_env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "git clone -b release " in command


def test_clone_failure_names_the_branch_and_the_default_branch(
    master_repo: Path, tmp_path: Path, git_env: dict[str, str]
) -> None:
    """A pinned branch the repository lacks: the log says what was tried."""
    executor = _executor(ContainerAgentExecutor)
    context = _context({"repository_url": master_repo.as_uri()}, source_branch="main")
    result, _command = _run_clone(executor, context, tmp_path, git_env)

    assert result.returncode != 0
    assert "Tried branch: main" in result.stdout
    assert "Repository default branch: master" in result.stdout


def test_resume_rebase_base_uses_the_default_branch() -> None:
    executor = _executor(ContainerAgentExecutor)
    assert (
        executor._resolve_resume_base_branch({}, {"default_branch": "master"})
        == "master"
    )
    assert (
        executor._resolve_resume_base_branch(
            {"source_branch": "develop"}, {"default_branch": "master"}
        )
        == "develop"
    )


def test_disabled_config_with_listed_repositories_says_why_it_clones(
    master_repo: Path,
) -> None:
    """Kept for existing API flows; the run log now states it."""
    executor = _executor(ContainerAgentExecutor)
    context = _context({"repository_url": master_repo.as_uri()})
    context["git_clone_config"]["enabled"] = False
    command = executor._prepare_init_commands(context)

    assert "git clone" in command
    assert "Git workspace cloning is off, but this flow lists" in command
