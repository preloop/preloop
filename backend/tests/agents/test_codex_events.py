"""Codex machine events cannot confirm completion through echoed input."""

import json
import subprocess
import sys

import pytest

from preloop.agents.codex_events import CODEX_EVENT_FILTER_SCRIPT

MARKER = "FLOW_EXECUTION_SUCCESS"


def run_filter(lines: list[str]) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-u", "-c", CODEX_EVENT_FILTER_SCRIPT],
        input="\n".join(lines) + "\n",
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.splitlines()


@pytest.mark.parametrize("kind", ["command_execution", "user_message", "error"])
def test_untrusted_event_text_cannot_confirm_success(kind: str) -> None:
    event = {"type": "item.completed", "item": {"type": kind, "text": MARKER}}
    output = run_filter([json.dumps(event), json.dumps({"type": "turn.completed"})])
    assert MARKER not in output


def test_actual_plaintext_prompt_echo_and_malformed_diagnostics_are_inert() -> None:
    # Codex 0.153.4 echoed this sequence after the wrapper's exec-start marker
    # in a failed-network installation run, with no model output or usage.
    output = run_filter(
        [
            "Reading prompt from stdin...",
            "OpenAI Codex v0.153.4",
            "user",
            "Output these two lines:",
            "EE0171_FLOW_OK",
            MARKER,
            '{"type":"item.completed",broken',
            "tokens used",
            "1234",
        ]
    )
    assert MARKER not in output
    assert "tokens used" not in output


def test_assistant_completion_preserves_final_output_and_usage() -> None:
    output = run_filter(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "EE0171_FLOW_OK\n" + MARKER,
                    },
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 8805,
                        "cached_input_tokens": 100,
                        "output_tokens": 116,
                    },
                }
            ),
        ]
    )
    assert MARKER in output
    assert "EE0171_FLOW_OK" in output
    assert output[-2:] == ["tokens used", "8,921"]


def test_incomplete_or_failed_turn_does_not_confirm_success() -> None:
    message = json.dumps(
        {"type": "item.completed", "item": {"type": "agent_message", "text": MARKER}}
    )
    assert MARKER not in run_filter([message])
    assert MARKER not in run_filter([message, '{"type":"turn.failed"}'])


def test_only_last_assistant_message_confirms_completed_turn() -> None:
    messages = [
        json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": text}}
        )
        for text in [MARKER, "Unable to finish"]
    ]
    output = run_filter(messages + ['{"type":"turn.completed"}'])
    assert MARKER not in output
    assert "Unable to finish" in output


def test_codex_generated_pipeline_preserves_nonzero_cli_exit(tmp_path) -> None:
    from preloop.agents.codex import CodexAgent
    from preloop.utils.execve_limits import PROMPT_FILE_PATH

    script = CodexAgent({})._build_codex_script({"prompt": "synthetic check"})
    start = script.index('cat "' + PROMPT_FILE_PATH + '" | codex exec')
    end = script.index("set -e", start)
    prompt = tmp_path / "prompt"
    prompt.write_text("synthetic check")
    pipeline = script[start:end].replace(PROMPT_FILE_PATH, str(prompt))
    # Keep wrapper log files inside this test's directory.
    from preloop.agents.completion_nudge import AGENT_OUTPUT_LOG_PATH
    from preloop.agents.stream_recovery import ATTEMPT_LOG_PATH

    pipeline = pipeline.replace(AGENT_OUTPUT_LOG_PATH, str(tmp_path / "output"))
    pipeline = pipeline.replace(ATTEMPT_LOG_PATH, str(tmp_path / "attempt"))
    shell = 'codex() { echo \'{"type":"turn.failed"}\'; return 42; };\n'
    result = subprocess.run(
        ["bash", "-c", shell + pipeline + 'exit "$CODEX_EXIT_CODE"'],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 42, result.stderr
    assert MARKER not in result.stdout.splitlines()


def test_live_nudge_preserves_cli_exit_under_sh(tmp_path, monkeypatch) -> None:
    import os

    from preloop.agents.codex import CodexAgent
    from preloop.agents.container import LIVE_NUDGE_LOG_PATH, LIVE_NUDGE_PROMPT_PATH

    stub = tmp_path / "codex"
    stub.write_text("#!/bin/sh\nexit 43\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    prompt = tmp_path / "prompt"
    prompt.write_text("synthetic nudge")
    agent = CodexAgent({})
    agent._build_codex_script({"prompt": "synthetic check"})
    command = agent.live_nudge_command.replace(LIVE_NUDGE_PROMPT_PATH, str(prompt))
    command = command.replace(LIVE_NUDGE_LOG_PATH, str(tmp_path / "output"))
    result = subprocess.run(["sh", "-c", command], capture_output=True, text=True)
    assert result.returncode == 43, result.stderr
