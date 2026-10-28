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


def test_unsafe_gateway_and_native_bytes_preserve_batch_and_hashes(
    db_session: Session, ci_resources: tuple[Any, ...], client: TestClient
) -> None:
    import json
    from unittest.mock import patch
    from preloop.models.crud.flow_execution_log import crud_flow_execution_log

    _, _, token, context, execution = owned_run(db_session, ci_resources)
    doc = capture_tools(
        {
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": "binary",
                    "output": "before\x00\ud800after",
                }
            ]
        },
        {},
        capture_content=True,
    )
    persist_tool_evidence(
        db_session,
        usage=SimpleNamespace(
            id=uuid4(), flow_execution_id=execution.id, account_id=context.account_id
        ),
        document=doc,
    )
    events = [
        {"type": "agent_log_line", "payload": {"line": "surrounding log"}},
        {
            "type": "agent_log_line",
            "payload": {
                "line": json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "binary-native\x00\ud800",
                            "type": "command_execution",
                            "command": "before\x00\ud800after",
                            "exit_code": 0,
                        },
                    }
                )
            },
        },
    ]
    for event in events:
        event["_persistence_id"] = str(uuid4())
    with patch("preloop.config.settings.model_gateway_capture_content", True):
        crud_flow_execution_log.append_logs(
            db_session, [(str(execution.id), event) for event in events]
        )
    db_session.commit()
    body = client.get(
        f"/api/v1/flows/executions/{execution.id}/tool-evidence",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert body["total"] == 2
    for record in body["records"]:
        assert record["integrity_valid"]
        assert digest(record["document"]) == record["sha256"]
        assert record["document"]["storage_redacted"]
        assert not record["document"]["capture_complete"]
        assert record["document"]["observations"][0]["value"] == "beforeafter"
        assert record["document"]["observations"][0]["redacted"]
    logs = crud_flow_execution_log.get_by_execution_id(
        db_session, execution_id=execution.id
    )
    assert any("surrounding log" in str(row.metadata_) for row in logs)


def test_page_byte_cap_progress_missing_coverage_and_corrupt_chunks(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.models import models
    from preloop.models.crud import execution_tool_evidence as evidence_crud
    from preloop.models.crud.base import CRUDBase
    from preloop.services.execution_tool_evidence import canonical_bytes

    # A budget below two records forces one large record per page.
    budget = 2_000_000
    monkeypatch.setattr(evidence_crud, "MAX_PAGE_BYTES", budget)

    _, _, token, context, execution = owned_run(db_session, ci_resources)
    for index in range(4):
        usage = CRUDBase(models.ApiUsage).create(
            db_session,
            obj_in={
                "account_id": context.account_id,
                "flow_execution_id": execution.id,
                "endpoint": "/responses",
                "method": "POST",
                "status_code": 200,
                "duration": 0.1,
            },
        )
        if index < 3:
            persist_tool_evidence(
                db_session,
                usage=usage,
                document=capture_tools(
                    {
                        "input": [
                            {
                                "type": "function_call_output",
                                "call_id": str(index),
                                "output": "x" * 1500000,
                            }
                        ]
                    },
                    {},
                    capture_content=True,
                ),
            )
    CRUDBase(models.FlowExecutionLog).create(
        db_session,
        obj_in={
            "execution_id": execution.id,
            "log_type": "execution_tool_evidence",
            "metadata_": {"chunks": ["broken"], "sha256": "bad"},
        },
    )
    db_session.commit()
    url = f"/api/v1/flows/executions/{execution.id}/tool-evidence"
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get(url + "?limit=25", headers=headers).status_code == 422
    first = client.get(url + "?limit=10", headers=headers).json()
    assert len(first["records"]) == 1
    assert first["has_more"] and first["next_skip"] == 1
    assert len(canonical_bytes(first["records"])) <= budget
    pages = [first]
    while pages[-1]["has_more"]:
        pages.append(
            client.get(
                url + f"?skip={pages[-1]['next_skip']}&limit=10", headers=headers
            ).json()
        )
        assert pages[-1]["records"], "a page always returns at least one record"
    assert pages[-1]["next_skip"] == 4
    assert all(p["missing_gateway_requests"] == 1 for p in pages)
    records = [r for page in pages for r in page["records"]]
    assert len({r["id"] for r in records}) == 4
    corrupt = [r for r in records if not r["integrity_valid"]]
    assert len(corrupt) == 1 and corrupt[0]["document"] is None


def test_callback_diagnostics_only_return_owned_safe_projection(
    db_session: Session, ci_resources: tuple[Any, ...], client: TestClient
) -> None:
    from datetime import datetime
    from preloop.models import models
    from preloop.models.crud.base import CRUDBase

    principal, _, token, context, execution = owned_run(db_session, ci_resources)
    other, _, _ = provision(db_session, ci_resources)
    own_delivery = None
    for owner in (principal, other):
        endpoint = CRUDBase(models.WebhookEndpoint).create(
            db_session,
            obj_in={
                "account_id": context.account_id,
                "ci_principal_id": owner.id,
                "url": "https://private.example/hook",
                "secret_encrypted": "private-key",
            },
        )
        delivery = CRUDBase(models.WebhookDelivery).create(
            db_session,
            obj_in={
                "account_id": context.account_id,
                "endpoint_id": endpoint.id,
                "event_id": uuid4(),
                "event_type": "flow.execution.finished",
                "subject_id": execution.id,
                "occurred_at": datetime.now(),
                "next_attempt_at": datetime.now(),
                "payload": {"private": "body"},
                "last_error": "private-error",
                "status": "delivered",
                "attempt_count": 2,
                "response_status": 204,
            },
        )
        if owner.id == principal.id:
            own_delivery = str(delivery.id)
    response = client.get(
        f"/api/v1/flows/executions/{execution.id}/tool-evidence",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    deliveries = response.json()["terminal_deliveries"]
    assert len(deliveries) == 1 and deliveries[0]["id"] == own_delivery
    assert set(deliveries[0]) == {
        "id",
        "status",
        "attempt_count",
        "response_status",
        "next_attempt_at",
    }
    assert "private" not in response.text


def test_evidence_failure_keeps_usage_row_and_log_batch(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database error on the evidence insert must not roll back its callers."""
    import json
    from unittest.mock import patch

    from preloop.models import models
    from preloop.models.crud import execution_tool_evidence as evidence_crud
    from preloop.models.crud.base import CRUDBase
    from preloop.models.crud.flow_execution_log import crud_flow_execution_log

    _, _, token, context, execution = owned_run(db_session, ci_resources)
    usage = CRUDBase(models.ApiUsage).create(
        db_session,
        obj_in={
            "account_id": context.account_id,
            "flow_execution_id": execution.id,
            "endpoint": "/responses",
            "method": "POST",
            "status_code": 200,
            "duration": 0.1,
        },
    )
    real_canonical = evidence_crud.canonical_bytes
    calls = {"n": 0}

    def nul_on_first_insert(value: Any) -> bytes:
        # Smuggle a NUL past sanitization into the first JSONB insert only,
        # so Postgres itself rejects it mid-transaction.
        encoded = real_canonical(value)
        if isinstance(value, dict) and "observations" in value:
            calls["n"] += 1
            if calls["n"] in (1, 3):
                return encoded.replace(b'"version"', b'"v\x00ersion"')
        return encoded

    monkeypatch.setattr(evidence_crud, "canonical_bytes", nul_on_first_insert)
    persist_tool_evidence(
        db_session,
        usage=usage,
        document=capture_tools(
            {},
            {"output": [{"type": "function_call", "call_id": "c", "name": "n"}]},
            capture_content=True,
        ),
    )
    events = [
        {"type": "agent_log_line", "payload": {"line": "line before"}},
        {
            "type": "agent_log_line",
            "payload": {
                "line": json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "cmd-1",
                            "type": "command_execution",
                            "command": "ls",
                            "exit_code": 0,
                        },
                    }
                )
            },
        },
        {"type": "agent_log_line", "payload": {"line": "line after"}},
    ]
    for event in events:
        event["_persistence_id"] = str(uuid4())
    with patch("preloop.config.settings.model_gateway_capture_content", True):
        crud_flow_execution_log.append_logs(
            db_session, [(str(execution.id), event) for event in events]
        )
    db_session.commit()
    assert db_session.get(models.ApiUsage, usage.id) is not None
    logs = crud_flow_execution_log.get_by_execution_id(
        db_session, execution_id=execution.id
    )
    text = " ".join(str(row.metadata_) + str(row.message) for row in logs)
    assert "line before" in text and "line after" in text
    body = client.get(
        f"/api/v1/flows/executions/{execution.id}/tool-evidence",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert body["total"] == 2
    assert body["missing_gateway_requests"] == 0
    assert calls["n"] == 4
    for record in body["records"]:
        assert record["integrity_valid"]
        assert record["document"]["capture_failed"] is True
        assert record["document"]["capture_complete"] is False


def test_replayed_request_history_is_stored_once(
    db_session: Session, ci_resources: tuple[Any, ...], client: TestClient
) -> None:
    """Stored bytes grow with new exchanges, not with replayed history."""
    from preloop.models import models
    from preloop.models.crud.base import CRUDBase
    from preloop.services.execution_tool_evidence import canonical_bytes

    _, _, token, context, execution = owned_run(db_session, ci_resources)
    history: list[dict[str, Any]] = []
    sizes = []
    url = f"/api/v1/flows/executions/{execution.id}/tool-evidence"
    headers = {"Authorization": f"Bearer {token}"}
    for turn in range(6):
        history += [
            {"type": "function_call", "call_id": f"c{turn}", "name": "read"},
            {
                "type": "function_call_output",
                "call_id": f"c{turn}",
                "output": "y" * 1000,
            },
        ]
        usage = CRUDBase(models.ApiUsage).create(
            db_session,
            obj_in={
                "account_id": context.account_id,
                "flow_execution_id": execution.id,
                "endpoint": "/responses",
                "method": "POST",
                "status_code": 200,
                "duration": 0.1,
            },
        )
        for _ in range(2):  # a retry of the same request adds nothing new
            persist_tool_evidence(
                db_session,
                usage=SimpleNamespace(
                    id=uuid4() if _ else usage.id,
                    flow_execution_id=execution.id,
                    account_id=context.account_id,
                ),
                document=capture_tools(
                    {"input": list(history)}, {}, capture_content=True
                ),
            )
        db_session.commit()
        records = []
        skip = 0
        while True:
            page = client.get(url + f"?skip={skip}&limit=10", headers=headers).json()
            records += page["records"]
            if not page["has_more"]:
                break
            skip = page["next_skip"]
        sizes.append(
            sum(len(canonical_bytes(r["document"]["observations"])) for r in records)
        )
        assert all(r["document"]["capture_complete"] for r in records)
        results = [
            o
            for r in records
            for o in r["document"]["observations"]
            if o["kind"] == "result"
        ]
        assert [o["call_id"] for o in results] == [f"c{i}" for i in range(turn + 1)]
    growth = [b - a for a, b in zip(sizes, sizes[1:], strict=False)]
    assert max(growth) == min(growth), sizes


def test_request_dedupe_preserves_changed_observations_for_same_call_id(
    db_session: Session, ci_resources: tuple[Any, ...], client: TestClient
) -> None:
    _, _, token, context, execution = owned_run(db_session, ci_resources)
    for output in ("first", "updated", "updated"):
        persist_tool_evidence(
            db_session,
            usage=SimpleNamespace(
                id=uuid4(),
                flow_execution_id=execution.id,
                account_id=context.account_id,
            ),
            document=capture_tools(
                {
                    "input": [
                        {
                            "type": "function_call_output",
                            "call_id": "same",
                            "output": output,
                        }
                    ]
                },
                {},
                capture_content=True,
            ),
        )
    db_session.commit()
    body = client.get(
        f"/api/v1/flows/executions/{execution.id}/tool-evidence",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    values = [
        item["value"]
        for row in body["records"]
        for item in row["document"]["observations"]
    ]
    assert values == ["first", "updated"]
    assert (
        sum(
            row["document"].get("request_duplicates_omitted", 0)
            for row in body["records"]
        )
        == 1
    )
    assert all(row["integrity_valid"] for row in body["records"])
