"""DeepSeek conversation transport, launch and native archive invariants."""

import json
import queue
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from preloop.agents import deepseek_conversation as driver
from preloop.agents.harness import DeepSeekAgent, PiAgent
from preloop.agents.session_manifest import (
    SessionRestoreError,
    pack_session,
    restore_session,
    select_session_files,
    unpack_session,
)

SID = "session-11111111-1111-4111-8111-111111111111"


def context() -> dict[str, Any]:
    """Build a bound hosted launch with no secret interpolation."""
    return {
        "prompt": "Discuss the repository",
        "execution_id": "example",
        "flow_name": "Questions",
        "trigger_event_data": {
            "_conversation_id": "example-thread",
            "_session_thread_id": "example-thread",
        },
    }


def test_launcher_keeps_uid_plugin_pin_and_checkpoint_order() -> None:
    """Interactive mode retains the existing governed launch and captures last."""
    script = DeepSeekAgent({})._build_harness_script(context())
    for text in (
        "@deepseek-ai/dsh@0.1.5-rc.2",
        "setpriv --reuid 10000",
        'node "$PRELOOP_PLUGIN_DIR/bootstrap.mjs"',
        "python3 /tmp/deepseek_conversation.py",
        "python3 /tmp/preloop-native-session.py restore",
        "python3 /tmp/preloop-native-session.py pack",
    ):
        assert text in script
    assert script.index("python3 /tmp/deepseek_conversation.py") < script.index(
        "python3 /tmp/preloop-native-session.py pack"
    )
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    with pytest.raises(ValueError, match="require hosted"):
        PiAgent({})._build_harness_script(context())


def test_resume_launcher_restores_explicit_session_before_driver() -> None:
    """The production wrapper carries the saved native identity into the driver."""
    ctx = context()
    ctx["trigger_event_data"]["_resume"] = {
        "thread_id": "example-thread",
        "cli_session": {"agent_type": "deepseek", "session_id": SID},
    }
    script = DeepSeekAgent({})._build_harness_script(ctx)
    restore = (
        'python3 /tmp/preloop-native-session.py restore "$DSH_HOME/sessions" deepseek'
    )
    launch = "python3 /tmp/deepseek_conversation.py"
    pack = 'python3 /tmp/preloop-native-session.py pack "$DSH_HOME/sessions" deepseek'
    assert script.count(restore) == 1
    assert script.index("npm install") < script.index(restore)
    assert script.index(restore) < script.index(launch) < script.index(pack)
    assert f"PRELOOP_CLI_SESSION_ID={SID}" in script
    assert '"${PRELOOP_CLI_SESSION_ID:-}"' in script
    # Exercise the generated restore/admission fragment: status 10 must retain
    # the explicit session, while restore failure must prevent driver startup.
    fragment = script.split("PRELOOP_CLI_SESSION_RESTORED=0", 1)[1].split("\ncd ", 1)[0]
    for status in (10, 1):
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"python3() {{ return {status}; }}\n"
                + fragment
                + '\nprintf "%s" "$PRELOOP_CLI_SESSION_ID"',
            ],
            text=True,
            capture_output=True,
        )
        assert result.returncode == (0 if status == 10 else 1)
        assert result.stdout == (SID if status == 10 else "")


@pytest.mark.parametrize("failure", ["arguments", "missing_executable"])
def test_startup_failure_emits_structured_marker(
    failure: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bad argv and process startup errors fail without exposing a traceback."""
    monkeypatch.setattr(
        driver.sys,
        "argv",
        ["driver"] if failure == "arguments" else ["driver", "prompt", ""],
    )
    with patch.object(driver.subprocess, "Popen", side_effect=FileNotFoundError):
        assert driver.main() == 1
    captured = capsys.readouterr()
    expected = "ValueError" if failure == "arguments" else "FileNotFoundError"
    assert captured.out == f"PRELOOP_CONVERSATION_FAILED {expected}\n"
    assert not captured.err


def test_runtime_filters_capability_and_requires_matching_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No runtime capability enters dsh; a mismatched resume fails before input."""
    monkeypatch.setenv("PRELOOP_CONVERSATION_TOKEN", "synthetic-capability")
    process = MagicMock()
    process.stdout = iter([])
    with patch.object(driver.subprocess, "Popen", return_value=process) as popen:
        runtime = driver.DeepSeekRuntime(SID)
    env = popen.call_args.kwargs["env"]
    assert "PRELOOP_CONVERSATION_TOKEN" not in env
    runtime.lines = queue.Queue()
    runtime.lines.put(json.dumps({"preloop": True, "ready": True, "session": "wrong"}))
    with pytest.raises(RuntimeError, match="different session"):
        runtime.ready()


@pytest.mark.parametrize("reason", ["completed", "error", "aborted", None])
def test_turn_persists_final_answer_and_rejects_incomplete_outcome(
    reason: str | None,
) -> None:
    """Only a native completed turn may count as an answer; errors are durable."""
    runtime = driver.DeepSeekRuntime.__new__(driver.DeepSeekRuntime)
    runtime.control = MagicMock(return_value={"stop": False})
    with (
        patch.object(runtime, "send"),
        patch.object(
            runtime,
            "event",
            side_effect=[
                {"turn": 3, "delta": "wrong turn"},
                {"turn": 2, "delta": "Partial"},
                {"turn": 2, "finished": True, "answer": "Answer", "reason": reason},
            ],
        ),
    ):
        if reason == "completed":
            runtime.submit("Question", 2)
        else:
            with pytest.raises(RuntimeError, match="did not finish"):
                runtime.submit("Question", 2)
    assert runtime.control.call_args.args[0] == {
        "turn": 2,
        "answer": "Answer",
        "finished": True,
        "error": reason != "completed",
        "interrupted": False,
    }


def log(root: Path, sid: str, parent: str | None = None) -> Path:
    """Write the pinned native header and a durable synthetic event."""
    path = root / "--workspace--" / sid / "session.v3.jsonl"
    path.parent.mkdir(parents=True)
    header = {"type": "session", "version": 3, "id": sid, "cwd": "/workspace"}
    if parent:
        header["parentSession"] = parent
    path.write_text(
        json.dumps(header)
        + "\n"
        + json.dumps(
            {
                "type": "user/message",
                "seq": 0,
                "time": 1,
                "data": {"text": "amber"},
            }
        )
        + "\n"
    )
    return path


def test_stop_cancels_live_turn_and_persists_interruption() -> None:
    """Ending a working conversation cancels its native turn before checkpointing."""
    runtime = driver.DeepSeekRuntime.__new__(driver.DeepSeekRuntime)
    runtime.control = MagicMock(return_value={"stop": True})
    with (
        patch.object(runtime, "send") as send,
        patch.object(
            runtime,
            "event",
            return_value={
                "turn": 1,
                "finished": True,
                "answer": "Partial",
                "reason": "aborted",
            },
        ),
    ):
        runtime.submit("Question", 1)
    assert send.call_args.args[0] == {"cancel": True}
    assert runtime.control.call_args.args[0] == {
        "turn": 1,
        "answer": "Partial",
        "finished": True,
        "error": False,
        "interrupted": True,
    }


def test_archive_selects_root_and_descendants_and_excludes_credentials(
    tmp_path: Path,
) -> None:
    """Only native logs belonging to this explicit root cross the archive boundary."""
    root = tmp_path / "sessions"
    main = log(root, SID)
    child = log(root, "session-child", SID)
    log(root, "session-unrelated")
    (main.parent / "credentials.json").write_text('{"token":"synthetic-secret"}')
    (main.parent / "preloop.json").write_text('{"token":"synthetic-secret"}')
    selected = select_session_files(root, "deepseek", SID)
    assert set(selected) == {str(p.relative_to(root)) for p in (main, child)}
    assert b"synthetic-secret" not in b"".join(selected.values())
    identity = dict(
        harness="deepseek",
        harness_version="0.1.5-rc.2",
        session_id=SID,
        thread_id="thread",
    )
    archive = pack_session(
        selected, **identity, expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    restored = unpack_session(archive, **identity)
    assert restored == selected
    restore_session(restored, tmp_path / "restored")
    with pytest.raises(SessionRestoreError, match="mismatch"):
        unpack_session(archive, **{**identity, "harness_version": "0.2.2-rc.5"})


@pytest.mark.parametrize(
    "damage", ["missing", "truncated", "sequence", "symlink", "version"]
)
def test_missing_or_corrupt_native_state_fails(tmp_path: Path, damage: str) -> None:
    """Missing and incompatible archives never authorize a cold conversation."""
    path = log(tmp_path, SID)
    if damage == "missing":
        path.unlink()
    elif damage == "truncated":
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
    elif damage == "sequence":
        path.write_text(path.read_text().replace('"seq": 0', '"seq": 2'))
    elif damage == "version":
        path.write_text(path.read_text().replace('"version": 3', '"version": 4'))
    else:
        (path.parent / "link").symlink_to(path)
    with pytest.raises(SessionRestoreError):
        select_session_files(tmp_path, "deepseek", SID)
