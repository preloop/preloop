"""Regression for preloop/preloop#609: no launch string may reach MAX_ARG_STRLEN.

PR 609 was a Dependabot bump whose body is three packages' release notes,
34,908 bytes of HTML. Preset 002 interpolated that body into a 48,693 byte
template, producing an 83,358 byte prompt; the opencode launcher base64'd it
into its own script, which came to 132,681 bytes, 1,609 past the 131,072 the
kernel allows for one execve string. The pod never started and the reviewer
reported ``exec /bin/bash: argument list too long``.

These tests drive the real preset and the real script builders with a
deliberately larger body than #609 had, and assert the property that failed:
every individual string the launcher hands to execve stays under the limit.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from preloop.agents.codex import CodexAgent
from preloop.agents.container import (
    K8S_ARTIFACT_WRAPPER_SCRIPT,
    K8S_INNER_SCRIPT_ENV_PREFIX,
    ContainerAgentExecutor,
)
from preloop.agents.errors import AgentStartError
from preloop.agents.gemini import GeminiAgent
from preloop.agents.openhands import OpenHandsAgent
from preloop.agents.opencode import OpenCodeAgent
from preloop.services.flow_failure_category import (
    FAILURE_CATEGORY_LAUNCH_PAYLOAD_TOO_LARGE,
)
from preloop.utils.execve_limits import (
    MAX_ARG_STRLEN,
    MAX_LAUNCH_STRING_BYTES,
    chunked_env,
    largest_launch_string,
    prompt_transport_env,
)
from preloop.utils.prompt_filters import parse_placeholders, truncate_value

PRESET_PATH = (
    Path(__file__).resolve().parents[2] / "presets" / "002-pull-request-reviewer.yaml"
)

# Bigger than PR 609's real body (34,908 bytes), so the test keeps failing if
# the cap is removed even though #609 itself would now squeak through.
HUGE_DESCRIPTION_BYTES = 200 * 1024


def _huge_description() -> str:
    """A release-note shaped body of ~200 KiB, in the style that broke #609."""
    entry = (
        "<h4>Bumps <code>@sentry/browser</code> from 10.73.0 to 10.74.1</h4>\n"
        "<blockquote><p>Fixes a leak in the replay integration and restores "
        "the `beforeSend` contract for transport errors.</p></blockquote>\n"
        '<li><a href="https://github.com/getsentry/sentry-javascript/commit/'
        '0123456789abcdef0123456789abcdef01234567">0123456</a> chore: bump</li>\n'
    )
    body = entry * (HUGE_DESCRIPTION_BYTES // len(entry) + 1)
    assert len(body.encode()) >= HUGE_DESCRIPTION_BYTES
    return body


def _render_preset(description: str) -> str:
    """Render preset 002 the way the orchestrator does, filters included."""
    template = yaml.safe_load(PRESET_PATH.read_text())["prompt_template"]
    values = {
        "project.name": "preloop/preloop",
        "trigger_event.type": "pull_request_opened",
        "trigger_event.payload.object_attributes.title": (
            "Bump the npm-minor-patch group in /frontend with 3 updates"
        ),
        "trigger_event.payload.object_attributes.description": description,
        "trigger_event.payload.object_attributes.author": "dependabot[bot]",
        "trigger_event.payload.object_attributes.url": (
            "https://github.com/preloop/preloop/pull/609"
        ),
        "trigger_event.payload.object_attributes.source_branch": (
            "dependabot/npm_and_yarn/frontend/npm-minor-patch-4f3d2e"
        ),
        "trigger_event.payload.object_attributes.target_branch": "main",
        "trigger_event.payload.object_attributes.referenced_issues": "[]",
    }
    rendered = template
    seen: set[str] = set()
    for item in parse_placeholders(template):
        if item.raw in seen or item.name not in values:
            continue
        seen.add(item.raw)
        rendered = rendered.replace(
            item.raw, truncate_value(values[item.name], item.limit)
        )
    return rendered


def _agent_context(prompt: str, harness: str) -> dict:
    return {
        "prompt": prompt,
        "execution_id": "a8844f7a-ab85-4ef6-9b7d-3282f5f10398",
        "flow_id": "3f2c1d4e-0000-4000-8000-000000000001",
        "flow_name": "pull-request-reviewer",
        "agent_config": {},
        "model_identifier": "claude-sonnet-4-5",
        "model_provider": "openai" if harness == "codex" else "anthropic",
        "model_api_key": "sk-test",
        "opencode_model": "claude-sonnet-4-5",
        "codex_model": "gpt-5.4",
        "gemini_model": "gemini-3-pro-preview",
    }


BUILDERS = {
    "codex": (CodexAgent, "_build_codex_script"),
    "gemini": (GeminiAgent, "_build_gemini_script"),
    "opencode": (OpenCodeAgent, "_build_opencode_script"),
    "openhands": (OpenHandsAgent, "_build_openhands_script"),
}


class TestPresetHygiene:
    def test_the_preset_caps_the_description_it_injects(self):
        rendered = _render_preset(_huge_description())
        # The whole prompt is now smaller than the description alone was.
        assert len(rendered.encode()) < HUGE_DESCRIPTION_BYTES
        assert "[truncated by Preloop" in rendered

    def test_a_short_description_is_injected_whole_and_unmarked(self):
        rendered = _render_preset("Closes #123. One line.")
        assert "Closes #123. One line." in rendered
        assert "[truncated by Preloop" not in rendered


@pytest.mark.parametrize("harness", sorted(BUILDERS))
class TestNoLauncherStringReachesTheKernelLimit:
    def test_the_generated_script_stays_far_under_the_limit(self, harness):
        agent_class, builder = BUILDERS[harness]
        prompt = _render_preset(_huge_description())
        script = getattr(agent_class({}), builder)(_agent_context(prompt, harness))
        assert len(script.encode()) < MAX_LAUNCH_STRING_BYTES
        # And the prompt is not what makes it big: none of it is in there.
        assert prompt[:200] not in script

    def test_every_execve_string_of_the_kubernetes_launch_is_legal(self, harness):
        """The full Job payload: args, wrapper, script chunks, prompt chunks."""
        agent_class, builder = BUILDERS[harness]
        prompt = _render_preset(_huge_description())
        script = getattr(agent_class({}), builder)(_agent_context(prompt, harness))

        wrapped = ContainerAgentExecutor._wrap_kubernetes_args_for_artifacts(
            ["-c", script]
        )
        assert wrapped is not None
        args, inner_script = wrapped
        assert args == ["-c", K8S_ARTIFACT_WRAPPER_SCRIPT]

        env = {
            "FLOW_ID": "3f2c1d4e-0000-4000-8000-000000000001",
            "EXECUTION_ID": "a8844f7a-ab85-4ef6-9b7d-3282f5f10398",
            **prompt_transport_env(prompt),
            **chunked_env(K8S_INNER_SCRIPT_ENV_PREFIX, inner_script),
        }
        biggest = largest_launch_string(args=args, env=env)
        assert biggest.size < MAX_ARG_STRLEN, biggest
        assert biggest.size < MAX_LAUNCH_STRING_BYTES, biggest

    def test_an_uncapped_description_would_also_survive_the_transport(self, harness):
        """Defence in depth: the transport holds even without the preset cap.

        The preset cap and the chunked transport are independent fixes. If a
        user writes a template without the filter, or a future preset forgets
        it, the launch must still start.
        """
        agent_class, builder = BUILDERS[harness]
        prompt = "PR body:\n" + _huge_description()  # 200 KiB, uncapped
        script = getattr(agent_class({}), builder)(_agent_context(prompt, harness))
        env = {
            **prompt_transport_env(prompt),
            **chunked_env(K8S_INNER_SCRIPT_ENV_PREFIX, script),
        }
        biggest = largest_launch_string(
            args=["-c", K8S_ARTIFACT_WRAPPER_SCRIPT], env=env
        )
        assert biggest.size < MAX_LAUNCH_STRING_BYTES, biggest


class TestThePreLaunchGuard:
    """The guard turns an E2BIG into an execution error that names the cause."""

    def _executor(self) -> ContainerAgentExecutor:
        executor = ContainerAgentExecutor.__new__(ContainerAgentExecutor)
        executor.logger = MagicMock()
        executor.agent_type = "opencode"
        return executor

    def test_an_oversized_docker_config_is_refused_before_creation(self):
        executor = self._executor()
        config = {
            "Env": ["AGENT_PROMPT=" + "x" * MAX_LAUNCH_STRING_BYTES],
            "Cmd": ["-c", "true"],
        }
        with pytest.raises(AgentStartError) as excinfo:
            executor._guard_docker_launch_payload(config, what="opencode container")
        assert excinfo.value.category == FAILURE_CATEGORY_LAUNCH_PAYLOAD_TOO_LARGE
        assert "env[AGENT_PROMPT]" in str(excinfo.value)
        assert "opencode container" in str(excinfo.value)
        executor.logger.warning.assert_called()

    def test_a_legal_docker_config_is_accepted_and_its_sizes_are_logged(self):
        executor = self._executor()
        config = {"Env": ["AGENT_CONFIG={}"], "Cmd": ["-c", "true"]}
        executor._guard_docker_launch_payload(config, what="opencode container")
        # Sizes are logged even on the happy path: "we are at 92% of the
        # limit" is the only warning that precedes the failure.
        assert executor.logger.warning.call_count == 1
        assert "largest execve string" in executor.logger.warning.call_args[0][0]

    def test_the_guard_names_the_job_not_just_the_string(self):
        executor = self._executor()
        with pytest.raises(AgentStartError) as excinfo:
            executor._guard_launch_payload(
                args=["-c", "x" * MAX_LAUNCH_STRING_BYTES],
                what="agent Job agent-a8844f7a",
            )
        assert "agent Job agent-a8844f7a" in str(excinfo.value)
        assert excinfo.value.category == FAILURE_CATEGORY_LAUNCH_PAYLOAD_TOO_LARGE


class TestTheClassificationStaysCorrect:
    def test_the_guard_message_classifies_as_launch_payload_too_large(self):
        """Even without the explicit category, the text must classify right.

        The orchestrator prefers the runner's explicit category, but the
        message is also stored and re-derived in places that only have text.
        """
        from preloop.services.flow_failure_category import derive_failure_category

        executor = ContainerAgentExecutor.__new__(ContainerAgentExecutor)
        executor.logger = MagicMock()
        executor.agent_type = "opencode"
        with pytest.raises(AgentStartError) as excinfo:
            executor._guard_launch_payload(
                args=["-c", "x" * MAX_LAUNCH_STRING_BYTES], what="agent Job agent-x"
            )
        assert (
            derive_failure_category(status="FAILED", error_message=str(excinfo.value))
            == FAILURE_CATEGORY_LAUNCH_PAYLOAD_TOO_LARGE
        )
