"""Credential issuance, context separation, and terminal lifecycle contracts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from preloop.agents.container import ContainerAgentExecutor
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.services.publication_credentials import (
    mint_repository_lease,
    validate_publication_tracker,
)
from preloop.services.trusted_publisher import PublicationError, PublicationLease


@pytest.fixture
def tracker() -> Any:
    return SimpleNamespace(
        tracker_type="github",
        auth_type="github_app",
        oauth_installation=SimpleNamespace(external_id="123"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("write", [False, True])
async def test_app_credential_is_explicitly_scoped_to_repository_and_permissions(
    tracker: Any, write: bool
) -> None:
    permissions = {"metadata": "read", "contents": "write" if write else "read"}
    if write:
        permissions["pull_requests"] = "write"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload == {"repositories": ["project"], "permissions": permissions}
        assert request.headers["Authorization"] == "Bearer app-jwt"
        return httpx.Response(
            201,
            json={
                "token": "scoped-lease",
                "expires_at": "2099-01-01T00:00:00Z",
                "repositories": [{"full_name": "example/project"}],
                "permissions": permissions,
            },
        )

    with (
        patch(
            "preloop.services.publication_credentials.settings.github_app",
            SimpleNamespace(app_id="1", private_key="-----BEGIN KEY-----"),
        ),
        patch(
            "preloop.services.publication_credentials.jwt.encode",
            return_value="app-jwt",
        ),
    ):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            token = await mint_repository_lease(
                tracker,
                "https://github.com/example/project.git",
                write=write,
                client=client,
            )
    assert token.token == "scoped-lease"
    assert "scoped-lease" not in repr(token)


@pytest.mark.asyncio
async def test_broker_rejects_provider_ignoring_permission_reduction(
    tracker: Any,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "token": "overscoped",
                "expires_at": "2099-01-01T00:00:00Z",
                "repositories": [{"full_name": "example/project"}],
                "permissions": {"contents": "write", "metadata": "read"},
            },
        )

    with (
        patch(
            "preloop.services.publication_credentials.settings.github_app",
            SimpleNamespace(app_id="1", private_key="-----BEGIN KEY-----"),
        ),
        patch(
            "preloop.services.publication_credentials.jwt.encode",
            return_value="app-jwt",
        ),
    ):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(PublicationError, match="scope"):
                await mint_repository_lease(
                    tracker,
                    "https://github.com/example/project.git",
                    write=False,
                    client=client,
                )


def test_pat_cannot_be_relabelled_readonly(tracker: Any) -> None:
    tracker.auth_type = "api_token"
    tracker.resolved_api_key = "write-pat"
    with pytest.raises(PublicationError, match="PAT"):
        validate_publication_tracker(tracker)


def test_agent_receives_read_credential_without_db_fallback_or_post_push() -> None:
    executor = ContainerAgentExecutor(agent_type="codex", config={}, image="test")
    context = {
        "git_clone_config": {
            "publication_mode": "isolated",
            "repositories": [
                {
                    "tracker_id": "tracker",
                    "repository_url": "https://github.com/example/project.git",
                    "clone_path": "/workspace",
                }
            ],
        },
        "git_credentials_map": {
            "tracker": {
                "token": "read-lease",
                "tracker_type": "github",
                "permission": "read",
            }
        },
        "_git_target_branch": "preloop/issue-1",
        "_git_source_branch": "main",
        "trigger_project_id": "project",
    }
    with patch.object(
        executor,
        "_get_token_from_project",
        side_effect=AssertionError("Broad token lookup forbidden"),
    ):
        assert executor._resolve_repository_token(
            {"tracker_id": "tracker"}, context
        ) == ("read-lease", "github")
        context["git_credentials_map"]["tracker"]["permission"] = "write"
        with pytest.raises(ValueError, match="read-only"):
            executor._resolve_repository_token({"tracker_id": "tracker"}, context)
    script = executor._prepare_git_post_execution_commands(context)
    assert "git bundle create" in script
    assert " HEAD " in script
    for forbidden in ["git push", "curl", "PRELOOP_GIT_TOKEN", "contents:write"]:
        assert forbidden not in script
    context["checkpoint_env"] = {"PRELOOP_CHECKPOINT_ENABLED": "1"}
    checkpointed = executor._prepare_git_post_execution_commands(context)
    assert checkpointed.index("_preloop_checkpoint ||") < checkpointed.index(
        "git bundle create"
    )
    assert "prepublication_failed; exit 1" in checkpointed
    context[executor.GIT_API_TOKENS_CONTEXT_KEY] = {0: "write-secret"}
    with pytest.raises(ValueError, match="Write API tokens"):
        executor._apply_git_credential_env({}, context)


@pytest.mark.asyncio
async def test_orchestrator_publication_failure_changes_terminal_status() -> None:
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.db = MagicMock()
    orchestrator._publication_executor = SimpleNamespace(cleanup=AsyncMock())
    orchestrator._evidence_archive = b"archive"
    orchestrator._publication_verification = None
    orchestrator._publication_runtime_stopped = True
    orchestrator.execution_logger = MagicMock()
    orchestrator._isolated_publication_policy = SimpleNamespace(
        read_lease=PublicationLease(
            "read",
            "https://github.com/example/project.git",
            datetime.now(timezone.utc) + timedelta(minutes=10),
        )
    )
    result = {"status": "SUCCEEDED", "result": {"status": "success"}}
    with (
        patch(
            "preloop.services.publication_hosted_verifier.verify_hosted_publication",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    verification=object(), manifest={}, checks=(), image="toolchain"
                )
            ),
        ),
        patch(
            "preloop.services.trusted_publisher.read_publication_bundle",
            return_value=b"bundle",
        ),
        patch(
            "preloop.services.isolated_publication.finish_isolated_publication",
            new=AsyncMock(side_effect=PublicationError("verification not attested")),
        ),
        patch(
            "preloop.services.publication_credentials.revoke_repository_lease",
            new=AsyncMock(),
        ) as revoke,
    ):
        await orchestrator._finish_isolated_publication(result)
    assert result["status"] == "FAILED"
    assert result["error_message"] == "verification not attested"
    revoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_prepare_policy_never_resolves_broad_pat(tracker: Any) -> None:
    from preloop.services.isolated_publication import prepare_isolated_publication

    tracker.id = "tracker"
    flow = SimpleNamespace(account_id="account", id="flow")
    context = {
        "execution_id": "11111111-1111-4111-8111-111111111111",
        "git_clone_config": {
            "publication_mode": "isolated",
            "verification": {
                "mode": "gate",
                "image": "toolchain@sha256:" + "a" * 64,
                "profile": {
                    "profile_id": "test",
                    "version": "v1",
                    "always": [
                        {"id": "check", "command": "true", "reason": "required"}
                    ],
                },
            },
            "repositories": [
                {
                    "repository_url": "https://github.com/example/project.git",
                    "tracker_id": "tracker",
                }
            ],
        },
        "trigger_event_data": {},
    }
    read = PublicationLease(
        "read-only",
        "https://github.com/example/project.git",
        datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    client = AsyncMock()
    client.get.side_effect = [
        httpx.Response(
            200,
            json={"full_name": "example/project", "default_branch": "main"},
            request=httpx.Request("GET", "https://api.github.com"),
        ),
        httpx.Response(
            200,
            json={"object": {"sha": "b" * 40}},
            request=httpx.Request("GET", "https://api.github.com"),
        ),
        httpx.Response(404, request=httpx.Request("GET", "https://api.github.com")),
    ]
    with (
        patch("preloop.services.runner_service.resolve_runner_pool", return_value=None),
        patch(
            "preloop.services.isolated_publication.crud_tracker.get_by_id_and_account",
            return_value=tracker,
        ),
        patch("preloop.services.isolated_publication.validate_publication_tracker"),
        patch(
            "preloop.services.isolated_publication.mint_repository_lease",
            new=AsyncMock(return_value=read),
        ) as mint,
        patch("preloop.services.isolated_publication.httpx.AsyncClient") as factory,
    ):
        factory.return_value.__aenter__.return_value = client
        policy = await prepare_isolated_publication(MagicMock(), flow, context)
    assert mint.call_args.kwargs["write"] is False
    assert context["git_credentials_map"]["tracker"]["permission"] == "read"
    assert policy.repository_url == "https://github.com/example/project.git"
    assert "write" not in json.dumps(context)
    assert policy.expected_remote_sha is None


@pytest.mark.asyncio
async def test_agent_cannot_forge_trusted_publication_state_or_binding() -> None:
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.execution_logger = MagicMock()
    orchestrator._isolated_publication_policy = object()
    orchestrator._opened_pr = None
    orchestrator._capture_evidence_archive = AsyncMock()
    orchestrator._capture_workspace_snapshot = AsyncMock()
    executor = SimpleNamespace(
        get_result_artifact=AsyncMock(
            return_value={
                "status": "success",
                "trusted_publication": {"branch": "main"},
            }
        )
    )
    result = await orchestrator._capture_result_artifact(executor, "runtime")
    assert result == {"status": "success"}
    orchestrator._note_opened_pr(
        'PRELOOP_PR_OPENED {"url":"https://github.com/example/project/pull/1","branch":"main","provider":"github"}'
    )
    assert orchestrator._opened_pr is None


@pytest.mark.asyncio
async def test_failed_runtime_cleanup_never_issues_publication_credentials() -> None:
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator._isolated_publication_policy = SimpleNamespace(read_lease=object())
    orchestrator._publication_runtime_stopped = False
    orchestrator.execution_logger = MagicMock()
    result = {"status": "SUCCEEDED"}
    with (
        patch(
            "preloop.services.isolated_publication.finish_isolated_publication",
            new=AsyncMock(),
        ) as publish,
        patch(
            "preloop.services.publication_credentials.revoke_repository_lease",
            new=AsyncMock(),
        ),
    ):
        await orchestrator._finish_isolated_publication(result)
    publish.assert_not_awaited()
    assert result["status"] == "FAILED"
    assert "cleanup" in result["error_message"]
