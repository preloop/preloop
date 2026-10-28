"""Native review credentials must never become publication authority."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from preloop.agents.container import ContainerAgentExecutor
from preloop.api.auth.key_scopes import (
    enforce_api_key_route_scope,
    enforce_runtime_api_key_scope,
    api_key_allowed_on_channel,
)
from preloop.config import settings
from preloop.models.schemas.flow import GitCloneConfig
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.services.read_only_review import prepare_read_only_review
from preloop.services.trusted_publisher import PublicationLease


@pytest.fixture
def review() -> tuple[Any, Any]:
    binding = SimpleNamespace(
        flow_id=uuid4(),
        account_id=uuid4(),
        project_id=uuid4(),
        tracker_id=uuid4(),
        head_sha="a" * 40,
        base_branch="main",
        tracker_type="github",
        repository_slug="example/project",
    )
    config = {
        "enabled": True,
        "publication_mode": "read_only",
        "repositories": [
            {
                "project_id": str(binding.project_id),
                "tracker_id": str(binding.tracker_id),
            }
        ],
    }
    flow = SimpleNamespace(
        id=binding.flow_id,
        account_id=binding.account_id,
        git_clone_config=config,
        runner_pool="server",
        allowed_mcp_tools=[],
        allowed_mcp_servers=[],
    )
    return binding, flow


@pytest.mark.parametrize(
    "extra",
    [
        {"create_pull_request": True},
        {"report_publication": {"enabled": True}},
        {"publication_approval": True},
        {"enabled": False},
        {"repositories": []},
        {"verification": {"mode": "gate"}},
    ],
)
def test_conflicting_publication_configuration_rejected(
    review: tuple[Any, Any], extra: dict[str, Any]
) -> None:
    _, flow = review
    with pytest.raises((ValueError, ValidationError)):
        GitCloneConfig.model_validate({**flow.git_clone_config, **extra})


@pytest.mark.asyncio
async def test_scoped_lease_binds_exact_repo_and_revision_without_broad_resolver(
    review: tuple[Any, Any],
) -> None:
    binding, flow = review
    lease = PublicationLease(
        "only-read",
        "https://github.com/example/project.git",
        datetime.now(timezone.utc) + timedelta(hours=1),
    )
    orch = FlowExecutionOrchestrator(MagicMock(), str(flow.id), {}, AsyncMock())
    orch.flow = flow
    orch.execution_log = MagicMock()
    context = {
        "git_clone_config": flow.git_clone_config,
        "git_credentials_map": {"broad": {"token": "must-disappear"}},
    }
    with (
        patch(
            "preloop.models.crud.crud_ci_execution.authorize_dispatch",
            return_value=binding,
        ),
        patch(
            "preloop.models.crud.crud_tracker.get_by_id_and_account",
            return_value=MagicMock(),
        ),
        patch(
            "preloop.services.read_only_review.mint_repository_lease",
            AsyncMock(return_value=lease),
        ) as mint,
        patch.object(orch, "_get_tracker_credentials_by_id", AsyncMock()) as broad,
        patch.object(
            orch, "_attach_trigger_tracker_credentials", AsyncMock()
        ) as trigger,
    ):
        await orch._attach_git_credentials(context)
    assert mint.call_args.kwargs["write"] is False
    assert mint.call_args.args[1] == lease.repository_url
    broad.assert_not_awaited()
    trigger.assert_not_awaited()
    assert list(context["git_credentials_map"]) == [lease.repository_url]
    repository = context["git_clone_config"]["repositories"][0]
    assert repository["commit"] == binding.head_sha
    assert "repository_url" not in flow.git_clone_config["repositories"][0]
    executor = ContainerAgentExecutor.__new__(ContainerAgentExecutor)
    executor.logger = MagicMock()
    assert executor._resolve_repository_token(repository, context)[0] == "only-read"
    # Exercise the final native clone generator with conflicting moving-PR
    # metadata. The controller pin must survive branch autodetection (#2095).
    context["trigger_event_data"] = {
        "payload": {
            "pull_request": {
                "number": 7,
                "head": {"sha": "b" * 40, "ref": "moving-pr"},
                "base": {"ref": "moving-default"},
            }
        }
    }
    command = executor._prepare_git_clone_command(context)
    assert "git clone --no-checkout https://github.com/example/project.git" in command
    assert f"git fetch --filter=blob:none origin {binding.head_sha}" in command
    assert f"git checkout --force {binding.head_sha}" in command
    assert f'"$(git rev-parse HEAD)" != {binding.head_sha}' in command
    assert "b" * 40 not in command
    assert "git clone -b" not in command
    assert executor._wrap_clone_with_workspace_restore(command, context) == command
    with patch.object(executor, "_get_token_from_project") as fallback:
        with pytest.raises(ValueError):
            executor._resolve_repository_token(
                {
                    **repository,
                    "repository_url": "https://github.com/other/project.git",
                },
                context,
            )
        with pytest.raises(ValueError):
            executor._prepare_git_clone_command(
                {
                    **context,
                    "git_credentials_map": {
                        "https://github.com/other/project.git": {"token": "wrong"}
                    },
                }
            )
        context["git_credentials_map"] = {}
        context["git_clone_config"]["publication_mode"] = "legacy"
        with pytest.raises(ValueError):
            executor._resolve_repository_token(repository, context)
        with pytest.raises(ValueError):
            executor._prepare_git_clone_command(context)
        with pytest.raises(ValueError):
            executor._prepare_init_commands(context)
        with pytest.raises(ValueError):
            executor._prepare_init_commands({**context, "git_clone_config": {}})
        with pytest.raises(ValueError):
            executor._prepare_git_clone_command({**context, "git_clone_config": {}})
    fallback.assert_not_called()
    orch._read_only_review_execution = True
    flow.git_clone_config = {"publication_mode": "legacy"}
    orch._tracker_client = MagicMock()
    assert await orch._get_tracker_client_for_status() is None
    assert await orch._publication_tracker_clients() == []
    # Agent-supplied success/bundle cannot cause either publication path.
    with patch(
        "preloop.services.isolated_publication.finish_isolated_publication", AsyncMock()
    ) as finish:
        orch._attach_product_evidence_records = MagicMock()
        await orch._finish_isolated_publication(
            {
                "status": "SUCCEEDED",
                "result": {"trusted_publication": {"bundle": "forged"}},
            }
        )
    finish.assert_not_awaited()
    context["git_clone_config"] = {
        "publication_mode": "isolated",
        "create_pull_request": True,
    }
    assert executor._prepare_git_post_execution_commands(context) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["tracker", "url", "restore", "mcp", "revoked", "human"]
)
async def test_invalid_authority_fails_before_lease(
    review: tuple[Any, Any], change: str
) -> None:
    binding, flow = review
    context = {}
    if change == "tracker":
        flow.git_clone_config["repositories"][0]["tracker_id"] = str(uuid4())
    elif change == "url":
        flow.git_clone_config["repositories"][0]["repository_url"] = (
            "https://github.com/foreign/repo"
        )
    elif change == "restore":
        context["workspace_restore_archive"] = b"old-workspace"
    elif change == "mcp":
        flow.allowed_mcp_tools = [{"name": "create_issue"}]
    with (
        patch(
            "preloop.models.crud.crud_ci_execution.authorize_dispatch",
            return_value=None if change == "human" else binding,
            side_effect=PermissionError("revoked") if change == "revoked" else None,
        ),
        patch(
            "preloop.models.crud.crud_tracker.get_by_id_and_account",
            return_value=MagicMock(),
        ),
        patch(
            "preloop.services.read_only_review.mint_repository_lease", AsyncMock()
        ) as mint,
    ):
        with pytest.raises((ValueError, PermissionError)):
            await prepare_read_only_review(MagicMock(), flow, MagicMock(), context)
    mint.assert_not_awaited()


@pytest.mark.parametrize("mode", ["off", "audit", "enforce"])
@pytest.mark.parametrize("scopes", [[], ["*"], ["mcp:read", "mcp:write"]])
def test_runtime_marker_denies_rest_even_with_legacy_scopes(
    monkeypatch: pytest.MonkeyPatch, mode: str, scopes: list[str]
) -> None:
    monkeypatch.setattr(settings, "api_key_scope_enforcement", mode)
    key = SimpleNamespace(context_data={"read_only_review": True}, scopes=scopes)
    request = Request(
        {"type": "http", "method": "POST", "path": "/api/v1/trackers", "headers": []}
    )
    with pytest.raises(HTTPException) as error:
        enforce_api_key_route_scope(key, request)
    assert error.value.status_code == 403
    for channel in ("console", "desktop"):
        assert not api_key_allowed_on_channel(key, channel)
    with pytest.raises(HTTPException) as runtime_error:
        enforce_runtime_api_key_scope(key)
    assert runtime_error.value.status_code == 403
    from preloop.api.auth.jwt import (
        _authenticate_with_api_key,
        get_user_from_token_if_valid_sync,
    )

    with pytest.raises(HTTPException) as auth_error:
        _authenticate_with_api_key(MagicMock(), key)
    assert auth_error.value.status_code == 403
    with patch("preloop.api.auth.jwt.crud_api_key.get_by_key", return_value=key):
        assert get_user_from_token_if_valid_sync("review-token", MagicMock()) is None


@pytest.mark.asyncio
async def test_native_result_persists_and_owned_callback_remains_available(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A local helper result completes without MCP or any publishing credential."""
    import json
    from preloop.models import crud
    from preloop.schemas.ci_principal import CiAction
    from preloop.schemas.ci_subscription import CiSubscriptionCreate
    from preloop.services import publication_credentials
    from tests.api.test_ci_principal import ci_resources, provision
    from tests.api.test_ci_execution import review_binding

    resources = ci_resources.__wrapped__(db_session)
    _, _, flow, _ = resources
    flow.git_clone_config = {**flow.git_clone_config, "publication_mode": "read_only"}
    db_session.commit()
    monkeypatch.setattr(
        publication_credentials, "validate_publication_tracker", lambda tracker: None
    )
    _, _, token = provision(db_session, resources)
    principal = crud.crud_ci_principal.authenticate(db_session, token=token)
    endpoint, _ = crud.crud_ci_subscription.create(
        db_session,
        context=principal,
        payload=CiSubscriptionCreate(url="https://example.com/completed"),
    )
    execution = crud.crud_ci_execution.create(
        db_session, context=principal, binding=review_binding(principal), event={}
    )
    orch = FlowExecutionOrchestrator(
        db_session, str(flow.id), execution.trigger_event_details, AsyncMock()
    )
    orch.execution_log = execution
    result_file = tmp_path / "result.json"
    result_file.write_text(
        json.dumps({"review": "Native helper reviewed the exact admitted revision"})
    )
    contexts = []
    monkeypatch.setattr(settings, "flow_artifact_direct_upload", True)

    async def complete(context: dict[str, Any]) -> tuple[dict[str, Any], str]:
        contexts.append(context)
        # A second context preparation must retain both leases for cleanup.
        await orch._attach_git_credentials(context)
        await orch._update_execution_log(status="RUNNING")
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from preloop.api.endpoints.flow_artifacts import router
        from preloop.models.db.session import get_db_session
        from tests.services.test_flow_artifacts_db import _evidence_body

        from preloop.api.endpoints import openai_gateway
        from preloop.api.deps import get_budget_enforcer

        app = FastAPI()
        app.include_router(router)
        app.include_router(openai_gateway.router, prefix="/v1")
        app.dependency_overrides[get_db_session] = lambda: db_session
        app.dependency_overrides[get_budget_enforcer] = lambda: None
        client = TestClient(app)
        service = MagicMock(
            response_warning=None, last_usage_id=None, codex_turn_state=None
        )
        service.create_response.return_value = {
            "id": "local-response",
            "status": "completed",
        }
        with patch.object(
            openai_gateway, "OpenAIGatewayService", return_value=service
        ) as gateway:
            response = client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer " + context["account_api_token"]},
                json={"model": "synthetic", "input": "local probe"},
            )
        assert response.status_code == 200, response.text
        assert (
            gateway.call_args.args[1].api_key.context_data["read_only_review"] is True
        )
        # The inference exception must not leak into helpers shared by browser
        # ingestion, security screening, native approval or account channels.
        from preloop.services.model_gateway_auth import authenticate_bearer_token
        from preloop.api.gateway_auth_dependency import get_browser_step_auth_context
        from preloop.services.model_gateway_errors import ModelGatewayAPIError
        from preloop.api.auth.jwt import authenticate_runtime_bearer_token

        token = context["account_api_token"]
        assert await authenticate_bearer_token(token, db_session) is None
        with pytest.raises(ModelGatewayAPIError):
            await get_browser_step_auth_context("Bearer " + token, db_session)
        with pytest.raises(HTTPException) as denied:
            authenticate_runtime_bearer_token(db_session, token)
        assert denied.value.status_code == 403
        url = f"/flows/executions/{execution.id}/artifacts"
        headers = {
            "Authorization": "Bearer "
            + context["evidence_env"]["PRELOOP_EVIDENCE_PUT_TOKEN"]
        }
        response = client.put(
            url,
            headers=headers,
            content=_evidence_body(result=result_file.read_bytes()),
        )
        assert response.status_code == 200, response.text
        assert (
            client.put(
                f"/flows/executions/{uuid4()}/artifacts",
                headers=headers,
                content=b"foreign",
            ).status_code
            == 403
        )
        assert client.get(url, headers=headers).status_code == 403
        assert (
            client.put(
                url,
                headers={"Authorization": "Bearer " + context["account_api_token"]},
                content=b"wrong-token",
            ).status_code
            == 401
        )
        return {
            "status": "SUCCEEDED",
            "result": json.loads(result_file.read_text()),
            "output_summary": "review complete",
        }, "local-stand-in"

    lease = PublicationLease(
        "only-read",
        f"https://github.com/{principal.repository_slug}.git",
        datetime.now(timezone.utc) + timedelta(hours=1),
    )
    with (
        patch(
            "preloop.services.read_only_review.mint_repository_lease",
            AsyncMock(return_value=lease),
        ) as mint,
        patch(
            "preloop.services.publication_credentials.revoke_repository_lease",
            AsyncMock(),
        ) as revoke,
        patch.object(orch, "_run_agent_with_retries", side_effect=complete),
        patch.object(orch, "_get_tracker_credentials_by_id", AsyncMock()) as broad,
    ):
        await orch.run()
    db_session.refresh(execution)
    assert execution.status == "SUCCEEDED", execution.error_message
    assert execution.result["review"] == json.loads(result_file.read_text())["review"]
    assert contexts[0]["read_only_review"] is True
    assert mint.call_args.kwargs["write"] is False
    broad.assert_not_awaited()
    assert mint.await_count == 2
    assert revoke.await_count == 2
    assert crud.crud_ci_execution.get(
        db_session,
        context=principal,
        execution_id=execution.id,
        action=CiAction.READ_RESULT,
    ).result["review"]
    payload = crud.crud_ci_subscription.callback_payload(
        db_session,
        endpoint=endpoint,
        account_id=flow.account_id,
        event_type="flow.execution.finished",
        subject_id=execution.id,
    )
    assert payload["result_ready"] is True
    assert payload["head_sha"] == "a" * 40


@pytest.mark.asyncio
async def test_legacy_mcp_dependency_cannot_drop_review_marker() -> None:
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from preloop.services.mcp_http import get_user_context_for_mcp

    user = MagicMock(spec=AuthenticatedUser)
    user.access_token = SimpleNamespace(
        api_key=SimpleNamespace(context_data={"read_only_review": True}, scopes=["*"])
    )
    request = Request(
        {"type": "http", "method": "POST", "path": "/mcp", "headers": [], "user": user}
    )
    with pytest.raises(HTTPException) as error:
        await get_user_context_for_mcp(request, MagicMock())
    assert error.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["expired", "empty", "repository"])
async def test_invalid_lease_never_enters_context(
    review: tuple[Any, Any], invalid: str
) -> None:
    binding, flow = review
    lease = PublicationLease(
        "" if invalid == "empty" else "read-lease",
        "https://github.com/foreign/repo.git"
        if invalid == "repository"
        else "https://github.com/example/project.git",
        datetime.now(timezone.utc) + timedelta(hours=-1 if invalid == "expired" else 1),
    )
    context: dict[str, Any] = {}
    with (
        patch(
            "preloop.models.crud.crud_ci_execution.authorize_dispatch",
            return_value=binding,
        ),
        patch(
            "preloop.models.crud.crud_tracker.get_by_id_and_account",
            return_value=MagicMock(),
        ),
        patch(
            "preloop.services.read_only_review.mint_repository_lease",
            AsyncMock(return_value=lease),
        ),
    ):
        with pytest.raises(ValueError, match="invalid or expired"):
            await prepare_read_only_review(MagicMock(), flow, MagicMock(), context)
    assert "git_credentials_map" not in context
