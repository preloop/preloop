"""Default review result schema on the Pull Request Reviewer preset.

The schema is what lets the native-result default engage, so a review ends
with a validated verdict. It must be usable as a strict structured-output
schema (Codex sends ``strict: true``), match the document the prompt asks
for, and reach unmodified derived flows without touching customized ones.
"""

from typing import Any
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from preloop.flow_presets import FLOW_PRESETS
from preloop.models.models.flow import Flow
from preloop.services.flow_presets_service import (
    apply_preset_update_to_flow,
    compute_content_hash,
    sync_preset_to_derived_flows,
)
from preloop.services.flow_result_schema import validate_result_document
from preloop.services.native_result import resolve_native_result


def _preset(name: str) -> dict[str, Any]:
    return next(preset for preset in FLOW_PRESETS if preset["name"] == name)


PRR = _preset("Pull Request Reviewer")

VALID_RESULT = {
    "status": "success",
    "reason": None,
    "review_posted": True,
    "review_action": "request_changes",
    "recommendation": "request_changes",
    "risk_level": "medium",
    "findings_count": 1,
    "findings": [
        {
            "severity": "high",
            "category": "bug",
            "file": "calculator.py",
            "line": 2,
            "title": "Guard checks the wrong operand",
            "detail": "Check the denominator, not the numerator.",
        }
    ],
    "what_looks_good": ["Small, focused change"],
    "issue_coverage": [],
}


def _strict_objects(schema: Any, path: str = "$") -> list[str]:
    """Paths of objects that break OpenAI strict structured outputs."""
    problems: list[str] = []
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            props = set((schema.get("properties") or {}).keys())
            if schema.get("additionalProperties") is not False:
                problems.append(f"{path}: additionalProperties must be false")
            if set(schema.get("required") or []) != props:
                problems.append(f"{path}: every property must be required")
        for key, value in schema.items():
            problems.extend(_strict_objects(value, f"{path}.{key}"))
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            problems.extend(_strict_objects(value, f"{path}[{index}]"))
    return problems


def test_pull_request_reviewer_has_a_validated_review_schema():
    schema = PRR["result_schema"]
    Draft202012Validator.check_schema(schema)
    assert PRR["validate_result"] is True
    assert _strict_objects(schema) == []
    finding = schema["properties"]["findings"]["items"]["properties"]
    assert set(finding) == {"severity", "category", "file", "line", "title", "detail"}
    for key in ("status", "findings", "recommendation", "what_looks_good"):
        assert key in schema["required"]


def test_review_schema_accepts_the_documented_result_and_refuses_gaps():
    schema = PRR["result_schema"]
    assert validate_result_document(VALID_RESULT, schema)["valid"] is True
    missing = {k: v for k, v in VALID_RESULT.items() if k != "recommendation"}
    assert validate_result_document(missing, schema)["valid"] is False
    bad_severity = {
        **VALID_RESULT,
        "findings": [{**VALID_RESULT["findings"][0], "severity": "urgent"}],
    }
    assert validate_result_document(bad_severity, schema)["valid"] is False


def test_prompt_asks_for_every_required_key():
    prompt = PRR["prompt_template"]
    for key in PRR["result_schema"]["required"]:
        assert f'"{key}"' in prompt, key


def test_native_result_default_engages_for_the_preset():
    from types import SimpleNamespace

    flow = SimpleNamespace(
        agent_config=PRR["agent_config"],
        agent_type="codex",
        git_clone_config=PRR["git_clone_config"],
        result_schema=PRR["result_schema"],
        validate_result=True,
    )
    assert resolve_native_result(flow, preset_slug="pull-request-reviewer") == (
        True,
        "default",
    )


def _flow(db, *, account_id=None, preset=None, **fields):
    row = Flow(
        id=uuid4(),
        name=fields.pop("name", "Review"),
        account_id=account_id,
        is_preset=preset is None,
        is_enabled=preset is not None,
        prompt_template=fields.pop("prompt_template", "Review v2"),
        allowed_mcp_tools=fields.pop("allowed_mcp_tools", []),
        agent_config={},
        source_preset_id=preset.id if preset is not None else None,
        **fields,
    )
    db.add(row)
    db.flush()
    return row


def test_sync_gives_unmodified_flows_the_schema_and_keeps_customized(
    db_session, test_user
):
    schema = PRR["result_schema"]
    preset = _flow(db_session, result_schema=schema, validate_result=True)
    current = {
        "source_prompt_hash": compute_content_hash("Review v2"),
        "source_tools_hash": compute_content_hash([]),
    }
    plain = _flow(db_session, account_id=test_user.account_id, preset=preset, **current)
    customized = _flow(
        db_session,
        account_id=test_user.account_id,
        preset=preset,
        prompt_template="My own review prompt",
        prompt_customized=True,
        source_prompt_hash="older",
        source_tools_hash=current["source_tools_hash"],
    )
    own_schema = {"type": "object"}
    own = _flow(
        db_session,
        account_id=test_user.account_id,
        preset=preset,
        result_schema=own_schema,
        **current,
    )

    result = sync_preset_to_derived_flows(db_session, preset.id)

    assert plain.result_schema == schema and plain.validate_result is True
    assert customized.result_schema is None
    assert own.result_schema == own_schema
    assert result.auto_updated == 1


def test_applying_a_preset_update_adopts_the_schema(db_session, test_user):
    schema = PRR["result_schema"]
    preset = _flow(db_session, result_schema=schema, validate_result=True)
    flow = _flow(
        db_session,
        account_id=test_user.account_id,
        preset=preset,
        prompt_template="customized",
        prompt_customized=True,
    )
    apply_preset_update_to_flow(db_session, flow.id)
    assert flow.result_schema == schema


def test_host_result_envelope_does_not_fail_a_strict_schema():
    """Runner and control-plane keys sit next to the agent's document."""
    schema = PRR["result_schema"]
    host_result = {
        **VALID_RESULT,
        "harness": "codex_cli",
        "session_id": "11111111-1111-4111-8111-111111111111",
        "usage": {"input_tokens": 10},
        "gateway_metered": True,
        "gateway_request_count": 4,
    }
    assert validate_result_document(host_result, schema)["valid"] is True
    declared = {
        "type": "object",
        "properties": {"usage": {"type": "string"}},
        "required": ["usage"],
    }
    assert validate_result_document({"usage": {}}, declared)["valid"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded,expected", [(True, True), (False, False)])
async def test_codex_host_delivery_carries_the_native_result_schema(
    monkeypatch, recorded, expected
):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from preloop.services.host_exec_delivery import (
        HOST_EXEC_RESULT_SCHEMA_KEY,
        hydrate_host_exec_job,
    )

    flow = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        allowed_mcp_tools=None,
        allowed_mcp_servers=None,
        git_clone_config=None,
        custom_commands=None,
        result_schema=PRR["result_schema"],
        validate_result=True,
    )
    execution = SimpleNamespace(
        id=uuid4(),
        flow_id=flow.id,
        trigger_event_details={
            "_native_result": {"value": recorded, "source": "default"}
        },
    )
    monkeypatch.setattr(
        "preloop.services.host_exec_delivery.crud_flow_execution.get",
        MagicMock(return_value=execution),
    )
    monkeypatch.setattr(
        "preloop.services.host_exec_delivery.crud_flow.get",
        MagicMock(return_value=flow),
    )
    job = {
        "execution_id": str(execution.id),
        "agent_type": "codex",
        "host_exec_profile": "codex-review",
        "completion_protocol": "host_exec",
    }
    hydrated = await hydrate_host_exec_job(MagicMock(), job)
    assert (HOST_EXEC_RESULT_SCHEMA_KEY in hydrated) is expected
    if expected:
        assert hydrated[HOST_EXEC_RESULT_SCHEMA_KEY] == PRR["result_schema"]


def test_claude_host_envelope_keys_are_not_validated():
    host_result = {**VALID_RESULT, "model": "claude-x", "num_turns": 3}
    assert validate_result_document(host_result, PRR["result_schema"])["valid"]
