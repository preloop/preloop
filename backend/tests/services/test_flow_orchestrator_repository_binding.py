"""The orchestrator side of a Jira project bound to a code-host repository."""

from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.services.repository_binding import (
    AppliedRepositoryBinding,
    RepositoryBindingError,
)

pytestmark = pytest.mark.asyncio

JIRA_TRACKER = "jira-tracker-id"
HOST_TRACKER = "github-tracker-id"
PR_URL = "https://github.com/acme/api/pull/12"


def _jira_trigger() -> Dict[str, Any]:
    return {
        "source": "jira",
        "tracker_id": JIRA_TRACKER,
        "project_id": "jira-project-id",
        "payload": {"issue": {"key": "PROJ-12", "fields": {"summary": "Fix it"}}},
    }


def _applied(stored: Dict[str, Any]) -> AppliedRepositoryBinding:
    effective = {
        **stored,
        "repositories": [
            {
                "tracker_id": HOST_TRACKER,
                "project_id": "repo-project-id",
                "clone_path": "/workspace",
                "branch": "develop",
            }
        ],
        "source_branch": "develop",
    }
    return AppliedRepositoryBinding(
        source="project",
        tracker_id=HOST_TRACKER,
        tracker_type="github",
        project_id="repo-project-id",
        repository="acme/api",
        base_branch="develop",
        git_clone_config=effective,
    )


def _orchestrator(git_clone_config: Optional[Dict[str, Any]]):
    orch = FlowExecutionOrchestrator(
        db=MagicMock(),
        flow_id="flow-id",
        trigger_event_data=_jira_trigger(),
        nats_client=AsyncMock(),
    )
    orch.flow = MagicMock()
    orch.flow.id = "flow-id"
    orch.flow.account_id = "account-id"
    orch.flow.git_clone_config = git_clone_config
    orch.flow.trigger_project_ids = None
    orch.flow.agent_type = "opencode"
    orch.execution_log = MagicMock()
    orch.execution_log.id = "execution-id"
    orch.ai_model = None
    return orch


async def _prepare(orch, *, applied, attach, credentials):
    with (
        patch.object(orch, "_resolve_prompt", AsyncMock(return_value="do it")),
        patch.object(
            orch, "_create_temporary_api_token", return_value=("token", uuid4())
        ),
        patch.object(orch, "_get_tracker_credentials_by_id", credentials),
        patch.object(orch, "_attach_trigger_tracker_credentials", attach),
        patch(
            "preloop.services.repository_binding.resolve_repository_binding",
            **(
                {"side_effect": applied}
                if isinstance(applied, Exception)
                else {"return_value": applied}
            ),
        ) as resolve,
    ):
        context = await orch._prepare_execution_context()
    return context, resolve


async def test_bound_run_clones_the_host_repository_with_its_credential() -> None:
    stored = {"enabled": True}
    orch = _orchestrator(stored)
    attach = AsyncMock()
    credentials = AsyncMock(return_value={"token": "host-token", "type": "github"})

    context, resolve = await _prepare(
        orch, applied=_applied(stored), attach=attach, credentials=credentials
    )

    assert resolve.call_args.kwargs["trigger_tracker_id"] == JIRA_TRACKER
    assert resolve.call_args.kwargs["trigger_project_id"] == "jira-project-id"
    assert context["git_clone_config"]["repositories"][0]["tracker_id"] == (
        HOST_TRACKER
    )
    assert context["repository_binding"]["repository"] == "acme/api"
    assert "token" not in str(context["repository_binding"])
    # Only the code-host tracker's credential; the Jira token never leaves.
    credentials.assert_awaited_once_with(HOST_TRACKER)
    assert list(context["git_credentials_map"]) == [HOST_TRACKER]
    attach.assert_not_awaited()
    # The flow row is untouched.
    assert stored == {"enabled": True}
    assert orch.flow.git_clone_config is stored


async def test_unbound_run_keeps_the_trigger_credential_path() -> None:
    orch = _orchestrator({"enabled": True})
    attach = AsyncMock()
    context, _ = await _prepare(
        orch, applied=None, attach=attach, credentials=AsyncMock(return_value=None)
    )
    assert "repository_binding" not in context
    assert context["git_clone_config"] == {"enabled": True}
    attach.assert_awaited_once()


async def test_binding_error_fails_before_any_token_is_minted() -> None:
    orch = _orchestrator({"enabled": True})
    with pytest.raises(RepositoryBindingError, match="none as default"):
        with patch.object(
            orch,
            "_create_temporary_api_token",
            side_effect=AssertionError("must not mint"),
        ):
            await _prepare(
                orch,
                applied=RepositoryBindingError("x marks none as default"),
                attach=AsyncMock(),
                credentials=AsyncMock(),
            )


class _JiraClient:
    def __init__(self) -> None:
        self.add_comment = AsyncMock()
        self.add_remote_link = AsyncMock()


def _terminal_orchestrator(trigger: Dict[str, Any], notifications: Any):
    orch = FlowExecutionOrchestrator(
        db=MagicMock(),
        flow_id=uuid4(),
        trigger_event_data=trigger,
        nats_client=MagicMock(),
    )
    orch.flow = SimpleNamespace(notifications=notifications)
    orch.execution_log = SimpleNamespace(
        id=uuid4(),
        trigger_event_details=trigger,
        failure_category=None,
        result={"pr_url": PR_URL, "pr_source_branch": "preloop/proj-12"},
    )
    orch.execution_logger = MagicMock()
    return orch


@pytest.mark.parametrize(
    "notifications", [None, {"on_success": {"comment_on_trigger_issue": True}}]
)
async def test_jira_run_writes_the_pull_request_back_once(notifications) -> None:
    client = _JiraClient()
    orch = _terminal_orchestrator(_jira_trigger(), notifications)
    with patch.object(
        orch, "_get_tracker_client_for_status", new=AsyncMock(return_value=client)
    ):
        await orch._notify_terminal(status="SUCCEEDED")

    client.add_comment.assert_awaited_once()
    key, body = client.add_comment.await_args.args
    assert key == "PROJ-12"
    assert PR_URL in body and "preloop/proj-12" in body
    client.add_remote_link.assert_awaited_once()
    assert client.add_remote_link.await_args.kwargs["global_id"] == (
        "preloop:pull-request:github.com/acme/api"
    )


async def test_github_run_is_not_written_back() -> None:
    client = _JiraClient()
    trigger = {
        "source": "github",
        "payload": {"issue": {"number": 42}},
    }
    orch = _terminal_orchestrator(trigger, None)
    with patch.object(
        orch, "_get_tracker_client_for_status", new=AsyncMock(return_value=client)
    ):
        await orch._notify_terminal(status="SUCCEEDED")
    client.add_comment.assert_not_awaited()
    client.add_remote_link.assert_not_awaited()


async def test_jira_write_back_failure_is_swallowed() -> None:
    orch = _terminal_orchestrator(_jira_trigger(), None)
    with patch.object(
        orch,
        "_get_tracker_client_for_status",
        new=AsyncMock(side_effect=RuntimeError("jira down")),
    ):
        assert await orch._write_pull_request_back_to_jira() is False


async def _prepare_host_checkout(orch, *, applied, attach, credentials):
    with (
        patch.object(orch, "_get_tracker_credentials_by_id", credentials),
        patch.object(orch, "_attach_trigger_tracker_credentials", attach),
        patch(
            "preloop.services.repository_binding.resolve_repository_binding",
            **(
                {"side_effect": applied}
                if isinstance(applied, Exception)
                else {"return_value": applied}
            ),
        ) as resolve,
    ):
        context = await orch.prepare_host_exec_checkout_context()
    return context, resolve


async def test_host_checkout_clones_the_bound_repository() -> None:
    stored = {"enabled": True}
    orch = _orchestrator(stored)
    attach = AsyncMock()
    credentials = AsyncMock(return_value={"token": "host-token", "type": "github"})

    context, resolve = await _prepare_host_checkout(
        orch, applied=_applied(stored), attach=attach, credentials=credentials
    )

    assert resolve.call_args.kwargs["trigger_tracker_id"] == JIRA_TRACKER
    assert context["git_clone_config"]["repositories"][0]["tracker_id"] == (
        HOST_TRACKER
    )
    assert context["repository_binding"]["repository"] == "acme/api"
    # Only the code-host tracker's credential; the Jira token never leaves.
    credentials.assert_awaited_once_with(HOST_TRACKER)
    assert list(context["git_credentials_map"]) == [HOST_TRACKER]
    attach.assert_not_awaited()
    assert orch.flow.git_clone_config is stored
    assert stored == {"enabled": True}


async def test_host_checkout_without_binding_keeps_the_trigger_path() -> None:
    orch = _orchestrator({"enabled": True})
    attach = AsyncMock()
    context, _ = await _prepare_host_checkout(
        orch, applied=None, attach=attach, credentials=AsyncMock(return_value=None)
    )
    assert "repository_binding" not in context
    assert context["git_clone_config"] == {"enabled": True}
    attach.assert_awaited_once()


async def test_host_checkout_binding_error_propagates() -> None:
    orch = _orchestrator({"enabled": True})
    credentials = AsyncMock()
    with pytest.raises(RepositoryBindingError, match="none as default"):
        await _prepare_host_checkout(
            orch,
            applied=RepositoryBindingError("x marks none as default"),
            attach=AsyncMock(),
            credentials=credentials,
        )
    credentials.assert_not_awaited()


async def test_host_checkout_skips_binding_when_clone_is_disabled() -> None:
    orch = _orchestrator({"enabled": False})
    context, resolve = await _prepare_host_checkout(
        orch,
        applied=AssertionError("must not resolve"),
        attach=AsyncMock(),
        credentials=AsyncMock(),
    )
    assert context is None
    resolve.assert_not_called()
