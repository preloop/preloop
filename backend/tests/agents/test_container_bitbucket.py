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
