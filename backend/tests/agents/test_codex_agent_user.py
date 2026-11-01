"""Hosted Codex runs as an unprivileged user that cannot change its hook (#2142).

Root writes the Codex configuration, the ``PreToolUse`` hook and its trust
state, then every ``codex`` launch goes through a root-owned launcher that
drops to ``preloop-agent``. These tests cover the launch script; the
container test in ``test_codex_agent_user_container.py`` checks the
permissions in the image.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from preloop.agents.codex import CodexAgent
from preloop.agents.codex_native_hook import (
    _LAUNCHER,
    AGENT_LAUNCHER_PATH,
    AGENT_USER,
    AGENT_USER_LINE,
    LOCKED_CODEX_FILES,
    codex_agent_user_block,
    codex_reclaim_workspace_block,
)


def _context(**config):
    return {
        "prompt": "Implement the change",
        "execution_id": "fixture",
        "flow_name": "fixture",
        "agent_config": config,
    }


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_codex_drops_privileges_after_the_hook_and_before_exec():
    script = CodexAgent({})._build_codex_script(
        {**_context(), "native_hook_required": True}
    )
    hook_at = script.index("Preloop native command policy")
    drop_at = script.index("Run Codex as the unprivileged agent user")
    assert hook_at < drop_at < script.index("PRELOOP_AGENT_EXEC_START")
    assert "$PRELOOP_AS_AGENT codex exec $CODEX_RESUME_ARGS" in script


def test_every_codex_launch_goes_through_the_launcher():
    agent = CodexAgent({})
    script = agent._build_codex_script({**_context(), "native_hook_required": True})
    launches = [
        m
        for m in re.finditer(r"codex exec(?! --help)", script)
        # "codex exec" inside comments and echo text is not a launch.
        if not script[script.rfind("\n", 0, m.start()) + 1 : m.start()]
        .lstrip()
        .startswith(("#", "echo"))
    ]
    assert launches, "expected codex exec launches"
    for match in launches:
        before = script[max(0, match.start() - 40) : match.start()]
        assert before.rstrip().endswith("$PRELOOP_AS_AGENT"), before
    assert AGENT_LAUNCHER_PATH in agent.live_nudge_command
    assert "$_pl_as_agent codex exec resume --last" in agent.live_nudge_command


def test_read_only_sandbox_also_runs_as_the_agent_user():
    script = CodexAgent({})._build_codex_script(_context(sandbox_type="read-only"))
    assert "Run Codex as the unprivileged agent user" in script
    assert "--disable shell_tool" in script


def test_block_locks_the_hook_files_and_cli():
    block = codex_agent_user_block()
    for name in ("config.toml", "hooks.json", "auth.json"):
        assert name in LOCKED_CODEX_FILES
    assert f"chown -h root:{AGENT_USER}" in block
    assert 'chmod 1770 "$CODEX_HOME"' in block
    # The hook CLI and node are execute-only: not writable, not dumpable.
    assert 'chmod 0711 "$_pl_bin"' in block
    assert "${_pl_cli:-}" in block
    assert "--no-new-privs" in _LAUNCHER
    assert "--bounding-set=-all" in _LAUNCHER
    assert "--inh-caps=-all" in _LAUNCHER


def _run_block(tmp_path: Path, fake_id: str | None) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if fake_id is not None:
        _exe(bin_dir / "id", fake_id)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    script = (
        "set -e\n"
        'CODEX_HOME="$HOME/.codex"\n'
        + codex_agent_user_block()
        + '\necho "AS_AGENT=[$PRELOOP_AS_AGENT]"\n'
    )
    return subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="checks the non-root fallback")
def test_non_root_container_keeps_running_and_says_why(tmp_path):
    proc = _run_block(tmp_path, fake_id=None)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "AS_AGENT=[]" in proc.stdout
    line = next(x for x in proc.stdout.splitlines() if x.startswith(AGENT_USER_LINE))
    assert "the container does not run as root" in line
    assert "can change its own hook configuration" in line


def test_image_without_the_agent_user_keeps_running_and_says_why(tmp_path):
    # A root container on an image published before the user existed.
    fake_id = (
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  "-u") echo 0 ;;\n'
        '  "-un") echo root ;;\n'
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    proc = _run_block(tmp_path, fake_id=fake_id)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "AS_AGENT=[]" in proc.stdout
    assert f"the image has no {AGENT_USER} user" in proc.stdout


def test_launcher_keeps_codex_credentials_and_drops_the_rest(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # A setpriv that prints the environment it would hand to Codex.
    _exe(bin_dir / "setpriv", "#!/bin/sh\nexec env\n")
    assert '-- /usr/bin/env -- "$@"' in _LAUNCHER
    launcher = tmp_path / "launcher"
    _exe(
        launcher,
        _LAUNCHER.replace("<HOME>", "/home/preloop-agent")
        .replace("<USER>", AGENT_USER)
        .replace("<CODEX_HOME>", "/root/.codex"),
    )
    proc = subprocess.run(
        [str(launcher), "codex"],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": "/root",
            "PRELOOP_API_TOKEN": "flow",
            "OPENAI_API_KEY": "model",
            "MCP_CONFIG_JSON": "{}",
            "PRELOOP_FLOW_API_URL": "https://preloop.example.com",
            "PRELOOP_GIT_CREDENTIALS": "store",
            "PRELOOP_GIT_TOKEN_0": "push",
            "PRELOOP_PUBLICATION_CAPABILITY_0": "mint",
            "PRELOOP_PUBLICATION_REFRESH_CAPABILITY": "mint",
            "PRELOOP_CHECKPOINT_PUT_TOKEN": "put",
            "PRELOOP_EVIDENCE_PUT_TOKEN": "put",
            "PRELOOP_NATIVE_SESSION_PUT_TOKEN": "put",
            "PRELOOP_MODEL_GATEWAY_TOKEN": "model",
            "GIT_CONFIG_COUNT": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    env = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    assert env["HOME"] == "/home/preloop-agent"
    assert env["CODEX_HOME"] == "/root/.codex"
    assert env["USER"] == AGENT_USER
    assert env["PRELOOP_API_TOKEN"] == "flow"
    assert env["OPENAI_API_KEY"] == "model"
    assert env["PRELOOP_FLOW_API_URL"] == "https://preloop.example.com"
    for dropped in (
        "MCP_CONFIG_JSON",
        "PRELOOP_GIT_CREDENTIALS",
        "PRELOOP_GIT_TOKEN_0",
        "PRELOOP_PUBLICATION_CAPABILITY_0",
        "PRELOOP_PUBLICATION_REFRESH_CAPABILITY",
        "PRELOOP_CHECKPOINT_PUT_TOKEN",
        "PRELOOP_EVIDENCE_PUT_TOKEN",
        "PRELOOP_NATIVE_SESSION_PUT_TOKEN",
        "PRELOOP_MODEL_GATEWAY_TOKEN",
        "GIT_CONFIG_COUNT",
    ):
        assert dropped not in env
    assert env["PATH"].startswith("/home/preloop-agent/.local/bin:")


def test_root_takes_the_workspace_back_before_its_own_git_steps():
    script = CodexAgent({})._build_codex_script(
        {**_context(), "native_hook_required": True}
    )
    reclaim_at = script.rindex("Take the workspace back from the agent user")
    last_round = script.rindex("$PRELOOP_AS_AGENT codex exec")
    assert last_round < reclaim_at
    assert reclaim_at < script.index("# Exit with codex's exit code")
    block = codex_reclaim_workspace_block()
    assert f"pkill -KILL -u {AGENT_USER}" in block
    assert "chown -R root:root /workspace" in block
    assert 'rm -rf "$_pl_gitdir/hooks"' in block
    assert "GIT_CONFIG_VALUE_0=/dev/null" in block


def test_every_executable_codex_ships_is_execute_only():
    # Not only the binary named codex: its code-mode host holds the same
    # environment.
    block = codex_agent_user_block()
    assert "-type f -perm -u+x -print0" in block
    assert "-name codex" not in block


def test_session_pack_root_matches_the_session_module():
    from preloop.agents.cli_session import SESSION_PACK_ROOT
    from preloop.agents.codex_native_hook import SESSION_PACK_ROOT as repeated

    assert repeated == SESSION_PACK_ROOT
    block = codex_reclaim_workspace_block()
    assert f"rm -rf {SESSION_PACK_ROOT}" in block
    assert f"install -d -m 0700 {SESSION_PACK_ROOT}" in block
    assert "PRELOOP_SETUP_FAILED agent_workspace_tampered" in block


def test_every_root_session_pack_follows_the_reclaim():
    script = CodexAgent({})._build_codex_script(
        {**_context(), "native_hook_required": True}
    )
    marker = "Take the workspace back from the agent user"
    packs = [m.start() for m in re.finditer(r"^\s*_pl_pack_cli_session$", script, re.M)]
    assert packs, "expected session pack calls"
    for at in packs:
        assert marker in script[:at]
        # The nearest preceding reclaim comes after the last Codex round
        # before this pack.
        reclaim_at = script.rindex(marker, 0, at)
        assert "$PRELOOP_AS_AGENT codex exec" not in script[reclaim_at:at]
