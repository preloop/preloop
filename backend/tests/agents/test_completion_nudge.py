"""Tests for the in-place completion nudge.

The nudge is a shell block embedded in the agent script, so the interesting
behaviour is bash behaviour: it must fire exactly when the harness exited 0
and nothing confirmed completion, and stay silent in every other case. These
tests run the rendered block with a stub "harness" and assert on its stdout,
which is the same stream the orchestrator parses.
"""

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from preloop.agents.completion_nudge import (
    AGENT_OUTPUT_LOG_PATH,
    COMPLETION_NUDGE_MARKER,
    COMPLETION_NUDGE_PROMPT,
    COMPLETION_NUDGE_RESULT_MARKER,
    COMPLETION_NUDGE_UNSUPPORTED_MARKER,
    FLOW_FAILURE_REPORT_PREFIX,
    FLOW_SUCCESS_SENTINEL,
    NUDGE_PROMPT_PATH,
    RESULT_ARTIFACT_PATH,
    build_completion_nudge_block,
    completion_nudge_enabled,
    completion_nudge_timeout_seconds,
)

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is required to exercise the block"
)


def _run_block(
    tmp_path: Path,
    *,
    exit_code: int = 0,
    output_log: str = "",
    result_json: str | None = None,
    resume_probe: str = "true",
    resume_command: str | None = None,
):
    """Render the block into a throwaway script and run it under bash."""
    output_log_path = tmp_path / "agent-output.log"
    output_log_path.write_text(output_log)
    result_path = tmp_path / "result.json"
    if result_json is not None:
        result_path.write_text(result_json)
    prompt_path = tmp_path / "nudge-prompt.txt"

    block = build_completion_nudge_block(
        agent_label="test",
        exit_code_var="HARNESS_EXIT_CODE",
        resume_probe=resume_probe,
        resume_command=(
            resume_command or f'echo "resumed with: $(cat {prompt_path} | head -1)"'
        ),
        timeout_seconds=42,
        output_log_path=str(output_log_path),
        result_path=str(result_path),
        prompt_path=str(prompt_path),
    )
    script = tmp_path / "agent.sh"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/bash
            set -e
            HARNESS_EXIT_CODE={exit_code}
            """
        )
        + block
    )
    completed = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "LC_ALL": "C"},
    )
    return completed, prompt_path


class TestNudgeFiringConditions:
    """When the reminder round runs, and when it must not."""

    def test_nudges_when_nothing_confirmed_completion(self, tmp_path):
        completed, prompt_path = _run_block(
            tmp_path, output_log="did some work\nand stopped\n"
        )

        assert completed.returncode == 0
        lines = completed.stdout.splitlines()
        assert COMPLETION_NUDGE_MARKER in lines
        assert any(
            line.startswith(f"{COMPLETION_NUDGE_RESULT_MARKER} exit=") for line in lines
        )
        # The reminder reached the harness as a prompt file, not as shell text.
        assert prompt_path.read_text() == COMPLETION_NUDGE_PROMPT

    def test_sentinel_on_its_own_line_suppresses_the_nudge(self, tmp_path):
        completed, _ = _run_block(
            tmp_path, output_log=f"work\n{FLOW_SUCCESS_SENTINEL}\n"
        )

        assert completed.stdout.strip() == ""

    def test_sentinel_inside_a_longer_line_still_nudges(self, tmp_path):
        """Matches the orchestrator's exact-line detector, not a substring."""
        completed, _ = _run_block(
            tmp_path,
            output_log=f"I will print {FLOW_SUCCESS_SENTINEL} when done\n",
        )

        assert COMPLETION_NUDGE_MARKER in completed.stdout.splitlines()

    @pytest.mark.parametrize(
        "result_json",
        [
            json.dumps({"status": "success"}),
            json.dumps({"status": "failure", "reason": "ran out of budget"}),
            json.dumps({"verdict": "pass_with_findings", "findings": []}),
        ],
    )
    def test_result_report_with_a_status_suppresses_the_nudge(
        self, tmp_path, result_json
    ):
        """Any status or verdict is the agent's answer; the orchestrator
        judges the value. Asking again would invite it to overwrite one."""
        completed, _ = _run_block(tmp_path, result_json=result_json)

        assert completed.stdout.strip() == ""

    @pytest.mark.parametrize(
        "result_json",
        [
            "",
            "{",
            json.dumps({"summary": "no status field"}),
            json.dumps(["not", "an", "object"]),
        ],
    )
    def test_unusable_result_report_still_nudges(self, tmp_path, result_json):
        completed, _ = _run_block(tmp_path, result_json=result_json)

        assert COMPLETION_NUDGE_MARKER in completed.stdout.splitlines()

    def test_non_zero_harness_exit_never_nudges(self, tmp_path):
        """A failed run is a failure, and its post-exec git block did not
        run: re-invoking here could produce the side effects the failure
        avoided."""
        completed, _ = _run_block(tmp_path, exit_code=1, output_log="crashed\n")

        assert completed.stdout.strip() == ""

    def test_runtime_without_resume_reports_unsupported(self, tmp_path):
        completed, prompt_path = _run_block(tmp_path, resume_probe="false")

        assert completed.stdout.strip() == (
            f"{COMPLETION_NUDGE_UNSUPPORTED_MARKER} test"
        )
        assert not prompt_path.exists()

    def test_failing_nudge_does_not_fail_the_script(self, tmp_path):
        """``set -e`` is in force in every agent script. A reminder round
        that errors must leave the run exactly where it was."""
        completed, _ = _run_block(
            tmp_path,
            resume_command="bash -c 'echo \"nudge broke\" >&2; exit 3'",
        )

        assert completed.returncode == 0
        assert f"{COMPLETION_NUDGE_RESULT_MARKER} exit=3" in completed.stdout

    def test_only_one_round_per_container(self, tmp_path):
        completed, _ = _run_block(tmp_path)

        assert completed.stdout.count(COMPLETION_NUDGE_MARKER + "\n") == 1


class TestNudgePromptSafety:
    """The reminder is read by a model and echoed into the same log stream."""

    def test_prompt_cannot_satisfy_the_contract_by_being_echoed(self):
        """An echoed prompt must not look like a confirmation: the sentinel
        never starts a line and the failure prefix never opens one."""
        for line in COMPLETION_NUDGE_PROMPT.splitlines():
            assert line.strip() != FLOW_SUCCESS_SENTINEL
            assert not line.startswith(FLOW_FAILURE_REPORT_PREFIX)

    def test_prompt_forbids_new_work_and_side_effects(self):
        lowered = COMPLETION_NUDGE_PROMPT.lower()
        assert "do not redo" in lowered
        assert "side effects" in lowered
        assert lowered.rstrip().endswith("then stop.")

    def test_prompt_names_both_channels_and_the_result_path(self):
        assert FLOW_SUCCESS_SENTINEL in COMPLETION_NUDGE_PROMPT
        assert FLOW_FAILURE_REPORT_PREFIX in COMPLETION_NUDGE_PROMPT
        assert RESULT_ARTIFACT_PATH in COMPLETION_NUDGE_PROMPT

    def test_prompt_has_no_em_dashes(self):
        assert "—" not in COMPLETION_NUDGE_PROMPT


class TestNudgeSettings:
    """Kill switch and wall clock."""

    def test_enabled_by_default(self):
        assert completion_nudge_enabled({}) is True

    def test_disabled_for_the_orchestrators_own_confirmation_round(self):
        assert completion_nudge_enabled({"confirmation_nudge": True}) is False

    def test_per_execution_override_wins(self):
        assert (
            completion_nudge_enabled(
                {"confirmation_nudge": True, "completion_nudge_enabled": True}
            )
            is True
        )
        assert completion_nudge_enabled({"completion_nudge_enabled": False}) is False

    def test_global_kill_switch(self):
        with patch(
            "preloop.agents.completion_nudge.settings.flow_completion_nudge_enabled",
            False,
        ):
            assert completion_nudge_enabled({}) is False

    def test_timeout_has_a_floor(self):
        with patch(
            "preloop.agents.completion_nudge.settings."
            "flow_completion_nudge_timeout_seconds",
            1,
        ):
            assert completion_nudge_timeout_seconds() == 30


class TestNudgeContractConstants:
    """The markers and vocabulary are a contract with the orchestrator."""

    def test_constants_match_the_orchestrator(self):
        from preloop.services import flow_orchestrator as orch

        assert orch.FLOW_SUCCESS_SENTINEL == FLOW_SUCCESS_SENTINEL
        assert orch.FLOW_FAILURE_REPORT_PREFIX == FLOW_FAILURE_REPORT_PREFIX
        assert orch.COMPLETION_NUDGE_MARKER == COMPLETION_NUDGE_MARKER
        assert orch.COMPLETION_NUDGE_RESULT_MARKER == COMPLETION_NUDGE_RESULT_MARKER
        assert (
            orch.COMPLETION_NUDGE_UNSUPPORTED_MARKER
            == COMPLETION_NUDGE_UNSUPPORTED_MARKER
        )

    def test_result_marker_shares_the_start_marker_prefix(self):
        """Why the orchestrator must match the start marker exactly."""
        assert COMPLETION_NUDGE_RESULT_MARKER.startswith(COMPLETION_NUDGE_MARKER)


class TestRuntimeCapability:
    """Which runtimes carry the in-place block."""

    def test_resumable_runtimes_opt_in(self):
        from preloop.agents.codex import CodexAgent
        from preloop.agents.opencode import OpenCodeAgent

        assert CodexAgent.supports_inplace_completion_nudge is True
        assert OpenCodeAgent.supports_inplace_completion_nudge is True

    def test_runtimes_without_resume_stay_out(self):
        from preloop.agents.aider import AiderAgent
        from preloop.agents.base import AgentExecutor
        from preloop.agents.gemini import GeminiAgent
        from preloop.agents.openhands import OpenHandsAgent
        from preloop.agents.remote_runner import RemoteRunnerExecutor

        assert AgentExecutor.supports_inplace_completion_nudge is False
        assert AiderAgent.supports_inplace_completion_nudge is False
        assert GeminiAgent.supports_inplace_completion_nudge is False
        assert OpenHandsAgent.supports_inplace_completion_nudge is False
        assert RemoteRunnerExecutor.supports_inplace_completion_nudge is False


class TestGeneratedAgentScripts:
    """The block as it lands in the real agent scripts."""

    def _codex_context(self, **overrides):
        context = {
            "prompt": "do the work",
            "model_identifier": "gpt-5.4",
            "agent_config": {},
        }
        context.update(overrides)
        return context

    def _script(self, agent_cls, builder, context):
        agent = agent_cls({})
        return getattr(agent, builder)(context)

    @pytest.mark.parametrize(
        "module,cls_name,builder",
        [
            ("preloop.agents.codex", "CodexAgent", "_build_codex_script"),
            ("preloop.agents.opencode", "OpenCodeAgent", "_build_opencode_script"),
        ],
    )
    def test_script_is_valid_bash_and_nudges_before_the_git_block(
        self, tmp_path, module, cls_name, builder
    ):
        import importlib

        agent_cls = getattr(importlib.import_module(module), cls_name)
        script = self._script(agent_cls, builder, self._codex_context())

        script_path = tmp_path / "agent.sh"
        script_path.write_text(script)
        syntax = subprocess.run(
            ["bash", "-n", str(script_path)], capture_output=True, text=True
        )
        assert syntax.returncode == 0, syntax.stderr

        assert COMPLETION_NUDGE_MARKER in script
        assert AGENT_OUTPUT_LOG_PATH in script
        assert NUDGE_PROMPT_PATH in script
        # The reminder must be strictly before anything that pushes.
        nudge_at = script.index(COMPLETION_NUDGE_MARKER)
        for side_effect in ("git push", "git commit"):
            if side_effect in script:
                assert nudge_at < script.index(side_effect), side_effect

    @pytest.mark.parametrize(
        "module,cls_name,builder",
        [
            ("preloop.agents.codex", "CodexAgent", "_build_codex_script"),
            ("preloop.agents.opencode", "OpenCodeAgent", "_build_opencode_script"),
        ],
    )
    def test_confirmation_round_script_carries_no_nudge(
        self, module, cls_name, builder
    ):
        """No nudge inside the orchestrator's own nudge session."""
        import importlib

        agent_cls = getattr(importlib.import_module(module), cls_name)
        script = self._script(
            agent_cls, builder, self._codex_context(confirmation_nudge=True)
        )

        assert COMPLETION_NUDGE_MARKER not in script
