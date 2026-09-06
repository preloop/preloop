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


@pytest.mark.parametrize("provider", ["github", "gitlab"])
def test_template_provider_comes_from_tracker_not_checkout_markers(tmp_path, provider):
    import shlex
    from preloop.utils.pr_metadata import repository_template

    for directory in (
        ".github/PULL_REQUEST_TEMPLATE",
        ".gitlab/merge_request_templates",
    ):
        path = tmp_path / directory
        path.mkdir(parents=True)
        (path / "custom.md").write_text(directory)
    executor = ContainerAgentExecutor("codex", {}, image="example/harness:stable")
    context = {
        "git_clone_config": {
            "enabled": False,
            "create_pull_request": True,
            "repositories": [{"tracker_id": "tracker", "clone_path": str(tmp_path)}],
        },
        "git_credentials_map": {
            "tracker": {"tracker_type": provider, "token": "read-only"}
        },
    }
    command = executor._prepare_init_commands(context)
    arguments = shlex.split(command)
    assert arguments[-1] == provider
    assert any("provider = sys.argv[3]" in argument for argument in arguments)
    name, template = repository_template(tmp_path, provider=arguments[-1])
    assert name == (
        ".gitlab/merge_request_templates/custom.md"
        if provider == "gitlab"
        else ".github/PULL_REQUEST_TEMPLATE/custom.md"
    )
    assert template


@pytest.mark.asyncio
async def test_recovery_is_committed_before_runtime_may_be_destroyed():
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.db = MagicMock()
    orchestrator.execution_log = SimpleNamespace(id="execution")
    orchestrator.execution_logger = MagicMock()
    orchestrator._evidence_archive = b"evidence"
    orchestrator._workspace_snapshot = b"workspace"
    await orchestrator._persist_isolated_recovery()
    assert orchestrator.execution_log.evidence_archive == b"evidence"
    assert orchestrator.execution_log.workspace_snapshot == b"workspace"
    orchestrator.db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_failed_recovery_commit_does_not_authorize_removal():
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.db = MagicMock()
    orchestrator.db.commit.side_effect = RuntimeError("local database unavailable")
    orchestrator.execution_log = SimpleNamespace(id="execution")
    orchestrator.execution_logger = MagicMock()
    orchestrator._evidence_archive = b"evidence"
    orchestrator._workspace_snapshot = b"workspace"
    with pytest.raises(RuntimeError, match="database unavailable"):
        await orchestrator._persist_isolated_recovery()
    orchestrator.db.rollback.assert_called_once()
    orchestrator.execution_logger.log_milestone.assert_not_called()


@pytest.mark.asyncio
async def test_private_restore_preserves_snapshot_without_provider_reresolution():
    from preloop.services.isolated_publication import prepare_isolated_publication

    restored = object()
    flow = SimpleNamespace(account_id="account", id="flow")
    context = {
        "execution_id": "execution",
        "git_clone_config": {"publication_mode": "isolated"},
    }
    with (
        patch(
            "preloop.services.runner_service.resolve_runner_pool",
            return_value="private",
        ),
        patch(
            "preloop.services.private_publication.restore_private_publication",
            new=AsyncMock(return_value=restored),
        ) as restore,
        patch(
            "preloop.services.isolated_publication.mint_repository_lease",
            new=AsyncMock(),
        ) as mint,
    ):
        assert (
            await prepare_isolated_publication(MagicMock(), flow, context) is restored
        )
    restore.assert_awaited_once()
    mint.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
async def test_private_success_requires_trusted_receipt_not_agent_claim(accepted):
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.db = MagicMock()
    orchestrator.execution_logger = MagicMock()
    orchestrator._publication_runtime_stopped = False
    orchestrator._isolated_publication_policy = SimpleNamespace(
        private=True,
        execution_id="execution",
        account_id="account",
        read_lease=object(),
    )
    receipt = (
        {"url": "https://github.com/example/project/pull/1", "head_sha": "a" * 40}
        if accepted
        else None
    )
    result = {
        "status": "SUCCEEDED",
        "result": {"trusted_publication": {"url": "forged"}},
    }
    execution = object()
    with (
        patch(
            "preloop.services.private_publication.trusted_private_receipt",
            return_value=receipt,
        ) as read,
        patch(
            "preloop.services.flow_orchestrator.crud_flow_execution.get",
            return_value=execution,
        ) as get,
        patch(
            "preloop.services.publication_credentials.revoke_repository_lease",
            new=AsyncMock(),
        ),
        patch(
            "preloop.services.publication_hosted_verifier.verify_hosted_publication",
            new=AsyncMock(),
        ) as verify,
        patch(
            "preloop.services.isolated_publication.finish_isolated_publication",
            new=AsyncMock(),
        ) as publish,
    ):
        await orchestrator._finish_isolated_publication(result)
    get.assert_called_once_with(
        orchestrator.db, id="execution", account_id="account", refresh=True
    )
    read.assert_called_once_with(execution)
    verify.assert_not_awaited()
    publish.assert_not_awaited()
    assert result["status"] == ("SUCCEEDED" if accepted else "FAILED")
    if accepted:
        assert result["result"]["trusted_publication"] == receipt
    else:
        assert "trusted_publication" not in result["result"]


@pytest.mark.asyncio
async def test_helper_then_controller_revocation_is_idempotent():
    from preloop.services.publication_credentials import revoke_repository_lease

    replies = iter([204, 401])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(next(replies)))
    ) as client:
        lease = PublicationLease(
            "known-token",
            "https://github.com/example/project.git",
            datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        await revoke_repository_lease(lease, client)
        await revoke_repository_lease(lease, client)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [403, 500])
async def test_revocation_authorization_or_provider_failure_is_not_success(code):
    from preloop.services.publication_credentials import revoke_repository_lease

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(code))
    ) as client:
        lease = PublicationLease(
            "known-token",
            "https://github.com/example/project.git",
            datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        with pytest.raises(PublicationError, match="revocation failed"):
            await revoke_repository_lease(lease, client)


@pytest.mark.parametrize("phase", ["verifying", "complete"])
def test_terminal_crud_preserves_current_private_state_over_stale_result(phase):
    from uuid import uuid4
    from preloop.models import schemas
    from preloop.models.crud import crud_flow_execution

    receipt = {"url": "https://github.com/example/project/pull/1", "head_sha": "a" * 40}
    state = {"phase": phase, "nonce": "b" * 64, "receipt": receipt}
    db = MagicMock()
    db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = (
        {"_private_publication": state, "trusted_publication": receipt},
    )
    execution = SimpleNamespace(
        id=uuid4(), result={"_private_publication": {"phase": "agent"}}
    )
    crud_flow_execution.update(
        db,
        db_obj=execution,
        obj_in=schemas.FlowExecutionUpdate(
            result={
                "summary": "done",
                "_private_publication": {"phase": "complete"},
                "trusted_publication": {"url": "forged"},
            }
        ),
    )
    assert execution.result["_private_publication"] == state
    assert execution.result["summary"] == "done"
    if phase == "complete":
        assert execution.result["trusted_publication"] == receipt
    else:
        assert "trusted_publication" not in execution.result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference,diagnostic",
    [
        ("hosted-job", "runtime retained"),
        (
            "runner:queued:original-pool:11111111-1111-4111-8111-111111111111",
            "Queued isolated execution",
        ),
    ],
)
async def test_recovery_without_replay_authority_fails_closed(reference, diagnostic):
    from preloop.services.flow_execution_runner import resume_existing_execution

    orchestrator = MagicMock()
    orchestrator.flow = SimpleNamespace(
        agent_type="codex",
        agent_config={},
        git_clone_config={"publication_mode": "isolated"},
    )
    orchestrator.execution_log = SimpleNamespace(id="execution", result={})
    orchestrator._update_execution_log = AsyncMock()
    orchestrator._notify_terminal = AsyncMock()
    orchestrator._monitor_agent_execution = AsyncMock()
    with patch("preloop.agents.create_agent_executor") as create:
        await resume_existing_execution(orchestrator, reference)
    create.assert_not_called()
    orchestrator._monitor_agent_execution.assert_not_awaited()
    assert orchestrator._update_execution_log.call_args.kwargs["status"] == "FAILED"
    assert (
        diagnostic
        in orchestrator._update_execution_log.call_args.kwargs["error_message"]
    )


@pytest.mark.asyncio
async def test_private_recovered_monitor_restores_policy_and_finalizes_before_status():
    from uuid import uuid4
    from preloop.services.flow_execution_runner import resume_existing_execution

    orchestrator = MagicMock()
    orchestrator.flow = SimpleNamespace(
        account_id=uuid4(),
        agent_type="codex",
        agent_config={},
        git_clone_config={"publication_mode": "isolated"},
    )
    orchestrator.agent_type = "codex"
    orchestrator.execution_log = SimpleNamespace(
        id=uuid4(), result={"_private_publication": {"phase": "verifying"}}
    )
    events = []

    async def monitor(*args):
        events.append("monitor")
        return {"status": "SUCCEEDED", "result": {}}

    async def finish(result):
        events.append("finalize")
        result["status"] = "FAILED"

    async def update(**kwargs):
        events.append(kwargs["status"])

    orchestrator._monitor_agent_execution = AsyncMock(side_effect=monitor)
    orchestrator._finish_isolated_publication = AsyncMock(side_effect=finish)
    orchestrator._replay_persisted_runner_logs = AsyncMock()
    orchestrator._update_execution_log = AsyncMock(side_effect=update)
    orchestrator._notify_terminal = AsyncMock()
    executor = SimpleNamespace(cleanup=AsyncMock())
    with (
        patch(
            "preloop.services.private_publication.load_private_monitoring_policy",
            return_value=object(),
        ) as restore,
        patch(
            "preloop.agents.create_executor_for_execution",
            return_value=executor,
        ) as create,
    ):
        await resume_existing_execution(
            orchestrator, f"runner:{uuid4()}:{orchestrator.execution_log.id}"
        )
    restore.assert_called_once()
    create.assert_called_once()
    assert events == ["monitor", "finalize", "FAILED"]
