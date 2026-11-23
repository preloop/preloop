"""Audit capture must not inherit console truncation or expose secret fields."""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.execution_tool_evidence import (
    _record_class,
    evidence_page,
    persist_native_command_evidence,
    persist_tool_evidence,
    tool_evidence_summary,
)
from preloop.services.execution_tool_evidence import (
    _redact_value,
    capture_tools,
    digest,
)
from preloop.services.platform_review import evidence_satisfied


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


def test_masked_nested_argument_json_keeps_escaped_nul_and_surrogate() -> None:
    """Re-encoding after a mask escapes what UTF-8 cannot hold, losing nothing."""
    doc = capture_tools(
        {},
        response('{"password":"secret","text":"\\ud800\\u0000x"}'),
        capture_content=True,
    )
    value = json.loads(doc["observations"][0]["value"])
    assert value["password"] != "secret"
    assert value["text"] == "\ud800\x00x"
    assert doc["observations"][0]["redacted"]
    assert doc["incomplete_reasons"] == ["masking"]
    assert "removed" not in doc["observations"][0]


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
    _, facts, _unexplained = _redact_value(source)
    assert facts == [{"rule": "authorization_bearer", "count": 1}]


def test_nested_sensitive_value_is_masked_on_the_gateway_path() -> None:
    """A dict or list under a sensitive key must not drop the document.

    A real mask on a gateway observation stays incomplete. A gateway tool
    observation that matches a rule sets ``capture_complete`` false.
    """
    payloads = (
        {"command": "echo safe", "credentials": {"password": "hunter2"}},
        {"command": "echo safe", "token": ["hunter2"]},
    )
    for payload in payloads:
        document = capture_tools(
            {},
            {
                "output": [
                    {
                        "type": "function_call_output",
                        "call_id": "call-nested",
                        "output": payload,
                    }
                ]
            },
            capture_content=True,
        )
        record = document["observations"][0]
        masked_key = "credentials" if "credentials" in payload else "token"
        assert record["redactions"] == [{"rule": "sensitive_field", "count": 1}]
        assert "hunter2" not in json.dumps(record["value"])
        assert record["value"]["command"] == "echo safe"
        assert record["value"][masked_key] == "[REDACTED]"
        assert record["redacted"] is True
        assert document["capture_complete"] is False
        assert document["storage_redacted"] is True
        stored = _stored_document(document)
        assert "hunter2" not in json.dumps(stored["observations"][0]["value"])
        assert stored["capture_complete"] is False


def _keep_native_document(
    db: object,
    *,
    execution_id: object,
    source: str,
    source_id: str,
    document: dict[str, Any],
    captured: dict[str, dict[str, Any]],
) -> None:
    """Record the document the native path was about to store."""
    del db, execution_id, source, source_id
    captured["document"] = document


def _native_document(command: dict[str, Any]) -> dict[str, Any]:
    """Run native command capture and return the document it would store."""
    from preloop.models.crud.execution_tool_evidence import (
        _persist_native_command_evidence,
    )

    captured: dict[str, dict[str, Any]] = {}
    line = json.dumps(
        {
            "type": "item.started",
            "item": {
                "id": "item-nested",
                "type": "command_execution",
                "command": command,
            },
        }
    )
    with (
        patch(
            "preloop.models.crud.flow_execution_log._account_for_execution",
            return_value=uuid4(),
        ),
        patch(
            "preloop.services.sensitive_data.storage.apply_storage_redaction",
            side_effect=lambda account, value: value,
        ),
        patch(
            "preloop.models.crud.execution_tool_evidence._persist_document",
            lambda db, **kwargs: _keep_native_document(db, captured=captured, **kwargs),
        ),
    ):
        _persist_native_command_evidence(
            MagicMock(), execution_id=str(uuid4()), line=line
        )
    return captured["document"]


def test_nested_sensitive_value_is_masked_on_the_native_path() -> None:
    """Native capture uses the same mask and keeps the size and JSONB check.

    A native command records the same rule facts. Its completeness stays the
    existing size and JSONB check, so a field mask does not clear it.
    """
    payloads = (
        {"command": "echo safe", "credentials": {"password": "hunter2"}},
        {"command": "echo safe", "token": ["hunter2"]},
    )
    for payload in payloads:
        document = _native_document(payload)
        record = document["observations"][0]
        assert record["redactions"] == [{"rule": "sensitive_field", "count": 1}]
        assert "hunter2" not in json.dumps(record["value"])
        assert record["redacted"] is True
        assert document["capture_complete"] is True
        assert document["storage_redacted"] is False


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


# A synthetic token the sk_key rule masks. Not a credential from a deployment.
_MASKED_COMMAND = "echo sk-1234567890abcd"
_PLAIN_COMMAND = "echo hello"


def _execution(db: Session, account_id: Any) -> models.FlowExecution:
    """One flow execution the evidence writers can attribute to an account."""
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


def _native_line(command: str) -> str:
    """One native command event, before the ordinary log string is capped."""
    return json.dumps(
        {
            "type": "item.started",
            "item": {
                "id": "item-command",
                "type": "command_execution",
                "command": command,
            },
        }
    )


def _enable_content_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """The native path reads this flag when it decides whether to keep text."""
    from preloop.config import settings

    monkeypatch.setattr(settings, "model_gateway_capture_content", True)


def _stored_native(
    db: Session, account_id: Any, command: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist one native command and return the stored document and summary."""
    execution = _execution(db, account_id)
    persist_native_command_evidence(
        db, execution_id=str(execution.id), line=_native_line(command)
    )
    page = evidence_page(db, execution=execution, skip=0, limit=10)
    assert len(page["records"]) == 1
    record = page["records"][0]
    assert record["integrity_valid"] is True
    document = record["document"]
    assert isinstance(document, dict)
    summary = tool_evidence_summary(db, execution_id=execution.id)
    return document, summary


def test_masked_native_command_is_masking_only(
    db_session: Session, test_user: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named mask on a native command is masking_only, and the summary says so.

    The stored record stays capture_complete. The gate reads the named rule
    counts instead of that flag.
    """
    _enable_content_capture(monkeypatch)
    document, summary = _stored_native(
        db_session, test_user.account_id, _MASKED_COMMAND
    )
    assert document["capture_complete"] is True
    assert "incomplete_reasons" not in document
    assert {
        "class": _record_class(document, valid=True),
        "incomplete_records": summary["incomplete_records"],
        "masking_only_records": summary["masking_only_records"],
        "masking_by_rule": summary["masking_by_rule"],
    } == {
        "class": "masking_only",
        "incomplete_records": 1,
        "masking_only_records": 1,
        "masking_by_rule": {"sk_key": 1},
    }


def test_unmasked_native_command_is_ok(
    db_session: Session, test_user: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A native command no rule masks is a complete record with zero counts."""
    _enable_content_capture(monkeypatch)
    document, summary = _stored_native(db_session, test_user.account_id, _PLAIN_COMMAND)
    assert _record_class(document, valid=True) == "ok"
    assert summary["incomplete_records"] == 0
    assert summary["masking_only_records"] == 0
    assert summary["masking_by_rule"] == {}


def test_captured_accepts_masked_native_command_and_complete_refuses_it(
    db_session: Session, test_user: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Level captured still accepts the masked native record. Complete refuses it."""
    _enable_content_capture(monkeypatch)
    _document, summary = _stored_native(
        db_session, test_user.account_id, _MASKED_COMMAND
    )
    assert (
        evidence_satisfied(summary, "captured"),
        evidence_satisfied(summary, "complete"),
    ) == (True, False)


def test_gateway_masked_value_stays_masking_only(
    db_session: Session, test_user: Any
) -> None:
    """A gateway record whose value a named rule masked stays masking_only."""
    execution = _execution(db_session, test_user.account_id)
    document = capture_tools(
        {},
        {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "exec_command",
                    "arguments": '{"command":"echo sk-1234567890abcd"}',
                }
            ]
        },
        capture_content=True,
    )
    persist_tool_evidence(
        db_session,
        usage=SimpleNamespace(
            id=uuid4(),
            flow_execution_id=execution.id,
            account_id=test_user.account_id,
        ),
        document=document,
    )
    page = evidence_page(db_session, execution=execution, skip=0, limit=10)
    stored = page["records"][0]["document"]
    assert page["records"][0]["integrity_valid"] is True
    assert _record_class(stored, valid=True) == "masking_only"


def _gate_summary(document: dict[str, Any]) -> dict[str, int]:
    """Counts ``evidence_satisfied`` reads, for one stored document."""
    kind = _record_class(document, valid=True)
    return {
        "missing_records": 0,
        "incomplete_records": 0 if kind == "ok" else 1,
        "masking_only_records": 1 if kind == "masking_only" else 0,
    }


def _inserted_document(db: MagicMock) -> dict[str, Any]:
    """The document the last evidence insert stored, digest included."""
    metadata = db.execute.call_args.args[0].compile().params["metadata"]
    stored = json.loads("".join(metadata["chunks"]))
    assert isinstance(stored, dict)
    assert digest(stored) == metadata["sha256"]
    return stored


def _storage_config(*, email: bool) -> Any:
    """An account storage policy, empty or one email redact rule."""
    from preloop.services.policy.schema import SensitiveDataConfig

    if not email:
        return SensitiveDataConfig()
    return SensitiveDataConfig.model_validate(
        {
            "rules": [
                {
                    "id": "redact-email",
                    "on": [
                        "tool.args",
                        "tool.result",
                        "model.request",
                        "model.response",
                    ],
                    "types": ["email"],
                    "action": "redact",
                }
            ]
        }
    )


def _persist_native_for_test(command: str, *, email_rule: bool) -> dict[str, Any]:
    """Store one native command with the real persist path and no database."""
    db = MagicMock()
    with (
        patch("preloop.config.settings.model_gateway_capture_content", True),
        patch(
            "preloop.models.crud.flow_execution_log._account_for_execution",
            return_value=uuid4(),
        ),
        patch(
            "preloop.services.sensitive_data.storage.resolve_config",
            return_value=_storage_config(email=email_rule),
        ),
    ):
        persist_native_command_evidence(
            db, execution_id=str(uuid4()), line=_native_line(command)
        )
    return _inserted_document(db)


def test_unexplained_native_scrub_is_stored_incomplete() -> None:
    """A native command scrubbed with no named rule is incomplete.

    ``captured`` and ``complete`` both refuse it. The stored record matches
    a gateway record: ``capture_complete`` false and reason
    ``credential_scrub``.
    """
    command = "curl https://user:pass@example.com/x"
    document = _persist_native_for_test(command, email_rule=False)
    assert document["capture_complete"] is False
    assert document["incomplete_reasons"] == ["credential_scrub"]
    assert "pass@" not in document["observations"][0]["value"]
    assert _record_class(document, valid=True) == "incomplete"
    summary = _gate_summary(document)
    assert evidence_satisfied(summary, "captured") is False
    assert evidence_satisfied(summary, "complete") is False
    assert evidence_satisfied(summary, "any") is True


def test_storage_redaction_makes_native_and_gateway_records_incomplete() -> None:
    """An account storage-redaction rule makes the stored record incomplete.

    Both evidence levels refuse it. A named masking rule is not involved.
    """
    native = _persist_native_for_test("echo alice@example.com", email_rule=True)
    assert "alice@example.com" not in json.dumps(native)
    assert "[REDACTED:email]" in json.dumps(native)
    assert native["capture_complete"] is False
    assert native["incomplete_reasons"] == ["storage_rule"]
    assert native["storage_redacted"] is True
    assert _record_class(native, valid=True) == "incomplete"
    native_summary = _gate_summary(native)
    assert evidence_satisfied(native_summary, "captured") is False
    assert evidence_satisfied(native_summary, "complete") is False

    gateway_input = capture_tools(
        {}, _tool_result("contact alice@example.com"), capture_content=True
    )
    assert gateway_input["capture_complete"] is True
    db = MagicMock()
    usage = SimpleNamespace(id=uuid4(), flow_execution_id=uuid4(), account_id=uuid4())
    with patch(
        "preloop.services.sensitive_data.storage.resolve_config",
        return_value=_storage_config(email=True),
    ):
        persist_tool_evidence(db, usage=usage, document=gateway_input)
    gateway = _inserted_document(db)
    assert "alice@example.com" not in json.dumps(gateway)
    assert "[REDACTED:email]" in json.dumps(gateway)
    assert gateway["capture_complete"] is False
    assert gateway["storage_redacted"] is True
    assert gateway["incomplete_reasons"] == ["storage_rule"]
    assert _record_class(gateway, valid=True) == "incomplete"
    gateway_summary = _gate_summary(gateway)
    assert evidence_satisfied(gateway_summary, "captured") is False
    assert evidence_satisfied(gateway_summary, "complete") is False


def test_prechange_storage_redacted_record_is_incomplete() -> None:
    """A record stored before this fix, flagged only by storage_redacted, fails closed."""
    document = {
        "version": 1,
        "capture_complete": True,
        "storage_redacted": True,
        "content_capture_enabled": True,
        "observations": [
            {
                "source": "native",
                "kind": "command",
                "value": "echo hello",
                "redacted": False,
            }
        ],
    }
    assert _record_class(document, valid=True) == "incomplete"


def test_prechange_redacted_observation_without_facts_is_incomplete() -> None:
    """A redacted observation with no named rule is incomplete even if marked complete."""
    document = {
        "version": 1,
        "capture_complete": True,
        "content_capture_enabled": True,
        "observations": [
            {
                "source": "native",
                "kind": "command",
                "value": "curl https://user:[REDACTED]@example.com/x",
                "redacted": True,
            }
        ],
    }
    assert _record_class(document, valid=True) == "incomplete"


def test_named_native_mask_stays_masking_only_without_a_database() -> None:
    """A named mask keeps capture_complete and is accepted only by captured."""
    document = _persist_native_for_test(_MASKED_COMMAND, email_rule=False)
    assert document["capture_complete"] is True
    assert "incomplete_reasons" not in document
    assert document["observations"][0]["redactions"] == [{"rule": "sk_key", "count": 1}]
    assert _record_class(document, valid=True) == "masking_only"
    summary = _gate_summary(document)
    assert (
        evidence_satisfied(summary, "captured"),
        evidence_satisfied(summary, "complete"),
        evidence_satisfied(summary, "any"),
    ) == (True, False, True)


def test_gateway_named_mask_stays_masking_only_without_a_database() -> None:
    """A named rule on a tool-call argument does not also record unsafe_text.

    Persisting runs ``scrub_structure`` again. That second scrub must not
    mark a value a named rule already masked as an unexplained change.
    ``captured`` still accepts the record; ``complete`` still refuses it.
    """
    document = capture_tools(
        {},
        {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "exec_command",
                    "arguments": '{"command":"echo sk-1234567890abcd"}',
                }
            ]
        },
        capture_content=True,
    )
    db = MagicMock()
    usage = SimpleNamespace(id=uuid4(), flow_execution_id=uuid4(), account_id=uuid4())
    with patch(
        "preloop.services.sensitive_data.storage.resolve_config",
        return_value=_storage_config(email=False),
    ):
        persist_tool_evidence(db, usage=usage, document=document)
    stored = _inserted_document(db)
    assert stored["incomplete_reasons"] == ["masking"]
    assert stored["observations"][0]["redactions"] == [{"rule": "sk_key", "count": 1}]
    assert "sk-1234567890abcd" not in stored["observations"][0]["value"]
    assert _record_class(stored, valid=True) == "masking_only"
    summary = _gate_summary(stored)
    assert (
        evidence_satisfied(summary, "captured"),
        evidence_satisfied(summary, "complete"),
        evidence_satisfied(summary, "any"),
    ) == (True, False, True)


def test_plain_native_command_stays_ok_without_a_database() -> None:
    """A native command no rule touches stays a complete record."""
    document = _persist_native_for_test(_PLAIN_COMMAND, email_rule=False)
    assert document["capture_complete"] is True
    assert _record_class(document, valid=True) == "ok"
    assert evidence_satisfied(_gate_summary(document), "complete") is True
