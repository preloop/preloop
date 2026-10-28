"""Audit capture must not inherit console truncation or expose secret fields."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from preloop.models.crud.execution_tool_evidence import persist_tool_evidence
from preloop.services.execution_tool_evidence import capture_tools, digest


def response(arguments: str) -> dict:
    return {
        "output": [
            {
                "type": "function_call",
                "call_id": "call-finalizer",
                "name": "exec_command",
                "arguments": arguments,
            }
        ]
    }


def test_complete_large_utf8_arguments_survive_capture_and_storage() -> None:
    arguments = json.dumps({"command": "echo " + "é" * 50000}, ensure_ascii=False)
    document = capture_tools({}, response(arguments), capture_content=True)
    assert document["capture_complete"] is True
    assert document["observations"][0]["value"] == arguments
    db = MagicMock()
    usage = SimpleNamespace(id=uuid4(), flow_execution_id=uuid4(), account_id=uuid4())
    with patch(
        "preloop.services.sensitive_data.storage.apply_storage_redaction",
        side_effect=lambda account, value: value,
    ):
        persist_tool_evidence(db, usage=usage, document=document)
    params = db.execute.call_args.args[0].compile().params
    metadata = params["metadata"]
    stored = json.loads("".join(metadata["chunks"]))
    assert stored["observations"][0]["value"] == arguments
    assert stored["execution_id"] == str(usage.flow_execution_id)
    assert digest(stored) == metadata["sha256"]
    assert max(map(len, metadata["chunks"])) <= 16000


def test_content_disabled_never_retains_arguments() -> None:
    document = capture_tools(
        {}, response('{"command":"private source"}'), capture_content=False
    )
    assert document["observations"][0]["value"] is None
    assert document["observations"][0]["redacted"] is True


def test_structured_secrets_are_redacted_with_explicit_flag() -> None:
    document = capture_tools(
        {},
        response('{"command":"echo safe","password":"private-secret"}'),
        capture_content=True,
    )
    record = document["observations"][0]
    assert "private-secret" not in record["value"]
    assert record["redacted"] is True
    assert json.loads(record["value"])["command"] == "echo safe"


def test_oversize_record_is_explicitly_missing_not_truncated_json() -> None:
    document = capture_tools(
        {}, response("x" * (3 * 1024 * 1024)), capture_content=True
    )
    assert document["capture_complete"] is False
    assert document["observations"] == []
