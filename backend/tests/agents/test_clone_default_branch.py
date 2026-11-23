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


@pytest.fixture
def main_repo(tmp_path: Path, git: Callable[..., str]) -> Path:
    """A bare repository whose only branch, and default branch, is ``main``."""
    source = tmp_path / "source-main"
    git("init", "-b", "main", str(source))
    (source / "README.md").write_text("example\n")
    git("-C", str(source), "add", "README.md")
    git("-C", str(source), "commit", "-m", "Initial example")
    bare = tmp_path / "example-main.git"
    git("clone", "--bare", str(source), str(bare))
    return bare


def _two_repo_context(main_repo: Path, master_repo: Path) -> dict[str, Any]:
    return {
        "flow_id": "example-flow",
        "execution_id": "example-execution",
        "flow_name": "reviewer",
        "trigger_event_data": {},
        "git_clone_config": {
            "enabled": True,
            "create_pull_request": True,
            "repositories": [
                {
                    "repository_url": main_repo.as_uri(),
                    "clone_path": "/workspace/first",
                    "default_branch": "main",
                },
                {
                    "repository_url": master_repo.as_uri(),
                    "clone_path": "/workspace/second",
                    "default_branch": "master",
                },
            ],
        },
    }


@pytest.mark.parametrize("executor_class", HARNESSES)
def test_each_repository_clones_its_own_default_branch(
    executor_class: type[ContainerAgentExecutor],
    main_repo: Path,
    master_repo: Path,
    tmp_path: Path,
    git_env: dict[str, str],
    git: Callable[..., str],
) -> None:
    """The first repository's ``main`` never decides the second's ``master``."""
    executor = _executor(executor_class)
    context = _two_repo_context(main_repo, master_repo)
    result, command = _run_clone(executor, context, tmp_path, git_env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"git clone -b main {main_repo.as_uri()}" in command
    assert f"git clone -b master {master_repo.as_uri()}" in command
    second = tmp_path / "workspace" / "second"
    assert git("-C", str(second), "rev-parse", "master") == git(
        "-C", str(master_repo), "rev-parse", "master"
    )


def test_each_repository_opens_its_pr_against_its_own_default(
    main_repo: Path, master_repo: Path
) -> None:
    executor = _executor(ContainerAgentExecutor)
    context = _two_repo_context(main_repo, master_repo)
    executor._prepare_git_clone_command(context)
    commands = executor._prepare_git_post_execution_commands(context)

    first, second = commands.split("/workspace/second", 1)
    assert "origin/main" in first and "origin/master" not in first
    assert "origin/master" in second and "origin/main" not in second


def test_configured_source_branch_still_applies_to_every_repository(
    main_repo: Path, master_repo: Path
) -> None:
    executor = _executor(ContainerAgentExecutor)
    context = _two_repo_context(main_repo, master_repo)
    context["git_clone_config"]["source_branch"] = "release"
    command = executor._prepare_git_clone_command(context)

    assert command.count("git clone -b release ") == 2


def test_host_exec_checkout_uses_each_repository_default(
    main_repo: Path, master_repo: Path
) -> None:
    from preloop.services.host_exec_delivery import build_host_exec_checkout

    checkout = build_host_exec_checkout(_two_repo_context(main_repo, master_repo))

    assert checkout is not None
    assert [repo["branch"] for repo in checkout["repositories"]] == [
        "main",
        "master",
    ]


def test_second_repository_without_a_default_follows_its_remote_head(
    main_repo: Path, master_repo: Path
) -> None:
    """A trigger's default names the first repository, not the others."""
    executor = _executor(ContainerAgentExecutor)
    context = _two_repo_context(main_repo, master_repo)
    for repo in context["git_clone_config"]["repositories"]:
        repo.pop("default_branch")
    context["trigger_event_data"] = {
        "source": "github",
        "payload": {"repository": {"full_name": "o/r", "default_branch": "main"}},
    }
    command = executor._prepare_git_clone_command(context)

    assert f"git clone -b main {main_repo.as_uri()}" in command
    assert f"git clone {master_repo.as_uri()}" in command


def test_resume_base_uses_the_trigger_payload_default_branch() -> None:
    """A raw ``repository_url`` flow: the event is the only branch source."""
    executor = _executor(ContainerAgentExecutor)
    repo = {"repository_url": "https://example.com/o/r.git"}
    gitlab = {"payload": {"project": {"default_branch": "master"}}}
    github = {"payload": {"repository": {"default_branch": "master"}}}
    pull = {
        "payload": {
            "pull_request": {"base": {"ref": "develop"}},
            "repository": {"default_branch": "master"},
        }
    }

    assert executor._resolve_resume_base_branch({}, repo, gitlab) == "master"
    assert executor._resolve_resume_base_branch({}, repo, github) == "master"
    assert executor._resolve_resume_base_branch({}, repo, pull) == "develop"
    assert executor._resolve_resume_base_branch({}, repo, github, 1) == "main"


def test_resume_rebase_lands_on_the_trigger_default_branch() -> None:
    executor = _executor(ContainerAgentExecutor)
    context = _context(
        {"repository_url": "https://example.com/o/r.git"},
        trigger={
            "payload": {"project": {"default_branch": "master"}},
            "_resume": {"execution_id": "prior", "source_branch": "preloop/x-1"},
        },
    )
    command = executor._prepare_git_clone_command(context)

    assert "origin/master" in command
    assert "origin/main" not in command


GATE_PROFILE = {
    "version": "v1",
    "profile_id": "example",
    "description": "Example gate profile",
    "always": [
        {"id": "lint", "command": "echo ok", "reason": "cheap", "scope": "backend"}
    ],
    "rules": [],
}


def test_each_repository_is_verified_against_its_own_base(
    main_repo: Path, master_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whitespace repair and the gate use each repository's base, not the first's."""
    import preloop.agents.container as container_module

    seen: list[tuple[str, str, str]] = []

    def record(kind: str) -> Callable[..., str]:
        def build(**kwargs: Any) -> str:
            seen.append((kind, kwargs["working_dir"], kwargs["base_branch"]))
            return "true\n"

        return build

    monkeypatch.setattr(
        container_module, "build_whitespace_repair_shell", record("repair")
    )
    monkeypatch.setattr(
        container_module, "build_verification_gate_shell", record("gate")
    )
    executor = _executor(ContainerAgentExecutor)
    context = _two_repo_context(main_repo, master_repo)
    config = context["git_clone_config"]
    config["verification"] = {"mode": "gate", "profile": GATE_PROFILE}
    for repo in config["repositories"]:
        repo["tracker_id"] = "tracker-1"
    context["git_credentials_map"] = {
        "tracker-1": {"token": "fake-token", "tracker_type": "github"}
    }
    executor._prepare_git_clone_command(context)
    executor._prepare_git_post_execution_commands(context)

    assert ("repair", "/workspace/first", "main") in seen
    assert ("repair", "/workspace/second", "master") in seen
    assert ("gate", "/workspace/first", "main") in seen
    assert ("gate", "/workspace/second", "master") in seen


def test_resume_rebase_prefers_the_pr_target_over_the_project_default() -> None:
    """Project default ``main``, the MR targets ``develop``: rebase on develop."""
    executor = _executor(ContainerAgentExecutor)
    context = _context(
        {"repository_url": "https://example.com/o/r.git", "default_branch": "main"},
        trigger={
            "payload": {
                "object_kind": "note",
                "object_attributes": {"note": "please fix"},
                "merge_request": {"iid": 3, "target_branch": "develop"},
                "project": {"default_branch": "main"},
            },
            "_resume": {"execution_id": "prior", "source_branch": "preloop/x-1"},
        },
    )
    command = executor._prepare_git_clone_command(context)

    assert "origin/develop" in command
    assert "origin/main" not in command


def test_recovery_artifacts_use_each_repositorys_own_base(
    main_repo: Path, master_repo: Path
) -> None:
    """branch.patch and branch.bundle diff each repository against its own base."""
    executor = _executor(ContainerAgentExecutor)
    context = _two_repo_context(main_repo, master_repo)
    for repo in context["git_clone_config"]["repositories"]:
        repo["tracker_id"] = "tracker-1"
    context["git_credentials_map"] = {
        "tracker-1": {"token": "fake-token", "tracker_type": "github"}
    }
    executor._prepare_git_clone_command(context)
    command = executor._prepare_git_post_execution_commands(context)

    second = command[command.index("cd /workspace/second") :]
    first = command[: command.index("cd /workspace/second")]
    assert "git format-patch --stdout main..HEAD" in first
    assert "git bundle create /workspace/evidence/branch.bundle main..HEAD" in first
    assert "git format-patch --stdout master..HEAD" in second
    assert "git bundle create /workspace/evidence/branch.bundle master..HEAD" in (
        second
    )
    assert "main..HEAD > /workspace/evidence/branch.patch" not in second
