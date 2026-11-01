"""A platform-created park resume is an authorized checkpoint resume.

With direct artifact upload on, ``checkpoint_context`` refuses any ``_resume``
the orchestrator did not mark as authorized. Approval-park and child-wait
resumes are created by the platform from a recorded decision and are linked
by ``resume_execution_id`` on the parked row, so the orchestrator verifies
that link and authorizes them. A ``_resume`` payload without that link (for
example one supplied in a webhook body) still raises.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from preloop.config import settings
from preloop.services.approval_park import build_resume_details
from preloop.services.checkpoint_runtime import checkpoint_context
from preloop.services.flow_child_wait import ChildOutcome, build_child_resume_details
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

pytestmark = pytest.mark.asyncio

FLOW_ID = str(uuid4())
ACCOUNT_ID = str(uuid4())


def _orchestrator(agent_type: str, trigger: dict) -> FlowExecutionOrchestrator:
    orch = FlowExecutionOrchestrator(
        db=MagicMock(),
        flow_id=FLOW_ID,
        trigger_event_data=trigger,
        nats_client=AsyncMock(),
    )
    orch.flow = MagicMock()
    orch.flow.id = FLOW_ID
    orch.flow.account_id = ACCOUNT_ID
    orch.flow.git_clone_config = None
    orch.flow.trigger_project_ids = None
    orch.flow.agent_type = agent_type
    orch.execution_log = MagicMock(ci_principal_id=None)
    orch.execution_log.id = str(uuid4())
    orch.ai_model = MagicMock()
    return orch


async def _prepare(orch: FlowExecutionOrchestrator, parked_row: object) -> dict:
    runtime = MagicMock()
    runtime.model_gateway_enabled = False
    runtime.to_execution_context.return_value = {}
    with (
        patch.object(orch, "_resolve_prompt", AsyncMock(return_value="do it")),
        patch.object(
            orch, "_create_temporary_api_token", return_value=("tok", uuid4())
        ),
        patch.object(orch, "_resolve_execution_model_runtime", return_value=runtime),
        patch.object(orch, "_resolve_cli_session_restore_archive", return_value=None),
        patch("preloop.services.model_routing.validate_native_resume_identity"),
        patch("preloop.models.crud.crud_flow_execution.get", return_value=parked_row),
    ):
        context = await orch._prepare_execution_context()
    return context


def _parked(**overrides: object) -> SimpleNamespace:
    values = {
        "id": uuid4(),
        "flow_id": FLOW_ID,
        "trigger_event_details": {"payload": {}},
        "cli_session": {"agent_type": "codex", "session_id": str(uuid4())},
        "park_request_id": uuid4(),
        "parked_compute_seconds": 10,
        "resume_execution_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _approval_details(parked: SimpleNamespace) -> dict:
    answer = {
        "request_id": str(parked.park_request_id),
        "status": "approved",
        "answered_at": "2026-11-01T00:00:00Z",
    }
    return build_resume_details(parked, answer)


def _child_details(parked: SimpleNamespace) -> dict:
    outcome = ChildOutcome(record={"id": str(uuid4()), "status": "completed"})
    return build_child_resume_details(parked, [outcome])


def _checkpoint_env(context: dict, monkeypatch: pytest.MonkeyPatch) -> dict:
    from preloop.models.crud import flow_artifact

    monkeypatch.setattr(settings, "flow_artifact_direct_upload", True)
    monkeypatch.setattr(flow_artifact, "latest", lambda *a, **k: MagicMock())
    with (
        patch(
            "preloop.api.endpoints.flow_artifacts.mint_artifact_capability",
            return_value="cap",
        ),
        patch(
            "preloop.services.checkpoint_runtime.artifact_reference",
            return_value=MagicMock(),
        ),
    ):
        return checkpoint_context(MagicMock(), context)


@pytest.mark.parametrize("agent_type", ["codex", "claude-code"])
@pytest.mark.parametrize("builder", [_approval_details, _child_details])
async def test_platform_park_resume_is_authorized(
    agent_type: str, builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    parked = _parked()
    orch = _orchestrator(agent_type, builder(parked))
    parked.resume_execution_id = orch.execution_log.id

    context = await _prepare(orch, parked)

    assert context["checkpoint_resume_authorized"] is True
    env = _checkpoint_env(context, monkeypatch)
    assert env["PRELOOP_CHECKPOINT_GET_TOKEN"] == "cap"


@pytest.mark.parametrize(
    "parked_row",
    [
        None,
        "other_resume",
        "other_flow",
    ],
)
async def test_unlinked_resume_payload_stays_refused(
    parked_row: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    parked = _parked()
    orch = _orchestrator("codex", _approval_details(parked))
    if parked_row == "other_resume":
        parked.resume_execution_id = uuid4()
        row: object = parked
    elif parked_row == "other_flow":
        parked.resume_execution_id = orch.execution_log.id
        parked.flow_id = str(uuid4())
        row = parked
    else:
        row = None

    context = await _prepare(orch, row)

    assert context.get("checkpoint_resume_authorized") is not True
    with pytest.raises(ValueError, match="checkpoint_resume_not_authorized"):
        _checkpoint_env(context, monkeypatch)
