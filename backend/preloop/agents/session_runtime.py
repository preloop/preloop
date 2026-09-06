"""Embed the shared validator in a sandbox and use scoped artifact transport."""

from __future__ import annotations

import base64
import inspect
import json
import shlex
from typing import Any

from preloop.agents import session_manifest

RUNTIME_MAIN = r"""
if __name__ == "__main__":
    import os
    import subprocess
    import sys
    import urllib.error
    import urllib.request
    from datetime import timedelta

    action, root, harness, sid, thread = sys.argv[1:]
    if action == "capture":
        print(capture_codex_session_id(Path(root), sid or None))
        sys.exit(0)
    version = subprocess.check_output([harness, "--version"], text=True).strip()
    url = os.environ.get("PRELOOP_CHECKPOINT_URL")
    token = os.environ.get("PRELOOP_NATIVE_SESSION_GET_TOKEN" if action == "restore" else "PRELOOP_NATIVE_SESSION_PUT_TOKEN")
    try:
        if not token or not url:
            print("PRELOOP_NATIVE_RESUME " + json.dumps({"mode": "cold_handoff", "reason": "artifact_unavailable_or_runner_unsupported"}))
            sys.exit(0)
        if action == "restore":
            if not sid:
                raise SessionRestoreError("missing explicit session id")
            request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    archive = response.read(MAX_BYTES + 1)
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 410):
                    print("PRELOOP_NATIVE_RESUME " + json.dumps({"mode": "cold_handoff", "reason": "missing_or_expired"}))
                    sys.exit(0)
                raise
            files = unpack_session(archive, harness=harness, harness_version=version, session_id=sid, thread_id=thread)
            if files is None:
                print("PRELOOP_NATIVE_RESUME " + json.dumps({"mode": "cold_handoff", "reason": "expired"}))
                sys.exit(0)
            restore_session(files, Path(root))
            print("PRELOOP_NATIVE_RESUME " + json.dumps({"mode": "native_resume", "session_id": sid}))
            sys.exit(10)
        files = select_session_files(Path(root), harness, sid)
        expiry = datetime.now(UTC) + timedelta(hours=int(os.environ.get("PRELOOP_NATIVE_SESSION_RETENTION_HOURS", "168")))
        archive = pack_session(files, harness=harness, harness_version=version, session_id=sid, thread_id=thread, expires_at=expiry)
        request = urllib.request.Request(url, data=archive, method="PUT", headers={"Authorization": "Bearer " + token, "Content-Type": "application/gzip"})
        with urllib.request.urlopen(request, timeout=60) as response:
            reference = json.loads(response.read(65536))
        print("PRELOOP_NATIVE_SESSION_ARTIFACT " + json.dumps({"agent_type": harness, "session_id": sid, "thread_id": thread, "harness_version": version, "expires_at": expiry.isoformat(), "artifact_reference": reference}))
    except (SessionRestoreError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print("PRELOOP_NATIVE_RESUME " + json.dumps({"mode": "resume_failed", "reason": type(exc).__name__}))
        sys.exit(1)
"""


def native_session_blocks(
    context: dict[str, Any], harness: str, root_expr: str, captured_sid_expr: str
) -> dict[str, str] | None:
    """Return verified checkpoint blocks for a bound implementation conversation."""
    event = context.get("trigger_event_data") or {}
    resume = event.get("_resume") or {}
    thread = event.get("_session_thread_id") or resume.get("thread_id")
    if not thread:
        return None
    sid = str((resume.get("cli_session") or {}).get("session_id") or "")
    from preloop.agents.cli_session import valid_session_id

    if sid and not valid_session_id(harness, sid):
        raise ValueError("resume_failed: invalid explicit native session id")
    source = inspect.getsource(session_manifest) + "\n" + RUNTIME_MAIN
    encoded = base64.b64encode(source.encode()).decode()
    script = "/tmp/preloop-native-session.py"
    common = f"{root_expr} {shlex.quote(harness)}"
    bootstrap = f"echo '{encoded}' | base64 -d > {script}\nchmod 700 {script}\n"
    if harness == "opencode":
        bootstrap = 'export XDG_DATA_HOME="/tmp/preloop-harness-data"\n' + bootstrap
    restore = (
        bootstrap
        + f"""
PRELOOP_CLI_SESSION_RESTORED=0
PRELOOP_CLI_SESSION_ID={shlex.quote(sid)}
if [ -n "$PRELOOP_CLI_SESSION_ID" ]; then
    set +e
    python3 {script} restore {common} "$PRELOOP_CLI_SESSION_ID" {shlex.quote(str(thread))}
    _pl_restore_status=$?
    set -e
    if [ "$_pl_restore_status" -eq 10 ]; then
        PRELOOP_CLI_SESSION_RESTORED=1
    elif [ "$_pl_restore_status" -ne 0 ]; then
        exit "$_pl_restore_status"
    fi
fi
"""
    )
    pack = f"""
if [ -n {captured_sid_expr} ]; then
    python3 {script} pack {common} {captured_sid_expr} {shlex.quote(str(thread))}
fi
"""
    blocks = {"decode": "", "restore": restore, "pack": pack}
    if harness == "codex":
        blocks["capture"] = f"""
_pl_expected_sid=""
if [ "$PRELOOP_CLI_SESSION_RESTORED" -eq 1 ]; then
    _pl_expected_sid="$PRELOOP_CLI_SESSION_ID"
fi
_pl_codex_sid=$(python3 {script} capture {common} "$_pl_expected_sid" {shlex.quote(str(thread))})
echo "PRELOOP_AGENT_SESSION codex $_pl_codex_sid"
"""
    return blocks


def parse_native_artifact_marker(line: str) -> dict[str, Any] | None:
    """Read bounded reference metadata, never session bytes, from the runner."""
    marker = "PRELOOP_NATIVE_SESSION_ARTIFACT "
    if marker not in line:
        return None
    try:
        obj = json.loads(line.split(marker, 1)[1])
        if not isinstance(obj, dict) or not isinstance(
            obj.get("artifact_reference"), dict
        ):
            return None
        return obj
    except ValueError:
        return None
