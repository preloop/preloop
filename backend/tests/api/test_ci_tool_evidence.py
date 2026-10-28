"""Tool evidence does not grant log access or another principal's execution."""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import crud
from preloop.models.crud.execution_tool_evidence import persist_tool_evidence
from preloop.services.execution_tool_evidence import capture_tools, digest
import pytest
from tests.api.test_ci_principal import provision
from tests.api.test_ci_execution_http import (
    ci_resources as create_ci_resources,
    client as create_client,
    owned_run,
)


@pytest.fixture
def ci_resources(db_session: Session) -> tuple[Any, ...]:
    return create_ci_resources.__wrapped__(db_session)


@pytest.fixture
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    return create_client.__wrapped__(db_session, monkeypatch)


def test_scoped_evidence_hashes_and_general_logs_remain_denied(
    db_session: Session, ci_resources: tuple[Any, ...], client: TestClient
) -> None:
    _, _, token, context, execution = owned_run(db_session, ci_resources)
    document = capture_tools(
        {},
        {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "exec_command",
                    "arguments": '{"command":"read source"}',
                }
            ]
        },
        capture_content=True,
    )
    persist_tool_evidence(
        db_session,
        usage=SimpleNamespace(
            id=uuid4(), flow_execution_id=execution.id, account_id=context.account_id
        ),
        document=document,
    )
    db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    path = f"/api/v1/flows/executions/{execution.id}"
    response = client.get(path + "/tool-evidence", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["binding"]["project_id"] == str(context.project_id)
    assert body["records"][0]["integrity_valid"] is True
    assert digest(body["records"][0]["document"]) == body["records"][0]["sha256"]
    assert body["terminal_deliveries"] == []
    assert client.get(path + "/logs", headers=headers).status_code == 403
    _, _, other_token = provision(db_session, ci_resources)
    denied = client.get(
        path + "/tool-evidence", headers={"Authorization": f"Bearer {other_token}"}
    )
    assert denied.status_code == 404, denied.text
    assert (
        client.get(
            f"/api/v1/flows/executions/{uuid4()}/tool-evidence", headers=headers
        ).status_code
        == 404
    )


def test_native_command_evidence_is_complete_and_deduplicated(
    db_session: Session, ci_resources: tuple[Any, ...], client: TestClient
) -> None:
    import json
    from unittest.mock import patch

    _, _, token, context, execution = owned_run(db_session, ci_resources)
    command = "printf '" + "é" * 40000 + "'"
    line = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "item-50",
                "type": "command_execution",
                "command": command,
                "exit_code": 0,
            },
        }
    )
    with patch("preloop.config.settings.model_gateway_capture_content", True):
        for _ in range(2):
            crud.crud_flow_execution.append_log(
                db_session,
                str(execution.id),
                {"type": "agent_log_line", "payload": {"line": line}},
            )
    response = client.get(
        f"/api/v1/flows/executions/{execution.id}/tool-evidence",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    observation = body["records"][0]["document"]["observations"][0]
    assert observation["value"] == command
    assert observation["exit_code"] == 0
    assert body["records"][0]["integrity_valid"] is True


def test_batched_native_capture_is_not_returned_in_generic_log_history(
    db_session: Session, ci_resources: tuple[Any, ...], client: TestClient
) -> None:
    import json
    from preloop.models.crud.flow_execution_log import crud_flow_execution_log
    from unittest.mock import patch

    _, _, token, _, execution = owned_run(db_session, ci_resources)
    event = {
        "type": "agent_log_line",
        "_persistence_id": str(uuid4()),
        "payload": {
            "line": json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "batch-1",
                        "type": "command_execution",
                        "command": "read fixture",
                        "exit_code": 0,
                    },
                }
            )
        },
    }
    with patch("preloop.config.settings.model_gateway_capture_content", True):
        crud_flow_execution_log.append_logs(db_session, [(str(execution.id), event)])
        crud_flow_execution_log.append_logs(db_session, [(str(execution.id), event)])
    logs = crud_flow_execution_log.get_by_execution_id(db_session, execution.id)
    assert len(logs) == 1
    assert logs[0].log_type == "agent_log_line"
    response = client.get(
        f"/api/v1/flows/executions/{execution.id}/tool-evidence",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1


def test_missing_historical_capture_is_not_reported_complete(
    db_session: Session, ci_resources: tuple[Any, ...], client: TestClient
) -> None:
    from preloop.models import models
    from preloop.models.crud.base import CRUDBase

    _, _, token, context, execution = owned_run(db_session, ci_resources)
    CRUDBase(models.ApiUsage).create(
        db_session,
        obj_in={
            "account_id": context.account_id,
            "flow_execution_id": execution.id,
            "flow_id": context.flow_id,
            "endpoint": "/openai/v1/responses",
            "method": "POST",
            "status_code": 200,
            "duration": 0.1,
        },
    )
    response = client.get(
        f"/api/v1/flows/executions/{execution.id}/tool-evidence",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["missing_gateway_requests"] == 1
    assert response.json()["coverage"] == "missing"
    assert response.json()["records"] == []
