"""Module 11 — drive the deliberately wasteful multi-turn session through the
gateway (recorded locally with pexpect+agg).

Runs ``research_agent.py --wasteful``: a scripted 8-turn research
conversation that advertises ten MCP-style tools (two ever invoked), carries
fat never-referenced JSON fields in its tool outputs, repeats one tool output
verbatim, breaks its own request prefix once, and re-sends the full history
every turn. The resulting runtime session is the raw material for the
optimize + replay-validation scene (module 12).

The runtime session id is resolved afterwards via the API (the gateway
groups the turns under the X-Preloop-Session-Id the agent sent) and saved to
``state/wasteful-session.json`` for modules 12/13.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import capture  # noqa: E402
import riglib  # noqa: E402

URL = riglib.env("RIG_URL").rstrip("/")
RIG = Path(__file__).resolve().parent.parent
CAST = riglib.run_dir() / "casts" / "11-wasteful-session.cast"
MP4 = riglib.run_dir() / "casts" / "11-wasteful-session.mp4"
AGENT_STATE = riglib.run_dir() / "state" / "wasteful-agent.json"
SESSION_STATE_NAME = "wasteful-session.json"
AGENT_NAME = "Research Agent (wasteful)"


def resolve_runtime_session(token: str, client_session_id: str) -> dict:
    """Find the runtime session the wasteful run landed in.

    The gateway keys the session on ``<principal>:<client_session_id>``, so
    match on that suffix; fall back to the newest session.
    """
    status, body = riglib.api_request(
        f"{URL}/api/v1/runtime-sessions?limit=50", token=token
    )
    if status != 200:
        raise SystemExit(
            f"GET /api/v1/runtime-sessions failed ({status}): {riglib.redact(body)}"
        )
    items = body.get("items") if isinstance(body, dict) else body
    items = items or []
    if client_session_id:
        for item in items:
            source_id = str(item.get("session_source_id") or "")
            if source_id.endswith(f":{client_session_id}"):
                return item
    if not items:
        raise SystemExit("no runtime sessions exist after the wasteful run")
    riglib.note(
        "wasteful session not matched by client session id; "
        "falling back to the newest runtime session"
    )
    return items[0]


def main() -> None:
    creds = riglib.load_creds()
    token = riglib.user_token(URL, creds)
    python = riglib.env("RIG_PYTHON")

    CAST.parent.mkdir(parents=True, exist_ok=True)
    rec = capture.CastRecorder(CAST)
    child = capture.spawn_shell(rec)  # local shell, recorded
    exit_code = None
    try:
        # Token via env in a hidden pre-step (never in the saved cast).
        child.send(f"export PRELOOP_USER_TOKEN='{token}' HISTFILE=\r")
        child.expect_exact(capture.PROMPT.encode())
        capture.clear_buffer(child)
        rec.reset()
        time.sleep(0.8)

        cmd = (
            f"{python} {RIG / 'research_agent.py'} --url {URL} --wasteful "
            f"--name '{AGENT_NAME}' --state-out {AGENT_STATE}"
        )
        capture.run_command(child, rec, cmd, mark="wasteful_session", timeout=1200)

        rec.mark("exit_code")
        capture.human_type(child, "echo rc=$?")
        child.send("\r")
        child.expect_exact(capture.PROMPT.encode(), timeout=15)
        time.sleep(1.5)
        rec.mark("end")

        buffer = b"".join(e[1].encode("utf-8", "replace") for e in rec.events[-50:])
        m = re.search(rb"rc=(\d+)", buffer)
        exit_code = int(m.group(1)) if m else None

        child.sendline("exit")
        child.close()
    finally:
        rec.compress_idle(cap=2.5)
        rec.save()
        capture.render(CAST, MP4)
        riglib.log(f"cast: {CAST}\nvideo: {MP4}")

    if exit_code == 3:
        riglib.skip(
            "wasteful session SKIPPED — no AI model available via the gateway "
            "(expected on a bare OSS install with no BYOK keys)"
        )
    if exit_code != 0:
        raise SystemExit(f"wasteful session failed (exit {exit_code}) — see cast")

    agent_state = riglib.load_state("wasteful-agent.json") or {}
    client_session_id = agent_state.get("client_session_id") or ""
    session = resolve_runtime_session(token, client_session_id)
    riglib.save_state(
        SESSION_STATE_NAME,
        {
            "runtime_session_id": session.get("id"),
            "session_source_id": session.get("session_source_id"),
            "client_session_id": client_session_id,
            "agent_id": agent_state.get("agent_id"),
            "display_name": AGENT_NAME,
        },
    )
    riglib.note(
        f"wasteful session ran through the gateway as runtime session "
        f"{session.get('id')} ({session.get('total_requests', '?')} requests, "
        f"{session.get('token_usage', {}).get('total_tokens', '?')} tokens)"
    )


if __name__ == "__main__":
    main()
