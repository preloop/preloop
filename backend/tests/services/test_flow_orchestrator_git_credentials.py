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
                with open(path) as cred_file:
                    captured["credential_file_content"] = cred_file.read()
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


APP_TOKEN = "ghs_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"


class TestTrackerCredentialResolution:
    """The tracker token must be resolved here, where minting is possible.

    A GitHub App tracker stores no API key. Reading ``resolved_api_key`` left
    the agent container with no credential at all: a public repository still
    cloned and the post-execution push then failed to authenticate
    (production execution 85b67a24).
    """

    def _tracker(self, *, api_key="", auth_type="github_app"):
        tracker = MagicMock()
        tracker.id = "tracker-app"
        tracker.resolved_api_key = api_key
        tracker.auth_type = auth_type
        tracker.tracker_type = "github"
        return tracker

    async def test_app_tracker_yields_a_minted_token(self, orchestrator):
        tracker = self._tracker()
        with (
            patch(
                "preloop.models.crud.crud_tracker.get",
                return_value=tracker,
            ),
            patch(
                "preloop.services.flow_orchestrator.resolve_tracker_git_token",
                AsyncMock(return_value=APP_TOKEN),
            ),
        ):
            creds = await orchestrator._get_tracker_credentials_by_id("tracker-app")

        assert creds == {
            "tracker_id": "tracker-app",
            "token": APP_TOKEN,
            "tracker_type": "github",
        }

    async def test_tracker_without_any_token_reports_an_empty_token(self, orchestrator):
        with (
            patch(
                "preloop.models.crud.crud_tracker.get",
                return_value=self._tracker(),
            ),
            patch(
                "preloop.services.flow_orchestrator.resolve_tracker_git_token",
                AsyncMock(return_value=None),
            ),
        ):
            creds = await orchestrator._get_tracker_credentials_by_id("tracker-app")

        assert creds["token"] == ""


class TestAttachTriggerTrackerCredentials:
    """Repositories configured without a ``tracker_id`` still need a token."""

    async def test_project_tracker_credentials_land_in_the_context(self, orchestrator):
        context = {"trigger_project_id": "project-1"}
        with (
            patch.object(
                orchestrator, "_resolve_project_tracker_id", return_value="tracker-app"
            ),
            patch.object(
                orchestrator,
                "_get_tracker_credentials_by_id",
                AsyncMock(
                    return_value={
                        "tracker_id": "tracker-app",
                        "token": APP_TOKEN,
                        "tracker_type": "github",
                    }
                ),
            ),
        ):
            await orchestrator._attach_trigger_tracker_credentials(context)

        assert context["trigger_tracker_id"] == "tracker-app"
        assert context["git_credentials_map"]["tracker-app"]["token"] == APP_TOKEN

    async def test_trigger_event_tracker_is_the_fallback(self, orchestrator):
        orchestrator.trigger_event_data = {"tracker_id": "tracker-from-event"}
        context = {}
        with (
            patch.object(
                orchestrator, "_resolve_project_tracker_id", return_value=None
            ),
            patch.object(
                orchestrator,
                "_get_tracker_credentials_by_id",
                AsyncMock(
                    return_value={
                        "tracker_id": "tracker-from-event",
                        "token": PAT,
                        "tracker_type": "github",
                    }
                ),
            ),
        ):
            await orchestrator._attach_trigger_tracker_credentials(context)

        assert context["trigger_tracker_id"] == "tracker-from-event"

    async def test_no_tracker_leaves_the_context_untouched(self, orchestrator):
        orchestrator.trigger_event_data = {}
        context = {}
        with patch.object(
            orchestrator, "_resolve_project_tracker_id", return_value=None
        ):
            await orchestrator._attach_trigger_tracker_credentials(context)

        assert context == {}

    async def test_a_tokenless_tracker_is_not_recorded(self, orchestrator):
        context = {"trigger_project_id": "project-1"}
        with (
            patch.object(
                orchestrator, "_resolve_project_tracker_id", return_value="tracker-app"
            ),
            patch.object(
                orchestrator,
                "_get_tracker_credentials_by_id",
                AsyncMock(return_value={"token": "", "tracker_type": "github"}),
            ),
        ):
            await orchestrator._attach_trigger_tracker_credentials(context)

        assert "trigger_tracker_id" not in context
        assert "git_credentials_map" not in context

    async def test_existing_repository_credentials_are_preserved(self, orchestrator):
        context = {
            "trigger_project_id": "project-1",
            "git_credentials_map": {
                "tracker-repo": {"token": PAT, "tracker_type": "github"}
            },
        }
        with (
            patch.object(
                orchestrator, "_resolve_project_tracker_id", return_value="tracker-app"
            ),
            patch.object(
                orchestrator,
                "_get_tracker_credentials_by_id",
                AsyncMock(
                    return_value={
                        "tracker_id": "tracker-app",
                        "token": APP_TOKEN,
                        "tracker_type": "github",
                    }
                ),
            ),
        ):
            await orchestrator._attach_trigger_tracker_credentials(context)

        assert set(context["git_credentials_map"]) == {"tracker-repo", "tracker-app"}
