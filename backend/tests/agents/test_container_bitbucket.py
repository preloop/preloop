"""Tests for Bitbucket Cloud repositories in the container agent executor."""

import pytest

from preloop.agents.container import ContainerAgentExecutor

PAYLOAD = {
    "repository": {
        "full_name": "ws/repo",
        "links": {"html": {"href": "https://bitbucket.org/ws/repo"}},
    },
    "pullrequest": {
        "id": 7,
        "source": {"branch": {"name": "feature"}, "commit": {"hash": "abc123"}},
        "destination": {"branch": {"name": "main"}},
    },
}


@pytest.fixture
def executor() -> ContainerAgentExecutor:
    return ContainerAgentExecutor(
        agent_type="codex",
        config={"test": True},
        image="test-image:latest",
        use_kubernetes=False,
    )


def _context(creds: dict) -> dict:
    return {
        "flow_id": "flow-1",
        "execution_id": "exec-1",
        "git_credentials_map": {"tracker-1": creds},
    }


REPO = {
    "repository_url": "https://bitbucket.org/ws/repo.git",
    "tracker_id": "tracker-1",
}


def test_credential_uses_tracker_username(executor: ContainerAgentExecutor) -> None:
    credential = executor._build_git_credential(
        REPO["repository_url"],
        REPO,
        _context(
            {"token": "tok", "tracker_type": "bitbucket", "username": "x-token-auth"}
        ),
    )
    assert credential is not None
    assert credential.username == "x-token-auth"
    assert credential.token == "tok"
    assert "tok" not in credential.repo_url


def test_credential_defaults_to_api_token_username(
    executor: ContainerAgentExecutor,
) -> None:
    credential = executor._build_git_credential(
        REPO["repository_url"],
        REPO,
        _context({"token": "tok", "tracker_type": "bitbucket"}),
    )
    assert credential.username == "x-bitbucket-api-token-auth"


def test_username_without_token_is_ignored(executor: ContainerAgentExecutor) -> None:
    username = executor._resolve_git_username(
        REPO,
        _context({"token": "", "username": "dev"}),
        "bitbucket",
        "bitbucket",
    )
    assert username == "x-bitbucket-api-token-auth"


def test_trigger_extraction(executor: ContainerAgentExecutor) -> None:
    trigger = {"payload": PAYLOAD}
    assert executor._extract_target_branch_from_trigger(trigger) == "main"
    assert executor._extract_source_branch_from_trigger(trigger) == "feature"
    assert executor._extract_commit_sha_from_trigger(trigger) == "abc123"
    assert (
        executor._extract_repo_url_from_trigger(trigger)
        == "https://bitbucket.org/ws/repo.git"
    )


def test_resolve_bitbucket_api_email(executor: ContainerAgentExecutor) -> None:
    good = _context({"token": "tok", "email": "dev@example.com"})
    assert executor._resolve_bitbucket_api_email(REPO, good) == "dev@example.com"

    # An email with shell metacharacters never reaches the generated script.
    bad = _context({"token": "tok", "email": 'dev"; rm -rf / #@x.com'})
    assert executor._resolve_bitbucket_api_email(REPO, bad) == ""

    # No token, no Basic fallback.
    tokenless = _context({"email": "dev@example.com"})
    assert executor._resolve_bitbucket_api_email(REPO, tokenless) == ""

    # No email shipped (access/OAuth token trackers).
    no_email = _context({"token": "tok"})
    assert executor._resolve_bitbucket_api_email(REPO, no_email) == ""


def _create_shell(executor: ContainerAgentExecutor, creds: dict, **kwargs) -> str:
    context = _context(creds)
    context["trigger_event_data"] = {"payload": PAYLOAD}
    return executor._build_pr_or_mr_create_shell(
        execution_context=context,
        git_config={"create_pull_request": True},
        token_ref="${PRELOOP_GIT_TOKEN_1}",
        tracker_type="bitbucket",
        host_kind="bitbucket",
        repo_url=kwargs.pop("repo_url", "https://bitbucket.org/ws/repo.git"),
        safe_target="preloop/issue-1",
        safe_source="main",
        repo_config=REPO,
        **kwargs,
    )


def test_bitbucket_create_shell_posts_with_bearer_auth(
    executor: ContainerAgentExecutor,
) -> None:
    script = _create_shell(executor, {"token": "tok", "tracker_type": "bitbucket"})
    assert script
    assert "https://api.bitbucket.org/2.0/repositories/ws/repo/pullrequests" in script
    assert 'PRELOOP_BB_AUTH="Authorization: Bearer ${PRELOOP_GIT_TOKEN_1}"' in script
    # The payload writer runs with the bitbucket kind.
    assert " bitbucket " in script
    # The capture shell emits the binding marker for this provider.
    assert '\\"provider\\": \\"bitbucket\\"' in script
    # No email was shipped: there is no Basic retry.
    assert "Basic" not in script
    # The token is referenced through the environment, never inlined.
    assert "tok" not in script.replace("token", "")


def test_bitbucket_create_shell_adds_basic_retry_with_email(
    executor: ContainerAgentExecutor,
) -> None:
    script = _create_shell(
        executor,
        {"token": "tok", "tracker_type": "bitbucket", "email": "dev@example.com"},
    )
    assert 'if [ "$HTTP_CODE" = "401" ]; then' in script
    assert "dev@example.com:${PRELOOP_GIT_TOKEN_1}" in script
    assert "Authorization: Basic" in script


def test_bitbucket_create_shell_requires_workspace_and_slug(
    executor: ContainerAgentExecutor,
) -> None:
    script = _create_shell(
        executor,
        {"token": "tok", "tracker_type": "bitbucket"},
        repo_url="https://bitbucket.org/only-one-segment",
    )
    assert script == ""


def test_get_token_from_project_returns_git_username(
    executor: ContainerAgentExecutor,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    project = SimpleNamespace(organization=SimpleNamespace(tracker_id="t-1"))
    tracker = SimpleNamespace(
        id="t-1",
        tracker_type="bitbucket",
        auth_type="api_token",
        resolved_api_key="tok",
        connection_details={"token_kind": "access_token"},
    )
    with (
        patch(
            "preloop.models.db.session.get_db_session",
            return_value=iter([MagicMock()]),
        ),
        patch("preloop.models.crud.crud_project.get", return_value=project),
        patch("preloop.models.crud.crud_tracker.get", return_value=tracker),
    ):
        token, tracker_type, username = executor._get_token_from_project(
            "p-1", "acct-1"
        )
    assert (token, tracker_type, username) == ("tok", "bitbucket", "x-token-auth")


def test_write_pr_payload_builds_bitbucket_shape(tmp_path) -> None:
    import json
    import pathlib
    import subprocess
    import sys

    from preloop.agents.container import (
        COMMIT_PR_BODY_FILE,
        COMMIT_PR_LIST_FILE,
        COMMIT_PR_TITLE_FILE,
        FLOW_PR_BODY_FILE,
        FLOW_PR_TITLE_FILE,
        WRITE_PR_PAYLOAD_PY,
    )

    files = {
        FLOW_PR_TITLE_FILE: "Add parser",
        FLOW_PR_BODY_FILE: "Adds a parser.",
        COMMIT_PR_TITLE_FILE: "",
        COMMIT_PR_BODY_FILE: "",
        COMMIT_PR_LIST_FILE: "",
    }
    out_path = tmp_path / "pr-payload.json"
    for path, content in files.items():
        pathlib.Path(path).write_text(content, encoding="utf-8")
    try:
        subprocess.run(
            [
                sys.executable,
                "-",
                str(out_path),
                "preloop/issue-1",
                "main",
                "bitbucket",
                "",
                "",
                "",
                "1",
            ],
            input=WRITE_PR_PAYLOAD_PY,
            text=True,
            check=True,
            capture_output=True,
        )
        payload = json.loads(out_path.read_text(encoding="utf-8"))
    finally:
        for path in files:
            pathlib.Path(path).unlink(missing_ok=True)

    assert payload["title"] == "Add parser"
    assert payload["description"] == "Adds a parser."
    assert payload["source"] == {"branch": {"name": "preloop/issue-1"}}
    assert payload["destination"] == {"branch": {"name": "main"}}
    assert "head" not in payload and "base" not in payload


# ---------------------------------------------------------------------------
# Managed Bitbucket Cloud publication (issue #1065): late push and PR REST
# reacquire a fresh access token through the execution-bound broker.
# ---------------------------------------------------------------------------

import json  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402
from uuid import uuid4  # noqa: E402


def _managed_context(tracker: str | None = None) -> dict:
    tracker = tracker or str(uuid4())
    return {
        "account_id": str(uuid4()),
        "execution_id": str(uuid4()),
        "_git_target_branch": "preloop/issue-1",
        "_git_source_branch": "main",
        "git_clone_config": {
            "enabled": True,
            "create_pull_request": True,
            "repositories": [
                {
                    "repository_url": "https://bitbucket.org/ws/repo.git",
                    "clone_path": "/workspace",
                    "tracker_id": tracker,
                }
            ],
        },
        "git_credentials_map": {
            tracker: {
                "token": "launch-token-a",
                "tracker_type": "bitbucket",
                "auth_type": "managed_oauth",
                "username": "x-token-auth",
            }
        },
    }


def test_managed_bitbucket_publication_refreshes_and_hides_the_launch_token(
    executor: ContainerAgentExecutor,
) -> None:
    data = _managed_context()
    commands = executor._prepare_git_post_execution_commands(data)
    assert "launch-token-a" not in commands
    assert commands.count("PRELOOP_PUBLICATION_REFRESH_URL=") == 2
    assert (
        commands.index("branch.bundle")
        < commands.index("PRELOOP_PUBLICATION_REFRESH_URL=")
        < commands.index("git push")
    )
    assert "PRELOOP_PUBLICATION_REPOSITORY=https://bitbucket.org/ws/repo.git" in (
        commands
    )
    # Bitbucket REST calls read the fresh header through a descriptor, never argv.
    assert '-H "$PRELOOP_BB_AUTH"' not in commands
    assert '--config <(printf \'header = "%s"\\n\' "$PRELOOP_BB_AUTH")' in commands
    assert "api.bitbucket.org/2.0/repositories/ws/repo/pullrequests" in commands
    # A pasted-token Basic retry never applies to a managed grant.
    assert "Authorization: Basic" not in commands
    subprocess.run(["bash", "-n"], input=commands, text=True, check=True)
    env = executor._git_credential_env(data)
    assert env["PRELOOP_PUBLICATION_CAPABILITY_0"] not in commands
    from preloop.api.endpoints.publication_credentials import publication_claims

    claims = publication_claims("Bearer " + env["PRELOOP_PUBLICATION_CAPABILITY_0"])
    assert str(claims["tracker_id"]) == next(iter(data["git_credentials_map"]))
    assert claims["repository_url"] == "https://bitbucket.org/ws/repo.git"
    assert str(claims["account_id"]) == data["account_id"]


@pytest.mark.parametrize("auth_type", ["api_token", "oauth_token"])
def test_pasted_bitbucket_tokens_keep_legacy_publication(
    executor: ContainerAgentExecutor, auth_type: str
) -> None:
    data = _managed_context()
    next(iter(data["git_credentials_map"].values()))["auth_type"] = auth_type
    commands = executor._prepare_git_post_execution_commands(data)
    assert "PRELOOP_PUBLICATION_REFRESH_URL=" not in commands
    assert '-H "$PRELOOP_BB_AUTH"' in commands


def test_isolated_mode_never_gets_managed_refresh_authority(
    executor: ContainerAgentExecutor,
) -> None:
    data = _managed_context()
    data["git_clone_config"]["publication_mode"] = "isolated"
    with pytest.raises(ValueError):
        executor._resolve_repository_token(
            data["git_clone_config"]["repositories"][0], data
        )


def test_managed_tracker_db_fallback_never_reads_a_stale_key(
    executor: ContainerAgentExecutor,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    project = SimpleNamespace(organization=SimpleNamespace(tracker_id="t-1"))
    tracker = SimpleNamespace(
        id="t-1",
        tracker_type="bitbucket",
        auth_type="managed_oauth",
        resolved_api_key="stale-key",
        connection_details={"token_kind": "access_token"},
    )
    with (
        patch(
            "preloop.models.db.session.get_db_session",
            return_value=iter([MagicMock()]),
        ),
        patch("preloop.models.crud.crud_project.get", return_value=project),
        patch("preloop.models.crud.crud_tracker.get", return_value=tracker),
    ):
        token, tracker_type, username = executor._get_token_from_project(
            "p-1", "acct-1"
        )
    assert token is None
    assert tracker_type == "bitbucket"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], text=True, check=True, capture_output=True
    ).stdout


@pytest.mark.parametrize(
    ("push_count", "broker"),
    [(1, "fresh"), (0, "fresh"), (1, "reconnect_required"), (1, "revoked_execution")],
)
def test_managed_bitbucket_late_publication_uses_token_b_after_clone_with_a(
    executor: ContainerAgentExecutor,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    push_count: int,
    broker: str,
) -> None:
    """Clone authenticated with token A; hours later push and PR use token B.

    The rendered wrapper runs against a local broker stand-in. ``push_count``
    0 is the retry case where the branch already exists remotely and only the
    PR step runs (it must still reacquire credentials and reuse the lookup
    path rather than open a duplicate). ``reconnect_required`` and
    ``revoked_execution`` are broker denials: no push, no PR, work retained.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from preloop.agents import container as container_module
    from preloop.api.endpoints.publication_credentials import publication_claims

    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Jane Doe")
    _git(repository, "config", "user.email", "jane@example.com")
    _git(repository, "checkout", "-b", "main")
    (repository / "source.txt").write_text("base")
    _git(repository, "add", "source.txt")
    _git(repository, "commit", "-m", "Base")
    base = _git(repository, "rev-parse", "HEAD").strip()
    _git(repository, "checkout", "-b", "preloop/issue-1")
    (repository / "source.txt").write_text("implemented")
    _git(repository, "add", "source.txt")
    _git(repository, "commit", "-m", "Implementation")
    _git(
        repository,
        "update-ref",
        "refs/remotes/origin/preloop/issue-1",
        base if push_count else "HEAD",
    )
    _git(repository, "remote", "add", "origin", "https://bitbucket.org/ws/repo.git")
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(container_module, "EVIDENCE_DIR_PATH", str(evidence))

    requests: list[dict] = []
    denial = {
        "reconnect_required": (409, "publication_reconnect_required"),
        "revoked_execution": (409, "publication_execution_closed"),
    }.get(broker)

    class Broker(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler protocol
            claims = publication_claims(self.headers["Authorization"])
            assert claims["repository_url"] == "https://bitbucket.org/ws/repo.git"
            requests.append(claims)
            if denial:
                self.send_response(denial[0])
                self.end_headers()
                self.wfile.write(json.dumps({"detail": denial[1]}).encode())
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {"token": "fresh-managed-token-b", "username": "x-token-auth"}
                ).encode()
            )

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Broker)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        container_module.settings,
        "preloop_url",
        f"http://127.0.0.1:{server.server_port}",
    )
    data = _managed_context()
    data["git_clone_config"]["repositories"][0]["clone_path"] = str(repository)
    push_receipt = tmp_path / "push.txt"
    rest_receipt = tmp_path / "rest.json"
    monkeypatch.setattr(
        executor,
        "_build_git_push_shell",
        lambda *args, **kwargs: (
            "printf 'protocol=https\\nhost=bitbucket.org\\npath=ws/repo.git\\n\\n' "
            f"| git credential fill > {push_receipt}"
        ),
    )
    # The real create shell is exercised elsewhere; here only its auth transport
    # matters, so a minimal stand-in keeps the Bitbucket header convention.
    monkeypatch.setattr(
        executor,
        "_build_pr_or_mr_create_shell",
        lambda **kwargs: (
            f'PRELOOP_BB_AUTH="Authorization: Bearer {kwargs["token_ref"]}"\n'
            'curl -sS --get -H "$PRELOOP_BB_AUTH" '
            "https://api.bitbucket.org/2.0/repositories/ws/repo/pullrequests"
        ),
    )
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    curl = bin_path / "curl"
    curl.write_text(
        "#!/usr/bin/env python3\nimport json,sys,os\nfrom pathlib import Path\n"
        "args=sys.argv[1:]\nconfig=Path(args[args.index('--config')+1]).read_text()\n"
        "Path(os.environ['TEST_REST_RECEIPT']).write_text(json.dumps({'args':args,'config':config}))\n"
    )
    curl.chmod(0o755)
    command = executor._prepare_git_post_execution_commands(data)
    command = command.replace("cd /workspace", "true")
    env = {
        **os.environ,
        **executor._git_credential_env(data),
        "HOME": str(tmp_path),
        "PATH": str(bin_path) + os.pathsep + os.environ["PATH"],
        "TEST_REST_RECEIPT": str(rest_receipt),
    }
    # The clone-time store still holds token A under x-token-auth.
    launch_store = tmp_path / "launch-store"
    launch_store.write_text("https://x-token-auth:launch-token-a@bitbucket.org\n")
    subprocess.run(
        [
            "git",
            "config",
            "--global",
            "credential.helper",
            f"store --file={launch_store}",
        ],
        env=env,
        check=True,
    )
    existing_stores = set(Path("/tmp").glob(".preloop-publication-*"))
    try:
        result = subprocess.run(
            ["bash", "-e", "-c", command], env=env, text=True, capture_output=True
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    output = result.stdout + result.stderr
    assert len(requests) == 1
    assert "fresh-managed-token-b" not in output
    assert "launch-token-a" not in output
    if denial:
        assert result.returncode != 0
        assert not push_receipt.exists()
        assert not rest_receipt.exists()
        assert (evidence / "branch.bundle").is_file()
    else:
        assert result.returncode == 0, result.stderr
        if push_count:
            receipt = push_receipt.read_text()
            assert "password=fresh-managed-token-b" in receipt
            assert "username=x-token-auth" in receipt
            assert "launch-token-a" not in receipt
        else:
            assert not push_receipt.exists()
        rest = json.loads(rest_receipt.read_text())
        assert "Authorization: Bearer fresh-managed-token-b" in rest["config"]
        assert all("fresh-managed-token-b" not in arg for arg in rest["args"])
        assert "launch-token-a" not in rest["config"]
    assert set(Path("/tmp").glob(".preloop-publication-*")) == existing_stores
