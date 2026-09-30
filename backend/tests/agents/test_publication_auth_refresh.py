"""Fresh publication auth replaces stale git storage and authenticates PR REST."""

import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest

from preloop.agents import publication_auth_client as client
from preloop.agents.container import ContainerAgentExecutor
from preloop.agents.resources import docker_memory_bytes


def git(repo: Path, *args: str, input: str | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input,
        text=True,
        check=True,
        capture_output=True,
    ).stdout


def context() -> dict[str, Any]:
    tracker = str(uuid4())
    return {
        "account_id": str(uuid4()),
        "execution_id": str(uuid4()),
        "_git_target_branch": "preloop/fix",
        "_git_source_branch": "main",
        "git_clone_config": {
            "enabled": True,
            "create_pull_request": True,
            "repositories": [
                {
                    "repository_url": "https://github.com/example/project.git",
                    "clone_path": "/workspace",
                    "tracker_id": tracker,
                }
            ],
        },
        "git_credentials_map": {
            tracker: {
                "token": "expired-start-token",
                "tracker_type": "github",
                "auth_type": "github_app",
            }
        },
    }


def executor() -> ContainerAgentExecutor:
    return ContainerAgentExecutor(
        agent_type="test", config={}, image="example:latest", use_kubernetes=False
    )


def test_refresh_wrapper_covers_push_and_pr_only_and_hides_secrets() -> None:
    data = context()
    runner = executor()
    commands = runner._prepare_git_post_execution_commands(data)
    assert "expired-start-token" not in commands
    assert commands.count("PRELOOP_PUBLICATION_REFRESH_URL=") == 2
    assert (
        commands.index("branch.bundle")
        < commands.index("PRELOOP_PUBLICATION_REFRESH_URL=")
        < commands.index("git push")
    )
    assert 'if [ "$PUSH_COMMIT_COUNT" -eq "0" ]; then' in commands
    assert '-H "Authorization: token ${PRELOOP_GIT_TOKEN_1}"' not in commands
    assert "--config <(printf" in commands
    assert "${PRELOOP_GIT_TOKEN_1}" in commands
    subprocess.run(["bash", "-n"], input=commands, text=True, check=True)
    env = runner._git_credential_env(data)
    assert env["PRELOOP_PUBLICATION_CAPABILITY_0"] not in commands


@pytest.mark.parametrize(
    "provider,auth_type",
    [("github", "api_token"), ("gitlab", "oauth_app"), ("bitbucket", "api_token")],
)
def test_non_app_providers_do_not_refresh(provider: str, auth_type: str) -> None:
    data = context()
    next(iter(data["git_credentials_map"].values())).update(
        tracker_type=provider, auth_type=auth_type
    )
    assert (
        "PRELOOP_PUBLICATION_REFRESH_URL="
        not in executor()._prepare_git_post_execution_commands(data)
    )


def test_empty_explicit_tracker_falls_back_to_actual_trigger_credentials() -> None:
    data = context()
    tracker = next(iter(data["git_credentials_map"]))
    data["trigger_tracker_id"] = tracker
    data["git_clone_config"]["repositories"][0]["tracker_id"] = "empty-tracker"
    commands = executor()._prepare_git_post_execution_commands(data)
    assert "PRELOOP_PUBLICATION_REFRESH_URL=" in commands


def test_bound_tracker_does_not_fallback_to_issue_tracker() -> None:
    data = context()
    tracker = next(iter(data["git_credentials_map"]))
    data["trigger_tracker_id"] = tracker
    data["repository_binding"] = {
        "repository_url": "https://github.com/example/project.git"
    }
    data["git_clone_config"]["repositories"][0]["tracker_id"] = "missing-tracker"
    assert (
        "PRELOOP_PUBLICATION_REFRESH_URL="
        not in executor()._prepare_git_post_execution_commands(data)
    )


def test_refresh_replaces_effective_stale_store_without_changing_legacy_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    repository = tmp_path / "app"
    repository.mkdir()
    git(repository, "init")
    git(repository, "remote", "add", "origin", "https://github.com/example/project.git")
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    git(legacy, "init")
    store = tmp_path / "clone-store"
    store.write_text(
        "https://x-access-token:expired-start-token@github.com\nhttps://oauth2:legacy-token@gitlab.com\n"
    )
    git(
        repository,
        "config",
        "--global",
        "credential.helper",
        "store --file=" + str(store),
    )
    monkeypatch.chdir(repository)
    monkeypatch.setenv(
        "PRELOOP_PUBLICATION_REPOSITORY", "https://github.com/example/project.git"
    )
    monkeypatch.setenv(
        "PRELOOP_PUBLICATION_REFRESH_URL", "https://controller.example.com/refresh"
    )
    monkeypatch.setenv("PRELOOP_PUBLICATION_REFRESH_CAPABILITY", "capability-secret")

    def issue(request: client.urllib.request.Request, **kwargs: Any) -> io.BytesIO:
        assert request.headers["Authorization"] == "Bearer capability-secret"
        assert "capability-secret" not in request.full_url
        return io.BytesIO(json.dumps({"token": "fresh-publication-token"}).encode())

    monkeypatch.setattr(client.urllib.request, "urlopen", issue)
    fresh = client.refresh()
    assert fresh == "fresh-publication-token"
    credentials = git(
        repository,
        "credential",
        "fill",
        input="protocol=https\nhost=github.com\npath=example/project.git\n\n",
    )
    assert "password=fresh-publication-token" in credentials
    assert "expired-start-token" not in credentials
    assert "password=legacy-token" in git(
        legacy, "credential", "fill", input="protocol=https\nhost=gitlab.com\n\n"
    )
    helper = git(repository, "config", "--local", "--get", "credential.helper").strip()
    fresh_store = Path(helper.removeprefix("store --file="))
    assert fresh_store.stat().st_mode & 0o777 == 0o600
    client.cleanup(str(repository))
    assert not fresh_store.exists()
    assert (
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "config",
                "--local",
                "--get-all",
                "credential.helper",
            ],
            capture_output=True,
        ).returncode
        == 1
    )
    assert "password=legacy-token" in git(
        legacy, "credential", "fill", input="protocol=https\nhost=gitlab.com\n\n"
    )


def test_origin_change_and_mint_failure_stop_before_git_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "PRELOOP_PUBLICATION_REPOSITORY", "https://github.com/example/project.git"
    )
    run = Mock(return_value=Mock(stdout="https://github.com/other/repository.git"))
    monkeypatch.setattr(client.subprocess, "run", run)
    mint = Mock()
    monkeypatch.setattr(client.urllib.request, "urlopen", mint)
    with pytest.raises(ValueError, match="origin changed"):
        client.refresh()
    mint.assert_not_called()
    run.assert_called_once()
    run.reset_mock()
    run.return_value.stdout = "https://github.com/example/project.git"
    monkeypatch.setenv(
        "PRELOOP_PUBLICATION_REFRESH_URL", "https://controller.example.com/refresh"
    )
    monkeypatch.setenv("PRELOOP_PUBLICATION_REFRESH_CAPABILITY", "capability")
    mint.side_effect = OSError("mint unavailable")
    with pytest.raises(OSError):
        client.refresh()
    run.assert_called_once()


@pytest.mark.parametrize(
    "quantity,expected",
    [
        ("4g", 4 * 1024**3),
        ("4Gi", 4 * 1024**3),
        ("512Mi", 512 * 1024**2),
        ("1.5g", int(1.5 * 1024**3)),
        ("4096", 4096),
    ],
)
def test_docker_accepts_chart_and_runtime_memory_quantities(
    quantity: str, expected: int
) -> None:
    assert docker_memory_bytes(quantity) == expected


def test_github_oauth_alias_refreshes() -> None:
    data = context()
    next(iter(data["git_credentials_map"].values()))["auth_type"] = "oauth_app"
    assert (
        "PRELOOP_PUBLICATION_REFRESH_URL="
        in executor()._prepare_git_post_execution_commands(data)
    )


@pytest.mark.parametrize(
    "push_count,mint_failure,publication_failure",
    [(1, False, False), (0, False, False), (1, True, False), (1, False, True)],
)
def test_runtime_refresh_authenticates_push_and_rest_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    push_count: int,
    mint_failure: bool,
    publication_failure: bool,
) -> None:
    """Execute the rendered wrapper after a simulated two-hour implementation."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from preloop.api.endpoints.publication_credentials import publication_claims
    from preloop.agents import container as container_module

    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.name", "Jane Doe")
    git(repository, "config", "user.email", "jane@example.com")
    git(repository, "checkout", "-b", "main")
    (repository / "source.txt").write_text("base")
    git(repository, "add", "source.txt")
    git(repository, "commit", "-m", "Base")
    base = git(repository, "rev-parse", "HEAD").strip()
    git(repository, "checkout", "-b", "preloop/fix")
    (repository / "source.txt").write_text("implemented")
    git(repository, "add", "source.txt")
    git(repository, "commit", "-m", "Implementation")
    git(
        repository,
        "update-ref",
        "refs/remotes/origin/preloop/fix",
        base if push_count else "HEAD",
    )
    git(repository, "remote", "add", "origin", "https://github.com/example/project.git")
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(container_module, "EVIDENCE_DIR_PATH", str(evidence))
    # Restore controller clock/transport via local server. Installation-start
    # auth has expired after implementation, but capability remains valid.
    requests = []

    class Server(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler protocol
            claims = publication_claims(self.headers["Authorization"])
            assert claims["repository_url"] == "https://github.com/example/project.git"
            requests.append(claims)
            self.send_response(502 if mint_failure else 200)
            self.end_headers()
            self.wfile.write(
                json.dumps({"token": "fresh-two-hour-publication-token"}).encode()
            )

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        container_module.settings,
        "preloop_url",
        f"http://127.0.0.1:{server.server_port}",
    )
    runner = executor()
    data = context()
    data["git_clone_config"]["repositories"][0]["clone_path"] = str(repository)
    push_receipt = tmp_path / "push.txt"
    rest_receipt = tmp_path / "rest.json"
    # The transport consumes credential stdin, as real git push does, and
    # records only synthetic test credentials in temporary local artifacts.
    monkeypatch.setattr(
        runner,
        "_build_git_push_shell",
        lambda *args, **kwargs: (
            f"printf 'protocol=https\\nhost=github.com\\npath=example/project.git\\n\\n' | git credential fill > {push_receipt}"
            + ("\nexit 9" if publication_failure else "")
        ),
    )
    monkeypatch.setattr(
        runner,
        "_build_pr_or_mr_create_shell",
        lambda **kwargs: (
            'curl -H "Authorization: token '
            + kwargs["token_ref"]
            + '" https://api.github.com/example'
        ),
    )
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    curl = bin_path / "curl"
    curl.write_text(
        "#!/usr/bin/env python3\nimport json,sys,os\nfrom pathlib import Path\nargs=sys.argv[1:]\nconfig=Path(args[args.index('--config')+1]).read_text()\nPath(os.environ['TEST_REST_RECEIPT']).write_text(json.dumps({'args':args,'config':config}))\n"
    )
    curl.chmod(0o755)
    command = runner._prepare_git_post_execution_commands(data)
    # Final wrapper directory convention is not relevant to auth transport.
    command = command.replace("cd /workspace", "true")
    env = {
        **os.environ,
        **runner._git_credential_env(data),
        "HOME": str(tmp_path),
        "PATH": str(bin_path) + os.pathsep + os.environ["PATH"],
        "TEST_REST_RECEIPT": str(rest_receipt),
    }
    stale_store = tmp_path / "expired-store"
    stale_store.write_text("https://x-access-token:expired-start-token@github.com\n")
    subprocess.run(
        [
            "git",
            "config",
            "--global",
            "credential.helper",
            "store --file=" + str(stale_store),
        ],
        env=env,
        check=True,
    )
    existing_stores = set(Path("/tmp").glob(".preloop-publication-*"))
    parent_receipt = tmp_path / "parent-trap.txt"
    command = f"trap 'printf finalized > {parent_receipt}' EXIT\n" + command
    try:
        result = subprocess.run(
            ["bash", "-e", "-c", command], env=env, text=True, capture_output=True
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert len(requests) == 1
    assert "fresh-two-hour-publication-token" not in result.stdout + result.stderr
    if mint_failure:
        assert result.returncode != 0
        assert not push_receipt.exists()
        assert not rest_receipt.exists()
        assert (evidence / "branch.bundle").is_file()
    elif publication_failure:
        assert result.returncode == 9
        assert "password=fresh-two-hour-publication-token" in push_receipt.read_text()
        assert not rest_receipt.exists()
        assert (evidence / "branch.bundle").is_file()
    else:
        assert result.returncode == 0, result.stderr
        if push_count:
            assert (
                "password=fresh-two-hour-publication-token" in push_receipt.read_text()
            )
        else:
            assert not push_receipt.exists()
        rest = json.loads(rest_receipt.read_text())
        assert "fresh-two-hour-publication-token" in rest["config"]
        assert all(
            "fresh-two-hour-publication-token" not in arg for arg in rest["args"]
        )
        assert "expired-start-token" not in rest["config"]
    assert parent_receipt.read_text() == "finalized"
    assert set(Path("/tmp").glob(".preloop-publication-*")) == existing_stores
    assert (
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "config",
                "--local",
                "--get-all",
                "credential.helper",
            ],
            capture_output=True,
        ).returncode
        == 1
    )


def test_configured_url_credentials_never_enter_refresh_script() -> None:
    data = context()
    data["git_clone_config"]["repositories"][0]["repository_url"] = (
        "https://user:configured-secret@github.com/example/project.git"
    )
    commands = executor()._prepare_git_post_execution_commands(data)
    assert "configured-secret" not in commands
    assert "https://github.com/example/project.git" in commands


def test_isolated_mode_cannot_receive_refresh_authority() -> None:
    data = context()
    data["git_clone_config"]["publication_mode"] = "isolated"
    data["_publication_refresh_env"] = {
        "PRELOOP_PUBLICATION_CAPABILITY_0": "write-capability"
    }
    with pytest.raises(ValueError, match="Write API tokens"):
        executor()._apply_git_credential_env({}, data)


@pytest.mark.parametrize("auth_type", ["GITHUB_APP", "OAUTH_APP"])
def test_case_normalized_app_auth_refreshes(auth_type: str) -> None:
    data = context()
    next(iter(data["git_credentials_map"].values()))["auth_type"] = auth_type
    assert (
        "PRELOOP_PUBLICATION_REFRESH_URL="
        in executor()._prepare_git_post_execution_commands(data)
    )


def test_restored_workspace_recreates_credential_free_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = tmp_path / "restored"
    repository.mkdir()
    git(repository, "init")
    git(
        repository,
        "remote",
        "add",
        "origin",
        "https://user:configured-secret@github.com/example/project.git",
    )
    data = context()
    data.pop("_git_source_branch")
    data["git_clone_config"]["repositories"][0].update(
        repository_url="https://user:configured-secret@github.com/example/project.git",
        clone_path=str(repository),
    )
    data["checkpoint_env"] = {"PRELOOP_CHECKPOINT_GET_TOKEN": "synthetic-capability"}
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global-config"))
    command = executor()._wrap_clone_with_workspace_restore("echo cold-clone", data)
    assert "configured-secret" not in command
    result = subprocess.run(
        ["bash", "-e", "-c", command], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert (
        git(repository, "remote", "get-url", "origin").strip()
        == "https://github.com/example/project.git"
    )
