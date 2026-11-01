"""Native structured results default to on for Codex review flows.

An explicit ``agent_config.native_result`` always wins. Absent, it defaults
on for Codex flows in read-only review mode and for flows from the Codex
review presets, provided they have an enabled result schema. The
no-confirmation failure for a Codex run names the flag and the doc section.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.services.native_result import (
    NATIVE_RESULT_DOC,
    NATIVE_RESULT_RECORD_KEY,
    apply_native_result_default,
    native_result_cold_start,
    resolve_native_result,
)

SCHEMA = {"type": "object", "required": ["status"]}
READ_ONLY = {"enabled": True, "publication_mode": "read_only"}


def _flow(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "agent_type": "codex",
        "agent_config": {},
        "git_clone_config": READ_ONLY,
        "result_schema": SCHEMA,
        "validate_result": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_read_only_review_defaults_native_result_on() -> None:
    assert resolve_native_result(_flow()) == (True, "default")


def test_codex_review_preset_defaults_native_result_on() -> None:
    flow = _flow(git_clone_config={"enabled": True})
    assert resolve_native_result(flow, preset_slug="pull-request-reviewer") == (
        True,
        "default",
    )
    assert resolve_native_result(flow, preset_slug="docs-currency-review")[0]
    # Not a review preset, not read-only: unchanged (off).
    assert resolve_native_result(flow, preset_slug="observe-eval") == (
        False,
        "default",
    )
    assert resolve_native_result(flow) == (False, "default")


@pytest.mark.parametrize("value", [True, False])
def test_explicit_value_is_respected(value: bool) -> None:
    flow = _flow(agent_config={"native_result": value})
    assert resolve_native_result(flow, preset_slug="pull-request-reviewer") == (
        value,
        "flow",
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"result_schema": None},
        {"validate_result": False},
        {"agent_type": "opencode"},
    ],
)
def test_default_needs_codex_and_an_enabled_schema(overrides: dict) -> None:
    # Native capture constrains to the schema, so there is nothing to turn on
    # without one; other runtimes do not read the flag.
    assert resolve_native_result(_flow(**overrides)) == (False, "default")


def test_apply_default_fills_only_an_absent_key() -> None:
    original = {"sandbox_type": "read-only"}
    applied = apply_native_result_default(original, True)
    assert applied == {"sandbox_type": "read-only", "native_result": True}
    assert "native_result" not in original
    explicit = {"native_result": False}
    assert apply_native_result_default(explicit, True) is explicit


def test_cold_start_uses_the_effective_value() -> None:
    flow = _flow()
    event = {"_resume": {"execution_id": "prior"}}
    assert not native_result_cold_start(flow, event)
    assert native_result_cold_start(flow, event, enabled=True)


def _orchestrator_stub(flow: SimpleNamespace, **extra: Any) -> SimpleNamespace:
    stub = SimpleNamespace(flow=flow, db=MagicMock(), execution_log=None, **extra)
    stub._flow_preset_slug = lambda: extra.get("preset_slug")
    return stub


def test_orchestrator_records_the_effective_value_on_the_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = SimpleNamespace(id="exec-1", trigger_event_details={"a": 1})
    updates: list[dict] = []

    def fake_update(db: Any, *, db_obj: Any, obj_in: dict) -> Any:
        updates.append(obj_in)
        db_obj.trigger_event_details = obj_in["trigger_event_details"]
        return db_obj

    from preloop.models import crud

    monkeypatch.setattr(crud.crud_flow_execution, "update", fake_update)
    stub = _orchestrator_stub(_flow())
    stub.execution_log = execution

    assert FlowExecutionOrchestrator._resolve_native_result(stub) is True
    assert execution.trigger_event_details == {
        "a": 1,
        NATIVE_RESULT_RECORD_KEY: {"value": True, "source": "default"},
    }
    stub.flow.agent_config = {"keep": 1}
    assert FlowExecutionOrchestrator._run_agent_config(stub) == {
        "keep": 1,
        "native_result": True,
    }


@pytest.mark.parametrize(
    ("enabled", "phrase"),
    [(False, "native_result was off"), (True, "native_result was on")],
)
def test_no_confirmation_hint_names_the_flag_and_the_doc(
    enabled: bool, phrase: str
) -> None:
    stub = _orchestrator_stub(_flow(), agent_type="codex")
    stub._native_result_effective = enabled
    hint = FlowExecutionOrchestrator._native_result_hint(stub)
    assert phrase in hint
    assert "agent_config.native_result" in hint
    assert NATIVE_RESULT_DOC in hint


def test_no_confirmation_hint_is_codex_only() -> None:
    stub = _orchestrator_stub(_flow(agent_type="opencode"), agent_type="opencode")
    assert FlowExecutionOrchestrator._native_result_hint(stub) == ""
