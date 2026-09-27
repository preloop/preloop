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
