"""Read-only native review on a private runner Codex host profile."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from preloop.agents.remote_runner import RemoteRunnerExecutor
from preloop.services import read_only_review as ror
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.services.host_exec import (
    finalize_runner_completion,
    runner_has_host_exec_profile,
)
from preloop.services.read_only_review import (
    HOST_READ_ONLY_LEASE_KEY,
    HOST_READ_ONLY_RESULT_KEY,
    prepare_read_only_review,
    read_only_private_runner,
    validate_read_only_executor,
    validate_read_only_flow,
)
from preloop.services.trusted_publisher import PublicationLease

SHA = "c" * 40
URL = "https://github.com/example/project.git"


@pytest.fixture
def review() -> tuple[Any, Any]:
    binding = SimpleNamespace(
        flow_id=uuid4(),
        account_id=uuid4(),
        project_id=uuid4(),
        tracker_id=uuid4(),
        head_sha=SHA,
        base_branch="main",
        tracker_type="github",
        repository_slug="example/project",
    )
    flow = SimpleNamespace(
        id=binding.flow_id,
        account_id=binding.account_id,
        git_clone_config={
            "enabled": True,
            "publication_mode": "read_only",
            "repositories": [
                {
                    "project_id": str(binding.project_id),
                    "tracker_id": str(binding.tracker_id),
                }
            ],
        },
        runner_pool="review-hosts",
        agent_type="codex",
        agent_config={"host_exec_profile": "codex-review"},
        allowed_mcp_tools=[],
        allowed_mcp_servers=[],
    )
    return binding, flow


def _lease() -> PublicationLease:
    return PublicationLease(
        "read-only-token", URL, datetime.now(timezone.utc) + timedelta(hours=1)
    )


def test_private_runner_host_profile_is_admitted(review: tuple[Any, Any]) -> None:
    _, flow = review
    assert read_only_private_runner(flow)
    validate_read_only_flow(flow)
    validate_read_only_executor(
        flow,
        {
            "agent_type": "codex",
            "agent_config": {"host_exec_profile": "codex-review"},
            "host_exec_profile": "codex-review",
            "trigger_event_data": {"_runner": "review-hosts"},
        },
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"runner_pool": "server"},
        {"runner_pool": "auto"},
        {"runner_pool": None},
        {"agent_config": {}},
        {"agent_type": "openhands"},
        {"agent_type": "claude_code"},
        {
            "agent_config": {
                "host_exec_profile": "codex-review",
                "execution_path": "persistent",
            }
        },
        {"agent_config": {"host_exec_profile": "codex-review", "harness": "codex"}},
        {
            "agent_config": {
                "host_exec_profile": "codex-review",
                "workspace": {"kind": "authorized_directory", "id": "repo"},
            }
        },
        {"allowed_mcp_tools": [{"name": "get_issue"}]},
        {"allowed_mcp_servers": ["preloop"]},
    ],
)
def test_private_runner_rejects_unsupported_shapes(
    review: tuple[Any, Any], changes: dict[str, Any]
) -> None:
    _, flow = review
    for key, value in changes.items():
        setattr(flow, key, value)
    with pytest.raises(ValueError):
        validate_read_only_flow(flow)


@pytest.mark.parametrize(
    "context",
    [
        {"host_exec_profile": "other-profile"},
        {"agent_config": {"host_exec_profile": "other-profile"}},
        {"agent_config": {"execution_path": "persistent"}},
        {"trigger_event_data": {"_runner": "another-pool"}},
        {"trigger_event_data": {"payload": {"_runner": "server"}}},
        {"host_exec_resume": {"session_id": "s", "execution_id": "e"}},
        {"resume_from": str(uuid4())},
        {"session_id": "native-session"},
    ],
)
def test_private_runner_rejects_effective_overrides(
    review: tuple[Any, Any], context: dict[str, Any]
) -> None:
    _, flow = review
    with pytest.raises(ValueError):
        validate_read_only_executor(flow, context)


def test_private_runner_reference_must_name_this_execution(
    review: tuple[Any, Any],
) -> None:
    _, flow = review
    execution_id = uuid4()
    own = SimpleNamespace(
        id=execution_id, agent_session_reference=f"runner:{uuid4()}:{execution_id}"
    )
    validate_read_only_executor(flow, {}, own)
    queued = SimpleNamespace(
        id=execution_id,
        agent_session_reference=f"runner:queued:review-hosts:{execution_id}",
    )
    validate_read_only_executor(flow, {}, queued)
    other = SimpleNamespace(
        id=execution_id, agent_session_reference=f"runner:{uuid4()}:{uuid4()}"
    )
    with pytest.raises(ValueError, match="resume a private runner session"):
        validate_read_only_executor(flow, {}, other)
    flow.runner_pool = "server"
    flow.agent_config = {}
    with pytest.raises(ValueError, match="resume a private runner session"):
        validate_read_only_executor(flow, {}, own)


def _patched_authority(binding: Any) -> Any:
    return (
        patch(
            "preloop.models.crud.crud_ci_execution.authorize_dispatch",
            return_value=binding,
        ),
        patch(
            "preloop.models.crud.crud_tracker.get_by_id_and_account",
            return_value=MagicMock(),
        ),
    )


@pytest.mark.asyncio
async def test_execution_context_checks_without_minting_and_delivery_mints(
    review: tuple[Any, Any],
) -> None:
    binding, flow = review
    orch = FlowExecutionOrchestrator(MagicMock(), str(flow.id), {}, AsyncMock())
    orch.flow = flow
    orch.execution_log = SimpleNamespace(id=uuid4(), agent_session_reference=None)
    authorize, tracker = _patched_authority(binding)
    with (
        authorize,
        tracker,
        patch(
            "preloop.services.read_only_review.mint_repository_lease",
            AsyncMock(return_value=_lease()),
        ) as mint,
    ):
        context: dict[str, Any] = {"git_clone_config": flow.git_clone_config}
        await orch._attach_git_credentials(context)
        mint.assert_not_awaited()
        assert context["read_only_review"] is True
        assert context["git_credentials_map"] == {}
        assert context["git_clone_config"]["repositories"][0]["commit"] == SHA
        assert not getattr(orch, "_read_only_review_leases", [])

        delivered: dict[str, Any] = {"git_clone_config": flow.git_clone_config}
        await orch._attach_git_credentials(delivered, mint_read_lease=True)
        assert mint.await_args.kwargs["write"] is False
        assert delivered["git_credentials_map"][URL]["permission"] == "read"
        assert len(orch._read_only_review_leases) == 1


@pytest.mark.asyncio
async def test_hosted_read_only_still_mints_in_execution_context(
    review: tuple[Any, Any],
) -> None:
    binding, flow = review
    flow.runner_pool = "server"
    flow.agent_config = {}
    authorize, tracker = _patched_authority(binding)
    with (
        authorize,
        tracker,
        patch(
            "preloop.services.read_only_review.mint_repository_lease",
            AsyncMock(return_value=_lease()),
        ) as mint,
    ):
        lease = await prepare_read_only_review(
            MagicMock(), flow, MagicMock(), {"git_clone_config": {}}
        )
    mint.assert_awaited_once()
    assert lease is not None


def test_lease_payload_marks_read_only_with_admitted_commit(
    review: tuple[Any, Any],
) -> None:
    _, flow = review
    execution_id = uuid4()
    execution = SimpleNamespace(
        id=execution_id,
        ci_review_binding={"head_sha": SHA},
        agent_session_reference=None,
        trigger_event_details={},
    )
    executor = RemoteRunnerExecutor(
        "codex",
        {},
        db=MagicMock(),
        pool="review-hosts",
        account_id=flow.account_id,
        flow=flow,
        execution=execution,
    )
    flow.custom_commands = None
    flow.timeout_seconds = 900
    flow.ai_model = None
    with patch(
        "preloop.agents.remote_runner.flow_launch_fingerprint", return_value="fp"
    ):
        payload = executor._lease_payload(
            execution_id=execution_id,
            flow_id=flow.id,
            prompt="Review the change",
            execution_context={
                "agent_type": "codex",
                "agent_config": flow.agent_config,
                "git_clone_config": flow.git_clone_config,
            },
        )
    assert payload[HOST_READ_ONLY_LEASE_KEY] == {"mode": "read_only", "commit": SHA}
    assert payload["host_exec_profile"] == "codex-review"
    for key in ("git_clone_config", "account_api_token", "allowed_mcp_tools"):
        assert key not in payload

    execution.ci_review_binding = None
    with (
        patch(
            "preloop.agents.remote_runner.flow_launch_fingerprint", return_value="fp"
        ),
        pytest.raises(ValueError, match="restricted CI execution"),
    ):
        executor._lease_payload(
            execution_id=execution_id,
            flow_id=flow.id,
            prompt="Review the change",
            execution_context={"git_clone_config": flow.git_clone_config},
        )


def _runner(caps: list[str]) -> Any:
    return SimpleNamespace(
        capabilities={
            "host_exec_profiles": [
                {"name": "codex-review", "capabilities": caps, "models": []}
            ]
        }
    )


def test_assignment_requires_read_only_capability() -> None:
    old = _runner(["host_exec", "codex_cli"])
    new = _runner(["host_exec", "codex_cli", "host_read_only_review"])
    assert runner_has_host_exec_profile(old, "codex-review", agent_type="codex")
    assert not runner_has_host_exec_profile(
        old, "codex-review", agent_type="codex", require_read_only_review=True
    )
    assert runner_has_host_exec_profile(
        new, "codex-review", agent_type="codex", require_read_only_review=True
    )


def _pending() -> dict[str, Any]:
    return {
        "completion_protocol": "host_exec",
        "agent_type": "codex",
        "host_exec_profile": "codex-review",
        HOST_READ_ONLY_LEASE_KEY: {"mode": "read_only", "commit": SHA},
    }


def _message(**receipt: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "status": "SUCCEEDED",
        "exit_code": 0,
        "completion_protocol": "host_exec",
        "host_exec_profile": "codex-review",
        "result": {
            "status": "success",
            "harness": "codex_cli",
            HOST_READ_ONLY_RESULT_KEY: {"commit": SHA, "sandbox": "read-only"},
        },
    }
    if receipt:
        message[HOST_READ_ONLY_LEASE_KEY] = receipt
    return message


@pytest.mark.parametrize(
    "receipt,error",
    [
        ({}, "did not confirm"),
        ({"commit": "d" * 40, "sandbox": "read-only"}, "admitted revision"),
        ({"commit": SHA, "sandbox": "workspace-write"}, "read-only sandbox"),
    ],
)
def test_completion_requires_runner_receipt(
    receipt: dict[str, Any], error: str
) -> None:
    status, message, _ = finalize_runner_completion(
        _message(**receipt), pending_job=_pending()
    )
    assert status == "FAILED"
    assert error in str(message)


def test_completion_records_trusted_receipt_only() -> None:
    status, error, result = finalize_runner_completion(
        _message(commit=SHA, sandbox="read-only", lease_revoked=True),
        pending_job=_pending(),
    )
    assert (status, error) == ("SUCCEEDED", None)
    assert result is not None
    assert result[HOST_READ_ONLY_RESULT_KEY] == {
        "commit": SHA,
        "sandbox": "read-only",
        "lease_revoked": True,
    }
    # Agent JSON cannot author the control-plane key on other host runs.
    pending = _pending()
    pending.pop(HOST_READ_ONLY_LEASE_KEY)
    _, _, result = finalize_runner_completion(_message(), pending_job=pending)
    assert result is not None and HOST_READ_ONLY_RESULT_KEY not in result


@pytest.mark.asyncio
async def test_delivered_leases_revoked_once() -> None:
    execution_id = uuid4()
    ror.register_delivered_read_lease(execution_id, _lease())
    ror.register_delivered_read_lease(execution_id, _lease())
    with patch(
        "preloop.services.publication_credentials.revoke_repository_lease",
        AsyncMock(),
    ) as revoke:
        assert await ror.revoke_delivered_read_leases(execution_id) == 2
        assert await ror.revoke_delivered_read_leases(execution_id) == 0
    assert revoke.await_count == 2


def _orchestrator_with(leases: list[PublicationLease]) -> Any:
    return SimpleNamespace(_read_only_review_leases=leases)


def test_delivery_binds_one_leased_repository_at_admitted_commit() -> None:
    from preloop.services.host_exec_delivery import (
        HostExecDeliveryError,
        _read_only_delivery,
    )

    lease = _lease()
    context = {
        "read_only_review": True,
        "git_clone_config": {"repositories": [{"commit": SHA}]},
    }
    checkout = {
        "repositories": [
            {"url": URL, "path": "workspace", "commit": SHA, "token": lease.token}
        ]
    }
    marker = {"mode": "read_only", "commit": SHA}
    execution_id = uuid4()
    assert _read_only_delivery(
        _orchestrator_with([lease]), context, checkout, execution_id, marker
    ) == {"mode": "read_only", "commit": SHA}
    assert ror._delivered_read_leases.pop(str(execution_id)) == [lease]
    bad_plans = [
        {"repositories": [{**checkout["repositories"][0], "token": "broad"}]},
        {"repositories": [{**checkout["repositories"][0], "branch": "main"}]},
        {"repositories": [{**checkout["repositories"][0], "commit": "d" * 40}]},
        {"repositories": checkout["repositories"] * 2},
    ]
    for plan in bad_plans:
        with pytest.raises(HostExecDeliveryError):
            _read_only_delivery(
                _orchestrator_with([lease]), context, plan, execution_id, marker
            )
    with pytest.raises(HostExecDeliveryError):
        _read_only_delivery(
            _orchestrator_with([lease]),
            context,
            checkout,
            execution_id,
            {"mode": "read_only", "commit": "d" * 40},
        )
    with pytest.raises(HostExecDeliveryError):
        _read_only_delivery(
            _orchestrator_with([]), context, checkout, execution_id, marker
        )
    ror._delivered_read_leases.pop(str(execution_id), None)


@pytest.mark.asyncio
async def test_delivery_rejects_read_only_setting_change() -> None:
    from preloop.services.host_exec_delivery import (
        HostExecDeliveryError,
        hydrate_host_exec_job,
    )

    execution = SimpleNamespace(id=uuid4(), flow_id=uuid4(), trigger_event_details={})
    flow = SimpleNamespace(
        git_clone_config={"enabled": True, "repositories": [{}]},
        custom_commands=None,
        allowed_mcp_tools=[],
        allowed_mcp_servers=[],
    )
    job = {
        "execution_id": str(execution.id),
        "agent_type": "codex",
        HOST_READ_ONLY_LEASE_KEY: {"mode": "read_only", "commit": SHA},
    }
    with (
        patch(
            "preloop.services.host_exec_delivery.crud_flow_execution.get",
            return_value=execution,
        ),
        patch("preloop.services.host_exec_delivery.crud_flow.get", return_value=flow),
    ):
        with pytest.raises(HostExecDeliveryError, match="read-only review setting"):
            await hydrate_host_exec_job(MagicMock(), job)
        flow.git_clone_config = {
            **flow.git_clone_config,
            "publication_mode": "read_only",
        }
        flow.allowed_mcp_tools = [{"name": "get_issue"}]
        with pytest.raises(HostExecDeliveryError, match="without MCP"):
            await hydrate_host_exec_job(MagicMock(), job)


def test_subscription_model_admitted_for_codex_host_profile(
    review: tuple[Any, Any],
) -> None:
    from preloop.services.model_routing import (
        FlowModelPrincipalError,
        validate_execution_model_principal,
    )

    _, flow = review
    flow.ai_model_id = uuid4()
    model = SimpleNamespace(is_principal_bound_oauth=True)
    with patch("preloop.services.model_routing.crud_ai_model.get", return_value=model):
        validate_execution_model_principal(MagicMock(), flow, {})
        flow.agent_config = {}
        with pytest.raises(FlowModelPrincipalError, match="host_exec_profile"):
            validate_execution_model_principal(MagicMock(), flow, {})
