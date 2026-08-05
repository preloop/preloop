"""Tests for credential-free cloning in the flow orchestrator (issue #173).

The orchestrator has its own clone path, separate from the container agents.
It used to embed the tracker token in the clone URL, so the cloned workspace's
``origin`` remote carried the secret and any ``git remote -v`` run by the agent
leaked it into the execution logs.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

pytestmark = pytest.mark.asyncio

PAT = "github_pat_11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"
REPO_URL = "https://github.com/acme/private.git"


@pytest.fixture
def orchestrator():
    orch = FlowExecutionOrchestrator(
        db=MagicMock(),
        flow_id="test-flow-id",
        trigger_event_data={},
        nats_client=AsyncMock(),
    )
    orch.flow = MagicMock()
    orch.flow.git_clone_config = {
        "enabled": True,
        "repository_url": REPO_URL,
        "clone_path": "repo",
        "branch": "main",
        "use_tracker_credentials": True,
    }
    orch.execution_log = MagicMock()
    orch.execution_log.id = "test-execution-id"
    return orch


class TestBuildCloneCredential:
    def test_returns_credential_without_touching_url(self, orchestrator):
        credential = orchestrator._build_clone_credential(
            REPO_URL, {"token": PAT, "tracker_type": "github"}
        )
        assert credential.repo_url == REPO_URL
        assert credential.token == PAT
        assert credential.username == "x-access-token"

    def test_strips_a_token_already_in_the_url(self, orchestrator):
        credential = orchestrator._build_clone_credential(
            f"https://{PAT}@github.com/acme/private.git",
            {"token": PAT, "tracker_type": "github"},
        )
        assert credential.repo_url == REPO_URL

    def test_gitlab_username(self, orchestrator):
        credential = orchestrator._build_clone_credential(
            "https://gitlab.com/acme/repo.git",
            {"token": "glpat-aBcDeFgHiJkLmNoPqRs", "tracker_type": "gitlab"},
        )
        assert credential.username == "gitlab-ci-token"

    def test_no_token_yields_no_credential(self, orchestrator):
        assert orchestrator._build_clone_credential(REPO_URL, {}) is None


class TestPerformGitCloneCommand:
    """Assert on the command and environment handed to the subprocess."""

    async def _run_clone(self, orchestrator, tracker_credentials):
        captured = {}

        async def fake_subprocess_shell(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            if captured["env"] and "GIT_CONFIG_VALUE_0" in captured["env"]:
                path = captured["env"]["GIT_CONFIG_VALUE_0"].split("=", 1)[1]
                captured["credential_file"] = path
                captured["credential_file_content"] = open(path).read()
            process = AsyncMock()
            process.returncode = 0
            process.communicate.return_value = (b"done", b"")
            return process

        with (
            patch.object(
                orchestrator,
                "_get_tracker_credentials",
                AsyncMock(return_value=tracker_credentials),
            ),
            patch(
                "asyncio.create_subprocess_shell",
                side_effect=fake_subprocess_shell,
            ),
        ):
            await orchestrator._perform_git_clone("/tmp/work")

        return captured

    async def test_clone_command_has_no_credentials(self, orchestrator):
        captured = await self._run_clone(
            orchestrator, {"token": PAT, "tracker_type": "github"}
        )
        assert PAT not in captured["cmd"]
        assert "@github.com" not in captured["cmd"]
        assert REPO_URL in captured["cmd"]

    async def test_token_is_delivered_through_a_credential_file(self, orchestrator):
        captured = await self._run_clone(
            orchestrator, {"token": PAT, "tracker_type": "github"}
        )
        assert PAT in captured["credential_file_content"]
        assert captured["env"]["GIT_CONFIG_KEY_0"] == "credential.helper"

    async def test_credential_file_is_removed_after_the_clone(self, orchestrator):
        captured = await self._run_clone(
            orchestrator, {"token": PAT, "tracker_type": "github"}
        )
        assert not os.path.exists(captured["credential_file"])

    async def test_url_token_is_stripped_before_cloning(self, orchestrator):
        orchestrator.flow.git_clone_config["repository_url"] = (
            f"https://{PAT}@github.com/acme/private.git"
        )
        captured = await self._run_clone(
            orchestrator, {"token": PAT, "tracker_type": "github"}
        )
        assert PAT not in captured["cmd"]

    async def test_clone_without_credentials_passes_no_env(self, orchestrator):
        captured = await self._run_clone(orchestrator, None)
        assert PAT not in captured["cmd"]
        assert captured["env"] is None, "inherit the parent environment as before"

    async def test_repo_url_is_shell_quoted(self, orchestrator):
        """The URL can come from a webhook payload, so it is untrusted."""
        orchestrator.flow.git_clone_config["repository_url"] = (
            "https://github.com/acme/repo.git; echo pwned"
        )
        captured = await self._run_clone(orchestrator, None)
        assert "; echo pwned" not in captured["cmd"].replace(
            "'https://github.com/acme/repo.git; echo pwned'", ""
        )
