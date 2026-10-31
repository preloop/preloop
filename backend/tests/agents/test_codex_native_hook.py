"""The Preloop PreToolUse hook inside hosted Codex containers.

Hosted Codex must check native shell commands against Preloop tool rules
the way a private-runner host profile does: deny rules block, and an
approval-required rule parks the run (#2137) through the execution's
runtime credential. These tests run the real shell block against a fake
``codex app-server`` and a fake ``preloop`` CLI.
"""

from __future__ import annotations

import json
import stat
import subprocess
import textwrap
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from preloop.agents.codex import CodexAgent
from preloop.agents.codex_native_hook import HOOK_READY_LINE, codex_native_hook_block
from preloop.services import native_flow_park
from preloop.services.flow_failure_category import (
    FAILURE_CATEGORY_APPROVAL_UNAVAILABLE,
    derive_failure_category,
)

# A fake app-server: lists the hook from hooks.json and reports it trusted
# only once config.toml records its hash under [hooks.state].
FAKE_CODEX = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    if sys.argv[1:] != ["app-server"]:
        sys.exit(2)
    for line in sys.stdin:
        m = json.loads(line)
        if m.get("id") == 1:
            print(json.dumps({"id": 1, "result": {}}), flush=True)
        elif m.get("id") == 2:
            hooks = json.load(open(os.path.join(home, "hooks.json")))["hooks"]
            config = open(os.path.join(home, "config.toml")).read()
            key = os.path.join(home, "hooks.json") + ":pre_tool_use:0:0"
            entry = hooks["PreToolUse"][0]["hooks"][0]
            trusted = 'trusted_hash = "sha256:fixture"' in config and key in config
            print(json.dumps({"id": 2, "result": {"data": [{"cwd": os.getcwd(),
                "hooks": [{"key": key, "eventName": "preToolUse",
                "command": entry["command"], "enabled": True, "async": False,
                "currentHash": "sha256:fixture",
                "trustStatus": "trusted" if trusted else "untrusted"}]}]}}),
                flush=True)
            break
    """
)


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_block(tmp_path: Path, *, required: bool, cli: bool, token: str = "tok"):
    home = tmp_path / "home"
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text('model = "gpt-5.4"\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _exe(bin_dir / "codex", FAKE_CODEX)
    if cli:
        _exe(bin_dir / "preloop", "#!/bin/sh\nexit 0\n")
    # No network in tests: a curl that always fails.
    _exe(bin_dir / "curl", "#!/bin/sh\nexit 22\n")
    script = (
        "set -e\n"
        'CODEX_HOME="$HOME/.codex"\nexport CODEX_HOME\n'
        + codex_native_hook_block(required=required)
        + "\necho AFTER_HOOK\n"
    )
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(home),
        "PRELOOP_API_TOKEN": token,
        "PRELOOP_MCP_URL": "https://preloop.example.com/mcp/v1",
    }
    proc = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc, codex_home


def test_hook_is_installed_trusted_and_fail_closed(tmp_path):
    proc, codex_home = _run_block(tmp_path, required=True, cli=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert HOOK_READY_LINE in proc.stdout
    assert "AFTER_HOOK" in proc.stdout
    hooks = json.loads((codex_home / "hooks.json").read_text())["hooks"]
    command = hooks["PreToolUse"][0]["hooks"][0]["command"]
    assert hooks["PreToolUse"][0]["matcher"] == "*"
    assert command.endswith(
        "agents permission-hook --source codex_cli --hook-event PreToolUse "
        "--require-flow-credential"
    )
    config = (codex_home / "config.toml").read_text()
    assert 'trusted_hash = "sha256:fixture"' in config
    # The agent's own commands do not see the runtime credential.
    assert 'PRELOOP_FLOW_TOKEN = ""' in config


def test_missing_cli_refuses_a_run_with_approval_rules(tmp_path):
    proc, _ = _run_block(tmp_path, required=True, cli=False)
    assert proc.returncode == 78
    assert "AFTER_HOOK" not in proc.stdout
    assert "PRELOOP_SETUP_FAILED approval_unavailable" in proc.stdout
    message = next(
        line for line in proc.stdout.splitlines() if "approval_unavailable:" in line
    )
    assert (
        derive_failure_category(
            status="FAILED", error_message=f"Setup commands failed: {message}"
        )
        == FAILURE_CATEGORY_APPROVAL_UNAVAILABLE
    )


def test_missing_cli_without_approval_rules_warns_and_continues(tmp_path):
    proc, _ = _run_block(tmp_path, required=False, cli=False)
    assert proc.returncode == 0
    assert "AFTER_HOOK" in proc.stdout
    assert "Preloop native command hook not installed" in proc.stdout


def test_missing_runtime_credential_is_not_a_working_hook(tmp_path):
    proc, _ = _run_block(tmp_path, required=True, cli=True, token="")
    assert proc.returncode == 78
    assert "no runtime credential" in proc.stdout


def _context(**config):
    return {
        "prompt": "Implement the change",
        "execution_id": "fixture",
        "flow_name": "fixture",
        "agent_config": config,
    }


def test_hosted_script_installs_the_hook_before_codex_starts():
    script = CodexAgent({})._build_codex_script(
        {**_context(), "native_hook_required": True}
    )
    hook_at = script.index("Preloop native command policy")
    assert hook_at < script.index("codex exec $CODEX_RESUME_ARGS")
    assert hook_at > script.index("cat > ~/.codex/config.toml")
    assert "_pl_hook_required=1" in script


def test_read_only_sandbox_keeps_its_shellless_launch():
    script = CodexAgent({})._build_codex_script(_context(sandbox_type="read-only"))
    assert "Preloop native command policy" not in script
    assert "--disable shell_tool" in script


def test_orchestrator_gates_hosted_codex_instead_of_refusing(monkeypatch):
    from preloop.agents.errors import AgentStartError
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    monkeypatch.setattr(
        native_flow_park, "hosted_approval_gap", lambda _db, _flow: "refused"
    )
    fake = SimpleNamespace(db=None, agent_type=None, flow=SimpleNamespace())
    fake.flow.agent_type = "codex"
    context = {"agent_config": {}}
    FlowExecutionOrchestrator._refuse_hosted_approval_gap(fake, context)
    assert context["native_hook_required"] is True

    fake.flow.agent_type = "opencode"
    with pytest.raises(AgentStartError) as raised:
        FlowExecutionOrchestrator._refuse_hosted_approval_gap(
            fake, {"agent_config": {}}
        )
    assert raised.value.category == FAILURE_CATEGORY_APPROVAL_UNAVAILABLE


def test_container_credential_takes_the_unattended_park_path(db_session, test_user):
    """The hook in the container sends the execution's runtime token, which
    names the execution, so approval-required calls park that execution."""
    from unittest.mock import patch

    from preloop.api.endpoints.agent_permission import _resolve_permission_identity
    from preloop.models.crud import crud_flow, crud_flow_execution
    from preloop.models.crud import crud_runtime_session
    from preloop.models.schemas.flow import FlowCreate
    from preloop.models.schemas.flow_execution import FlowExecutionCreate
    from preloop.services.flow_runtime_token import create_flow_runtime_token

    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Hosted Codex",
            prompt_template="Do it",
            trigger_event_source="github",
            trigger_event_types=["push"],
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )
    session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="flow_execution",
        session_source_id=str(execution.id),
        runtime_principal_type="flow_execution",
        runtime_principal_id=str(execution.id),
        runtime_principal_name=flow.name,
        started_at=datetime.now(timezone.utc),
        last_activity_at=datetime.now(timezone.utc),
    )
    db_session.commit()
    token, _ = create_flow_runtime_token(
        db_session,
        flow=flow,
        execution_id=execution.id,
        runtime_session_id=session.id,
    )
    db_session.commit()

    class _Factory:
        def __call__(self):
            return _Ctx()

    class _Ctx:
        def __enter__(self):
            return db_session

        def __exit__(self, *exc):
            return False

    with patch(
        "preloop.api.endpoints.agent_permission.get_session_factory",
        return_value=_Factory(),
    ):
        identity = _resolve_permission_identity(token)
    assert identity.flow_execution_id == uuid.UUID(str(execution.id))
    assert identity.flow_id == uuid.UUID(str(flow.id))


def test_pinned_cli_checksums_are_full_sha256():
    from preloop.agents.codex_native_hook import HOOK_CLI_SHA256

    assert set(HOOK_CLI_SHA256) == {"amd64", "arm64"}
    assert all(len(v) == 64 and int(v, 16) >= 0 for v in HOOK_CLI_SHA256.values())
