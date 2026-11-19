"""Audit capture must not inherit console truncation or expose secret fields."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from preloop.models.crud.execution_tool_evidence import persist_tool_evidence
from preloop.services.execution_tool_evidence import (
    _redact_value,
    capture_tools,
    digest,
)


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


def test_latest_exchange_capture_grows_linearly_for_replayed_history() -> None:
    history = []
    captured_bytes = 0
    for index in range(100):
        history.extend(
            [
                {
                    "type": "function_call",
                    "call_id": f"call-{index}",
                    "name": "read",
                    "arguments": "x" * 1000,
                },
                {
                    "type": "function_call_output",
                    "call_id": f"call-{index}",
                    "output": "y" * 1000,
                },
            ]
        )
        document = capture_tools({"input": history}, {}, capture_content=True)
        assert document["request_scope"] == "latest_tool_exchange"
        assert document["capture_complete"] is True
        assert [r["call_id"] for r in document["observations"]] == [f"call-{index}"] * 2
        captured_bytes += len(json.dumps(document))
    assert captured_bytes < 300000


def test_latest_chat_parallel_results_survive_history_selection() -> None:
    history = [
        {"role": "tool", "tool_call_id": "old", "content": "old"},
        {
            "role": "assistant",
            "tool_calls": [
                {"id": name, "function": {"name": "read", "arguments": "{}"}}
                for name in ("one", "two")
            ],
        },
        *[
            {"role": "tool", "tool_call_id": name, "content": "ok"}
            for name in ("one", "two")
        ],
    ]
    doc = capture_tools({"messages": history}, {}, capture_content=True)
    assert [r["call_id"] for r in doc["observations"]] == ["one", "two", "one", "two"]
    assert doc["capture_complete"]


def test_sanitization_precedes_canonicalization_of_nested_argument_json() -> None:
    doc = capture_tools(
        {},
        response('{"password":"secret","text":"\\ud800\\u0000x"}'),
        capture_content=True,
    )
    assert json.loads(doc["observations"][0]["value"])["text"] == "x"
    assert doc["observations"][0]["redacted"]


_ORDINARY_SOURCE = """\
name: review
permissions:
  id-token: write
  contents: read

def build(binding, head):
    token = build_permit(binding, head)
    if token:
        headers["Authorization"] = "Bearer " + token
    auth = b64(("x-access-token:" + gh_token).encode())
"""


def _tool_result(text: str) -> dict:
    return {
        "output": [
            {
                "type": "function_call_output",
                "call_id": "call-read",
                "output": text,
            }
        ]
    }


def _stored_document(document: dict) -> dict:
    """Return the document persist_tool_evidence would write."""
    db = MagicMock()
    usage = SimpleNamespace(id=uuid4(), flow_execution_id=uuid4(), account_id=uuid4())
    with patch(
        "preloop.services.sensitive_data.storage.apply_storage_redaction",
        side_effect=lambda account, value: value,
    ):
        persist_tool_evidence(db, usage=usage, document=document)
    metadata = db.execute.call_args.args[0].compile().params["metadata"]
    return json.loads("".join(metadata["chunks"]))


def test_ordinary_source_is_stored_complete_and_byte_identical() -> None:
    """A workflow permission and a function call are not a secret."""
    document = capture_tools({}, _tool_result(_ORDINARY_SOURCE), capture_content=True)
    assert document["capture_complete"] is True
    assert document["storage_redacted"] is False
    assert document["redaction_ruleset"] == "2026-11-19"
    assert document["observations"][0]["value"] == _ORDINARY_SOURCE
    assert document["observations"][0]["redacted"] is False
    assert "redactions" not in document["observations"][0]
    stored = _stored_document(document)
    assert stored["observations"][0]["value"] == _ORDINARY_SOURCE
    assert stored["capture_complete"] is True
    assert stored["storage_redacted"] is False
    assert stored["redaction_ruleset"] == "2026-11-19"


def test_placeholder_url_is_stored_complete() -> None:
    """A format placeholder in a URL is not a userinfo secret."""
    source = 'f"https://x-access-token:{token}@github.com/Org/repo.git"'
    document = capture_tools({}, _tool_result(source), capture_content=True)
    assert document["observations"][0]["value"] == source
    assert document["capture_complete"] is True
    assert document["storage_redacted"] is False
    assert "redactions" not in document["observations"][0]
    stored = _stored_document(document)
    assert stored["observations"][0]["value"] == source
    assert stored["capture_complete"] is True
    assert stored["storage_redacted"] is False


def test_one_bearer_counts_once_on_gateway_and_native_paths() -> None:
    """One header is one hit, including after the storage scrubber."""
    source = "Authorization: Bearer ${GITHUB_TOKEN}"
    document = capture_tools({}, _tool_result(source), capture_content=True)
    assert document["observations"][0]["redactions"] == [
        {"rule": "authorization_bearer", "count": 1}
    ]
    assert "${GITHUB_TOKEN}" not in document["observations"][0]["value"]
    _, facts = _redact_value(source)
    assert facts == [{"rule": "authorization_bearer", "count": 1}]


def test_already_masked_sensitive_field_is_not_a_new_redaction() -> None:
    """Replacing a marker with itself does not flag the observation."""
    for source in (
        '{"password": "[REDACTED]"}',
        '{"password": "***REDACTED***"}',
    ):
        document = capture_tools({}, _tool_result(source), capture_content=True)
        record = document["observations"][0]
        assert record["value"] == source
        assert record["redacted"] is False
        assert "redactions" not in record


def test_real_looking_key_is_masked_and_records_the_rule() -> None:
    """A credential literal stays incomplete and names the rule that fired."""
    secret = "sk-testsecret1234567890"
    source = f'api_key = "{secret}"\n'
    document = capture_tools({}, _tool_result(source), capture_content=True)
    record = document["observations"][0]
    assert secret not in record["value"]
    assert "***REDACTED***" in record["value"]
    assert record["redacted"] is True
    assert record["redactions"] == [{"rule": "keyword_assignment", "count": 1}]
    assert document["capture_complete"] is False
    assert document["storage_redacted"] is True
    assert document["redaction_ruleset"] == "2026-11-19"
    stored = _stored_document(document)
    assert secret not in stored["observations"][0]["value"]
    assert stored["observations"][0]["redactions"] == [
        {"rule": "keyword_assignment", "count": 1}
    ]
    assert stored["capture_complete"] is False
    assert stored["storage_redacted"] is True
    assert stored["redaction_ruleset"] == "2026-11-19"


def test_latest_results_are_retained_before_trailing_prose() -> None:
    history = [
        {
            "type": "function_call",
            "call_id": "current",
            "name": "read",
            "arguments": "{}",
        },
        {
            "type": "function_call_output",
            "call_id": "current",
            "output": "current result",
        },
        {"role": "user", "content": "Continue using that result"},
        {"type": "reasoning", "summary": []},
    ]
    document = capture_tools({"input": history}, {}, capture_content=True)
    assert document["capture_complete"]
    assert [r["call_id"] for r in document["observations"]] == ["current", "current"]
    assert document["observations"][-1]["value"] == "current result"
