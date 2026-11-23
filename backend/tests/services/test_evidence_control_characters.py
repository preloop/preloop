"""Control characters JSONB can store are kept exactly in tool evidence.

Text converted from a PDF carries form feeds, coloured command output carries
ESC sequences, and an agent typing into a terminal sends U+0003 or U+0004.
None of these is a secret or unsafe, so a record holding them is complete and
its digest covers the exact text. U+0000 is stored exactly too: the canonical
JSON escapes it. Only lone surrogates, which cannot be encoded as UTF-8, are
removed, and the record names that category and its count.
"""

import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.execution_tool_evidence import (
    _record_class,
    evidence_page,
    persist_tool_evidence,
    tool_evidence_summary,
)
from preloop.services.execution_tool_evidence import capture_tools, digest
from preloop.services.platform_review import evidence_satisfied

# Form feed page breaks, ESC colour sequences, a C1 control (NEL) and DEL.
HARMLESS_TEXT = (
    "page one\x0cpage two\n"
    "\x1b[32mpassed\x1b[0m \x1b[31mfailed\x1b[0m\n"
    "next\x85line\x7fend\x01\x1f"
)


def _result(text: str, call_id: str = "call-read") -> dict[str, Any]:
    return {
        "output": [{"type": "function_call_output", "call_id": call_id, "output": text}]
    }


def _write_stdin_arguments(char: str) -> str:
    """Arguments as the agent sends them: JSON with the control char escaped."""
    return json.dumps(
        {
            "session_id": 7,
            "chars": char,
            "yield_time_ms": 1000,
            "max_output_tokens": 2000,
        }
    )


def _execution(db: Session, account_id: Any) -> models.FlowExecution:
    flow = models.Flow(
        name=f"evidence-{uuid4().hex[:8]}",
        prompt_template="test",
        agent_type="codex",
        agent_config={},
        account_id=account_id,
    )
    db.add(flow)
    db.flush()
    execution = models.FlowExecution(flow_id=flow.id, status="RUNNING")
    db.add(execution)
    db.flush()
    return execution


def _store_and_read(
    db: Session, account_id: Any, document: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Insert on real Postgres, read back through the paged evidence API."""
    execution = _execution(db, account_id)
    persist_tool_evidence(
        db,
        usage=SimpleNamespace(
            id=uuid4(), flow_execution_id=execution.id, account_id=account_id
        ),
        document=document,
    )
    page = evidence_page(db, execution=execution, skip=0, limit=10)
    assert len(page["records"]) == 1, "the insert did not land"
    record = page["records"][0]
    assert record["integrity_valid"] is True
    # The endpoint returns the page through FastAPI's JSON response.
    wire = json.loads(JSONResponse(content=page).body)
    stored = wire["records"][0]["document"]
    assert stored == record["document"]
    assert digest(stored) == record["sha256"]
    return stored, tool_evidence_summary(db, execution_id=execution.id)


def test_harmless_controls_are_stored_byte_for_byte(
    db_session: Session, test_user: Any
) -> None:
    document = capture_tools({}, _result(HARMLESS_TEXT), capture_content=True)
    assert document["capture_complete"] is True
    assert document["storage_redacted"] is False
    assert "incomplete_reasons" not in document
    observation = document["observations"][0]
    assert observation["value"] == HARMLESS_TEXT
    assert observation["redacted"] is False
    stored, summary = _store_and_read(db_session, test_user.account_id, document)
    assert stored["capture_complete"] is True
    assert stored["storage_redacted"] is False
    assert stored["observations"][0]["value"] == HARMLESS_TEXT
    assert _record_class(stored, valid=True) == "ok"
    assert summary["incomplete_records"] == 0
    assert evidence_satisfied(summary, "complete") is True


@pytest.mark.parametrize("char", ["\x03", "\x04"])
def test_write_stdin_control_char_is_stored_exactly_on_both_sides(
    db_session: Session, test_user: Any, char: str
) -> None:
    """The call's argument string is kept as sent, not decoded and re-encoded.

    Before the fix the nested JSON was parsed, the control character dropped
    and the object re-encoded with sorted keys: ``{"chars":"",...}``.
    """
    arguments = _write_stdin_arguments(char)
    call = {
        "type": "function_call",
        "call_id": "call-stdin",
        "name": "write_stdin",
        "arguments": arguments,
    }
    output = {
        "type": "function_call_output",
        "call_id": "call-stdin",
        "output": f"^C{char}\n$ ",
    }
    request = {"input": [{"role": "user", "content": "go"}, call, output]}
    response = {"output": [dict(call, call_id="call-next")]}
    document = capture_tools(request, response, capture_content=True)
    assert document["capture_complete"] is True, document.get("incomplete_reasons")
    values = {
        (item["source"], item["kind"]): item["value"]
        for item in document["observations"]
    }
    assert values == {
        ("response", "call"): arguments,
        ("request", "call"): arguments,
        ("request", "result"): f"^C{char}\n$ ",
    }
    assert all(item["redacted"] is False for item in document["observations"])
    stored, summary = _store_and_read(db_session, test_user.account_id, document)
    assert [item["value"] for item in stored["observations"]] == [
        item["value"] for item in document["observations"]
    ]
    assert json.loads(stored["observations"][0]["value"])["chars"] == char
    assert summary["incomplete_records"] == 0


def test_nul_is_stored_exactly_on_real_postgres(
    db_session: Session, test_user: Any
) -> None:
    """Canonical JSON escapes U+0000, so the record needs no other encoding."""
    text = "head\x00tail\x00"
    document = capture_tools({}, _result(text), capture_content=True)
    assert document["capture_complete"] is True
    assert document["observations"][0]["value"] == text
    stored, summary = _store_and_read(db_session, test_user.account_id, document)
    assert stored["observations"][0]["value"] == text
    assert stored["capture_complete"] is True
    assert summary["incomplete_records"] == 0


def test_lone_surrogate_is_removed_and_named_on_real_postgres(
    db_session: Session, test_user: Any
) -> None:
    """A lone surrogate cannot be UTF-8 encoded: removed, counted, refused."""
    text = "left\ud800mid\udfffright"
    document = capture_tools({}, _result(text), capture_content=True)
    observation = document["observations"][0]
    assert observation["value"] == "leftmidright"
    assert observation["redacted"] is True
    assert observation["removed"] == [{"category": "lone_surrogates", "count": 2}]
    assert document["capture_complete"] is False
    assert document["incomplete_reasons"] == ["lone_surrogates"]
    stored, summary = _store_and_read(db_session, test_user.account_id, document)
    assert stored["observations"][0]["removed"] == observation["removed"]
    assert _record_class(stored, valid=True) == "incomplete"
    assert summary["incomplete_records"] == 1
    assert summary["masking_only_records"] == 0
    assert summary["incomplete_by_reason"] == {"lone_surrogates": 1}
    assert summary["removed_by_category"] == {"lone_surrogates": 2}
    assert evidence_satisfied(summary, "captured") is False


def test_escaped_surrogate_in_wire_arguments_is_kept_exactly() -> None:
    """The wire string holds a six-character escape, which is storable as is."""
    arguments = '{"chars": "\\ud800"}'
    document = capture_tools(
        {},
        {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "c",
                    "name": "write_stdin",
                    "arguments": arguments,
                }
            ]
        },
        capture_content=True,
    )
    assert document["capture_complete"] is True
    assert document["observations"][0]["value"] == arguments


def test_masked_credential_next_to_form_feed_is_masking_only(
    db_session: Session, test_user: Any
) -> None:
    # A synthetic token the sk_key rule masks. Not a real credential.
    text = "page\x0cOPENAI sk-1234567890abcdefghij\x0cnext"
    document = capture_tools({}, _result(text), capture_content=True)
    value = document["observations"][0]["value"]
    assert "sk-1234567890abcdefghij" not in value
    assert value.count("\x0c") == 2
    assert document["incomplete_reasons"] == ["masking"]
    stored, summary = _store_and_read(db_session, test_user.account_id, document)
    assert _record_class(stored, valid=True) == "masking_only"
    assert summary["masking_only_records"] == 1
    assert summary["incomplete_by_reason"] == {"masking": 1}
    assert evidence_satisfied(summary, "captured") is True


def test_masked_nested_arguments_with_control_chars_keep_them() -> None:
    """A mask inside call arguments re-encodes them; the controls survive."""
    arguments = json.dumps(
        {"command": "echo sk-1234567890abcdefghij", "chars": "\x03\x0c\x1b"}
    )
    document = capture_tools(
        {},
        {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "c",
                    "name": "exec_command",
                    "arguments": arguments,
                }
            ]
        },
        capture_content=True,
    )
    value = json.loads(document["observations"][0]["value"])
    assert value["chars"] == "\x03\x0c\x1b"
    assert "sk-1234567890abcdefghij" not in value["command"]
    assert document["incomplete_reasons"] == ["masking"]
