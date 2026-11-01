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
    assert 'CODEX_RESUME_ARGS=""' in setup
    assert "starts cold" in setup


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"result_schema": SCHEMA, "validate_result": False},
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
    [{"feedback": {"enabled": True}}, {"max_resumes_per_pr": 2}],
)
def test_native_result_save_accepts_follow_up_policies(policy: dict) -> None:
    """Follow-up rounds start cold, so feedback policies remain valid."""
    from preloop.models.schemas.flow import FlowCreate, FlowUpdate

    config = {"native_result": True, **policy}
    FlowCreate(
        name="Review",
        prompt_template="Review",
        agent_type="codex",
        agent_config=config,
        result_schema=SCHEMA,
    )
    FlowUpdate(agent_config=config)


def test_native_result_cold_start_detects_resumed_rounds() -> None:
    from types import SimpleNamespace

    from preloop.services.native_result import native_result_cold_start

    flow = SimpleNamespace(agent_config={"native_result": True})
    assert not native_result_cold_start(flow, {})
    assert native_result_cold_start(flow, {"_resume": {"execution_id": "prior"}})
    flow.agent_config["native_result"] = False
    assert not native_result_cold_start(flow, {"_resume": {"execution_id": "prior"}})


def _resumed_context() -> dict:
    return {
        "prompt": "Return native result",
        "execution_id": "fixture",
        "flow_name": "fixture",
        "agent_config": {"native_result": True},
        "result_schema": SCHEMA,
        "cli_session_restore_archive": b"archive-bytes",
        "native_session_reference": {"artifact_id": "prior"},
        "trigger_event_data": {
            "_resume": {
                "execution_id": "prior",
                "cli_session": {
                    "agent_type": "codex",
                    "session_id": "019a0000-0000-7000-8000-000000000000",
                },
            }
        },
    }


def test_native_result_resume_builds_cold_start_script() -> None:
    """A resumed round builds a cold command instead of raising."""
    context = _resumed_context()
    agent = CodexAgent({})
    script = agent._build_codex_script(context)
    assert "--output-schema /tmp/preloop-native-result-schema.json" in script
    assert "starts cold" in script
    assert context["cli_session_restore_archive"] == b"archive-bytes"
    plain = CodexAgent({})._build_codex_script(
        {**context, "agent_config": {}, "result_schema": None}
    )
    # The archive restore is only present when native_result is off.
    assert script.count("base64 -d") < plain.count("base64 -d")


def test_native_result_runtime_guard_clears_resume_instead_of_exiting(
    tmp_path,
) -> None:
    """A session restored at runtime (Docker volume) is not resumed or fatal."""
    import subprocess

    import preloop.agents.codex as codex_module

    setup, _ = CodexAgent._native_result_output(
        {"agent_config": {"native_result": True}, "result_schema": SCHEMA}
    )
    log = tmp_path / "agent.log"
    setup = setup.replace(str(codex_module.AGENT_OUTPUT_LOG_PATH), str(log))
    setup = setup.replace("/tmp/preloop-native-result-schema.json", str(tmp_path / "s"))
    setup = setup.replace("/workspace/result.json", str(tmp_path / "r"))
    result = subprocess.run(
        [
            "bash",
            "-c",
            'set -e; CODEX_RESUME_ARGS="resume prior"; PRELOOP_CLI_SESSION_RESTORED=1\n'
            + setup
            + 'echo "args=[$CODEX_RESUME_ARGS] restored=$PRELOOP_CLI_SESSION_RESTORED"',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "args=[] restored=0" in result.stdout
    assert "starts cold" in log.read_text()
