"""The prompt reaches the gemini and opencode CLIs on stdin, never in argv.

preloop/preloop#679 removed every oversized string from the launch the control
plane performs. It left the *inner* exec alone: inside the container the two
harnesses still built ``--prompt "$(cat <file>)"`` and ``-- "$(cat <file>)"``,
which puts the whole prompt into a single ``execve`` string. Linux caps one
such string at ``MAX_ARG_STRLEN`` (128 KiB), and the rendered prompt for the
pull request that produced preloop/preloop#609 was already 83 KiB, so the
margin was a factor of 1.6 on an ordinary payload.

These tests are the guard against a later edit putting it back. They inspect
the rendered script rather than running a real CLI, and they run the rendered
command line against a stub CLI to prove the prompt still arrives intact.
"""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from preloop.agents.completion_nudge import NUDGE_PROMPT_PATH
from preloop.agents.failure_analysis import analyze_agent_failure
from preloop.agents.gemini import GeminiAgent
from preloop.agents.opencode import OpenCodeAgent
from preloop.agents.stream_recovery import RECOVERY_PROMPT_PATH
from preloop.utils.execve_limits import (
    PROMPT_FILE_PATH,
    PROMPT_NOT_DELIVERED_MARKER,
    build_prompt_delivery_guard,
)

# Above MAX_ARG_STRLEN would be the interesting size for the kernel; 200 KiB
# is comfortably past it and still cheap to render in a unit test.
LARGE_PROMPT = "Refactor the database module and report back.\n" * 4800

SMALL_PROMPT = "Refactor the database module"

# Every file a harness may feed to a CLI as a prompt.
PROMPT_PATHS = (PROMPT_FILE_PATH, NUDGE_PROMPT_PATH, RECOVERY_PROMPT_PATH)


def gemini_script(prompt: str) -> str:
    """Render the gemini container script for ``prompt``."""
    return GeminiAgent({})._build_gemini_script(
        {
            "prompt": prompt,
            "execution_id": "exec-1",
            "flow_name": "test-flow",
        }
    )


def opencode_script(prompt: str) -> str:
    """Render the opencode container script for ``prompt``."""
    return OpenCodeAgent({})._build_opencode_script(
        {
            "prompt": prompt,
            "opencode_model": "model-1",
            "execution_id": "exec-1",
            "flow_name": "test-flow",
            "completion_nudge_enabled": True,
        }
    )


def prompt_invocations(script: str, program: str) -> list[str]:
    """Command lines in ``script`` that run ``program`` against a prompt file.

    Covers the main invocation and the nudge and recovery re-invocations: all
    of them are the inner ``execve`` this issue is about.
    """
    return [
        line
        for line in script.splitlines()
        if program in line and any(path in line for path in PROMPT_PATHS)
    ]


def cli_command(line: str) -> str:
    """The CLI invocation alone, with the log-filter pipeline cut off."""
    return line.split("| node")[0].strip()


def longest_argument(line: str) -> int:
    """Length of the longest single argument of a rendered command line."""
    return max(len(argument) for argument in shlex.split(cli_command(line)))


def make_stub(directory: Path, program: str) -> None:
    """Install a stub CLI that records its stdin and its argv."""
    directory.mkdir(parents=True, exist_ok=True)
    stub = directory / program
    stub.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do printf "%s\\n" "$arg"; done > "$PRELOOP_STUB_ARGV"\n'
        'cat > "$PRELOOP_STUB_STDIN"\n'
    )
    stub.chmod(0o755)


def run_invocation(
    tmp_path: Path,
    line: str,
    program: str,
    prompt_path: str,
    prompt: str,
    *,
    prelude: str = "",
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """Run one rendered invocation with a stub CLI on PATH.

    The prompt file is relocated into ``tmp_path`` (the container paths are
    absolute), the prompt bytes are written to it, and the log-filter pipeline
    is removed so the test needs no node.
    """
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, program)
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_bytes(prompt.encode("utf-8"))
    stdin_capture = tmp_path / "stdin.bin"
    argv_capture = tmp_path / "argv.txt"

    command = f"{prelude}\n{cli_command(line)}".replace(prompt_path, str(prompt_file))
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{bin_dir}:{environment['PATH']}",
            "PRELOOP_STUB_STDIN": str(stdin_capture),
            "PRELOOP_STUB_ARGV": str(argv_capture),
            "PRELOOP_DISABLE_TELEMETRY": "true",
        }
    )
    completed = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        env=environment,
    )
    return completed, stdin_capture, argv_capture


class TestRenderedCommandLines:
    """What the rendered script may and may not contain."""

    @pytest.mark.parametrize(
        "builder,program",
        [(gemini_script, "gemini"), (opencode_script, "opencode")],
    )
    def test_no_argument_holds_prompt_bytes(self, builder, program):
        """A 200 KiB prompt appears in no command line of the script."""
        script = builder(LARGE_PROMPT)
        assert len(LARGE_PROMPT.encode()) > 200 * 1024
        assert LARGE_PROMPT not in script
        for path in PROMPT_PATHS:
            assert f'"$(cat {path})"' not in script
            assert f"$(cat {path})" not in script
        invocations = prompt_invocations(script, program)
        assert invocations
        for line in invocations:
            assert longest_argument(line) < 512

    @pytest.mark.parametrize(
        "builder,program",
        [(gemini_script, "gemini"), (opencode_script, "opencode")],
    )
    def test_longest_argument_independent_of_prompt_size(self, builder, program):
        """The inner command line does not grow with the prompt."""
        small = prompt_invocations(builder(SMALL_PROMPT), program)
        large = prompt_invocations(builder(LARGE_PROMPT), program)
        assert small and len(small) == len(large)
        for small_line, large_line in zip(small, large, strict=False):
            assert small_line == large_line
            assert longest_argument(small_line) == longest_argument(large_line)

    def test_gemini_redirects_the_prompt_file_onto_stdin(self):
        """The main invocation reads the prompt file, with no --prompt flag."""
        script = gemini_script(SMALL_PROMPT)
        [main] = [
            line
            for line in prompt_invocations(script, "gemini")
            if "--resume" not in line
        ]
        assert f"< {PROMPT_FILE_PATH}" in main
        assert "--prompt" not in main

    def test_opencode_passes_no_positional_message(self):
        """The main invocation reads the prompt file, with no trailing ``--``."""
        script = opencode_script(SMALL_PROMPT)
        [main] = [
            line
            for line in prompt_invocations(script, "opencode")
            if "--session" not in line
        ]
        assert f"< {PROMPT_FILE_PATH}" in main
        assert " -- " not in main

    @pytest.mark.parametrize(
        "builder,program,path",
        [
            (gemini_script, "gemini", RECOVERY_PROMPT_PATH),
            (opencode_script, "opencode", RECOVERY_PROMPT_PATH),
            (opencode_script, "opencode", NUDGE_PROMPT_PATH),
        ],
    )
    def test_reinvocations_carry_no_prompt_in_argv(self, builder, program, path):
        """The nudge and recovery rounds redirect their prompt file too."""
        script = builder(SMALL_PROMPT)
        [line] = [
            candidate
            for candidate in prompt_invocations(script, program)
            if path in candidate
        ]
        assert f"< {path}" in line
        assert f"$(cat {path})" not in line

    @pytest.mark.parametrize(
        "builder,program",
        [(gemini_script, "gemini"), (opencode_script, "opencode")],
    )
    def test_script_guards_against_an_undelivered_prompt(self, builder, program):
        """The guard sits in the script, before the CLI runs."""
        script = builder(SMALL_PROMPT)
        guard = build_prompt_delivery_guard(PROMPT_FILE_PATH)
        assert guard in script
        main = [
            line
            for line in prompt_invocations(script, program)
            if PROMPT_FILE_PATH in line
        ][0]
        assert script.index(guard) < script.index(main)


class TestPromptArrivesOnStdin:
    """The rendered command line, run against a stub CLI."""

    @pytest.mark.parametrize(
        "builder,program",
        [(gemini_script, "gemini"), (opencode_script, "opencode")],
    )
    @pytest.mark.parametrize(
        "prompt",
        [
            LARGE_PROMPT,
            "Refactor the database module",
            "Grüße: τι κάνεις; 汉字 🚀 — keep every byte",
        ],
        ids=["large", "small", "multibyte"],
    )
    def test_stub_receives_the_prompt_byte_for_byte(
        self, tmp_path, builder, program, prompt
    ):
        """Everything in the prompt file reaches the CLI's stdin, unchanged."""
        script = builder(SMALL_PROMPT)
        main = [
            line
            for line in prompt_invocations(script, program)
            if PROMPT_FILE_PATH in line
        ][0]
        completed, stdin_capture, argv_capture = run_invocation(
            tmp_path, main, program, PROMPT_FILE_PATH, prompt
        )
        assert completed.returncode == 0, completed.stderr
        assert stdin_capture.read_bytes() == prompt.encode("utf-8")
        arguments = argv_capture.read_text().splitlines()
        assert all(prompt[:64] not in argument for argument in arguments)

    def test_opencode_nudge_prompt_arrives_on_stdin(self, tmp_path):
        """The reminder round delivers its prompt the same way."""
        script = opencode_script(SMALL_PROMPT)
        [nudge] = [
            line
            for line in prompt_invocations(script, "opencode")
            if NUDGE_PROMPT_PATH in line
        ]
        completed, stdin_capture, _ = run_invocation(
            tmp_path, nudge, "opencode", NUDGE_PROMPT_PATH, "You stopped early."
        )
        assert completed.returncode == 0, completed.stderr
        assert stdin_capture.read_bytes() == b"You stopped early."

    @pytest.mark.parametrize(
        "builder,program",
        [(gemini_script, "gemini"), (opencode_script, "opencode")],
    )
    def test_empty_prompt_fails_with_a_named_error(self, tmp_path, builder, program):
        """A prompt that never arrived stops the run before the CLI starts."""
        script = builder(SMALL_PROMPT)
        main = [
            line
            for line in prompt_invocations(script, program)
            if PROMPT_FILE_PATH in line
        ][0]
        guard = build_prompt_delivery_guard(PROMPT_FILE_PATH)
        completed, stdin_capture, _ = run_invocation(
            tmp_path, main, program, PROMPT_FILE_PATH, "", prelude=guard
        )
        assert completed.returncode != 0
        assert PROMPT_NOT_DELIVERED_MARKER in completed.stderr
        assert not stdin_capture.exists()

    def test_named_error_classifies_as_a_prompt_delivery_failure(self):
        """The marker becomes a verdict, not an unlabelled crash."""
        logs = (
            "Flow Execution Started\n"
            f"{PROMPT_NOT_DELIVERED_MARKER} agent prompt at {PROMPT_FILE_PATH} "
            "is missing or empty\n"
        )
        analysis = analyze_agent_failure(logs)
        assert "prompt did not reach the container" in analysis.message
        assert PROMPT_NOT_DELIVERED_MARKER in analysis.message
        assert analysis.transient is False

    def test_dropped_transport_chunk_classifies_the_same_way(self):
        """The chunk transport's own markers land in the same category."""
        logs = (
            "PRELOOP_LAUNCH_PAYLOAD_TRUNCATED /tmp/preloop/prompt.txt: "
            "got 10 bytes, expected 4096\n"
        )
        analysis = analyze_agent_failure(logs)
        assert "prompt did not reach the container" in analysis.message
        assert analysis.transient is False
