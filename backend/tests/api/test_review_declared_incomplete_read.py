"""An unfinished review is readable through the restricted result read."""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import crud
from preloop.services import review_integrity
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from tests.api.test_ci_execution_http import client as make_client, owned_run
from tests.api.test_ci_principal import ci_resources as make_resources


@pytest.fixture
def ci_resources(db_session: Session) -> tuple[Any, ...]:
    return make_resources.__wrapped__(db_session)


@pytest.fixture
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    return make_client.__wrapped__(db_session, monkeypatch)


@pytest.mark.asyncio
async def test_declared_unfinished_review_is_stored_and_readable(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, token, _, execution = owned_run(db_session, ci_resources)
    orch = FlowExecutionOrchestrator(db_session, execution.flow_id, {}, AsyncMock())
    orch.execution_log = execution
    stored_flow = crud.crud_flow.get(db_session, id=execution.flow_id)
    # A detached view: the stored flow (and its CI authorization) is unchanged.
    orch.flow = SimpleNamespace(
        id=stored_flow.id,
        account_id=stored_flow.account_id,
        agent_type="codex",
        agent_config={"native_result": True},
        git_clone_config={
            **(stored_flow.git_clone_config or {}),
            "publication_mode": "read_only",
        },
    )
    orch.flow.result_schema = {
        "type": "object",
        "required": ["review", "status"],
        "properties": {
            "review": {"type": "string"},
            "status": {"type": "string", "enum": ["success", "incomplete"]},
            "not_reviewed": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    }
    orch.flow.validate_result = True
    assert review_integrity.applies(orch.flow, execution)
    orch._native_result_effective = True
    monkeypatch.setattr(orch, "_publish_update", AsyncMock())
    document = {
        "review": "Ran out of time after reading parser.py; one finding so far.",
        "status": "incomplete",
        "not_reviewed": ["cli.py"],
    }
    executor = SimpleNamespace(
        get_result_artifact=AsyncMock(return_value=document),
        get_workspace_snapshot=None,
        get_evidence_archive=None,
        evidence_transport_error=None,
    )
    assert await orch._capture_result_artifact(executor, "session") == document
    with patch(
        "preloop.models.crud.execution_tool_evidence.inspection_summary",
        return_value={"diff_commands": 1},
    ):
        status, attempt, merged = orch._apply_review_integrity_gate(
            "SUCCEEDED", {"status": "SUCCEEDED"}, document
        )
    assert status == "FAILED"
    assert attempt["failure_category"] == review_integrity.CATEGORY_DEGENERATE
    await orch._update_execution_log(
        status=status,
        result=merged,
        failure_category=attempt["failure_category"],
        error_message=attempt["error_message"],
        end_time=datetime.now(timezone.utc),
    )
    response = client.get(
        f"/api/v1/flows/executions/{execution.id}/result",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "FAILED"
    stored = {k: v for k, v in body["result"].items() if k != "_review_integrity"}
    assert stored == document
