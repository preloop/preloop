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
