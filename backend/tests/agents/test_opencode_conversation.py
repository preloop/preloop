"""Pinned OpenCode event semantics and hosted launcher integration."""

import queue
import subprocess
from io import BytesIO
from typing import Any

from preloop.agents.opencode import OpenCodeAgent
from preloop.agents.opencode_conversation import (
    TurnText,
    child_environment,
    read_events,
)


def event(kind: str, **properties: Any) -> dict[str, Any]:
    """Build a synthetic event in the pinned 1.18.29 SSE envelope."""
    return {"type": kind, "properties": {"sessionID": "ses_example", **properties}}


def test_streams_only_current_assistant_text_and_deduplicates_snapshot() -> None:
    """User text, reasoning and child session output must never become the reply."""
    text = TurnText("ses_example")
    text.accept(event("message.updated", info={"id": "msg_user", "role": "user"}))
    text.accept(
        event(
            "message.part.updated",
            part={
                "id": "prt_user",
                "messageID": "msg_user",
                "type": "text",
                "text": "User input",
            },
        )
    )
    text.accept(
        event(
            "message.updated",
            info={"id": "msg_assistant", "role": "assistant", "parentID": "msg_user"},
        )
    )
    text.accept(
        event(
            "message.part.updated",
            part={
                "id": "prt_answer",
                "messageID": "msg_assistant",
                "type": "text",
                "text": "",
            },
        )
    )
    text.accept(
        event(
            "message.part.delta",
            messageID="msg_assistant",
            partID="prt_answer",
            field="text",
            delta="Amber",
        )
    )
    assert text.text == "Amber"
    text.accept(
        event(
            "message.part.updated",
            part={
                "id": "prt_answer",
                "messageID": "msg_assistant",
                "type": "text",
                "text": "Amber",
            },
        )
    )
    text.accept(
        event(
            "message.part.delta",
            sessionID="ses_child",
            messageID="msg_assistant",
            partID="prt_answer",
            field="text",
            delta="wrong",
        )
    )
    text.accept(
        event(
            "message.part.updated",
            part={
                "id": "prt_reasoning",
                "messageID": "msg_assistant",
                "type": "reasoning",
                "text": "private reasoning",
            },
        )
    )
    assert text.text == "Amber"


def test_sse_disconnect_is_visible() -> None:
    """A disconnected stream signals failure rather than claiming a live phase."""
    events: queue.Queue[dict[str, Any] | None] = queue.Queue()
    read_events(BytesIO(b'data: {"type":"server.connected"}\n\n'), events)
    assert events.get_nowait() == {"type": "server.connected"}
    assert events.get_nowait() is None


def test_agent_cannot_inherit_runtime_write_capability(monkeypatch: Any) -> None:
    """Agent tools cannot impersonate the trusted conversation driver."""
    monkeypatch.setenv("PRELOOP_CONVERSATION_TOKEN", "synthetic-capability")
    monkeypatch.setenv("PRELOOP_DISABLE_TELEMETRY", "true")
    assert "PRELOOP_CONVERSATION_TOKEN" not in child_environment()
    assert child_environment()["PRELOOP_DISABLE_TELEMETRY"] == "true"


def test_interactive_launcher_uses_live_driver_and_native_checkpoint() -> None:
    """The hosted shell invokes the persistent server driver before checkpointing."""
    script = OpenCodeAgent({})._build_opencode_script(
        {
            "prompt": "Ask about a repository",
            "opencode_model": "flash",
            "execution_id": "example",
            "flow_name": "Questions",
            "trigger_event_data": {
                "_conversation_id": "example-conversation",
                "_session_thread_id": "example-conversation",
            },
        }
    )
    assert "opencode-ai@1.18.29" in script
    assert "python3 /tmp/preloop-opencode-conversation.py" in script
    assert "python3 /tmp/preloop-native-session.py pack" in script
    assert "opencode run $OPENCODE_RESUME_ARGS" not in script
    result = subprocess.run(
        ["bash", "-n"], input=script, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
