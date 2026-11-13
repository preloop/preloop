"""Hosted read-only review: the native hook credential is proven before launch.

Drives the real path (#2264): a restricted CI identity triggers a read-only
Codex review through the CI route, product code mints the execution and its
runtime key, and the key the in-container hook would present is checked
against ``POST /api/v1/agents/permission-check``. An invalid binding must end
the run before the container (and so any model call) starts.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from preloop.agents.errors import AgentStartError
from preloop.api.endpoints import agent_permission, flows
from preloop.api.middleware import ci_auth
from preloop.models import crud, models
from preloop.models.db.session import get_db_session
from preloop.services.flow_failure_category import (
    FAILURE_CATEGORY_HOOK_BINDING_INVALID,
)
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from tests.api.test_ci_execution import ci_resources as create_ci_resources
from tests.api.test_ci_principal import provision
from tests.services.test_ci_execution_admission import mock_provider, provider_payload

PERMISSION_CHECK_URL = "/api/v1/agents/permission-check"
HOOK_CALL = {
    "source": "codex_cli",
    "tool_name": "Bash",
    "tool_input": {"command": "pwd && ls -la /workspace"},
    "session_id": "synthetic-codex-session",
    "cwd": "/workspace",
}


@pytest.fixture
def review_flow(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, ...]:
    """A hosted read-only Codex review flow with its restricted CI identity."""
    from preloop.services import publication_credentials

    monkeypatch.setattr(
        publication_credentials, "validate_publication_tracker", lambda tracker: None
    )
    resources = create_ci_resources.__wrapped__(db_session)
    flow = resources[2]
    flow.git_clone_config = {**flow.git_clone_config, "publication_mode": "read_only"}
    flow.agent_type = "codex"
    flow.agent_config = {}
    db_session.commit()
    return resources


@pytest.fixture
def permission_client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The real permission-check route on the test transaction."""
    factory = sessionmaker(
        bind=db_session.connection(), join_transaction_mode="create_savepoint"
    )
    monkeypatch.setattr(agent_permission, "get_session_factory", lambda: factory)
    app = FastAPI()
    app.include_router(agent_permission.router, prefix="/api/v1")
    return TestClient(app)


def _ci_client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    def sessions() -> Any:
        yield db_session

    app = FastAPI()
    app.include_router(flows.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = sessions
    monkeypatch.setattr(ci_auth, "get_auth_db_session", sessions)
    app.add_middleware(ci_auth.RestrictedCiAuthMiddleware)
    return TestClient(app)


def _trigger_and_mint(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    resources: tuple[Any, ...],
) -> tuple[FlowExecutionOrchestrator, str]:
    """Trigger through the CI route, then mint the key as the launch does."""
    from preloop.services.flow_trigger_service import FlowTriggerService

    _, _, ci_token = provision(db_session, resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=ci_token)
    assert context is not None
    mock_provider(monkeypatch, context, payload=provider_payload(context))
    monkeypatch.setattr(
        "preloop.sync.services.event_bus.get_nats_client",
        AsyncMock(return_value=Mock()),
    )
    dispatch = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "_start_flow_execution", dispatch)
    response = _ci_client(db_session, monkeypatch).post(
        f"/api/v1/flows/{resources[2].id}/trigger",
        headers={"Authorization": f"Bearer {ci_token}"},
        json={"pr_number": 7, "head_sha": "a" * 40},
    )
    assert response.status_code in (200, 201, 202), response.text
    dispatch.assert_awaited_once()
    execution = dispatch.await_args.kwargs["precreated_execution"]
    flow = crud.crud_flow.get(db_session, id=execution.flow_id)
    orch = FlowExecutionOrchestrator(db_session, flow.id, {}, AsyncMock())
    orch.flow = flow
    orch.execution_log = execution
    orch.agent_type = "codex"
    token, key_id = orch._create_temporary_api_token()
    assert token and key_id
    return orch, token


def _launch_context(token: str) -> dict[str, Any]:
    return {
        "agent_type": "codex",
        "agent_config": {},
        "account_api_token": token,
        "read_only_review": True,
    }


def _check(client: TestClient, token: str) -> Any:
    allow = AsyncMock(return_value=("allow", "No rule applies.", "req-1", False))
    with patch.object(agent_permission, "request_agent_permission", allow):
        return client.post(
            PERMISSION_CHECK_URL,
            json=HOOK_CALL,
            headers={"Authorization": f"Bearer {token}"},
        )


def test_minted_review_key_passes_preflight_and_permission_check(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    review_flow: tuple[Any, ...],
    permission_client: TestClient,
) -> None:
    orch, token = _trigger_and_mint(db_session, monkeypatch, review_flow)
    key = crud.crud_api_key.get_by_key(db_session, key=token)
    assert key.context_data["read_only_review"] is True
    assert key.scopes == ["mcp:read"]
    orch._refuse_invalid_hook_binding(_launch_context(token))
    response = _check(permission_client, token)
    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "allow"


def _widen_scope(db: Session, key: models.ApiKey) -> None:
    key.scopes = ["mcp:read", "mcp:write"]


def _attach_agent(db: Session, key: models.ApiKey) -> None:
    key.context_data = {**key.context_data, "managed_agent_id": str(key.id)}


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [_widen_scope, _attach_agent])
async def test_invalid_binding_ends_run_before_launch_and_is_refused(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    review_flow: tuple[Any, ...],
    permission_client: TestClient,
    corrupt: Any,
) -> None:
    orch, token = _trigger_and_mint(db_session, monkeypatch, review_flow)
    key = crud.crud_api_key.get_by_key(db_session, key=token)
    corrupt(db_session, key)
    db_session.commit()

    response = _check(permission_client, token)
    assert response.status_code in (401, 403), response.text

    executor = MagicMock()
    executor.start = AsyncMock(return_value="never")
    executor.cleanup = AsyncMock()
    monkeypatch.setattr(
        "preloop.services.flow_orchestrator.create_executor_for_execution",
        lambda *args, **kwargs: executor,
    )
    monkeypatch.setattr(
        "preloop.services.flow_orchestrator.crud_flow_execution.admit_runtime_start",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(orch, "_refuse_hosted_approval_gap", lambda context: None)
    with pytest.raises(AgentStartError) as exc:
        await orch._start_agent_session(_launch_context(token))
    assert exc.value.category == FAILURE_CATEGORY_HOOK_BINDING_INVALID
    assert "binding is invalid for this execution" in str(exc.value)
    # The container never starts, so no model call can be made and no
    # startup receipt can be observed.
    executor.start.assert_not_awaited()
    assert orch._startup_receipt is None
    stored = crud.crud_flow_execution.get(db_session, id=orch.execution_log.id)
    assert stored.startup_receipt is None


def test_read_only_review_without_runtime_credential_is_refused(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    review_flow: tuple[Any, ...],
) -> None:
    orch, _ = _trigger_and_mint(db_session, monkeypatch, review_flow)
    context = _launch_context("")
    with pytest.raises(AgentStartError) as exc:
        orch._refuse_invalid_hook_binding(context)
    assert exc.value.category == FAILURE_CATEGORY_HOOK_BINDING_INVALID


def test_shell_locked_codex_and_other_harnesses_are_not_checked() -> None:
    orch = FlowExecutionOrchestrator(MagicMock(), MagicMock(), {}, AsyncMock())
    orch.flow = MagicMock(agent_type="codex")
    orch.agent_type = "codex"
    locked = _launch_context("not-a-key")
    locked["agent_config"] = {"sandbox_type": "read-only"}
    with patch(
        "preloop.api.endpoints.agent_permission.resolve_permission_identity_in_session"
    ) as resolve:
        orch._refuse_invalid_hook_binding(locked)
        orch.agent_type = "opencode"
        orch._refuse_invalid_hook_binding(_launch_context("not-a-key"))
    resolve.assert_not_called()
