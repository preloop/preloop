"""Native final-answer capture remains distinct from capture-time validation."""

import base64
import json

import pytest

from preloop.agents.codex import CodexAgent

SCHEMA = {
    "type": "object",
    "required": ["status", "verdict"],
    "properties": {"status": {"type": "string"}, "verdict": {"type": "string"}},
    "additionalProperties": False,
}


def test_native_output_materializes_exact_schema_without_shell_interpolation() -> None:
    schema = {**SCHEMA, "description": "literal $(touch /tmp/forbidden) `id`"}
    setup, arguments = CodexAgent._native_result_output(
        {"agent_config": {"native_result": True}, "result_schema": schema}
    )
    encoded = setup.split("printf %s ", 1)[1].split(" |", 1)[0]
    assert json.loads(base64.b64decode(encoded)) == schema
    assert "$(touch" not in setup
    assert "--output-schema" in arguments
    assert "--output-last-message /workspace/result.json" in arguments
    assert "cannot resume" in setup


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"result_schema": SCHEMA, "validate_result": False},
        {"result_schema": SCHEMA, "cli_session_restore_archive": "archive"},
    ],
)
def test_native_result_does_not_silently_drop_schema(extra: dict) -> None:
    with pytest.raises(ValueError):
        CodexAgent._native_result_output(
            {"agent_config": {"native_result": True}, **extra}
        )


def test_capture_schema_alone_does_not_change_generation_mode() -> None:
    assert CodexAgent._native_result_output({"result_schema": SCHEMA}) == ("", "")


def test_native_script_disables_unconstrained_followup_and_clears_stale_result() -> (
    None
):
    agent = CodexAgent({})
    script = agent._build_codex_script(
        {
            "prompt": "Return native result",
            "execution_id": "fixture",
            "flow_name": "fixture",
            "agent_config": {"native_result": True},
            "result_schema": SCHEMA,
        }
    )
    assert agent.supports_confirmation_nudge is False
    assert not agent.live_nudge_command
    assert "rm -f -- /workspace/result.json" in script
    assert "--output-schema /tmp/preloop-native-result-schema.json" in script
    assert "PRELOOP_COMPLETION_NUDGE_START" not in script


@pytest.mark.parametrize(
    "policy",
    [
        {"feedback": {"enabled": True}},
        {"max_resumes_per_pr": 2},
        {"cli_session_restore_archive": "archive"},
        {"native_session_reference": {"session_id": "prior"}},
        {"confirmation_nudge": True},
    ],
)
def test_native_result_save_rejects_resume_policies(policy: dict) -> None:
    from pydantic import ValidationError
    from preloop.models.schemas.flow import FlowCreate, FlowUpdate

    config = {"native_result": True, **policy}
    with pytest.raises(ValidationError, match="native_result"):
        FlowCreate(
            name="Review",
            prompt_template="Review",
            agent_type="codex",
            agent_config=config,
            result_schema=SCHEMA,
        )
    with pytest.raises(ValidationError, match="native_result"):
        FlowUpdate(agent_config=config)


def test_native_result_admission_refuses_historical_resume_without_cold_fallback() -> (
    None
):
    from types import SimpleNamespace
    from preloop.services.native_result import validate_native_result_admission

    flow = SimpleNamespace(
        agent_config={"native_result": True, "max_resumes_per_pr": 0}
    )
    validate_native_result_admission(flow, {})
    with pytest.raises(ValueError, match="cold start"):
        validate_native_result_admission(
            flow,
            {
                "_resume": {
                    "execution_id": "prior",
                    "cli_session": {"session_id": "prior"},
                }
            },
        )
    flow.agent_config["feedback"] = {"enabled": True}
    with pytest.raises(ValueError, match="feedback"):
        validate_native_result_admission(flow)
    flow.agent_config["native_result"] = False
    validate_native_result_admission(flow, {"_resume": {"execution_id": "prior"}})
